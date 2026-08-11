use crate::model::{ComponentHealth, PlatformKind, ProcessEvidence};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum PortError {
    #[error("platform capability unavailable: {0}")]
    Unavailable(&'static str),
    #[error("platform operation failed: {0}")]
    Operation(String),
    #[error("platform I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectorLaunchConfigV1 {
    pub cloud_api_endpoint: String,
    pub cloud_endpoint: String,
    pub display_name: String,
    pub profile: String,
    pub connector_version: String,
    pub application_root: PathBuf,
    pub state_directory: PathBuf,
    pub database_file: PathBuf,
    pub lock_file: PathBuf,
}

impl ConnectorLaunchConfigV1 {
    pub fn validate(&self) -> Result<(), PortError> {
        validate_endpoint(&self.cloud_api_endpoint, "https", true)?;
        validate_endpoint(&self.cloud_endpoint, "wss", false)?;
        for (label, value) in [
            ("connector display name", self.display_name.as_str()),
            ("connector profile", self.profile.as_str()),
            ("connector version", self.connector_version.as_str()),
        ] {
            if value.is_empty()
                || value != value.trim()
                || value.chars().any(char::is_control)
            {
                return Err(PortError::Operation(format!("{label} is invalid")));
            }
        }
        validate_managed_path(&self.application_root, "application root")?;
        for (label, path) in [
            ("connector state directory", &self.state_directory),
            ("connector database file", &self.database_file),
            ("connector lock file", &self.lock_file),
        ] {
            validate_managed_path(path, label)?;
            if !path.starts_with(&self.application_root) {
                return Err(PortError::Operation(format!(
                    "{label} must be inside the managed application root"
                )));
            }
        }
        if self.database_file == self.lock_file
            || self.database_file.parent() != Some(self.state_directory.as_path())
            || self.lock_file.parent() != Some(self.state_directory.as_path())
        {
            return Err(PortError::Operation(
                "connector managed state paths are inconsistent".to_owned(),
            ));
        }
        Ok(())
    }

    pub(crate) fn environment(&self) -> Result<BTreeMap<String, String>, PortError> {
        self.validate()?;
        Ok(BTreeMap::from([
            (
                "HERMES_CONNECTOR_API_ENDPOINT".to_owned(),
                self.cloud_api_endpoint.clone(),
            ),
            (
                "HERMES_CONNECTOR_CLOUD_ENDPOINT".to_owned(),
                self.cloud_endpoint.clone(),
            ),
            (
                "HERMES_CONNECTOR_DISPLAY_NAME".to_owned(),
                self.display_name.clone(),
            ),
            (
                "HERMES_CONNECTOR_PROFILE".to_owned(),
                self.profile.clone(),
            ),
            (
                "HERMES_CONNECTOR_VERSION".to_owned(),
                self.connector_version.clone(),
            ),
            (
                "HERMES_CONNECTOR_STATE_DIR".to_owned(),
                path_string(&self.state_directory, "connector state directory")?,
            ),
            (
                "HERMES_CONNECTOR_DATABASE_FILE".to_owned(),
                path_string(&self.database_file, "connector database file")?,
            ),
            (
                "HERMES_CONNECTOR_LOCK_FILE".to_owned(),
                path_string(&self.lock_file, "connector lock file")?,
            ),
            (
                "HERMES_HOME".to_owned(),
                path_string(&self.application_root, "application root")?,
            ),
        ]))
    }
}

fn validate_endpoint(value: &str, required_scheme: &str, reject_query: bool) -> Result<(), PortError> {
    let endpoint = reqwest::Url::parse(value)
        .map_err(|_| PortError::Operation("connector endpoint is invalid".to_owned()))?;
    if endpoint.scheme() != required_scheme
        || endpoint.host_str().is_none()
        || !endpoint.username().is_empty()
        || endpoint.password().is_some()
        || endpoint.fragment().is_some()
        || reject_query && endpoint.query().is_some()
    {
        return Err(PortError::Operation(
            "connector endpoint is invalid".to_owned(),
        ));
    }
    Ok(())
}

fn validate_managed_path(path: &Path, label: &str) -> Result<(), PortError> {
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir))
    {
        return Err(PortError::Operation(format!(
            "{label} must be an absolute managed path"
        )));
    }
    Ok(())
}

