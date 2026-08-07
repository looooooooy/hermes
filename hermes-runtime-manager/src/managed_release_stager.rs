use crate::managed_payload_archive::unpack_managed_payload;
use crate::ports::PortError;
use crate::update_coordinator::{StagedReleaseV1, UpdateReleaseStager};
use crate::update_download::ArtifactDownloadReceiptV1;
use serde::Deserialize;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const MAX_INSTALLER_OUTPUT_BYTES: usize = 1024 * 1024;

#[derive(Debug)]
pub struct PrivatePythonManagedReleaseStager {
    private_python: PathBuf,
    installer_zipapp: PathBuf,
    runtime_manager: PathBuf,
    qualified_toolchain_root: PathBuf,
    releases_root: PathBuf,
    staging_root: PathBuf,
    target: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LocalInstallerReceiptV1 {
    schema_version: u8,
    release_id: String,
    target: String,
    release_path: PathBuf,
    release_digest: String,
    content_verified: bool,
    private_toolchain_used: bool,
    network_dependency_install_allowed: bool,
    reused_existing: bool,
}

impl PrivatePythonManagedReleaseStager {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        private_python: PathBuf,
        installer_zipapp: PathBuf,
        runtime_manager: PathBuf,
        qualified_toolchain_root: PathBuf,
        releases_root: PathBuf,
        staging_root: PathBuf,
        target: String,
    ) -> Result<Self, PortError> {
        require_regular(&private_python, "Private Python")?;
        require_regular(&installer_zipapp, "Managed Release installer zipapp")?;
        require_regular(&runtime_manager, "Runtime Manager")?;
        require_directory(&qualified_toolchain_root, "qualified Toolchain root")?;
        prepare_root(&releases_root, "releases root")?;
        prepare_root(&staging_root, "update staging root")?;
        if !safe_component(&target, 64) {
            return Err(PortError::Operation("managed release target is invalid".to_owned()));
        }
        Ok(Self {
            private_python,
            installer_zipapp,
            runtime_manager,
            qualified_toolchain_root,
            releases_root,
            staging_root,
            target,
        })
    }

    pub fn stage_archive(
        &self,
        archive: &Path,
        release_id: &str,
        release_generation: u64,
    ) -> Result<StagedReleaseV1, PortError> {
        if release_generation == 0 || !safe_component(release_id, 160) {
            return Err(PortError::Operation("managed release identity is invalid".to_owned()));
        }
        require_regular(archive, "Managed Release archive")?;
        let staging = self.staging_root.join(format!("payload-{release_id}"));
        if staging.is_symlink() {
            return Err(PortError::Operation("Managed Release staging path is symlinked".to_owned()));
        }
        if staging.exists() {
            fs::remove_dir_all(&staging)?;
        }
        if unpack_managed_payload(archive, &staging).is_err() {
            let _ = fs::remove_dir_all(&staging);
            return Err(PortError::Operation(
                "Managed Release archive extraction failed".to_owned(),
            ));
        }

        let result = self.run_installer(&staging, release_id);
        let _ = fs::remove_dir_all(&staging);
        let receipt = result?;
        self.validate_receipt(&receipt, release_id)?;
        Ok(StagedReleaseV1 {
            release_id: release_id.to_owned(),
            release_generation,
            release_path: receipt.release_path,
            content_verified: true,
        })
    }

    fn run_installer(
        &self,
        payload_root: &Path,
        release_id: &str,
    ) -> Result<LocalInstallerReceiptV1, PortError> {
        let mut command = Command::new(&self.private_python);
        command
            .arg("-I")
            .arg(&self.installer_zipapp)
            .arg("--payload")
            .arg(payload_root)
            .arg("--runtime-manager")
            .arg(&self.runtime_manager)
            .arg("--qualified-toolchain")
            .arg(&self.qualified_toolchain_root)
            .arg("--releases-root")
            .arg(&self.releases_root)
            .arg("--expected-release-id")
            .arg(release_id)
            .arg("--expected-target")
            .arg(&self.target)
            .env("PATH", "")
            .env("PYTHONNOUSERSITE", "1")
            .env("UV_OFFLINE", "1")
            .env("UV_NO_SYSTEM_CONFIG", "1")
            .env("UV_NO_PYTHON_DOWNLOADS", "1")
            .env_remove("PYTHONHOME")
            .env_remove("PYTHONPATH")
            .env_remove("VIRTUAL_ENV")
            .env_remove("UV_PYTHON")
            .env_remove("UV_PROJECT_ENVIRONMENT")
            .env_remove("UV_TOOL_BIN_DIR");
        let output = command
            .output()
            .map_err(|_| PortError::Operation("Managed Release installer launch failed".to_owned()))?;
        if !output.status.success() {
            return Err(PortError::Operation(
                "Managed Release installer failed closed".to_owned(),
            ));
        }
        if output.stdout.is_empty() || output.stdout.len() > MAX_INSTALLER_OUTPUT_BYTES {
            return Err(PortError::Operation(
                "Managed Release installer receipt is empty or oversized".to_owned(),
            ));
        }
        serde_json::from_slice(&output.stdout).map_err(|_| {
            PortError::Operation("Managed Release installer receipt is invalid JSON".to_owned())
        })
    }

