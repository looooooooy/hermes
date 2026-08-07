use crate::release_control::{
    ProductReleaseManifestV1, ReleaseControlVerificationReportV1,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use thiserror::Error;

const MAX_ARTIFACT_BYTES: u64 = 8 * 1024 * 1024 * 1024;
const DOWNLOAD_CHUNK_BYTES: usize = 4 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ReleaseArtifactKindV1 {
    Installer,
    BootstrapPayload,
    ManagedReleasePayload,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ArtifactDownloadSpecV1 {
    pub schema_version: u8,
    pub release_id: String,
    pub release_generation: u64,
    pub target: String,
    pub kind: ReleaseArtifactKindV1,
    pub object_key: String,
    pub file_name: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub platform_signature: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DownloadChunkV1 {
    pub start: u64,
    pub total_size: u64,
    pub bytes: Vec<u8>,
}

pub trait ArtifactRangeSource {
    fn read_range(
        &mut self,
        start: u64,
        maximum_bytes: usize,
    ) -> Result<DownloadChunkV1, UpdateDownloadError>;
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ArtifactDownloadReceiptV1 {
    pub schema_version: u8,
    pub release_id: String,
    pub release_generation: u64,
    pub target: String,
    pub kind: ReleaseArtifactKindV1,
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub final_path: PathBuf,
    pub resumed_from_bytes: u64,
    pub downloaded_bytes: u64,
    pub reused_existing: bool,
    pub content_verified: bool,
}

#[derive(Debug, Error)]
pub enum UpdateDownloadError {
    #[error("download release verification report is not eligible or signature-verified")]
    UnverifiedRelease,
    #[error("download release verification report does not match the signed release")]
    ReleaseIdentityMismatch,
    #[error("download target is missing from the signed release: {0}")]
    MissingTarget(String),
    #[error("download artifact contract is invalid: {0}")]
    InvalidArtifact(String),
    #[error("download cache root must be an absolute non-symlink directory")]
    InvalidCacheRoot,
    #[error("download is already in progress for this artifact")]
    Busy,
    #[error("download source returned an invalid range: {0}")]
    InvalidRange(String),
    #[error("download source ended before the signed artifact size")]
    PrematureEof,
    #[error("downloaded artifact SHA-256 does not match the signed release")]
    DigestMismatch,
    #[error("downloaded artifact size does not match the signed release")]
    SizeMismatch,
    #[error("download I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

pub fn download_spec_from_verified_release(
    release: &ProductReleaseManifestV1,
    verification: &ReleaseControlVerificationReportV1,
    target: &str,
    kind: ReleaseArtifactKindV1,
) -> Result<ArtifactDownloadSpecV1, UpdateDownloadError> {
    if !verification.eligible || !verification.signatures_verified {
        return Err(UpdateDownloadError::UnverifiedRelease);
    }
    if verification.release_id != release.release_id
        || verification.release_generation != release.release_generation
        || verification.product_version != release.product_version
    {
        return Err(UpdateDownloadError::ReleaseIdentityMismatch);
    }
    let target_manifest = release
        .targets
        .get(target)
        .ok_or_else(|| UpdateDownloadError::MissingTarget(target.to_owned()))?;
    let artifact = match kind {
        ReleaseArtifactKindV1::Installer => &target_manifest.installer,
        ReleaseArtifactKindV1::BootstrapPayload => &target_manifest.bootstrap_payload,
        ReleaseArtifactKindV1::ManagedReleasePayload => &target_manifest.managed_release_payload,
    };
    let file_name = artifact
        .object_key
        .rsplit('/')
        .next()
        .ok_or_else(|| UpdateDownloadError::InvalidArtifact("object_key has no filename".to_owned()))?;
    let spec = ArtifactDownloadSpecV1 {
        schema_version: 1,
        release_id: release.release_id.clone(),
        release_generation: release.release_generation,
        target: target.to_owned(),
        kind,
        object_key: artifact.object_key.clone(),
        file_name: file_name.to_owned(),
        sha256: artifact.sha256.clone(),
        size_bytes: artifact.size_bytes,
        platform_signature: artifact.platform_signature.clone(),
    };
    validate_download_spec(&spec)?;
    if kind == ReleaseArtifactKindV1::Installer && spec.platform_signature.is_none() {
        return Err(UpdateDownloadError::InvalidArtifact(
            "installer must declare a platform_signature expectation".to_owned(),
        ));
    }
    Ok(spec)
}

pub fn download_verified_artifact(
    spec: &ArtifactDownloadSpecV1,
    source: &mut dyn ArtifactRangeSource,
    cache_root: &Path,
) -> Result<ArtifactDownloadReceiptV1, UpdateDownloadError> {
    validate_download_spec(spec)?;
    prepare_cache_root(cache_root)?;

    let digest_dir = cache_root.join(&spec.sha256[..2]).join(&spec.sha256);
    prepare_private_dir(&digest_dir)?;
    let final_path = digest_dir.join(&spec.file_name);
    let partial_path = digest_dir.join(format!(".{}.partial", spec.file_name));
    let lock_path = digest_dir.join(format!(".{}.download-lock", spec.file_name));
    let _lock = DownloadLock::acquire(&lock_path)?;

    if final_path.exists() || final_path.is_symlink() {
        verify_artifact_file(&final_path, spec)?;
        return Ok(receipt(spec, final_path, spec.size_bytes, 0, true));
    }

    let mut resumed_from = partial_length(&partial_path, spec.size_bytes)?;
    if resumed_from == spec.size_bytes && resumed_from > 0 {
        match verify_artifact_file(&partial_path, spec) {
            Ok(()) => {
                fs::rename(&partial_path, &final_path)?;
                return Ok(receipt(spec, final_path, resumed_from, 0, false));
            }
            Err(UpdateDownloadError::DigestMismatch | UpdateDownloadError::SizeMismatch) => {
                fs::remove_file(&partial_path)?;
                resumed_from = 0;
            }
            Err(error) => return Err(error),
        }
    }

    let mut file = open_partial(&partial_path, resumed_from == 0)?;
    let mut offset = resumed_from;
    while offset < spec.size_bytes {
        let remaining = spec.size_bytes - offset;
        let maximum = usize::try_from(remaining.min(DOWNLOAD_CHUNK_BYTES as u64))
            .map_err(|_| UpdateDownloadError::InvalidRange("remaining range is too large".to_owned()))?;
        let chunk = source.read_range(offset, maximum)?;
        if chunk.start != offset {
            return Err(UpdateDownloadError::InvalidRange(
                "source range start does not match requested offset".to_owned(),
            ));
        }
        if chunk.total_size != spec.size_bytes {
            return Err(UpdateDownloadError::InvalidRange(
                "source total size does not match signed artifact size".to_owned(),
            ));
        }
        if chunk.bytes.is_empty() {
            return Err(UpdateDownloadError::PrematureEof);
        }
        if chunk.bytes.len() > maximum
            || u64::try_from(chunk.bytes.len()).unwrap_or(u64::MAX) > remaining
        {
            return Err(UpdateDownloadError::InvalidRange(
                "source returned more bytes than the requested bounded range".to_owned(),
            ));
        }
        file.write_all(&chunk.bytes)?;
        offset += chunk.bytes.len() as u64;
    }
    file.sync_all()?;
    drop(file);

    match verify_artifact_file(&partial_path, spec) {
        Ok(()) => {}
        Err(UpdateDownloadError::DigestMismatch | UpdateDownloadError::SizeMismatch) => {
            fs::remove_file(&partial_path)?;
            return Err(UpdateDownloadError::DigestMismatch);
        }
        Err(error) => return Err(error),
    }
    if final_path.exists() || final_path.is_symlink() {
        verify_artifact_file(&final_path, spec)?;
        fs::remove_file(&partial_path)?;
        return Ok(receipt(
            spec,
            final_path,
            resumed_from,
            spec.size_bytes - resumed_from,
            true,
        ));
    }
    fs::rename(&partial_path, &final_path)?;
    verify_artifact_file(&final_path, spec)?;
    Ok(receipt(
        spec,
        final_path,
        resumed_from,
        spec.size_bytes - resumed_from,
        false,
    ))
}

fn receipt(
    spec: &ArtifactDownloadSpecV1,
    final_path: PathBuf,
    resumed_from_bytes: u64,
    downloaded_bytes: u64,
    reused_existing: bool,
) -> ArtifactDownloadReceiptV1 {
    ArtifactDownloadReceiptV1 {
        schema_version: 1,
        release_id: spec.release_id.clone(),
        release_generation: spec.release_generation,
        target: spec.target.clone(),
        kind: spec.kind,
        object_key: spec.object_key.clone(),
        sha256: spec.sha256.clone(),
        size_bytes: spec.size_bytes,
        final_path,
        resumed_from_bytes,
        downloaded_bytes,
        reused_existing,
        content_verified: true,
    }
}

fn validate_download_spec(spec: &ArtifactDownloadSpecV1) -> Result<(), UpdateDownloadError> {
    if spec.schema_version != 1 {
        return invalid_artifact("schema_version must be 1");
    }
    if spec.release_id.is_empty() || spec.release_id.len() > 160 {
        return invalid_artifact("release_id is invalid");
    }
    if spec.release_generation == 0 {
        return invalid_artifact("release_generation must be non-zero");
    }
    if !safe_component(&spec.target, 64) {
        return invalid_artifact("target is invalid");
    }
    if !safe_file_name(&spec.file_name) {
        return invalid_artifact("file_name is invalid");
    }
    if spec.object_key.rsplit('/').next() != Some(spec.file_name.as_str()) {
        return invalid_artifact("object_key filename does not match file_name");
    }
    if !lower_sha256(&spec.sha256) {
        return invalid_artifact("sha256 is invalid");
    }
    if spec.size_bytes == 0 || spec.size_bytes > MAX_ARTIFACT_BYTES {
        return invalid_artifact("size_bytes is outside the bounded artifact limit");
    }
    if let Some(signature) = &spec.platform_signature {
        if !safe_component(signature, 96) {
            return invalid_artifact("platform_signature is invalid");
        }
    }
    Ok(())
}

fn prepare_cache_root(cache_root: &Path) -> Result<(), UpdateDownloadError> {
    if !cache_root.is_absolute() {
        return Err(UpdateDownloadError::InvalidCacheRoot);
    }
    prepare_private_dir(cache_root)
}

fn prepare_private_dir(path: &Path) -> Result<(), UpdateDownloadError> {
    if path.is_symlink() {
        return Err(UpdateDownloadError::InvalidCacheRoot);
    }
    if path.exists() {
        if !path.is_dir() {
            return Err(UpdateDownloadError::InvalidCacheRoot);
        }
    } else {
        fs::create_dir_all(path)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn partial_length(path: &Path, maximum: u64) -> Result<u64, UpdateDownloadError> {
    if !path.exists() && !path.is_symlink() {
        return Ok(0);
    }
    if path.is_symlink() || !path.is_file() {
        return Err(UpdateDownloadError::InvalidArtifact(
            "partial download must be a regular non-symlink file".to_owned(),
        ));
    }
    let size = path.metadata()?.len();
    if size > maximum {
        return Err(UpdateDownloadError::SizeMismatch);
    }
    Ok(size)
}

fn open_partial(path: &Path, truncate: bool) -> Result<File, UpdateDownloadError> {
    if path.is_symlink() {
        return Err(UpdateDownloadError::InvalidArtifact(
            "partial download path is symlinked".to_owned(),
        ));
    }
    let mut options = OpenOptions::new();
    options.create(true).write(true);
    if truncate {
        options.truncate(true);
    } else {
        options.append(true);
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    Ok(options.open(path)?)
}

fn verify_artifact_file(
    path: &Path,
    spec: &ArtifactDownloadSpecV1,
) -> Result<(), UpdateDownloadError> {
    if path.is_symlink() || !path.is_file() {
        return Err(UpdateDownloadError::InvalidArtifact(
            "artifact path is missing or symlinked".to_owned(),
        ));
    }
    let metadata = path.metadata()?;
    if metadata.len() != spec.size_bytes {
        return Err(UpdateDownloadError::SizeMismatch);
    }
    let observed = sha256_file(path)?;
    if observed != spec.sha256 {
        return Err(UpdateDownloadError::DigestMismatch);
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, UpdateDownloadError> {
    let mut file = File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(hex_lower(&digest.finalize()))
}

fn safe_component(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+')
        })
}

fn safe_file_name(value: &str) -> bool {
    safe_component(value, 255) && value != "." && value != ".."
}

fn lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn hex_lower(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn invalid_artifact<T>(message: &str) -> Result<T, UpdateDownloadError> {
    Err(UpdateDownloadError::InvalidArtifact(message.to_owned()))
}

struct DownloadLock {
    path: PathBuf,
}

impl DownloadLock {
    fn acquire(path: &Path) -> Result<Self, UpdateDownloadError> {
        match fs::create_dir(path) {
            Ok(()) => Ok(Self {
                path: path.to_path_buf(),
            }),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                Err(UpdateDownloadError::Busy)
            }
            Err(error) => Err(UpdateDownloadError::Io(error)),
        }
    }
}

impl Drop for DownloadLock {
    fn drop(&mut self) {
        let _ = fs::remove_dir(&self.path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::release_control::{
        ProductTargetV1, ReleaseArtifactV1, ReleaseSecurityPolicyV1, ReleaseSourceV1,
    };
    use std::collections::BTreeMap;
    use std::time::{SystemTime, UNIX_EPOCH};

    struct MemoryRangeSource {
        bytes: Vec<u8>,
        calls: Vec<u64>,
        total_size_override: Option<u64>,
    }

    impl MemoryRangeSource {
        fn new(bytes: Vec<u8>) -> Self {
            Self {
                bytes,
                calls: Vec::new(),
                total_size_override: None,
            }
        }
    }

    impl ArtifactRangeSource for MemoryRangeSource {
        fn read_range(
            &mut self,
            start: u64,
            maximum_bytes: usize,
        ) -> Result<DownloadChunkV1, UpdateDownloadError> {
            self.calls.push(start);
            let start_index = usize::try_from(start).unwrap();
            if start_index >= self.bytes.len() {
                return Ok(DownloadChunkV1 {
                    start,
                    total_size: self.total_size_override.unwrap_or(self.bytes.len() as u64),
                    bytes: Vec::new(),
                });
            }
            let end = (start_index + maximum_bytes).min(self.bytes.len());
            Ok(DownloadChunkV1 {
                start,
                total_size: self.total_size_override.unwrap_or(self.bytes.len() as u64),
                bytes: self.bytes[start_index..end].to_vec(),
            })
        }
    }

    #[test]
    fn verified_release_binds_exact_download_spec() {
        let payload = b"installer".to_vec();
        let release = release_fixture(&payload);
        let report = verification_fixture(&release);
        let spec = download_spec_from_verified_release(
            &release,
            &report,
            "linux-x86_64",
            ReleaseArtifactKindV1::Installer,
        )
        .unwrap();
        assert_eq!(spec.release_id, release.release_id);
        assert_eq!(spec.sha256, sha256_bytes(&payload));
        assert_eq!(spec.file_name, "Hermes-1.0.1.deb");
    }

    #[test]
    fn downloader_resumes_partial_and_verifies_final_sha() {
        let payload = b"Hermes managed release payload".repeat(1024);
        let spec = direct_spec(&payload, ReleaseArtifactKindV1::ManagedReleasePayload);
        let cache = temp_root("resume");
        let digest_dir = cache.join(&spec.sha256[..2]).join(&spec.sha256);
        prepare_private_dir(&digest_dir).unwrap();
        let partial = digest_dir.join(format!(".{}.partial", spec.file_name));
        let prefix = 4096usize;
        fs::write(&partial, &payload[..prefix]).unwrap();

        let mut source = MemoryRangeSource::new(payload.clone());
        let receipt = download_verified_artifact(&spec, &mut source, &cache).unwrap();
        assert_eq!(receipt.resumed_from_bytes, prefix as u64);
        assert_eq!(receipt.downloaded_bytes, payload.len() as u64 - prefix as u64);
        assert_eq!(source.calls.first().copied(), Some(prefix as u64));
        assert_eq!(fs::read(receipt.final_path).unwrap(), payload);
        let _ = fs::remove_dir_all(cache);
    }

    #[test]
    fn complete_corrupt_partial_is_discarded_and_restarted() {
        let payload = b"correct artifact".repeat(1024);
        let spec = direct_spec(&payload, ReleaseArtifactKindV1::BootstrapPayload);
        let cache = temp_root("corrupt-partial");
        let digest_dir = cache.join(&spec.sha256[..2]).join(&spec.sha256);
        prepare_private_dir(&digest_dir).unwrap();
        let partial = digest_dir.join(format!(".{}.partial", spec.file_name));
        fs::write(&partial, vec![b'x'; payload.len()]).unwrap();

        let mut source = MemoryRangeSource::new(payload.clone());
        let receipt = download_verified_artifact(&spec, &mut source, &cache).unwrap();
        assert_eq!(source.calls.first().copied(), Some(0));
        assert_eq!(receipt.resumed_from_bytes, 0);
        assert_eq!(fs::read(receipt.final_path).unwrap(), payload);
        let _ = fs::remove_dir_all(cache);
    }

    #[test]
    fn source_size_mismatch_fails_without_promoting_partial() {
        let payload = b"signed content".repeat(1024);
        let spec = direct_spec(&payload, ReleaseArtifactKindV1::BootstrapPayload);
        let cache = temp_root("source-size");
        let mut source = MemoryRangeSource::new(payload.clone());
        source.total_size_override = Some(payload.len() as u64 + 1);
        let error = download_verified_artifact(&spec, &mut source, &cache).unwrap_err();
        assert!(matches!(error, UpdateDownloadError::InvalidRange(_)));
        let final_path = cache
            .join(&spec.sha256[..2])
            .join(&spec.sha256)
            .join(&spec.file_name);
        assert!(!final_path.exists());
        let _ = fs::remove_dir_all(cache);
    }

    #[test]
    fn digest_mismatch_removes_poisoned_partial() {
        let signed = b"signed content".repeat(1024);
        let wrong = b"wrong! content".repeat(1024);
        assert_eq!(signed.len(), wrong.len());
        let spec = direct_spec(&signed, ReleaseArtifactKindV1::BootstrapPayload);
        let cache = temp_root("digest-mismatch");
        let mut source = MemoryRangeSource::new(wrong);
        let error = download_verified_artifact(&spec, &mut source, &cache).unwrap_err();
        assert!(matches!(error, UpdateDownloadError::DigestMismatch));
        let digest_dir = cache.join(&spec.sha256[..2]).join(&spec.sha256);
        assert!(!digest_dir
            .join(format!(".{}.partial", spec.file_name))
            .exists());
        assert!(!digest_dir.join(&spec.file_name).exists());
        let _ = fs::remove_dir_all(cache);
    }

    #[test]
    fn existing_verified_final_is_reused_without_source_reads() {
        let payload = b"cached payload".repeat(1024);
        let spec = direct_spec(&payload, ReleaseArtifactKindV1::BootstrapPayload);
        let cache = temp_root("reuse");
        let digest_dir = cache.join(&spec.sha256[..2]).join(&spec.sha256);
        prepare_private_dir(&digest_dir).unwrap();
        fs::write(digest_dir.join(&spec.file_name), &payload).unwrap();
        let mut source = MemoryRangeSource::new(payload);
        let receipt = download_verified_artifact(&spec, &mut source, &cache).unwrap();
        assert!(receipt.reused_existing);
        assert!(source.calls.is_empty());
        let _ = fs::remove_dir_all(cache);
    }

    #[test]
    fn existing_download_lock_fails_closed() {
        let payload = b"payload".repeat(1024);
        let spec = direct_spec(&payload, ReleaseArtifactKindV1::BootstrapPayload);
        let cache = temp_root("busy");
        let digest_dir = cache.join(&spec.sha256[..2]).join(&spec.sha256);
        prepare_private_dir(&digest_dir).unwrap();
        fs::create_dir(digest_dir.join(format!(".{}.download-lock", spec.file_name))).unwrap();
        let mut source = MemoryRangeSource::new(payload);
        assert!(matches!(
            download_verified_artifact(&spec, &mut source, &cache),
            Err(UpdateDownloadError::Busy)
        ));
        let _ = fs::remove_dir_all(cache);
    }

    fn release_fixture(installer_payload: &[u8]) -> ProductReleaseManifestV1 {
        let installer = ReleaseArtifactV1 {
            object_key: format!(
                "artifacts/v1/sha256/{}/{}/Hermes-1.0.1.deb",
                &sha256_bytes(installer_payload)[..2],
                sha256_bytes(installer_payload)
            ),
            sha256: sha256_bytes(installer_payload),
            size_bytes: installer_payload.len() as u64,
            platform_signature: Some("linux-package-signature".to_owned()),
        };
        let side = ReleaseArtifactV1 {
            object_key: format!(
                "artifacts/v1/sha256/{}/{}/payload.tar.zst",
                &sha256_bytes(b"side")[..2],
                sha256_bytes(b"side")
            ),
            sha256: sha256_bytes(b"side"),
            size_bytes: 4,
            platform_signature: None,
        };
        let mut targets = BTreeMap::new();
        targets.insert(
            "linux-x86_64".to_owned(),
            ProductTargetV1 {
                minimum_os: "ubuntu-24.04".to_owned(),
                installer,
                bootstrap_payload: side.clone(),
                managed_release_payload: side,
            },
        );
        ProductReleaseManifestV1 {
            schema_version: 1,
            product: "hermes-desktop".to_owned(),
            product_version: "1.0.1".to_owned(),
            release_id: "1.0.1+20260807.1.g12345678".to_owned(),
            release_generation: 101,
            published_at: "2026-08-07T00:00:00Z".to_owned(),
            source: ReleaseSourceV1 {
                repository: "looooooooy/hermes".to_owned(),
                git_commit: "1".repeat(40),
                workflow_run_id: "123".to_owned(),
            },
            components: required_components(),
            contracts: required_contracts(),
            targets,
            security: ReleaseSecurityPolicyV1 {
                security_critical: false,
                minimum_safe_release_generation: 100,
                mandatory_after: None,
            },
        }
    }

    fn verification_fixture(
        release: &ProductReleaseManifestV1,
    ) -> ReleaseControlVerificationReportV1 {
        ReleaseControlVerificationReportV1 {
            schema_version: 1,
            product_version: release.product_version.clone(),
            release_id: release.release_id.clone(),
            release_generation: release.release_generation,
            channel: crate::release_control::ReleaseChannelV1::Stable,
            channel_generation: 10,
            block_generation: 5,
            effective_minimum_safe_generation: 100,
            rollback_authorized: false,
            decision: "forward_update".to_owned(),
            signatures_verified: true,
            eligible: true,
        }
    }

    fn direct_spec(payload: &[u8], kind: ReleaseArtifactKindV1) -> ArtifactDownloadSpecV1 {
        let digest = sha256_bytes(payload);
        ArtifactDownloadSpecV1 {
            schema_version: 1,
            release_id: "1.0.1+20260807.1.g12345678".to_owned(),
            release_generation: 101,
            target: "linux-x86_64".to_owned(),
            kind,
            object_key: format!("artifacts/v1/sha256/{}/{}/payload.bin", &digest[..2], digest),
            file_name: "payload.bin".to_owned(),
            sha256: sha256_bytes(payload),
            size_bytes: payload.len() as u64,
            platform_signature: None,
        }
    }

    fn sha256_bytes(bytes: &[u8]) -> String {
        hex_lower(&Sha256::digest(bytes))
    }

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("hermes-update-download-{name}-{nonce}"))
    }

    fn required_components() -> BTreeMap<String, String> {
        [
            ("desktop", "1.0.1"),
            ("runtime_manager", "1.0.1"),
            ("private_python", "3.13.14"),
            ("uv", "0.12.2"),
            ("core", "0.19.0-hermes.1"),
            ("plugin", "0.1.0"),
            ("connector", "0.1.0"),
        ]
        .into_iter()
        .map(|(name, version)| (name.to_owned(), version.to_owned()))
        .collect()
    }

    fn required_contracts() -> BTreeMap<String, u32> {
        [
            ("runtime", 1),
            ("host_spi", 1),
            ("local_protocol", 1),
            ("cloud_protocol", 1),
        ]
        .into_iter()
        .map(|(name, version)| (name.to_owned(), version))
        .collect()
    }
}
