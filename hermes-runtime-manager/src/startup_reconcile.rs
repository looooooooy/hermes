use crate::manager::RuntimeManager;
use crate::model::LifecycleState;
use crate::ports::PortError;
use crate::update_coordinator::UpdateReleaseActivator;
use std::sync::Arc;

pub trait StartupReadinessProbe: Send + Sync {
    fn ready(&self, release_id: &str) -> Result<bool, PortError>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StartupReconcileOutcome {
    NoActiveRelease,
    AlreadyReady { release_id: String },
    Reconciled { release_id: String },
}

pub struct RuntimeStartupReconciler {
    manager: Arc<RuntimeManager>,
    activator: Arc<dyn UpdateReleaseActivator>,
    readiness: Arc<dyn StartupReadinessProbe>,
}

impl RuntimeStartupReconciler {
    pub fn new(
        manager: Arc<RuntimeManager>,
        activator: Arc<dyn UpdateReleaseActivator>,
        readiness: Arc<dyn StartupReadinessProbe>,
    ) -> Self {
        Self {
            manager,
            activator,
            readiness,
        }
    }

    pub fn reconcile(&self) -> Result<StartupReconcileOutcome, PortError> {
        let snapshot = self
            .manager
            .snapshot()
            .map_err(|_| operation("Runtime Manager startup snapshot failed"))?;
        let Some(active) = snapshot.active_release else {
            if snapshot.state != LifecycleState::Absent {
                return Err(operation(
                    "Runtime Manager without active release must remain absent",
                ));
            }
            return Ok(StartupReconcileOutcome::NoActiveRelease);
        };
        if snapshot.state == LifecycleState::Ready {
            return Ok(StartupReconcileOutcome::AlreadyReady { release_id: active });
        }
        if snapshot.state != LifecycleState::Stopped && snapshot.state != LifecycleState::Degraded {
            return Err(operation(
                "Runtime Manager startup reconcile requires stopped or degraded state",
            ));
        }
        self.manager
            .transition(LifecycleState::Starting)
            .map_err(|_| operation("Runtime Manager could not enter starting state"))?;

        if let Err(error) = self.activator.rollback(&active) {
            let _ = self.manager.transition(LifecycleState::Failed);
            return Err(error);
        }
        match self.readiness.ready(&active) {
            Ok(true) => {
                self.manager
                    .transition(LifecycleState::Ready)
                    .map_err(|_| operation("Runtime Manager could not enter ready state"))?;
                Ok(StartupReconcileOutcome::Reconciled { release_id: active })
            }
            Ok(false) => {
                let _ = self.manager.transition(LifecycleState::Failed);
                Err(operation(
                    "exact active release did not become authoritatively ready",
                ))
            }
            Err(error) => {
                let _ = self.manager.transition(LifecycleState::Failed);
                Err(error)
            }
        }
    }
}

fn operation(message: &str) -> PortError {
    PortError::Operation(message.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ComponentHealth, PlatformKind};
    use crate::ports::{InstallLayout, ServiceManager};
    use crate::update_coordinator::{StagedReleaseV1, UpdateReleaseActivator};
    use std::path::{Path, PathBuf};
    use std::sync::Mutex;

    struct Services;
    impl ServiceManager for Services {
        fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> { Ok(()) }
        fn start_host(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> { Ok(()) }
        fn stop_host(&self) -> Result<(), PortError> { Ok(()) }
        fn start_connector(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> { Ok(()) }
        fn stop_connector(&self) -> Result<(), PortError> { Ok(()) }
        fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> { Ok(Vec::new()) }
    }

    struct Layout(PathBuf);
    impl InstallLayout for Layout {
        fn platform(&self) -> PlatformKind { PlatformKind::Windows }
        fn application_root(&self) -> Result<PathBuf, PortError> { Ok(self.0.clone()) }
        fn releases_root(&self) -> Result<PathBuf, PortError> { Ok(self.0.join("releases")) }
        fn toolchains_root(&self) -> Result<PathBuf, PortError> { Ok(self.0.join("toolchains")) }
        fn state_root(&self) -> Result<PathBuf, PortError> { Ok(self.0.join("state")) }
        fn logs_root(&self) -> Result<PathBuf, PortError> { Ok(self.0.join("logs")) }
    }

    struct Activator {
        calls: Mutex<Vec<String>>,
        fail: bool,
    }
    impl UpdateReleaseActivator for Activator {
        fn activate(&self, _staged: &StagedReleaseV1) -> Result<(), PortError> {
            Err(operation("startup must not call update activate"))
        }
        fn rollback(&self, release_id: &str) -> Result<(), PortError> {
            self.calls.lock().unwrap().push(release_id.to_owned());
            if self.fail { Err(operation("switch failed")) } else { Ok(()) }
        }
    }

    struct Readiness(bool);
    impl StartupReadinessProbe for Readiness {
        fn ready(&self, _release_id: &str) -> Result<bool, PortError> { Ok(self.0) }
    }

    fn manager() -> Arc<RuntimeManager> {
        let root = std::env::temp_dir().join(format!("hermes-startup-reconcile-{}", std::process::id()));
        let manager = Arc::new(RuntimeManager::new(Arc::new(Services), Arc::new(Layout(root))));
        manager.transition(LifecycleState::Installing).unwrap();
        manager.transition(LifecycleState::Stopped).unwrap();
        manager.record_activation("1.0.2+win", "102").unwrap();
        manager
    }

    #[test]
    fn stopped_active_release_reconciles_exact_identity_before_ready() {
        let manager = manager();
        let activator = Arc::new(Activator { calls: Mutex::new(Vec::new()), fail: false });
        let outcome = RuntimeStartupReconciler::new(
            manager.clone(),
            activator.clone(),
            Arc::new(Readiness(true)),
        )
        .reconcile()
        .unwrap();
        assert_eq!(
            outcome,
            StartupReconcileOutcome::Reconciled { release_id: "1.0.2+win".to_owned() }
        );
        assert_eq!(manager.state().unwrap(), LifecycleState::Ready);
        assert_eq!(activator.calls.lock().unwrap().as_slice(), ["1.0.2+win"]);
    }

    #[test]
    fn startup_readiness_failure_is_failed_not_ready() {
        let manager = manager();
        let result = RuntimeStartupReconciler::new(
            manager.clone(),
            Arc::new(Activator { calls: Mutex::new(Vec::new()), fail: false }),
            Arc::new(Readiness(false)),
        )
        .reconcile();
        assert!(result.is_err());
        assert_eq!(manager.state().unwrap(), LifecycleState::Failed);
    }
}
