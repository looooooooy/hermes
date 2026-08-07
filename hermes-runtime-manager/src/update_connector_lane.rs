use crate::manager::RuntimeManager;
use crate::model::PlatformKind;
use crate::ports::{PortError, ServiceManager};
use crate::update_coordinator::UpdateConnectorLane;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

/// Freeze Cloud command admission by stopping the Connector before runtime cutover.
///
/// The drain state is process-local and idempotent so a Safe Window probe may freeze
/// command admission before it inspects Host evidence, while the coordinator's later
/// drain phase can call the same lane again without restarting or double-stopping it.
pub struct GracefulServiceManagerConnectorLane {
    manager: Arc<RuntimeManager>,
    services: Arc<dyn ServiceManager>,
    releases_root: PathBuf,
    platform: PlatformKind,
    drained: Mutex<bool>,
}

impl GracefulServiceManagerConnectorLane {
    pub fn new(
        manager: Arc<RuntimeManager>,
        services: Arc<dyn ServiceManager>,
        releases_root: PathBuf,
        platform: PlatformKind,
    ) -> Result<Self, PortError> {
        if !releases_root.is_absolute() || releases_root.is_symlink() {
            return Err(PortError::Operation(
                "Connector update-lane release root must be absolute and non-symlinked".to_owned(),
            ));
        }
        Ok(Self {
            manager,
            services,
            releases_root,
            platform,
            drained: Mutex::new(false),
        })
    }

    fn active_connector(&self) -> Result<(String, PathBuf), PortError> {
        let release_id = self
            .manager
            .snapshot()
            .map_err(|_| PortError::Operation("Runtime Manager snapshot failed".to_owned()))?
            .active_release
            .ok_or_else(|| PortError::Operation("active release is unavailable".to_owned()))?;
        if !safe_release_id(&release_id) {
            return Err(PortError::Operation("active release identity is invalid".to_owned()));
        }
        let release = self.releases_root.join(&release_id);
        if release.is_symlink() || !release.is_dir() {
            return Err(PortError::Operation(
                "active immutable release is missing or symlinked".to_owned(),
            ));
        }
        let release = release.canonicalize()?;
        let root = self.releases_root.canonicalize()?;
        if release.parent() != Some(root.as_path()) || release.file_name() != Some(release_id.as_ref()) {
            return Err(PortError::Operation(
                "active immutable release escaped release root".to_owned(),
            ));
        }
        let executable = connector_executable(&release, self.platform);
        if executable.is_symlink() || !executable.is_file() {
            return Err(PortError::Operation(
                "active Connector executable is missing or symlinked".to_owned(),
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if executable.metadata()?.permissions().mode() & 0o111 == 0 {
                return Err(PortError::Operation(
                    "active Connector executable is not executable".to_owned(),
                ));
            }
        }
        Ok((release_id, executable))
    }
}

impl UpdateConnectorLane for GracefulServiceManagerConnectorLane {
    fn drain(&self) -> Result<(), PortError> {
        let mut drained = self
            .drained
            .lock()
            .map_err(|_| PortError::Operation("Connector drain state lock is poisoned".to_owned()))?;
        if *drained {
            return Ok(());
        }
        self.services.stop_connector()?;
        *drained = true;
        Ok(())
    }

    fn reconcile(&self) -> Result<(), PortError> {
        let mut drained = self
            .drained
            .lock()
            .map_err(|_| PortError::Operation("Connector drain state lock is poisoned".to_owned()))?;
        if !*drained {
            return Ok(());
        }
        let (release_id, executable) = self.active_connector()?;
        self.services.start_connector(&executable, &release_id)?;
        *drained = false;
        Ok(())
    }
}

fn connector_executable(release: &Path, platform: PlatformKind) -> PathBuf {
    match platform {
        PlatformKind::Windows => release
            .join("connector")
            .join("venv")
            .join("Scripts")
            .join("hermes-connector.exe"),
        PlatformKind::Macos | PlatformKind::Linux => release
            .join("connector")
            .join("venv")
            .join("bin")
            .join("hermes-connector"),
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
    use std::fs;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[derive(Default)]
    struct RecordingServices {
        calls: Mutex<Vec<String>>,
    }

    impl ServiceManager for RecordingServices {
        fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
            Ok(())
        }
        fn start_host(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
            Ok(())
        }
        fn stop_host(&self) -> Result<(), PortError> {
            Ok(())
        }
        fn start_connector(&self, executable: &Path, release_id: &str) -> Result<(), PortError> {
            self.calls.lock().unwrap().push(format!(
                "start:{}:{}",
                release_id,
                executable.file_name().unwrap().to_string_lossy()
            ));
            Ok(())
        }
        fn stop_connector(&self) -> Result<(), PortError> {
            self.calls.lock().unwrap().push("stop".to_owned());
            Ok(())
        }
        fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
            Ok(vec![])
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

    #[test]
    fn repeated_drain_and_reconcile_are_idempotent() {
        let root = temp_root();
        let release_id = "1.0.1+active";
        make_release(&root.join("releases"), release_id);
        let services = Arc::new(RecordingServices::default());
        let manager = ready_manager(root.clone(), services.clone(), release_id);
        let lane = GracefulServiceManagerConnectorLane::new(
            manager,
            services.clone(),
            root.join("releases"),
            current_platform(),
        )
        .unwrap();

        lane.drain().unwrap();
        lane.drain().unwrap();
        lane.reconcile().unwrap();
        lane.reconcile().unwrap();

        let calls = services.calls.lock().unwrap().clone();
        assert_eq!(calls.iter().filter(|call| call.as_str() == "stop").count(), 1);
        assert_eq!(calls.iter().filter(|call| call.starts_with("start:")).count(), 1);
        assert!(calls.last().is_some_and(|call| call.starts_with(&format!("start:{release_id}:"))));
        let _ = fs::remove_dir_all(root);
    }

    fn ready_manager(
        root: PathBuf,
        services: Arc<RecordingServices>,
        release_id: &str,
    ) -> Arc<RuntimeManager> {
        let manager = Arc::new(RuntimeManager::new(services, Arc::new(Layout(root))));
        manager.transition(LifecycleState::Installing).unwrap();
        manager.transition(LifecycleState::Stopped).unwrap();
        manager.transition(LifecycleState::Starting).unwrap();
        manager.transition(LifecycleState::Ready).unwrap();
        manager.record_activation(release_id, "1").unwrap();
        manager
    }

    fn make_release(root: &Path, release_id: &str) {
        let release = root.join(release_id);
        let connector = connector_executable(&release, current_platform());
        fs::create_dir_all(connector.parent().unwrap()).unwrap();
        fs::write(&connector, b"connector").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&connector, fs::Permissions::from_mode(0o500)).unwrap();
        }
    }

    fn current_platform() -> PlatformKind {
        #[cfg(target_os = "macos")]
        return PlatformKind::Macos;
        #[cfg(target_os = "windows")]
        return PlatformKind::Windows;
        #[cfg(target_os = "linux")]
        return PlatformKind::Linux;
    }

    fn temp_root() -> PathBuf {
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let sequence = SEQUENCE.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "hermes-update-connector-lane-{}-{stamp}-{sequence}",
            std::process::id()
        ))
    }
}
