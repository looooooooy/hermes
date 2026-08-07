use crate::model::{PlatformKind, ToolchainArtifactV1, ToolchainManifestV1};
use crate::platform::current_platform;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use thiserror::Error;

const BUNDLE_SCHEMA_V1: u8 = 1;
const BUNDLE_MANIFEST_NAME: &str = "TOOLCHAIN-BUNDLE.json";
const INSTALLED_MANIFEST_NAME: &str = "TOOLCHAIN-MANIFEST.json";
const MAX_MANIFEST_BYTES: u64 = 512 * 1024;
const MAX_FILE_BYTES: u64 = 1024 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolchainBundleFileV1 {
    pub path: String,
    pub sha256: String,
    pub executable: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PrivateToolchainBundleV1 {
    pub schema_version: u8,
    pub bundle_id: String,
    pub platform: PlatformKind,
    pub architecture: String,
    pub python_version: String,
    pub uv_version: String,
    pub python_path: String,
    pub uv_path: String,
    pub files: Vec<ToolchainBundleFileV1>,
}

#[derive(Debug, Error)]
pub enum ToolchainInstallError {
    #[error("toolchain bundle is invalid: {0}")]
    Invalid(&'static str),
    #[error("toolchain bundle integrity failed: {0}")]
    Integrity(String),
    #[error("toolchain I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("toolchain manifest JSON is invalid: {0}")]
    Json(#[from] serde_json::Error),
}

pub struct PrivateToolchainInstaller;

impl PrivateToolchainInstaller {
    pub fn install(
        source_root: &Path,
        toolchains_root: &Path,
    ) -> Result<ToolchainManifestV1, ToolchainInstallError> {
        validate_absolute_root(source_root, "source root")?;
        validate_absolute_root(toolchains_root, "toolchains root")?;
        let bundle = load_bundle(source_root)?;
        validate_bundle(&bundle)?;
        verify_source_tree(source_root, &bundle)?;

        fs::create_dir_all(toolchains_root)?;
        harden_directory(toolchains_root)?;
        let final_root = toolchains_root.join(&bundle.bundle_id);
        let stage_root = toolchains_root.join(format!(
            ".{}.staging.{}",
            bundle.bundle_id,
            std::process::id()
        ));

        if stage_root.exists() {
            if stage_root.is_symlink() {
                return Err(ToolchainInstallError::Invalid(
                    "staging destination is a symlink",
                ));
            }
            fs::remove_dir_all(&stage_root)?;
        }

        if final_root.exists() {
            return verify_existing_install(&final_root, &bundle);
        }

        fs::create_dir(&stage_root)?;
        harden_directory(&stage_root)?;
        let result = (|| {
            for file in &bundle.files {
                let relative = safe_relative(&file.path)?;
                let source = source_root.join(&relative);
                let destination = stage_root.join(&relative);
                if let Some(parent) = destination.parent() {
                    fs::create_dir_all(parent)?;
                    harden_directory(parent)?;
                }
                copy_verified(&source, &destination, &file.sha256, file.executable)?;
            }

            let installed = build_installed_manifest(&stage_root, &bundle)?;
            let manifest_bytes = serde_json::to_vec_pretty(&installed)?;
            write_private_file(&stage_root.join(INSTALLED_MANIFEST_NAME), &manifest_bytes, false)?;
            fs::rename(&stage_root, &final_root)?;
            verify_existing_install(&final_root, &bundle)
        })();

        if result.is_err() && stage_root.exists() && !stage_root.is_symlink() {
            let _ = fs::remove_dir_all(&stage_root);
        }
        result
    }
}

fn load_bundle(source_root: &Path) -> Result<PrivateToolchainBundleV1, ToolchainInstallError> {
    let path = source_root.join(BUNDLE_MANIFEST_NAME);
    let metadata = checked_regular_file(&path)?;
    if metadata.len() > MAX_MANIFEST_BYTES {
        return Err(ToolchainInstallError::Invalid("bundle manifest is too large"));
    }
    let bytes = fs::read(path)?;
    Ok(serde_json::from_slice(&bytes)?)
}

fn validate_bundle(bundle: &PrivateToolchainBundleV1) -> Result<(), ToolchainInstallError> {
    if bundle.schema_version != BUNDLE_SCHEMA_V1 {
        return Err(ToolchainInstallError::Invalid(
            "unsupported toolchain bundle schema",
        ));
    }
    if bundle.platform != current_platform() {
        return Err(ToolchainInstallError::Invalid(
            "toolchain bundle platform does not match host",
        ));
    }
    if bundle.architecture != std::env::consts::ARCH {
        return Err(ToolchainInstallError::Invalid(
            "toolchain bundle architecture does not match host",
        ));
    }
    if bundle.bundle_id.is_empty()
        || bundle.bundle_id.len() > 128
        || !bundle
            .bundle_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_'))
    {
        return Err(ToolchainInstallError::Invalid("invalid toolchain bundle id"));
    }
    if bundle.python_version.trim().is_empty() || bundle.uv_version.trim().is_empty() {
        return Err(ToolchainInstallError::Invalid("toolchain versions are empty"));
    }
    let python = safe_relative(&bundle.python_path)?;
    let uv = safe_relative(&bundle.uv_path)?;
    let mut paths = BTreeSet::new();
    let mut python_declared = false;
    let mut uv_declared = false;
    for file in &bundle.files {
        let path = safe_relative(&file.path)?;
        if !is_sha256(&file.sha256) {
            return Err(ToolchainInstallError::Invalid("invalid file sha256"));
        }
        if !paths.insert(path.clone()) {
            return Err(ToolchainInstallError::Invalid("duplicate toolchain file"));
        }
        if path == python {
            python_declared = file.executable;
        }
        if path == uv {
            uv_declared = file.executable;
        }
    }
    if !python_declared || !uv_declared {
        return Err(ToolchainInstallError::Invalid(
            "python and uv must be declared executable files",
        ));
    }
    Ok(())
}

fn verify_source_tree(
    source_root: &Path,
    bundle: &PrivateToolchainBundleV1,
) -> Result<(), ToolchainInstallError> {
    for file in &bundle.files {
        let path = source_root.join(safe_relative(&file.path)?);
        let metadata = checked_regular_file(&path)?;
        if metadata.len() > MAX_FILE_BYTES {
            return Err(ToolchainInstallError::Invalid("toolchain file is too large"));
        }
        if sha256_file(&path)? != file.sha256 {
            return Err(ToolchainInstallError::Integrity(file.path.clone()));
        }
    }
    Ok(())
}

fn build_installed_manifest(
    root: &Path,
    bundle: &PrivateToolchainBundleV1,
) -> Result<ToolchainManifestV1, ToolchainInstallError> {
    let python = root.join(safe_relative(&bundle.python_path)?);
    let uv = root.join(safe_relative(&bundle.uv_path)?);
    let python_entry = bundle
        .files
        .iter()
        .find(|file| file.path == bundle.python_path)
        .ok_or(ToolchainInstallError::Invalid("python file is missing"))?;
    let uv_entry = bundle
        .files
        .iter()
        .find(|file| file.path == bundle.uv_path)
        .ok_or(ToolchainInstallError::Invalid("uv file is missing"))?;
    Ok(ToolchainManifestV1 {
        schema_version: 1,
        platform: bundle.platform,
        architecture: bundle.architecture.clone(),
        python: ToolchainArtifactV1 {
            path: python,
            sha256: python_entry.sha256.clone(),
            version: bundle.python_version.clone(),
        },
        uv: ToolchainArtifactV1 {
            path: uv,
            sha256: uv_entry.sha256.clone(),
            version: bundle.uv_version.clone(),
        },
        offline_only: true,
    })
}

fn verify_existing_install(
    final_root: &Path,
    bundle: &PrivateToolchainBundleV1,
) -> Result<ToolchainManifestV1, ToolchainInstallError> {
    if final_root.is_symlink() || !final_root.is_dir() {
        return Err(ToolchainInstallError::Invalid(
            "existing toolchain destination is not a directory",
        ));
    }
    for file in &bundle.files {
        let path = final_root.join(safe_relative(&file.path)?);
        checked_regular_file(&path)?;
        if sha256_file(&path)? != file.sha256 {
            return Err(ToolchainInstallError::Integrity(format!(
                "published toolchain file changed: {}",
                file.path
            )));
        }
    }
    let expected = build_installed_manifest(final_root, bundle)?;
    let manifest_path = final_root.join(INSTALLED_MANIFEST_NAME);
    let actual: ToolchainManifestV1 = serde_json::from_slice(&fs::read(&manifest_path)?)?;
    if actual != expected {
        return Err(ToolchainInstallError::Integrity(
            "installed toolchain manifest mismatch".to_owned(),
        ));
    }
    Ok(actual)
}

fn copy_verified(
    source: &Path,
    destination: &Path,
    expected_sha256: &str,
    executable: bool,
) -> Result<(), ToolchainInstallError> {
    checked_regular_file(source)?;
    let mut input = fs::File::open(source)?;
    let mut output = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(destination)?;
    let mut buffer = [0u8; 1024 * 1024];
    let mut digest = Sha256::new();
    let mut total = 0u64;
    loop {
        let read = input.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        total += read as u64;
        if total > MAX_FILE_BYTES {
            return Err(ToolchainInstallError::Invalid("toolchain file is too large"));
        }
        digest.update(&buffer[..read]);
        output.write_all(&buffer[..read])?;
    }
    output.flush()?;
    let actual = hex_digest(digest.finalize().as_slice());
    if actual != expected_sha256 {
        return Err(ToolchainInstallError::Integrity(
            destination.display().to_string(),
        ));
    }
    harden_file(destination, executable)?;
    Ok(())
}

fn write_private_file(
    path: &Path,
    payload: &[u8],
    executable: bool,
) -> Result<(), ToolchainInstallError> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    file.write_all(payload)?;
    file.flush()?;
    harden_file(path, executable)
}

fn checked_regular_file(path: &Path) -> Result<fs::Metadata, ToolchainInstallError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(ToolchainInstallError::Invalid(
            "toolchain input must be a regular non-symlink file",
        ));
    }
    Ok(metadata)
}

