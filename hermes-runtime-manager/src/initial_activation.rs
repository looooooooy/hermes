use crate::manager::{ManagerError, RuntimeManager};
use crate::model::{
    authoritative_components_ready, ComponentHealth, LifecycleState, ManagerSnapshotV1,
    PlatformKind,
};
use crate::ports::{PortError, ServiceManager};
use crate::release_layout::validated_console_script;
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};
use thiserror::Error;

pub trait InitialReadinessProbe: Send + Sync {
    fn wait_until_ready(
        &self,
        release_id: &str,
        timeout: Duration,
    ) -> Result<Vec<ComponentHealth>, PortError>;
}

pub struct ServiceManagerInitialReadinessProbe {
    service_manager: Arc<dyn ServiceManager>,
    poll_interval: Duration,
}

impl ServiceManagerInitialReadinessProbe {
    pub fn new(service_manager: Arc<dyn ServiceManager>) -> Self {
        Self {
            service_manager,
            poll_interval: Duration::from_millis(250),
        }
    }
}

impl InitialReadinessProbe for ServiceManagerInitialReadinessProbe {
    fn wait_until_ready(
        &self,
        _release_id: &str,
        timeout: Duration,
    ) -> Result<Vec<ComponentHealth>, PortError> {
        let deadline = Instant::now() + timeout;
        loop {
            let components = self.service_manager.component_health()?;
            if authoritative_components_ready(&components) {
                return Ok(components);
            }
            if Instant::now() >= deadline {
                return Err(PortError::Operation(
                    "managed runtime readiness timed out".to_owned(),
                ));
            }
            thread::sleep(self.poll_interval.min(deadline.saturating_duration_since(Instant::now())));
        }
    }
}

