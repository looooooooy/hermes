use crate::manager_state::{ManagerStateError, ManagerStateFile, RestoredManagerState};
use crate::model::{LifecycleState, ManagerSnapshotV1, PlatformKind, TransitionError};
use crate::ports::{InstallLayout, PortError, ServiceManager};
use std::sync::{Arc, RwLock};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ManagerError {
    #[error(transparent)]
    Transition(#[from] TransitionError),
    #[error(transparent)]
    Port(#[from] PortError),
    #[error(transparent)]
    PersistentState(#[from] ManagerStateError),
    #[error("runtime manager state lock poisoned")]
    StateLock,
}

#[derive(Debug, Clone, Default)]
struct ReleaseIdentityState {
    active_release: Option<String>,
    previous_release: Option<String>,
    runtime_generation: Option<String>,
}

impl ReleaseIdentityState {
    fn restored(&self) -> RestoredManagerState {
        RestoredManagerState {
            active_release: self.active_release.clone(),
            previous_release: self.previous_release.clone(),
            runtime_generation: self.runtime_generation.clone(),
        }
    }

    fn from_restored(state: RestoredManagerState) -> Self {
        Self {
            active_release: state.active_release,
            previous_release: state.previous_release,
            runtime_generation: state.runtime_generation,
        }
    }
}

pub struct RuntimeManager {
    state: RwLock<LifecycleState>,
    service_manager: Arc<dyn ServiceManager>,
    layout: Arc<dyn InstallLayout>,
    release_identity: RwLock<ReleaseIdentityState>,
    persistent_state: Option<ManagerStateFile>,
}

impl RuntimeManager {
    pub fn new(
        service_manager: Arc<dyn ServiceManager>,
        layout: Arc<dyn InstallLayout>,
    ) -> Self {
        Self {
            state: RwLock::new(LifecycleState::Absent),
            service_manager,
            layout,
            release_identity: RwLock::new(ReleaseIdentityState::default()),
            persistent_state: None,
        }
    }

    pub fn new_persistent(
        service_manager: Arc<dyn ServiceManager>,
        layout: Arc<dyn InstallLayout>,
    ) -> Result<Self, ManagerError> {
        let persistent_state = ManagerStateFile::new(layout.state_root()?)?;
        let restored = persistent_state.load()?;
        let lifecycle = if restored.active_release.is_some() {
            LifecycleState::Stopped
        } else {
            LifecycleState::Absent
        };
        Ok(Self {
            state: RwLock::new(lifecycle),
            service_manager,
            layout,
            release_identity: RwLock::new(ReleaseIdentityState::from_restored(restored)),
            persistent_state: Some(persistent_state),
        })
    }

    pub fn platform(&self) -> PlatformKind {
        self.layout.platform()
    }

    pub fn state(&self) -> Result<LifecycleState, ManagerError> {
        self.state
            .read()
            .map(|value| *value)
            .map_err(|_| ManagerError::StateLock)
    }

    pub fn transition(&self, next: LifecycleState) -> Result<LifecycleState, ManagerError> {
        let mut state = self.state.write().map_err(|_| ManagerError::StateLock)?;
        *state = state.transition(next)?;
        Ok(*state)
    }

    pub fn snapshot(&self) -> Result<ManagerSnapshotV1, ManagerError> {
        let identity = self
            .release_identity
            .read()
            .map_err(|_| ManagerError::StateLock)?
            .clone();

        Ok(ManagerSnapshotV1 {
            schema_version: 1,
            state: self.state()?,
            platform: self.platform(),
            active_release: identity.active_release,
            previous_release: identity.previous_release,
            runtime_generation: identity.runtime_generation,
            components: self.service_manager.component_health()?,
        })
    }

    pub fn record_activation(
        &self,
        release_id: impl Into<String>,
        generation: impl Into<String>,
    ) -> Result<(), ManagerError> {
        let release_id = release_id.into();
        let generation = generation.into();
        let mut identity = self
            .release_identity
            .write()
            .map_err(|_| ManagerError::StateLock)?;
        let next = ReleaseIdentityState {
            active_release: Some(release_id),
            previous_release: identity.active_release.clone(),
            runtime_generation: Some(generation),
        };
        self.persist_identity(&next)?;
        *identity = next;
        Ok(())
    }

    pub fn record_rollback(
        &self,
        restored_release_id: impl Into<String>,
        failed_release_id: impl Into<String>,
        restored_generation: Option<String>,
    ) -> Result<(), ManagerError> {
        let next = ReleaseIdentityState {
            active_release: Some(restored_release_id.into()),
            previous_release: Some(failed_release_id.into()),
            runtime_generation: restored_generation,
        };
        let mut identity = self
            .release_identity
            .write()
            .map_err(|_| ManagerError::StateLock)?;
        self.persist_identity(&next)?;
        *identity = next;
        Ok(())
    }

    fn persist_identity(&self, identity: &ReleaseIdentityState) -> Result<(), ManagerError> {
        if let Some(state_file) = &self.persistent_state {
            state_file.store(&identity.restored())?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ComponentHealth;
    use crate::ports::{InstallLayout, ServiceManager};
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(1);

    struct FakeServiceManager;

    impl ServiceManager for FakeServiceManager {
        fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
            Ok(())
        }
        fn start_host(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
            Ok(())
        }
        fn stop_host(&self) -> Result<(), PortError> {
            Ok(())
        }
        fn start_connector(
            &self,
            _executable: &Path,
            _release_id: &str,
        ) -> Result<(), PortError> {
            Ok(())
        }
        fn stop_connector(&self) -> Result<(), PortError> {
            Ok(())
        }
        fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
            Ok(Vec::new())
        }
    }

    struct FakeLayout {
        root: PathBuf,
    }

    impl InstallLayout for FakeLayout {
        fn platform(&self) -> PlatformKind {
            PlatformKind::Windows
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

    fn temp_root() -> PathBuf {
        let id = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "hermes-runtime-manager-persistent-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn persistent_manager(root: &Path) -> RuntimeManager {
        RuntimeManager::new_persistent(
            Arc::new(FakeServiceManager),
            Arc::new(FakeLayout {
                root: root.to_path_buf(),
            }),
        )
        .unwrap()
    }

    #[test]
    fn persistent_activation_reloads_as_stopped_not_ready() {
        let root = temp_root();
        let manager = persistent_manager(&root);
        manager.record_activation("1.0.0+win", "100").unwrap();
        manager.record_activation("1.0.1+win", "101").unwrap();
        drop(manager);

        let restored = persistent_manager(&root);
        let snapshot = restored.snapshot().unwrap();
        assert_eq!(snapshot.state, LifecycleState::Stopped);
        assert_eq!(snapshot.active_release.as_deref(), Some("1.0.1+win"));
        assert_eq!(snapshot.previous_release.as_deref(), Some("1.0.0+win"));
        assert_eq!(snapshot.runtime_generation.as_deref(), Some("101"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn persistent_rollback_reloads_exact_restored_identity() {
        let root = temp_root();
        let manager = persistent_manager(&root);
        manager.record_activation("1.0.0+win", "100").unwrap();
        manager.record_activation("1.0.1+win", "101").unwrap();
        manager
            .record_rollback("1.0.0+win", "1.0.1+win", Some("100".to_owned()))
            .unwrap();
        drop(manager);

        let restored = persistent_manager(&root);
        let snapshot = restored.snapshot().unwrap();
        assert_eq!(snapshot.state, LifecycleState::Stopped);
        assert_eq!(snapshot.active_release.as_deref(), Some("1.0.0+win"));
        assert_eq!(snapshot.previous_release.as_deref(), Some("1.0.1+win"));
        assert_eq!(snapshot.runtime_generation.as_deref(), Some("100"));
        let _ = fs::remove_dir_all(root);
    }
}
