use crate::manager::RuntimeManager;
use crate::model::PlatformKind;
use crate::ports::{Clock, PortError, ServiceManager};
use crate::release_control::BlockManifestV1;
use crate::update_coordinator::{
    StagedReleaseV1, UpdateArtifactFetcher, UpdateHealthEvidenceV1, UpdateHealthGate,
    UpdateReleaseActivator, UpdateRollbackPolicy,
};
use crate::update_download::{
    download_verified_artifact, ArtifactDownloadReceiptV1, ArtifactDownloadSpecV1,
};
use crate::update_http::{HttpDownloadGrantV1, HttpRangeSource};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

pub trait UpdateCloudConnectivityProbe: Send + Sync {
    fn cloud_connected(&self) -> Result<bool, PortError>;
}

pub trait UpdateLiveSessionProbe: Send + Sync {
    fn live_session_ok(&self) -> Result<bool, PortError>;
}

#[derive(Debug, Default)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn unix_ms(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|value| value.as_millis().min(u128::from(u64::MAX)) as u64)
            .unwrap_or(0)
    }
}

pub struct HttpUpdateArtifactFetcher {
    grant: HttpDownloadGrantV1,
    cache_root: PathBuf,
}

impl HttpUpdateArtifactFetcher {
    pub fn new(grant: HttpDownloadGrantV1, cache_root: PathBuf) -> Result<Self, PortError> {
        if !cache_root.is_absolute() || cache_root.is_symlink() {
            return Err(PortError::Operation(
                "update cache root must be absolute and non-symlinked".to_owned(),
            ));
        }
        Ok(Self { grant, cache_root })
    }
}

impl UpdateArtifactFetcher for HttpUpdateArtifactFetcher {
    fn fetch(&self, spec: &ArtifactDownloadSpecV1) -> Result<ArtifactDownloadReceiptV1, PortError> {
        let mut source = HttpRangeSource::from_grant(spec, &self.grant).map_err(|_| {
            PortError::Operation("signed HTTPS download grant is invalid".to_owned())
        })?;
        download_verified_artifact(spec, &mut source, &self.cache_root).map_err(|_| {
            PortError::Operation("verified update artifact download failed".to_owned())
        })
    }
}

pub struct ServiceManagerReleaseActivator {
    manager: Arc<RuntimeManager>,
    service_manager: Arc<dyn ServiceManager>,
    releases_root: PathBuf,
    platform: PlatformKind,
}

impl ServiceManagerReleaseActivator {
    pub fn new(
        manager: Arc<RuntimeManager>,
        service_manager: Arc<dyn ServiceManager>,
        releases_root: PathBuf,
        platform: PlatformKind,
    ) -> Result<Self, PortError> {
        if !releases_root.is_absolute() || releases_root.is_symlink() {
            return Err(PortError::Operation(
                "release activation root must be absolute and non-symlinked".to_owned(),
            ));
        }
        Ok(Self {
            manager,
            service_manager,
            releases_root,
            platform,
        })
    }

    fn switch_to(&self, release_id: &str) -> Result<(), PortError> {
        let release_dir = self.release_dir(release_id)?;
        let host = self.console_script(&release_dir, "host", "hermes")?;
        let connector = self.console_script(&release_dir, "connector", "hermes-connector")?;
        self.service_manager.stop_connector()?;
        self.service_manager.stop_host()?;
        self.service_manager.start_host(&host, release_id)?;
        if let Err(error) = self.service_manager.start_connector(&connector, release_id) {
            let _ = self.service_manager.stop_host();
            return Err(error);
        }
        Ok(())
    }

    fn release_dir(&self, release_id: &str) -> Result<PathBuf, PortError> {
        if !safe_release_id(release_id) {
            return Err(PortError::Operation("release identity is invalid".to_owned()));
        }
        let path = self.releases_root.join(release_id);
        if path.is_symlink() || !path.is_dir() {
            return Err(PortError::Operation(
                "immutable release directory is missing or symlinked".to_owned(),
            ));
        }
        let resolved = path.canonicalize()?;
        let root = self.releases_root.canonicalize()?;
        if resolved.parent() != Some(root.as_path()) || resolved.file_name() != Some(release_id.as_ref()) {
            return Err(PortError::Operation(
                "immutable release escaped the release root".to_owned(),
            ));
        }
        Ok(resolved)
    }

