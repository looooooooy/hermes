pub mod ipc;
pub mod local_ipc;
pub mod manager;
pub mod model;
pub mod platform;
pub mod ports;

pub use manager::{ManagerError, RuntimeManager};
pub use model::{LifecycleState, ManagerSnapshotV1, ManagedReleaseManifestV1, ToolchainManifestV1};