fn safe_relative(value: &str) -> Result<PathBuf, ToolchainInstallError> {
    let path = Path::new(value);
    if value.is_empty() || path.is_absolute() {
        return Err(ToolchainInstallError::Invalid("toolchain path must be relative"));
    }
    if path.components().any(|component| !matches!(component, Component::Normal(_))) {
        return Err(ToolchainInstallError::Invalid("unsafe toolchain relative path"));
    }
    Ok(path.to_path_buf())
}

fn validate_absolute_root(path: &Path, _label: &'static str) -> Result<(), ToolchainInstallError> {
    if !path.is_absolute() {
        return Err(ToolchainInstallError::Invalid("toolchain root must be absolute"));
    }
    if path.exists() && path.is_symlink() {
        return Err(ToolchainInstallError::Invalid("toolchain root cannot be a symlink"));
    }
    Ok(())
}

fn sha256_file(path: &Path) -> Result<String, ToolchainInstallError> {
    let mut file = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(hex_digest(digest.finalize().as_slice()))
}

fn hex_digest(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(HEX[(byte >> 4) as usize] as char);
        output.push(HEX[(byte & 0x0f) as usize] as char);
    }
    output
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[cfg(unix)]
fn harden_directory(path: &Path) -> Result<(), ToolchainInstallError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}