    fn console_script(
        &self,
        release_dir: &Path,
        runtime: &str,
        name: &str,
    ) -> Result<PathBuf, PortError> {
        let path = match self.platform {
            PlatformKind::Windows => release_dir
                .join(runtime)
                .join("venv")
                .join("Scripts")
                .join(format!("{name}.exe")),
            PlatformKind::Macos | PlatformKind::Linux => release_dir
                .join(runtime)
                .join("venv")
                .join("bin")
                .join(name),
        };
        if path.is_symlink() || !path.is_file() {
            return Err(PortError::Operation(
                "release console entrypoint is missing or symlinked".to_owned(),
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if path.metadata()?.permissions().mode() & 0o111 == 0 {
                return Err(PortError::Operation(
                    "release console entrypoint is not executable".to_owned(),
                ));
            }
        }
        Ok(path)
    }
}

impl UpdateReleaseActivator for ServiceManagerReleaseActivator {
    fn activate(&self, staged: &StagedReleaseV1) -> Result<(), PortError> {
        let previous = self
            .manager
            .snapshot()
            .map_err(|_| PortError::Operation("Runtime Manager activation snapshot failed".to_owned()))?
            .active_release
            .ok_or_else(|| PortError::Operation("active release is unavailable".to_owned()))?;
        if staged.release_id == previous {
            return Err(PortError::Operation("target release is already active".to_owned()));
        }
        if let Err(error) = self.switch_to(&staged.release_id) {
            let restored = self.switch_to(&previous);
            return match restored {
                Ok(()) => Err(error),
                Err(_) => Err(PortError::Operation(
                    "target activation failed and previous release restoration also failed"
                        .to_owned(),
                )),
            };
        }
        Ok(())
    }

    fn rollback(&self, release_id: &str) -> Result<(), PortError> {
        self.switch_to(release_id)
    }
}

pub struct ServiceManagerUpdateHealthGate {
    service_manager: Arc<dyn ServiceManager>,
    cloud: Arc<dyn UpdateCloudConnectivityProbe>,
    session: Arc<dyn UpdateLiveSessionProbe>,
}

impl ServiceManagerUpdateHealthGate {
    pub fn new(
        service_manager: Arc<dyn ServiceManager>,
        cloud: Arc<dyn UpdateCloudConnectivityProbe>,
        session: Arc<dyn UpdateLiveSessionProbe>,
    ) -> Self {
        Self {
            service_manager,
            cloud,
            session,
        }
    }
}

impl UpdateHealthGate for ServiceManagerUpdateHealthGate {
    fn verify(&self, _release_id: &str) -> Result<UpdateHealthEvidenceV1, PortError> {
        let components = self.service_manager.component_health()?;
        let components_ready = !components.is_empty() && components.iter().all(|item| item.ready);
        let agent_ready = components.iter().any(|item| {
            item.ready
                && matches!(
                    item.name.to_ascii_lowercase().as_str(),
                    "hermes core" | "host" | "hermes host" | "agent"
                )
        });
        Ok(UpdateHealthEvidenceV1 {
            agent_ready,
            cloud_connected: self.cloud.cloud_connected()?,
            live_session_ok: self.session.live_session_ok()?,
            components_ready,
        })
    }
}

#[derive(Debug, Clone)]
pub struct SignedBlockRollbackPolicy {
    block: BlockManifestV1,
}

impl SignedBlockRollbackPolicy {
    pub fn new(block: BlockManifestV1) -> Self {
        Self { block }
    }
}

impl UpdateRollbackPolicy for SignedBlockRollbackPolicy {
    fn rollback_allowed(&self, release_id: &str) -> Result<bool, PortError> {
        if !safe_release_id(release_id) {
            return Err(PortError::Operation("rollback release identity is invalid".to_owned()));
        }
        Ok(!self
            .block
            .blocked_releases
            .iter()
            .any(|item| item.release_id == release_id))
    }
}

fn safe_release_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 160
        && value != "."
        && value != ".."
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+')
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ComponentHealth, LifecycleState, PlatformKind};
    use crate::ports::InstallLayout;
    use crate::update_download::ReleaseArtifactKindV1;
    use std::sync::Mutex;

    #[derive(Default)]
    struct RecordingServices {
        calls: Mutex<Vec<String>>,
        fail_connector_for: Mutex<Option<String>>,
    }

