use crate::model::{PlatformKind, ToolchainManifestV1};
use crate::{PrivateToolchainBundleV1, PrivateToolchainInstaller, ToolchainInstallError};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Output};
use thiserror::Error;

const LICENSE_EVIDENCE_NAME: &str = "LICENSE-EVIDENCE.json";
const UPSTREAM_SOURCE_NAME: &str = "UPSTREAM-SOURCE.json";
const BUNDLE_MANIFEST_NAME: &str = "TOOLCHAIN-BUNDLE.json";
const PROHIBITED_HOST_TOOLS: &[&str] = &[
    "python", "python3", "pip", "pip3", "uv", "git", "node", "npm", "cargo", "rustc",
];
const MAX_EVIDENCE_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BlankMachineGateReport {
    pub schema_version: u8,
    pub scope: String,
    pub platform: PlatformKind,
    pub architecture: String,
    pub installed_root: PathBuf,
    pub python_version: String,
    pub uv_version: String,
    pub prohibited_host_tools: Vec<String>,
    pub upstream_license_files: usize,
    pub runtime_license_files: usize,
    pub host_path_isolated: bool,
    pub provenance_verified: bool,
    pub private_runtime_executed: bool,
    pub full_product_lifecycle_verified: bool,
}

#[derive(Debug, Error)]
pub enum BlankMachineGateError {
    #[error("blank-machine gate input is invalid: {0}")]
    Invalid(&'static str),
    #[error("blank-machine gate failed: {0}")]
    Failed(String),
    #[error(transparent)]
    Toolchain(#[from] ToolchainInstallError),
    #[error("blank-machine gate I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("blank-machine gate JSON is invalid: {0}")]
    Json(#[from] serde_json::Error),
}

#[derive(Debug, Deserialize)]
struct LicenseEvidenceV1 {
    schema_version: u8,
    scope: String,
    legal_sufficiency_asserted: bool,
    upstream_license_files: Vec<LicenseEvidenceFileV1>,
    runtime_license_files: Vec<LicenseEvidenceFileV1>,
}

#[derive(Debug, Deserialize)]
struct LicenseEvidenceFileV1 {
    bundle_path: String,
    sha256: String,
}

#[derive(Debug, Deserialize)]
struct UpstreamSourceV1 {
    schema_version: u8,
    python: serde_json::Value,
    uv: serde_json::Value,
}

pub fn run_blank_machine_toolchain_gate(
    qualified_bundle_root: &Path,
    sandbox_root: &Path,
) -> Result<BlankMachineGateReport, BlankMachineGateError> {
    validate_absolute_input(qualified_bundle_root, true)?;
    validate_absolute_input(sandbox_root, false)?;
    if sandbox_root.exists() || sandbox_root.is_symlink() {
        return Err(BlankMachineGateError::Invalid(
            "sandbox root must not exist before the gate",
        ));
    }

    assert_host_developer_tools_unavailable()?;
    let bundle = load_qualified_bundle(qualified_bundle_root)?;
    let toolchains_root = sandbox_root.join("toolchains");
    let manifest = PrivateToolchainInstaller::install(qualified_bundle_root, &toolchains_root)?;
    let installed_root = toolchains_root.join(&bundle.bundle_id);

    let (upstream_license_files, runtime_license_files) =
        verify_installed_provenance(&installed_root)?;
    let python_version = execute_private_python(&manifest)?;
    let uv_version = execute_private_uv(&manifest)?;

    Ok(BlankMachineGateReport {
        schema_version: 1,
        scope: "zero_host_dependency_private_toolchain".to_owned(),
        platform: manifest.platform,
        architecture: manifest.architecture.clone(),
        installed_root,
        python_version,
        uv_version,
        prohibited_host_tools: PROHIBITED_HOST_TOOLS
            .iter()
            .map(|value| (*value).to_owned())
            .collect(),
        upstream_license_files,
        runtime_license_files,
        host_path_isolated: true,
        provenance_verified: true,
        private_runtime_executed: true,
        // This gate intentionally stops at the private runtime boundary. ServiceManager,
        // SecretStore, login/pair, Cloud connectivity, and live Session acceptance are
        // separate product-closure gates and must never be inferred from this result.
        full_product_lifecycle_verified: false,
    })
}

fn load_qualified_bundle(
    root: &Path,
) -> Result<PrivateToolchainBundleV1, BlankMachineGateError> {
    let manifest_path = root.join(BUNDLE_MANIFEST_NAME);
    checked_regular_file(&manifest_path)?;
    let bundle: PrivateToolchainBundleV1 = serde_json::from_slice(&read_bounded(
        &manifest_path,
        MAX_EVIDENCE_BYTES,
        "toolchain bundle manifest is too large",
    )?)?;

    let declared = bundle
        .files
        .iter()
        .map(|entry| entry.path.as_str())
        .collect::<std::collections::BTreeSet<_>>();
    for required in [LICENSE_EVIDENCE_NAME, UPSTREAM_SOURCE_NAME] {
        if !declared.contains(required) {
            return Err(BlankMachineGateError::Invalid(
                "qualified bundle does not declare required provenance evidence",
            ));
        }
    }
    Ok(bundle)
}

fn assert_host_developer_tools_unavailable() -> Result<(), BlankMachineGateError> {
    for tool in PROHIBITED_HOST_TOOLS {
        match Command::new(tool).arg("--version").output() {
            Ok(output) => {
                return Err(BlankMachineGateError::Failed(format!(
                    "host developer tool unexpectedly resolves from PATH: {tool} (status {})",
                    output.status
                )))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(BlankMachineGateError::Failed(format!(
                    "could not prove host developer tool is unavailable: {tool}: {error}"
                )))
            }
        }
    }
    Ok(())
}

fn verify_installed_provenance(root: &Path) -> Result<(usize, usize), BlankMachineGateError> {
    if root.is_symlink() || !root.is_dir() {
        return Err(BlankMachineGateError::Invalid(
            "installed toolchain root is missing or symlinked",
        ));
    }
    let canonical_root = root.canonicalize()?;
    let evidence_path = root.join(LICENSE_EVIDENCE_NAME);
    let source_path = root.join(UPSTREAM_SOURCE_NAME);
    require_file_inside(&canonical_root, &evidence_path, "license evidence")?;
    require_file_inside(&canonical_root, &source_path, "upstream source evidence")?;

    let evidence: LicenseEvidenceV1 = serde_json::from_slice(&read_bounded(
        &evidence_path,
        MAX_EVIDENCE_BYTES,
        "license evidence is too large",
    )?)?;
    if evidence.schema_version != 1
        || evidence.scope != "engineering_source_and_license_provenance"
        || evidence.legal_sufficiency_asserted
    {
        return Err(BlankMachineGateError::Invalid(
            "installed license evidence contract is invalid",
        ));
    }
    if evidence.upstream_license_files.len() < 3 || evidence.runtime_license_files.is_empty() {
        return Err(BlankMachineGateError::Invalid(
            "installed provenance evidence is incomplete",
        ));
    }

    for entry in evidence
        .upstream_license_files
        .iter()
        .chain(evidence.runtime_license_files.iter())
    {
        let relative = safe_relative(&entry.bundle_path)?;
        if !is_sha256(&entry.sha256) {
            return Err(BlankMachineGateError::Invalid(
                "installed provenance evidence contains an invalid SHA-256",
            ));
        }
        let path = root.join(relative);
        require_file_inside(&canonical_root, &path, "provenance file")?;
        let actual = sha256_file(&path)?;
        if actual != entry.sha256 {
            return Err(BlankMachineGateError::Failed(format!(
                "installed provenance digest mismatch: {}",
                entry.bundle_path
            )));
        }
    }

    let upstream: UpstreamSourceV1 = serde_json::from_slice(&read_bounded(
        &source_path,
        MAX_EVIDENCE_BYTES,
        "upstream source evidence is too large",
    )?)?;
    if upstream.schema_version != 1 || !upstream.python.is_object() || !upstream.uv.is_object() {
        return Err(BlankMachineGateError::Invalid(
            "installed upstream source evidence is incomplete",
        ));
    }

    Ok((
        evidence.upstream_license_files.len(),
        evidence.runtime_license_files.len(),
    ))
}

fn execute_private_python(
    manifest: &ToolchainManifestV1,
) -> Result<String, BlankMachineGateError> {
    let executable = canonical_regular_file(&manifest.python.path, "private Python")?;
    let mut command = isolated_command(&executable);
    command.args([
        "-I",
        "-c",
        "import sys; print(sys.version.split()[0]); print(sys.executable)",
    ]);
    let output = run_checked(command, "private Python")?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let lines = stdout.lines().collect::<Vec<_>>();
    if lines.len() < 2 || lines[0] != manifest.python.version {
        return Err(BlankMachineGateError::Failed(format!(
            "private Python version mismatch: expected {}, got {:?}",
            manifest.python.version, lines.first()
        )));
    }
    let reported_executable = PathBuf::from(lines[1]).canonicalize()?;
    if reported_executable != executable {
        return Err(BlankMachineGateError::Failed(
            "private Python resolved a different interpreter than the manifest path".to_owned(),
        ));
    }
    Ok(lines[0].to_owned())
}

fn execute_private_uv(manifest: &ToolchainManifestV1) -> Result<String, BlankMachineGateError> {
    let executable = canonical_regular_file(&manifest.uv.path, "private uv")?;
    let mut command = isolated_command(&executable);
    command.arg("--version");
    let output = run_checked(command, "private uv")?;
    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    let expected = format!("uv {}", manifest.uv.version);
    if stdout != expected && !stdout.starts_with(&(expected.clone() + " ")) {
        return Err(BlankMachineGateError::Failed(format!(
            "private uv version mismatch: expected prefix {expected:?}, got {stdout:?}"
        )));
    }
    Ok(stdout)
}

fn isolated_command(executable: &Path) -> Command {
    let mut command = Command::new(executable);
    for key in [
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "PIP_CONFIG_FILE",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "UV",
        "UV_PYTHON",
        "UV_PROJECT_ENVIRONMENT",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
    ] {
        command.env_remove(key);
    }
    command.env("PYTHONNOUSERSITE", "1");
    command.env("UV_OFFLINE", "1");
    command
}

fn run_checked(mut command: Command, label: &str) -> Result<Output, BlankMachineGateError> {
    let output = command.output()?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        return Err(BlankMachineGateError::Failed(format!(
            "{label} failed under isolated host PATH with status {}: {}",
            output.status,
            stderr.trim()
        )));
    }
    Ok(output)
}

fn validate_absolute_input(path: &Path, must_exist: bool) -> Result<(), BlankMachineGateError> {
    if !path.is_absolute() {
        return Err(BlankMachineGateError::Invalid("gate paths must be absolute"));
    }
    if path.is_symlink() {
        return Err(BlankMachineGateError::Invalid("gate paths cannot be symlinks"));
    }
    if must_exist && !path.is_dir() {
        return Err(BlankMachineGateError::Invalid(
            "qualified bundle root does not exist",
        ));
    }
    Ok(())
}

fn checked_regular_file(path: &Path) -> Result<fs::Metadata, BlankMachineGateError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(BlankMachineGateError::Invalid(
            "gate input must be a regular non-symlink file",
        ));
    }
    Ok(metadata)
}

