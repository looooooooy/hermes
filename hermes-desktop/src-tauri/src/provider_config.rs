use hermes_runtime_manager::ports::SecretStore;
use hermes_runtime_manager::{
    MacOSKeychainSecretStore, PROVIDER_ACCOUNT, PROVIDER_SERVICE,
};
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use thiserror::Error;
use zeroize::Zeroizing;

pub const MAX_METADATA_BYTES: usize = 16 * 1024;
const PROVIDER_METADATA_SCHEMA: u8 = 1;
const PROVIDER_NAME: &str = "deepseek";
const PROVIDER_KEY_ENV: &str = "DEEPSEEK_API_KEY";

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum ProviderStateV1 {
    Connected,
    Attention,
    NotConfigured,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ProviderStatusV1 {
    pub provider: String,
    pub model: String,
    pub state: ProviderStateV1,
    pub note: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ProviderSaveRequest {
    pub provider: String,
    pub model: String,
    pub base_url: Option<String>,
    pub api_key: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProviderMetadataV1 {
    schema_version: u8,
    provider: String,
    model: String,
    base_url: Option<String>,
    key_env: String,
    keychain_service: String,
    keychain_account: String,
}

#[derive(Debug, Error)]
pub enum ProviderConfigError {
    #[error("provider configuration is invalid")]
    Invalid,
    #[error("provider configuration storage failed")]
    Storage,
    #[error("provider secure storage failed")]
    SecretStore,
    #[error("managed provider authentication check failed")]
    HostProbe,
}

trait ProviderMetadataStore: Send + Sync {
    fn read(&self) -> Result<Option<Vec<u8>>, ProviderConfigError>;
    fn write(&self, payload: &[u8]) -> Result<(), ProviderConfigError>;
    fn delete(&self) -> Result<(), ProviderConfigError>;
    fn path(&self) -> PathBuf;
}

trait ManagedHostAuthProbe: Send + Sync {
    fn authenticated(
        &self,
        release_id: &str,
        metadata_path: &Path,
    ) -> Result<bool, ProviderConfigError>;
}

pub struct FileProviderMetadataStore {
    path: PathBuf,
}

impl FileProviderMetadataStore {
    pub fn new(path: PathBuf) -> Result<Self, ProviderConfigError> {
        if !path.is_absolute()
            || path
                .components()
                .any(|component| matches!(component, std::path::Component::ParentDir))
        {
            return Err(ProviderConfigError::Invalid);
        }
        Ok(Self { path })
    }

    fn validate_existing(&self) -> Result<Option<std::fs::Metadata>, ProviderConfigError> {
        match fs::symlink_metadata(&self.path) {
            Ok(metadata) => {
                if metadata.file_type().is_symlink()
                    || !metadata.is_file()
                    || metadata.uid() != unsafe { libc::geteuid() }
                    || metadata.permissions().mode() & 0o777 != 0o600
                    || metadata.len() > MAX_METADATA_BYTES as u64
                {
                    return Err(ProviderConfigError::Storage);
                }
                Ok(Some(metadata))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(_) => Err(ProviderConfigError::Storage),
        }
    }

    fn ensure_parent(&self) -> Result<&Path, ProviderConfigError> {
        let parent = self.path.parent().ok_or(ProviderConfigError::Invalid)?;
        fs::create_dir_all(parent).map_err(|_| ProviderConfigError::Storage)?;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
            .map_err(|_| ProviderConfigError::Storage)?;
        let metadata = fs::symlink_metadata(parent).map_err(|_| ProviderConfigError::Storage)?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o077 != 0
        {
            return Err(ProviderConfigError::Storage);
        }
        Ok(parent)
    }
}

impl ProviderMetadataStore for FileProviderMetadataStore {
    fn read(&self) -> Result<Option<Vec<u8>>, ProviderConfigError> {
        if self.validate_existing()?.is_none() {
            return Ok(None);
        }
        fs::read(&self.path)
            .map(Some)
            .map_err(|_| ProviderConfigError::Storage)
    }

    fn write(&self, payload: &[u8]) -> Result<(), ProviderConfigError> {
        if payload.is_empty() || payload.len() > MAX_METADATA_BYTES {
            return Err(ProviderConfigError::Invalid);
        }
        self.validate_existing()?;
        let parent = self.ensure_parent()?;
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| ProviderConfigError::Storage)?
            .as_nanos();
        let temporary = parent.join(format!(".provider-v1-{}-{nonce}.tmp", std::process::id()));
        let result = (|| {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&temporary)
                .map_err(|_| ProviderConfigError::Storage)?;
            file.write_all(payload)
                .and_then(|_| file.sync_all())
                .map_err(|_| ProviderConfigError::Storage)?;
            fs::rename(&temporary, &self.path).map_err(|_| ProviderConfigError::Storage)?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        result
    }

    fn delete(&self) -> Result<(), ProviderConfigError> {
        if self.validate_existing()?.is_some() {
            fs::remove_file(&self.path).map_err(|_| ProviderConfigError::Storage)?;
        }
        Ok(())
    }

    fn path(&self) -> PathBuf {
        self.path.clone()
    }
}

struct CommandManagedHostAuthProbe {
    application_root: PathBuf,
}

impl ManagedHostAuthProbe for CommandManagedHostAuthProbe {
    fn authenticated(
        &self,
        release_id: &str,
        metadata_path: &Path,
    ) -> Result<bool, ProviderConfigError> {
        if !safe_release_id(release_id)
            || metadata_path
                != self
                    .application_root
                    .join("profiles/work/provider-v1.json")
        {
            return Err(ProviderConfigError::HostProbe);
        }
        let executable = self
            .application_root
            .join("releases")
            .join(release_id)
            .join("host/venv/bin/hermes");
        validate_managed_host(&executable, &self.application_root, release_id)?;
        let home = std::env::var_os("HOME").ok_or(ProviderConfigError::HostProbe)?;
        let output = Command::new(executable)
            .args(["auth", "status", PROVIDER_NAME])
            .env_clear()
            .env("HOME", home)
            .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
            .env("HERMES_HOME", &self.application_root)
            .env("HERMES_MANAGED_PROVIDER_CONFIG", metadata_path)
            .output()
            .map_err(|_| ProviderConfigError::HostProbe)?;
        Ok(output.status.success()
            && matches!(output.stdout.as_slice(), b"deepseek: logged in" | b"deepseek: logged in\n"))
    }
}

pub struct ProviderConfigService {
    secrets: Arc<dyn SecretStore>,
    metadata: Arc<dyn ProviderMetadataStore>,
    host_probe: Arc<dyn ManagedHostAuthProbe>,
}

impl ProviderConfigService {
    pub fn new(application_root: PathBuf) -> Result<Self, ProviderConfigError> {
        let metadata_path = application_root.join("profiles/work/provider-v1.json");
        Self::with_adapters(
            application_root.clone(),
            Arc::new(MacOSKeychainSecretStore::new()),
            Arc::new(FileProviderMetadataStore::new(metadata_path)?),
            Arc::new(CommandManagedHostAuthProbe { application_root }),
        )
    }

    fn with_adapters(
        application_root: PathBuf,
        secrets: Arc<dyn SecretStore>,
        metadata: Arc<dyn ProviderMetadataStore>,
        host_probe: Arc<dyn ManagedHostAuthProbe>,
    ) -> Result<Self, ProviderConfigError> {
        if !application_root.is_absolute()
            || application_root
                .components()
                .any(|component| matches!(component, std::path::Component::ParentDir))
        {
            return Err(ProviderConfigError::Invalid);
        }
        Ok(Self {
            secrets,
            metadata,
            host_probe,
        })
    }

    pub fn save(
        &self,
        mut request: ProviderSaveRequest,
        active_release: Option<&str>,
    ) -> Result<ProviderStatusV1, ProviderConfigError> {
        validate_request(&request)?;
        let secret = Zeroizing::new(std::mem::take(&mut request.api_key).into_bytes());
        let metadata = ProviderMetadataV1 {
            schema_version: PROVIDER_METADATA_SCHEMA,
            provider: request.provider,
            model: request.model,
            base_url: request.base_url,
            key_env: PROVIDER_KEY_ENV.to_owned(),
            keychain_service: PROVIDER_SERVICE.to_owned(),
            keychain_account: PROVIDER_ACCOUNT.to_owned(),
        };
        validate_metadata(&metadata)?;
        let payload = serde_json::to_vec_pretty(&metadata).map_err(|_| ProviderConfigError::Invalid)?;
        if payload.len() > MAX_METADATA_BYTES {
            return Err(ProviderConfigError::Invalid);
        }

        let prior_secret = self
            .secrets
            .get(PROVIDER_SERVICE, PROVIDER_ACCOUNT)
            .map_err(|_| ProviderConfigError::SecretStore)?
            .map(Zeroizing::new);
        let prior_metadata = self.metadata.read()?;
        self.secrets
            .put(PROVIDER_SERVICE, PROVIDER_ACCOUNT, secret.as_slice())
            .map_err(|_| ProviderConfigError::SecretStore)?;
        if self.metadata.write(&payload).is_err() {
            let secret_restored = match &prior_secret {
                Some(value) => self
                    .secrets
                    .put(PROVIDER_SERVICE, PROVIDER_ACCOUNT, value.as_slice()),
                None => self.secrets.delete(PROVIDER_SERVICE, PROVIDER_ACCOUNT),
            };
            let metadata_restored = match &prior_metadata {
                Some(value) => self.metadata.write(value),
                None => self.metadata.delete(),
            };
            if secret_restored.is_err() || metadata_restored.is_err() {
                return Err(ProviderConfigError::Storage);
            }
            return Err(ProviderConfigError::Storage);
        }
        self.status(active_release)
    }

    pub fn status(
        &self,
        active_release: Option<&str>,
    ) -> Result<ProviderStatusV1, ProviderConfigError> {
        let raw_metadata = self.metadata.read()?;
        let secret = self
            .secrets
            .get(PROVIDER_SERVICE, PROVIDER_ACCOUNT)
            .map_err(|_| ProviderConfigError::SecretStore)?
            .map(Zeroizing::new);
        let Some(raw_metadata) = raw_metadata else {
            return Ok(status(
                "deepseek-chat",
                if secret.is_some() {
                    ProviderStateV1::Attention
                } else {
                    ProviderStateV1::NotConfigured
                },
            ));
        };
        let metadata: ProviderMetadataV1 =
            serde_json::from_slice(&raw_metadata).map_err(|_| ProviderConfigError::Invalid)?;
        validate_metadata(&metadata)?;
        if secret.as_ref().is_none_or(|value| value.is_empty()) {
            return Ok(status(&metadata.model, ProviderStateV1::Attention));
        }
        let Some(release_id) = active_release else {
            return Ok(status(&metadata.model, ProviderStateV1::Attention));
        };
        let authenticated = self
            .host_probe
            .authenticated(release_id, &self.metadata.path())?;
        Ok(status(
            &metadata.model,
            if authenticated {
                ProviderStateV1::Connected
            } else {
                ProviderStateV1::Attention
            },
        ))
    }

    pub fn delete(&self) -> Result<ProviderStatusV1, ProviderConfigError> {
        self.secrets
            .delete(PROVIDER_SERVICE, PROVIDER_ACCOUNT)
            .map_err(|_| ProviderConfigError::SecretStore)?;
        self.metadata.delete()?;
        Ok(status("deepseek-chat", ProviderStateV1::NotConfigured))
    }

}

fn status(model: &str, state: ProviderStateV1) -> ProviderStatusV1 {
    let note = match state {
        ProviderStateV1::Connected => "Managed Host verified the Keychain credential",
        ProviderStateV1::Attention => "Provider configuration needs verification",
        ProviderStateV1::NotConfigured => "Provider is not configured",
    };
    ProviderStatusV1 {
        provider: PROVIDER_NAME.to_owned(),
        model: model.to_owned(),
        state,
        note: note.to_owned(),
    }
}

fn validate_request(request: &ProviderSaveRequest) -> Result<(), ProviderConfigError> {
    if request.provider != PROVIDER_NAME
        || !matches!(request.model.as_str(), "deepseek-chat" | "deepseek-reasoner")
        || !(16..=8192).contains(&request.api_key.len())
        || request.api_key.chars().any(char::is_control)
        || request.api_key != request.api_key.trim()
    {
        return Err(ProviderConfigError::Invalid);
    }
    if let Some(base_url) = &request.base_url {
        validate_https_url(base_url)?;
    }
    Ok(())
}

fn validate_metadata(metadata: &ProviderMetadataV1) -> Result<(), ProviderConfigError> {
    if metadata.schema_version != PROVIDER_METADATA_SCHEMA
        || metadata.provider != PROVIDER_NAME
        || !matches!(metadata.model.as_str(), "deepseek-chat" | "deepseek-reasoner")
        || metadata.key_env != PROVIDER_KEY_ENV
        || metadata.keychain_service != PROVIDER_SERVICE
        || metadata.keychain_account != PROVIDER_ACCOUNT
    {
        return Err(ProviderConfigError::Invalid);
    }
    if let Some(base_url) = &metadata.base_url {
        validate_https_url(base_url)?;
    }
    Ok(())
}

fn validate_https_url(value: &str) -> Result<(), ProviderConfigError> {
    let url = reqwest::Url::parse(value).map_err(|_| ProviderConfigError::Invalid)?;
    if url.scheme() != "https"
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err(ProviderConfigError::Invalid);
    }
    Ok(())
}

fn safe_release_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 160
        && value != "."
        && value != ".."
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+'))
}

fn validate_managed_host(
    executable: &Path,
    application_root: &Path,
    release_id: &str,
) -> Result<(), ProviderConfigError> {
    let expected = application_root
        .join("releases")
        .join(release_id)
        .join("host/venv/bin/hermes");
    if executable != expected {
        return Err(ProviderConfigError::HostProbe);
    }
    for candidate in executable.ancestors() {
        let metadata = fs::symlink_metadata(candidate).map_err(|_| ProviderConfigError::HostProbe)?;
        if metadata.file_type().is_symlink() {
            return Err(ProviderConfigError::HostProbe);
        }
        if candidate == executable
            && (!metadata.is_file() || metadata.permissions().mode() & 0o111 == 0)
        {
            return Err(ProviderConfigError::HostProbe);
        }
        if candidate == application_root {
            return Ok(());
        }
    }
    Err(ProviderConfigError::HostProbe)
}

#[cfg(test)]
mod tests {
    use super::*;
    use hermes_runtime_manager::ports::{PortError, SecretStore};
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[derive(Default)]
    struct FakeSecretStore {
        value: Mutex<Option<Vec<u8>>>,
    }

    impl SecretStore for FakeSecretStore {
        fn put(&self, _namespace: &str, _account: &str, secret: &[u8]) -> Result<(), PortError> {
            *self.value.lock().unwrap() = Some(secret.to_vec());
            Ok(())
        }

        fn get(&self, _namespace: &str, _account: &str) -> Result<Option<Vec<u8>>, PortError> {
            Ok(self.value.lock().unwrap().clone())
        }

        fn delete(&self, _namespace: &str, _account: &str) -> Result<(), PortError> {
            *self.value.lock().unwrap() = None;
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakeMetadataStore {
        value: Mutex<Option<Vec<u8>>>,
        fail_write_after_store: Mutex<bool>,
    }

    impl ProviderMetadataStore for FakeMetadataStore {
        fn read(&self) -> Result<Option<Vec<u8>>, ProviderConfigError> {
            Ok(self.value.lock().unwrap().clone())
        }

        fn write(&self, payload: &[u8]) -> Result<(), ProviderConfigError> {
            *self.value.lock().unwrap() = Some(payload.to_vec());
            let mut fail = self.fail_write_after_store.lock().unwrap();
            if *fail {
                *fail = false;
                return Err(ProviderConfigError::Storage);
            }
            Ok(())
        }

        fn delete(&self) -> Result<(), ProviderConfigError> {
            *self.value.lock().unwrap() = None;
            Ok(())
        }

        fn path(&self) -> PathBuf {
            PathBuf::from("/Applications/Hermes/managed/profiles/work/provider-v1.json")
        }
    }

    struct FakeHostProbe {
        authenticated: bool,
    }

    impl ManagedHostAuthProbe for FakeHostProbe {
        fn authenticated(
            &self,
            _release_id: &str,
            _metadata_path: &Path,
        ) -> Result<bool, ProviderConfigError> {
            Ok(self.authenticated)
        }
    }

    fn request() -> ProviderSaveRequest {
        ProviderSaveRequest {
            provider: "deepseek".to_owned(),
            model: "deepseek-chat".to_owned(),
            base_url: Some("https://api.deepseek.com".to_owned()),
            api_key: "sk-valid-provider-secret-value".to_owned(),
        }
    }

    fn service(
        secret: Arc<FakeSecretStore>,
        metadata: Arc<FakeMetadataStore>,
        authenticated: bool,
    ) -> ProviderConfigService {
        ProviderConfigService::with_adapters(
            PathBuf::from("/Applications/Hermes/managed"),
            secret,
            metadata,
            Arc::new(FakeHostProbe { authenticated }),
        )
        .unwrap()
    }

    fn temp_root(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "hermes-provider-{name}-{}-{unique}",
            std::process::id()
        ));
        fs::create_dir_all(&root).unwrap();
        root.canonicalize().unwrap()
    }

    #[test]
    fn provider_config_rejects_unknown_provider_model_url_and_unsafe_key() {
        let secret = Arc::new(FakeSecretStore::default());
        let metadata = Arc::new(FakeMetadataStore::default());
        let service = service(secret, metadata, true);

        let mut unknown_provider = request();
        unknown_provider.provider = "openai".to_owned();
        assert!(service.save(unknown_provider, Some("1.0.0")).is_err());

        let mut unknown_model = request();
        unknown_model.model = "deepseek-coder".to_owned();
        assert!(service.save(unknown_model, Some("1.0.0")).is_err());

        let mut http_url = request();
        http_url.base_url = Some("http://api.deepseek.com".to_owned());
        assert!(service.save(http_url, Some("1.0.0")).is_err());

        let mut short_key = request();
        short_key.api_key = "short".to_owned();
        assert!(service.save(short_key, Some("1.0.0")).is_err());

        let mut control_key = request();
        control_key.api_key = "sk-valid-provider\nsecret".to_owned();
        assert!(service.save(control_key, Some("1.0.0")).is_err());
    }

    #[test]
    fn file_metadata_rejects_symlink_loose_permissions_and_oversized_json() {
        let root = temp_root("metadata");
        let profile = root.join("profiles/work");
        fs::create_dir_all(&profile).unwrap();
        fs::set_permissions(&profile, fs::Permissions::from_mode(0o700)).unwrap();
        let path = profile.join("provider-v1.json");
        let outside = root.join("outside.json");
        fs::write(&outside, b"{}").unwrap();
        symlink(&outside, &path).unwrap();
        let store = FileProviderMetadataStore::new(path.clone()).unwrap();
        assert!(store.read().is_err());

        fs::remove_file(&path).unwrap();
        fs::write(&path, b"{}").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(store.read().is_err());

        fs::write(&path, vec![b'x'; MAX_METADATA_BYTES + 1]).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(store.read().is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn failed_metadata_overwrite_restores_prior_keychain_and_metadata() {
        let secret = Arc::new(FakeSecretStore::default());
        let metadata = Arc::new(FakeMetadataStore::default());
        let service = service(secret.clone(), metadata.clone(), true);
        service.save(request(), Some("1.0.0")).unwrap();
        let prior_secret = secret.value.lock().unwrap().clone();
        let prior_metadata = metadata.value.lock().unwrap().clone();
        *metadata.fail_write_after_store.lock().unwrap() = true;

        let mut replacement = request();
        replacement.model = "deepseek-reasoner".to_owned();
        replacement.api_key = "sk-replacement-provider-secret".to_owned();
        assert!(service.save(replacement, Some("1.0.0")).is_err());

        assert_eq!(*secret.value.lock().unwrap(), prior_secret);
        assert_eq!(*metadata.value.lock().unwrap(), prior_metadata);
    }

    #[test]
    fn connected_requires_valid_metadata_keychain_item_and_exact_host_receipt() {
        let secret = Arc::new(FakeSecretStore::default());
        let metadata = Arc::new(FakeMetadataStore::default());
        let connected = service(secret.clone(), metadata.clone(), true);
        let saved = connected.save(request(), Some("1.0.0")).unwrap();
        assert_eq!(saved.state, ProviderStateV1::Connected);

        let no_host = service(secret.clone(), metadata.clone(), false);
        assert_eq!(
            no_host.status(Some("1.0.0")).unwrap().state,
            ProviderStateV1::Attention
        );

        secret.delete("ignored", "ignored").unwrap();
        assert_ne!(
            connected.status(Some("1.0.0")).unwrap().state,
            ProviderStateV1::Connected
        );
    }
}