#[cfg(windows)]
fn harden_directory(_path: &Path) -> Result<(), ToolchainInstallError> {
    Ok(())
}

#[cfg(unix)]
fn harden_file(path: &Path, executable: bool) -> Result<(), ToolchainInstallError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(
        path,
        fs::Permissions::from_mode(if executable { 0o500 } else { 0o400 }),
    )?;
    Ok(())
}

#[cfg(windows)]
fn harden_file(_path: &Path, _executable: bool) -> Result<(), ToolchainInstallError> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        std::env::temp_dir().join(format!("hermes-{name}-{}-{unique}", std::process::id()))
    }

    fn write_bundle(source: &Path) -> PrivateToolchainBundleV1 {
        fs::create_dir_all(source.join("python/bin")).expect("python dir");
        fs::create_dir_all(source.join("uv/bin")).expect("uv dir");
        let python = source.join("python/bin/python3");
        let uv = source.join("uv/bin/uv");
        fs::write(&python, b"private-python").expect("python");
        fs::write(&uv, b"private-uv").expect("uv");
        let bundle = PrivateToolchainBundleV1 {
            schema_version: 1,
            bundle_id: "cpython-3.13-test".to_owned(),
            platform: current_platform(),
            architecture: std::env::consts::ARCH.to_owned(),
            python_version: "3.13.test".to_owned(),
            uv_version: "0.test".to_owned(),
            python_path: "python/bin/python3".to_owned(),
            uv_path: "uv/bin/uv".to_owned(),
            files: vec![
                ToolchainBundleFileV1 {
                    path: "python/bin/python3".to_owned(),
                    sha256: sha256_file(&python).expect("python digest"),
                    executable: true,
                },
                ToolchainBundleFileV1 {
                    path: "uv/bin/uv".to_owned(),
                    sha256: sha256_file(&uv).expect("uv digest"),
                    executable: true,
                },
            ],
        };
        fs::write(
            source.join(BUNDLE_MANIFEST_NAME),
            serde_json::to_vec_pretty(&bundle).expect("manifest"),
        )
        .expect("write manifest");
        bundle
    }

    #[test]
    fn installs_verified_toolchain_atomically_and_reuses_identical_install() {
        let root = temp_root("toolchain-install");
        let source = root.join("source");
        let destination = root.join("managed/toolchains");
        let bundle = write_bundle(&source);
        let manifest = PrivateToolchainInstaller::install(&source, &destination).expect("install");
        assert_eq!(manifest.python.version, bundle.python_version);
        assert_eq!(manifest.uv.version, bundle.uv_version);
        assert!(manifest.python.path.starts_with(destination.join(&bundle.bundle_id)));
        assert!(manifest.offline_only);

        let reused = PrivateToolchainInstaller::install(&source, &destination).expect("reuse");
        assert_eq!(reused, manifest);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rejects_modified_source_before_publish() {
        let root = temp_root("toolchain-tamper");
        let source = root.join("source");
        let destination = root.join("managed/toolchains");
        write_bundle(&source);
        fs::write(source.join("python/bin/python3"), b"tampered").expect("tamper");

        let error = PrivateToolchainInstaller::install(&source, &destination)
            .expect_err("tamper must fail");
        assert!(matches!(error, ToolchainInstallError::Integrity(_)));
        assert!(!destination.join("cpython-3.13-test").exists());
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(unix)]
    #[test]
    fn rejects_symlinked_toolchain_input() {
        use std::os::unix::fs::symlink;

        let root = temp_root("toolchain-symlink");
        let source = root.join("source");
        let destination = root.join("managed/toolchains");
        write_bundle(&source);
        let python = source.join("python/bin/python3");
        fs::remove_file(&python).expect("remove python");
        symlink(source.join("uv/bin/uv"), &python).expect("symlink python");

        let error = PrivateToolchainInstaller::install(&source, &destination)
            .expect_err("symlink must fail");
        assert!(matches!(error, ToolchainInstallError::Invalid(_)));
        let _ = fs::remove_dir_all(root);
    }
}
