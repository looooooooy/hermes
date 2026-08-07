use crate::model::{ComponentHealth, PlatformKind, ProcessEvidence};
use std::path::{Path, PathBuf};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum PortError {
    #[error("platform capability unavailable: {0}")]
    Unavailable(&'static str),
    #[error("platform operation failed: {0}")]
    Operation(String),
    #[error("platform I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

pub trait ServiceManager: Send + Sync {
    fn install_bootstrap(&self, runtime_manager: &Path) -> Result<(), PortError>;
    fn start_host(&self, executable: &Path, release_id: &str) -> Result<(), PortError>;
    fn stop_host(&self) -> Result<(), PortError>;
    fn start_connector(&self, executable: &Path, release_id: &str) -> Result<(), PortError>;
    fn stop_connector(&self) -> Result<(), PortError>;
    fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError>;
}

pub trait SecretStore: Send + Sync {
    fn put(&self, namespace: &str, account: &str, secret: &[u8]) -> Result<(), PortError>;
    fn get(&self, namespace: &str, account: &str) -> Result<Option<Vec<u8>>, PortError>;
    fn delete(&self, namespace: &str, account: &str) -> Result<(), PortError>;
}

pub trait LocalIpc: Send + Sync {
    fn endpoint(&self) -> Result<PathBuf, PortError>;
    fn verify_peer(&self, peer_pid: u32) -> Result<ProcessEvidence, PortError>;
}

pub trait ProcessIdentity: Send + Sync {
    fn inspect(&self, pid: u32) -> Result<ProcessEvidence, PortError>;
    fn current_executable(&self) -> Result<PathBuf, PortError>;
}

pub trait InstallLayout: Send + Sync {
    fn platform(&self) -> PlatformKind;
    fn application_root(&self) -> Result<PathBuf, PortError>;
    fn releases_root(&self) -> Result<PathBuf, PortError>;
    fn toolchains_root(&self) -> Result<PathBuf, PortError>;
    fn state_root(&self) -> Result<PathBuf, PortError>;
    fn logs_root(&self) -> Result<PathBuf, PortError>;
}

pub trait Clock: Send + Sync {
    fn unix_ms(&self) -> u64;
}
