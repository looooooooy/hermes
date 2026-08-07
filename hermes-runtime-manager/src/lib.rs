pub mod blank_machine;
pub mod ipc;
pub mod local_ipc;
pub mod manager;
pub mod model;
pub mod platform;
pub mod ports;
pub mod toolchain;
#[cfg(windows)]
pub mod windows_pipe;
#[cfg(windows)]
pub mod windows_secret_store;
#[cfg(windows)]
pub mod windows_task_execution;
#[cfg(windows)]
pub mod windows_task_scheduler;

pub use blank_machine::{
    run_blank_machine_toolchain_gate, BlankMachineGateError, BlankMachineGateReport,
};
pub use manager::{ManagerError, RuntimeManager};
pub use model::{LifecycleState, ManagerSnapshotV1, ManagedReleaseManifestV1, ToolchainManifestV1};
pub use toolchain::{PrivateToolchainBundleV1, PrivateToolchainInstaller, ToolchainInstallError};
#[cfg(windows)]
pub use windows_secret_store::WindowsCredentialSecretStore;
#[cfg(windows)]
pub use windows_task_execution::{
    run_registered_task_and_wait_for_fresh_completion, WindowsTaskRunEvidence,
};
#[cfg(windows)]
pub use windows_task_scheduler::{
    WindowsScheduledAction, WindowsTaskRegistration, WindowsTaskSchedulerBootstrap,
};