    impl ServiceManager for RecordingServices {
        fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
            Ok(())
        }
        fn start_host(&self, _executable: &Path, release_id: &str) -> Result<(), PortError> {
            self.calls.lock().unwrap().push(format!("start-host:{release_id}"));
            Ok(())
        }
        fn stop_host(&self) -> Result<(), PortError> {
            self.calls.lock().unwrap().push("stop-host".to_owned());
            Ok(())
        }
        fn start_connector(&self, _executable: &Path, release_id: &str) -> Result<(), PortError> {
            self.calls
                .lock()
                .unwrap()
                .push(format!("start-connector:{release_id}"));
            if self.fail_connector_for.lock().unwrap().as_deref() == Some(release_id) {
                return Err(PortError::Operation("injected connector failure".to_owned()));
            }
            Ok(())
        }
        fn stop_connector(&self) -> Result<(), PortError> {
            self.calls.lock().unwrap().push("stop-connector".to_owned());
            Ok(())
        }
        fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
            Ok(vec![
                ComponentHealth {
                    name: "Hermes Core".to_owned(),
                    ready: true,
                    detail: "ready".to_owned(),
                    process: None,
                },
                ComponentHealth {
                    name: "Connector".to_owned(),
                    ready: true,
                    detail: "ready".to_owned(),
                    process: None,
                },
            ])
        }
    }

    struct Layout(PathBuf);
    impl InstallLayout for Layout {
        fn platform(&self) -> PlatformKind {
            current_platform()
        }
        fn application_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.0.clone())
        }
        fn releases_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.0.join("releases"))
        }
        fn toolchains_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.0.join("toolchains"))
        }
        fn state_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.0.join("state"))
        }
        fn logs_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.0.join("logs"))
        }
    }

    struct BoolCloud(bool);
    impl UpdateCloudConnectivityProbe for BoolCloud {
        fn cloud_connected(&self) -> Result<bool, PortError> {
            Ok(self.0)
        }
    }
    struct BoolSession(bool);
    impl UpdateLiveSessionProbe for BoolSession {
        fn live_session_ok(&self) -> Result<bool, PortError> {
            Ok(self.0)
        }
    }

    #[test]
    fn http_fetcher_rejects_grant_identity_before_network_access() {
        let root = temp_root();
        let spec = ArtifactDownloadSpecV1 {
            schema_version: 1,
            release_id: "1.0.1+build".to_owned(),
            release_generation: 2,
            target: "linux-x86_64".to_owned(),
            kind: ReleaseArtifactKindV1::ManagedReleasePayload,
            object_key: format!("artifacts/v1/sha256/aa/{}/runtime.bin", "a".repeat(64)),
            file_name: "runtime.bin".to_owned(),
            sha256: "a".repeat(64),
            size_bytes: 100,
            platform_signature: None,
        };
        let grant = HttpDownloadGrantV1 {
            object_key: spec.object_key.clone(),
            sha256: "b".repeat(64),
            size_bytes: 100,
            url: "https://updates.example.test/runtime.bin?token=short".to_owned(),
            expires_at: "2099-01-01T00:00:00Z".to_owned(),
        };
        let fetcher = HttpUpdateArtifactFetcher::new(grant, root.join("cache")).unwrap();
        assert!(fetcher.fetch(&spec).is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn activator_switches_exact_release_and_restores_previous_on_partial_failure() {
        let root = temp_root();
        let releases = root.join("releases");
        fs::create_dir_all(&releases).unwrap();
        let previous = "1.0.0+old";
        let target = "1.0.1+new";
        make_release(&releases, previous);
        make_release(&releases, target);
        let services = Arc::new(RecordingServices::default());
        let manager = ready_manager(&root, services.clone(), previous);
        *services.fail_connector_for.lock().unwrap() = Some(target.to_owned());
        let activator = ServiceManagerReleaseActivator::new(
            manager,
            services.clone(),
            releases,
            current_platform(),
        )
        .unwrap();
        let staged = StagedReleaseV1 {
            release_id: target.to_owned(),
            release_generation: 2,
            release_path: root.join("releases").join(target),
            content_verified: true,
        };

        assert!(activator.activate(&staged).is_err());
        let calls = services.calls.lock().unwrap().clone();
        assert!(calls.contains(&format!("start-host:{target}")));
        assert!(calls.contains(&format!("start-connector:{target}")));
        assert!(calls.contains(&format!("start-host:{previous}")));
        assert!(calls.contains(&format!("start-connector:{previous}")));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn health_gate_requires_components_cloud_and_live_session() {
        let services = Arc::new(RecordingServices::default());
        let healthy = ServiceManagerUpdateHealthGate::new(
            services.clone(),
            Arc::new(BoolCloud(true)),
            Arc::new(BoolSession(true)),
        )
        .verify("release")
        .unwrap();
        assert!(healthy.healthy());

        let unhealthy = ServiceManagerUpdateHealthGate::new(
            services,
            Arc::new(BoolCloud(false)),
            Arc::new(BoolSession(true)),
        )
        .verify("release")
        .unwrap();
        assert!(!unhealthy.healthy());
    }

    fn current_platform() -> PlatformKind {
        crate::platform::current_platform()
    }

    fn temp_root() -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "hermes-update-adapters-{}-{}",
            std::process::id(),
            SystemClock.unix_ms()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn make_release(root: &Path, release_id: &str) {
        let release = root.join(release_id);
        let (host, connector) = match current_platform() {
            PlatformKind::Windows => (
                release.join("host/venv/Scripts/hermes.exe"),
                release.join("connector/venv/Scripts/hermes-connector.exe"),
            ),
            PlatformKind::Macos | PlatformKind::Linux => (
                release.join("host/venv/bin/hermes"),
                release.join("connector/venv/bin/hermes-connector"),
            ),
        };
        for path in [&host, &connector] {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, b"binary").unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
            }
        }
    }

    fn ready_manager(
        root: &Path,
        services: Arc<RecordingServices>,
        release_id: &str,
    ) -> Arc<RuntimeManager> {
        let manager = Arc::new(RuntimeManager::new(
            services,
            Arc::new(Layout(root.to_path_buf())),
        ));
        manager.transition(LifecycleState::Installing).unwrap();
        manager.transition(LifecycleState::Stopped).unwrap();
        manager.transition(LifecycleState::Starting).unwrap();
        manager.transition(LifecycleState::Ready).unwrap();
        manager.record_activation(release_id, "1").unwrap();
        manager
    }
}