fn path_string(path: &Path, label: &str) -> Result<String, PortError> {
    path.to_str()
        .filter(|value| !value.contains('\0'))
        .map(str::to_owned)
        .ok_or_else(|| PortError::Operation(format!("{label} is not valid UTF-8")))
}

pub trait ServiceManager: Send + Sync {
    fn install_bootstrap(&self, runtime_manager: &Path) -> Result<(), PortError>;
    fn start_host(&self, executable: &Path, release_id: &str) -> Result<(), PortError>;
    fn stop_host(&self) -> Result<(), PortError>;
    fn start_connector(&self, executable: &Path, release_id: &str) -> Result<(), PortError>;
    fn stop_connector(&self) -> Result<(), PortError>;
    fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError>;
}

pub trait SecretStore: Send + Sync {
    fn put(&self, namespace: &str, account: &str, secret: &[u8]) -> Result<(), PortError>;
    fn get(&self, namespace: &str, account: &str) -> Result<Option<Vec<u8>>, PortError>;
    fn delete(&self, namespace: &str, account: &str) -> Result<(), PortError>;
}

pub trait LocalIpc: Send + Sync {
    fn endpoint(&self) -> Result<PathBuf, PortError>;
    fn verify_peer(&self, peer_pid: u32) -> Result<ProcessEvidence, PortError>;
}

pub trait ProcessIdentity: Send + Sync {
    fn inspect(&self, pid: u32) -> Result<ProcessEvidence, PortError>;
    fn current_executable(&self) -> Result<PathBuf, PortError>;
}

pub trait InstallLayout: Send + Sync {
    fn platform(&self) -> PlatformKind;
    fn application_root(&self) -> Result<PathBuf, PortError>;
    fn releases_root(&self) -> Result<PathBuf, PortError>;
    fn toolchains_root(&self) -> Result<PathBuf, PortError>;
    fn state_root(&self) -> Result<PathBuf, PortError>;
    fn logs_root(&self) -> Result<PathBuf, PortError>;
}

pub trait Clock: Send + Sync {
    fn unix_ms(&self) -> u64;
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config(root: &Path) -> ConnectorLaunchConfigV1 {
        ConnectorLaunchConfigV1 {
            cloud_api_endpoint: "https://api.example.test/hermes".to_owned(),
            cloud_endpoint: "wss://api.example.test/hermes/internal/connector/ws".to_owned(),
            display_name: "Hermes workstation".to_owned(),
            profile: "enterprise".to_owned(),
            connector_version: "0.1.0".to_owned(),
            application_root: root.to_path_buf(),
            state_directory: root.join("connector/state"),
            database_file: root.join("connector/state/connector.sqlite3"),
            lock_file: root.join("connector/state/connector.lock"),
        }
    }

    #[test]
    fn connector_launch_config_rejects_insecure_endpoint_schemes() {
        let root = Path::new("/Applications/Hermes/managed");
        let mut http_api = config(root);
        http_api.cloud_api_endpoint = "http://api.example.test/hermes".to_owned();
        assert!(http_api.validate().is_err());

        let mut non_wss_cloud = config(root);
        non_wss_cloud.cloud_endpoint = "ws://api.example.test/connector".to_owned();
        assert!(non_wss_cloud.validate().is_err());
    }

    #[test]
    fn connector_launch_config_contains_only_non_secret_environment() {
        let environment = config(Path::new("/Applications/Hermes/managed"))
            .environment()
            .unwrap();

        assert_eq!(
            environment.get("HERMES_CONNECTOR_API_ENDPOINT").map(String::as_str),
            Some("https://api.example.test/hermes")
        );
        assert!(environment.keys().all(|key| {
            let upper = key.to_ascii_uppercase();
            !upper.ends_with("_TOKEN")
                && !upper.ends_with("_SECRET")
                && !upper.ends_with("_KEY")
        }));
    }
}