#[derive(Debug, Error)]
pub enum InitialActivationError {
    #[error(transparent)]
    Manager(#[from] ManagerError),
    #[error(transparent)]
    Port(#[from] PortError),
}

pub struct InitialReleaseActivator {
    manager: Arc<RuntimeManager>,
    service_manager: Arc<dyn ServiceManager>,
    readiness: Arc<dyn InitialReadinessProbe>,
    releases_root: PathBuf,
    platform: PlatformKind,
}

impl InitialReleaseActivator {
    pub fn new(
        manager: Arc<RuntimeManager>,
        service_manager: Arc<dyn ServiceManager>,
        readiness: Arc<dyn InitialReadinessProbe>,
        releases_root: PathBuf,
        platform: PlatformKind,
    ) -> Result<Self, PortError> {
        if !releases_root.is_absolute() || releases_root.is_symlink() {
            return Err(PortError::Operation(
                "initial activation root must be absolute and non-symlinked".to_owned(),
            ));
        }
        Ok(Self {
            manager,
            service_manager,
            readiness,
            releases_root,
            platform,
        })
    }

    pub fn activate(
        &self,
        release_id: &str,
        generation: &str,
    ) -> Result<ManagerSnapshotV1, InitialActivationError> {
        let before = self.manager.snapshot()?;
        if before.state != LifecycleState::Absent || before.active_release.is_some() {
            return Err(PortError::Operation(
                "initial activation requires an absent runtime".to_owned(),
            )
            .into());
        }

        let host = validated_console_script(
            &self.releases_root,
            release_id,
            self.platform,
            "host",
            "hermes",
        )?;
        let connector = validated_console_script(
            &self.releases_root,
            release_id,
            self.platform,
            "connector",
            "hermes-connector",
        )?;

        self.manager.transition(LifecycleState::Installing)?;
        let started = self
            .service_manager
            .start_host(&host, release_id)
            .and_then(|_| self.service_manager.start_connector(&connector, release_id))
            .and_then(|_| {
                self.readiness
                    .wait_until_ready(release_id, Duration::from_secs(120))
            })
            .and_then(|components| {
                if authoritative_components_ready(&components) {
                    Ok(())
                } else {
                    Err(PortError::Operation(
                        "managed runtime readiness evidence is incomplete".to_owned(),
                    ))
                }
            });

        if started.is_err() {
            let _ = self.service_manager.stop_connector();
            let _ = self.service_manager.stop_host();
            let _ = self.manager.transition(LifecycleState::Failed);
            return Err(PortError::Operation(
                "managed runtime initial activation failed".to_owned(),
            )
            .into());
        }

        self.manager.record_activation(release_id, generation)?;
        self.manager.transition(LifecycleState::Stopped)?;
        self.manager.transition(LifecycleState::Starting)?;
        self.manager.transition(LifecycleState::Ready)?;
        self.manager.snapshot().map_err(Into::into)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::manager::RuntimeManager;
    use crate::model::{ComponentHealth, LifecycleState, PlatformKind};
    use crate::ports::{InstallLayout, PortError, ServiceManager};
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};
    use std::time::Duration;

    struct FakeLayout {
        root: PathBuf,
    }

    impl InstallLayout for FakeLayout {
        fn platform(&self) -> PlatformKind {
            PlatformKind::Macos
        }

        fn application_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("application"))
        }

        fn releases_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("releases"))
        }

        fn toolchains_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("toolchains"))
        }

        fn state_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("state"))
        }

        fn logs_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("logs"))
        }
    }

    struct RecordingServices {
        calls: Arc<Mutex<Vec<String>>>,
        fail_connector_start: bool,
    }

    impl ServiceManager for RecordingServices {
        fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
            Ok(())
        }

        fn start_host(&self, _executable: &Path, release_id: &str) -> Result<(), PortError> {
            self.calls
                .lock()
                .unwrap()
                .push(format!("start-host:{release_id}"));
            Ok(())
        }

        fn stop_host(&self) -> Result<(), PortError> {
            self.calls.lock().unwrap().push("stop-host".to_owned());
            Ok(())
        }

        fn start_connector(
            &self,
            _executable: &Path,
            release_id: &str,
        ) -> Result<(), PortError> {
            self.calls
                .lock()
                .unwrap()
                .push(format!("start-connector:{release_id}"));
            if self.fail_connector_start {
                return Err(PortError::Operation(
                    "connector start failed safely".to_owned(),
                ));
            }
            Ok(())
        }

        fn stop_connector(&self) -> Result<(), PortError> {
            self.calls
                .lock()
                .unwrap()
                .push("stop-connector".to_owned());
            Ok(())
        }

        fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
            Ok(Vec::new())
        }
    }

    struct RecordingReadiness {
        calls: Arc<Mutex<Vec<String>>>,
        components: Option<Vec<ComponentHealth>>,
    }

    impl InitialReadinessProbe for RecordingReadiness {
        fn wait_until_ready(
            &self,
            release_id: &str,
            timeout: Duration,
        ) -> Result<Vec<ComponentHealth>, PortError> {
            assert_eq!(timeout, Duration::from_secs(120));
            self.calls
                .lock()
                .unwrap()
                .push(format!("wait-ready:{release_id}"));
            self.components
                .clone()
                .ok_or_else(|| PortError::Operation("readiness timed out".to_owned()))
        }
    }

    fn ready_components() -> Vec<ComponentHealth> {
        ["core", "agent_plugin", "connector", "cloud"]
            .into_iter()
            .map(|name| ComponentHealth {
                name: name.to_owned(),
                ready: true,
                detail: "ready".to_owned(),
                process: None,
            })
            .collect()
    }

    fn temp_root(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "hermes-initial-activation-{name}-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn make_release(root: &Path, release_id: &str) {
        use std::os::unix::fs::PermissionsExt;

        for executable in [
            root.join("releases")
                .join(release_id)
                .join("host/venv/bin/hermes"),
            root.join("releases")
                .join(release_id)
                .join("connector/venv/bin/hermes-connector"),
        ] {
            fs::create_dir_all(executable.parent().unwrap()).unwrap();
            fs::write(&executable, b"#!/bin/sh\nexit 0\n").unwrap();
            fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        }
    }

    fn harness(
        name: &str,
        fail_connector_start: bool,
        readiness: Result<Vec<ComponentHealth>, PortError>,
    ) -> (
        PathBuf,
        Arc<RuntimeManager>,
        Arc<Mutex<Vec<String>>>,
        InitialReleaseActivator,
    ) {
        let root = temp_root(name);
        let layout = Arc::new(FakeLayout { root: root.clone() });
        let calls = Arc::new(Mutex::new(Vec::new()));
        let services = Arc::new(RecordingServices {
            calls: calls.clone(),
            fail_connector_start,
        });
        let manager = Arc::new(
            RuntimeManager::new_persistent(services.clone(), layout.clone()).unwrap(),
        );
        let probe = Arc::new(RecordingReadiness {
            calls: calls.clone(),
            components: readiness.ok(),
        });
        let activator = InitialReleaseActivator::new(
            manager.clone(),
            services,
            probe,
            layout.releases_root().unwrap(),
            PlatformKind::Macos,
        )
        .unwrap();
        (root, manager, calls, activator)
    }

    #[test]
    fn absent_manager_activates_one_exact_staged_release() {
        let release_id = "desktop-0.1.0+macos-aarch64";
        let (root, manager, calls, activator) =
            harness("happy", false, Ok(ready_components()));
        make_release(&root, release_id);

        let snapshot = activator.activate(release_id, "1").unwrap();

        assert_eq!(snapshot.state, LifecycleState::Ready);
        assert_eq!(snapshot.active_release.as_deref(), Some(release_id));
        assert_eq!(snapshot.previous_release, None);
        assert_eq!(snapshot.runtime_generation.as_deref(), Some("1"));
        assert_eq!(
            *calls.lock().unwrap(),
            vec![
                format!("start-host:{release_id}"),
                format!("start-connector:{release_id}"),
                format!("wait-ready:{release_id}"),
            ]
        );
        assert_eq!(manager.state().unwrap(), LifecycleState::Ready);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn initial_activation_rejects_an_existing_active_release() {
        let release_id = "desktop-0.1.0+macos-aarch64";
        let (root, manager, calls, activator) =
            harness("existing", false, Ok(ready_components()));
        make_release(&root, release_id);
        manager.record_activation("desktop-0.0.9+macos-aarch64", "9").unwrap();

        assert!(activator.activate(release_id, "10").is_err());
        assert!(calls.lock().unwrap().is_empty());
        assert_eq!(
            manager.snapshot().unwrap().active_release.as_deref(),
            Some("desktop-0.0.9+macos-aarch64")
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn connector_start_failure_stops_host_and_does_not_persist_identity() {
        let release_id = "desktop-0.1.0+macos-aarch64";
        let (root, manager, calls, activator) =
            harness("connector-failure", true, Ok(ready_components()));
        make_release(&root, release_id);

        assert!(activator.activate(release_id, "1").is_err());

        assert_eq!(
            *calls.lock().unwrap(),
            vec![
                format!("start-host:{release_id}"),
                format!("start-connector:{release_id}"),
                "stop-connector".to_owned(),
                "stop-host".to_owned(),
            ]
        );
        let snapshot = manager.snapshot().unwrap();
        assert_eq!(snapshot.state, LifecycleState::Failed);
        assert_eq!(snapshot.active_release, None);
        let restored = RuntimeManager::new_persistent(
            Arc::new(RecordingServices {
                calls: calls.clone(),
                fail_connector_start: false,
            }),
            Arc::new(FakeLayout { root: root.clone() }),
        )
        .unwrap();
        assert_eq!(restored.snapshot().unwrap().active_release, None);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn readiness_timeout_stops_both_services_and_does_not_claim_ready() {
        let release_id = "desktop-0.1.0+macos-aarch64";
        let (root, manager, calls, activator) = harness(
            "timeout",
            false,
            Err(PortError::Operation("readiness timed out".to_owned())),
        );
        make_release(&root, release_id);

        assert!(activator.activate(release_id, "1").is_err());

        assert_eq!(
            *calls.lock().unwrap(),
            vec![
                format!("start-host:{release_id}"),
                format!("start-connector:{release_id}"),
                format!("wait-ready:{release_id}"),
                "stop-connector".to_owned(),
                "stop-host".to_owned(),
            ]
        );
        let snapshot = manager.snapshot().unwrap();
        assert_eq!(snapshot.state, LifecycleState::Failed);
        assert_eq!(snapshot.active_release, None);
        let _ = fs::remove_dir_all(root);
    }
}