fn canonical_regular_file(path: &Path, label: &str) -> Result<PathBuf, BlankMachineGateError> {
    if !path.is_absolute() || path.is_symlink() || !path.is_file() {
        return Err(BlankMachineGateError::Failed(format!(
            "{label} is not an absolute regular file: {}",
            path.display()
        )));
    }
    Ok(path.canonicalize()?)
}

fn require_file_inside(
    canonical_root: &Path,
    path: &Path,
    label: &str,
) -> Result<(), BlankMachineGateError> {
    checked_regular_file(path)?;
    let canonical = path.canonicalize()?;
    if !canonical.starts_with(canonical_root) {
        return Err(BlankMachineGateError::Failed(format!(
            "{label} escaped the immutable toolchain root: {}",
            path.display()
        )));
    }
    Ok(())
}

fn safe_relative(value: &str) -> Result<PathBuf, BlankMachineGateError> {
    let path = Path::new(value);
    if value.is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(BlankMachineGateError::Invalid(
            "provenance path must be a safe relative path",
        ));
    }
    Ok(path.to_path_buf())
}

fn read_bounded(
    path: &Path,
    maximum: u64,
    too_large: &'static str,
) -> Result<Vec<u8>, BlankMachineGateError> {
    let metadata = checked_regular_file(path)?;
    if metadata.len() > maximum {
        return Err(BlankMachineGateError::Invalid(too_large));
    }
    Ok(fs::read(path)?)
}

fn sha256_file(path: &Path) -> Result<String, BlankMachineGateError> {
    let mut file = fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(hex_digest(&digest.finalize()))
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
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provenance_paths_cannot_escape_toolchain_root() {
        assert!(safe_relative("licenses/upstream/uv/LICENSE-MIT").is_ok());
        assert!(safe_relative("../outside").is_err());
        assert!(safe_relative("/absolute").is_err());
    }

    #[test]
    fn sha256_contract_requires_lowercase_hex() {
        assert!(is_sha256(
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ));
        assert!(!is_sha256(
            "0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF"
        ));
    }
}
