pub mod ipc;
pub mod local_ipc;
pub mod manager;
pub mod model;
pub mod platform;
pub mod ports;
pub mod toolchain;
#[cfg(windows)]
pub mod windows_pipe;

pub use manager::{ManagerError, RuntimeManager};
pub use model::{LifecycleState, ManagerSnapshotV1, ManagedReleaseManifestV1, ToolchainManifestV1};
pub use toolchain::{PrivateToolchainBundleV1, PrivateToolchainInstaller, ToolchainInstallError};
