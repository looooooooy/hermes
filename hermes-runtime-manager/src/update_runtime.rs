use crate::manager::RuntimeManager;
use crate::model::PlatformKind;
use crate::ports::{PortError, ServiceManager};
use crate::update_connector_lane::GracefulServiceManagerConnectorLane;
use crate::update_coordinator::{UpdateConnectorLane, UpdateSafeWindowProbe};
use crate::update_safe_window::{
    DrainingSafeWindowProbe, HostUpdateSafetySource,
};
use std::path::PathBuf;
use std::sync::Arc;

/// Production composition for the update cutover boundary.
///
/// The same Connector lane instance is shared by the Safe Window probe and the
/// Update Coordinator. This is required because the probe freezes command
/// admission before it asks Host for authoritative task / interaction evidence,
/// while the coordinator enters its later `Draining` phase by calling the same
/// idempotent lane again.
pub struct ManagedUpdateSafeWindow {
    connector_lane: Arc<GracefulServiceManagerConnectorLane>,
    safe_window: Arc<DrainingSafeWindowProbe>,
}

impl ManagedUpdateSafeWindow {
    pub fn connector_lane(&self) -> Arc<dyn UpdateConnectorLane> {
        self.connector_lane.clone()
    }

    pub fn safe_window(&self) -> Arc<dyn UpdateSafeWindowProbe> {
        self.safe_window.clone()
    }
}

pub fn compose_managed_update_safe_window(
    manager: Arc<RuntimeManager>,
    services: Arc<dyn ServiceManager>,
    releases_root: PathBuf,
    platform: PlatformKind,
    host: Arc<dyn HostUpdateSafetySource>,
) -> Result<ManagedUpdateSafeWindow, PortError> {
    let connector_lane = Arc::new(GracefulServiceManagerConnectorLane::new(
        manager,
        services,
        releases_root,
        platform,
    )?);
    let safe_window = Arc::new(DrainingSafeWindowProbe::new(
        connector_lane.clone(),
        host,
    ));
    Ok(ManagedUpdateSafeWindow {
        connector_lane,
        safe_window,
    })
}

#[cfg(target_os = "macos")]
pub fn compose_macos_authoritative_update_safe_window(
    manager: Arc<RuntimeManager>,
    services: Arc<dyn ServiceManager>,
    releases_root: PathBuf,
) -> Result<ManagedUpdateSafeWindow, PortError> {
    use crate::host_update_safety_ipc::UnixHostUpdateSafetySource;

    compose_managed_update_safe_window(
        manager,
        services,
        releases_root,
        PlatformKind::Macos,
        Arc::new(UnixHostUpdateSafetySource::discover()?),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ComponentHealth, LifecycleState};
    use crate::ports::{InstallLayout, PortError};
    use crate::update_coordinator::UpdateSafeWindowEvidenceV1;
    use crate::update_safe_window::HostUpdateSafetySnapshotV1;
    use std::fs;
    use std::path::Path;
    use std::sync::Mutex;

    struct StaticHost(HostUpdateSafetySnapshotV1);

    impl HostUpdateSafetySource for StaticHost {
        fn snapshot(&self) -> Result<HostUpdateSafetySnapshotV1, PortError> {
            Ok(self.0.clone())
        }
    }

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
                "start:{release_id}:{}",
                executable.display()
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
    fn unsafe_evidence_restores_through_the_shared_lane() {
        let root = temp_root();
        let release_id = "1.0.0+active";
        make_release(&root.join("releases"), release_id);
        let services = Arc::new(RecordingServices::default());
        let manager = ready_manager(&root, services.clone(), release_id);
        let composition = compose_managed_update_safe_window(
            manager,
            services.clone(),
            root.join("releases"),
            current_platform(),
            Arc::new(StaticHost(HostUpdateSafetySnapshotV1 {
                schema_version: 1,
                profile: "default".to_owned(),
                runtime_generation: "generation-1".to_owned(),
                active_tasks: 0,
                pending_approvals: 1,
                pending_clarifications: 0,
                evidence_complete: true,
            })),
        )
        .unwrap();

        let evidence: UpdateSafeWindowEvidenceV1 = composition.safe_window().inspect().unwrap();
        assert!(!evidence.safe_to_update());
        assert_eq!(services.calls.lock().unwrap().len(), 2);
        assert_eq!(services.calls.lock().unwrap()[0], "stop");
        assert!(services.calls.lock().unwrap()[1].starts_with("start:1.0.0+active:"));

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn safe_evidence_keeps_shared_lane_drained_until_coordinator_reconcile() {
        let root = temp_root();
        let release_id = "1.0.0+active";
        make_release(&root.join("releases"), release_id);
        let services = Arc::new(RecordingServices::default());
        let manager = ready_manager(&root, services.clone(), release_id);
        let composition = compose_managed_update_safe_window(
            manager,
            services.clone(),
            root.join("releases"),
            current_platform(),
            Arc::new(StaticHost(HostUpdateSafetySnapshotV1 {
                schema_version: 1,
                profile: "default".to_owned(),
                runtime_generation: "generation-1".to_owned(),
                active_tasks: 0,
                pending_approvals: 0,
                pending_clarifications: 0,
                evidence_complete: true,
            })),
        )
        .unwrap();

        assert!(composition.safe_window().inspect().unwrap().safe_to_update());
        composition.connector_lane().drain().unwrap();
        assert_eq!(services.calls.lock().unwrap().as_slice(), ["stop"]);
        composition.connector_lane().reconcile().unwrap();
        let calls = services.calls.lock().unwrap().clone();
        assert_eq!(calls.iter().filter(|call| call.as_str() == "stop").count(), 1);
        assert_eq!(calls.iter().filter(|call| call.starts_with("start:")).count(), 1);

        let _ = fs::remove_dir_all(root);
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

    fn make_release(root: &Path, release_id: &str) {
        let release = root.join(release_id);
        let connector = connector_executable(&release, current_platform());
        fs::create_dir_all(connector.parent().unwrap()).unwrap();
        fs::write(&connector, b"connector").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&connector, fs::Permissions::from_mode(0o700)).unwrap();
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

    fn current_platform() -> PlatformKind {
        #[cfg(target_os = "macos")]
        {
            PlatformKind::Macos
        }
        #[cfg(target_os = "windows")]
        {
            PlatformKind::Windows
        }
        #[cfg(target_os = "linux")]
        {
            PlatformKind::Linux
        }
    }

    fn temp_root() -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "hermes-update-safe-window-composition-{}-{}",
            std::process::id(),
            std::thread::current().name().unwrap_or("test")
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }
}
