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
    #[error("runtime manager state lock poisoned")]
    StateLock,
}

pub struct RuntimeManager {
    state: RwLock<LifecycleState>,
    service_manager: Arc<dyn ServiceManager>,
    layout: Arc<dyn InstallLayout>,
    active_release: RwLock<Option<String>>,
    previous_release: RwLock<Option<String>>,
    runtime_generation: RwLock<Option<String>>,
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
            active_release: RwLock::new(None),
            previous_release: RwLock::new(None),
            runtime_generation: RwLock::new(None),
        }
    }

    pub fn platform(&self) -> PlatformKind {
        self.layout.platform()
    }

    pub fn state(&self) -> Result<LifecycleState, ManagerError> {
        self.state.read().map(|value| *value).map_err(|_| ManagerError::StateLock)
    }

    pub fn transition(&self, next: LifecycleState) -> Result<LifecycleState, ManagerError> {
        let mut state = self.state.write().map_err(|_| ManagerError::StateLock)?;
        *state = state.transition(next)?;
        Ok(*state)
    }

    pub fn snapshot(&self) -> Result<ManagerSnapshotV1, ManagerError> {
        let active_release = self
            .active_release
            .read()
            .map_err(|_| ManagerError::StateLock)?
            .clone();
        let previous_release = self
            .previous_release
            .read()
            .map_err(|_| ManagerError::StateLock)?
            .clone();
        let runtime_generation = self
            .runtime_generation
            .read()
            .map_err(|_| ManagerError::StateLock)?
            .clone();

        Ok(ManagerSnapshotV1 {
            schema_version: 1,
            state: self.state()?,
            platform: self.platform(),
            active_release,
            previous_release,
            runtime_generation,
            components: self.service_manager.component_health()?,
        })
    }

    pub fn record_activation(
        &self,
        release_id: impl Into<String>,
        generation: impl Into<String>,
    ) -> Result<(), ManagerError> {
        let release_id = release_id.into();
        let previous = self
            .active_release
            .write()
            .map_err(|_| ManagerError::StateLock)?
            .replace(release_id);
        *self
            .previous_release
            .write()
            .map_err(|_| ManagerError::StateLock)? = previous;
        *self
            .runtime_generation
            .write()
            .map_err(|_| ManagerError::StateLock)? = Some(generation.into());
        Ok(())
    }

    pub fn record_rollback(
        &self,
        restored_release_id: impl Into<String>,
        failed_release_id: impl Into<String>,
        restored_generation: Option<String>,
    ) -> Result<(), ManagerError> {
        let restored_release_id = restored_release_id.into();
        let failed_release_id = failed_release_id.into();
        *self
            .active_release
            .write()
            .map_err(|_| ManagerError::StateLock)? = Some(restored_release_id);
        *self
            .previous_release
            .write()
            .map_err(|_| ManagerError::StateLock)? = Some(failed_release_id);
        *self
            .runtime_generation
            .write()
            .map_err(|_| ManagerError::StateLock)? = restored_generation;
        Ok(())
    }
}