    fn validate_receipt(
        &self,
        receipt: &LocalInstallerReceiptV1,
        release_id: &str,
    ) -> Result<(), PortError> {
        if receipt.schema_version != 1
            || receipt.release_id != release_id
            || receipt.target != self.target
            || !receipt.content_verified
            || !receipt.private_toolchain_used
            || receipt.network_dependency_install_allowed
            || !lower_sha256(&receipt.release_digest)
        {
            return Err(PortError::Operation(
                "Managed Release installer receipt does not prove a verified offline release"
                    .to_owned(),
            ));
        }
        let release_path = receipt
            .release_path
            .canonicalize()
            .map_err(|_| PortError::Operation("assembled release path does not exist".to_owned()))?;
        let releases_root = self.releases_root.canonicalize()?;
        if release_path.parent() != Some(releases_root.as_path())
            || release_path.file_name() != Some(std::ffi::OsStr::new(release_id))
            || release_path.is_symlink()
            || !release_path.is_dir()
        {
            return Err(PortError::Operation(
                "assembled release escaped the immutable releases root".to_owned(),
            ));
        }
        let _ = receipt.reused_existing;
        Ok(())
    }
}

impl UpdateReleaseStager for PrivatePythonManagedReleaseStager {
    fn stage(
        &self,
        receipt: &ArtifactDownloadReceiptV1,
        release_id: &str,
        release_generation: u64,
    ) -> Result<StagedReleaseV1, PortError> {
        if !receipt.content_verified {
            return Err(PortError::Operation(
                "Managed Release staging requires a content-verified download".to_owned(),
            ));
        }
        self.stage_archive(&receipt.final_path, release_id, release_generation)
    }
}

fn require_regular(path: &Path, label: &str) -> Result<(), PortError> {
    if !path.is_absolute() || path.is_symlink() || !path.is_file() {
        return Err(PortError::Operation(format!(
            "{label} must be an absolute regular non-symlink file"
        )));
    }
    Ok(())
}

fn require_directory(path: &Path, label: &str) -> Result<(), PortError> {
    if !path.is_absolute() || path.is_symlink() || !path.is_dir() {
        return Err(PortError::Operation(format!(
            "{label} must be an absolute regular directory"
        )));
    }
    Ok(())
}

fn prepare_root(path: &Path, label: &str) -> Result<(), PortError> {
    if !path.is_absolute() || path.is_symlink() {
        return Err(PortError::Operation(format!(
            "{label} must be absolute and non-symlinked"
        )));
    }
    if path.exists() {
        if !path.is_dir() {
            return Err(PortError::Operation(format!("{label} is not a directory")));
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

fn safe_component(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value != "."
        && value != ".."
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+' | b':')
        })
}

fn lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(1);

    #[test]
    fn constructor_rejects_non_absolute_or_symlinked_trusted_inputs() {
        assert!(PrivatePythonManagedReleaseStager::new(
            PathBuf::from("python"),
            PathBuf::from("installer.pyz"),
            PathBuf::from("runtime-manager"),
            PathBuf::from("toolchain"),
            PathBuf::from("releases"),
            PathBuf::from("stage"),
            "linux-x86_64".to_owned(),
        )
        .is_err());
    }

    #[test]
    fn stage_rejects_unverified_download_before_archive_processing() {
        let root = temp_root();
        let python = regular_file(&root.join("python"));
        let installer = regular_file(&root.join("installer.pyz"));
        let manager = regular_file(&root.join("manager"));
        let toolchain = root.join("toolchain");
        fs::create_dir_all(&toolchain).unwrap();
        let stager = PrivatePythonManagedReleaseStager::new(
            python,
            installer,
            manager,
            toolchain,
            root.join("releases"),
            root.join("stage"),
            "linux-x86_64".to_owned(),
        )
        .unwrap();
        let receipt = ArtifactDownloadReceiptV1 {
            schema_version: 1,
            release_id: "1.0.1+build".to_owned(),
            release_generation: 2,
            target: "linux-x86_64".to_owned(),
            kind: crate::update_download::ReleaseArtifactKindV1::ManagedReleasePayload,
            object_key: "artifact".to_owned(),
            sha256: "a".repeat(64),
            size_bytes: 1,
            final_path: root.join("missing.tar.zst"),
            resumed_from_bytes: 0,
            downloaded_bytes: 1,
            reused_existing: false,
            content_verified: false,
        };
        assert!(stager.stage(&receipt, "1.0.1+build", 2).is_err());
        let _ = fs::remove_dir_all(root);
    }

    fn regular_file(path: &Path) -> PathBuf {
        fs::write(path, b"x").unwrap();
        path.to_path_buf()
    }

    fn temp_root() -> PathBuf {
        let id = COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "hermes-managed-stager-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }
}
