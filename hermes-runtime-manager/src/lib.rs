pub mod blank_machine;
pub mod ipc;
#[cfg(target_os = "linux")]
pub mod linux_secret_service;
#[cfg(target_os = "linux")]
pub mod linux_systemd_user;
pub mod local_ipc;
pub mod manager;
pub mod model;
pub mod platform;
pub mod portable_plugin_signature;
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
#[cfg(target_os = "linux")]
pub use linux_secret_service::LinuxSecretServiceStore;
#[cfg(target_os = "linux")]
pub use linux_systemd_user::{LinuxSystemdUserBootstrap, LinuxSystemdUserStatus};
pub use manager::{ManagerError, RuntimeManager};
pub use model::{LifecycleState, ManagerSnapshotV1, ManagedReleaseManifestV1, ToolchainManifestV1};
pub use portable_plugin_signature::{
    verify_portable_plugin_signature, verify_portable_plugin_signature_at,
    PluginTrustKeyV1, PluginTrustStoreV1, PortablePluginEntrypointV2,
    PortablePluginManifestV2, PortablePluginVerificationError,
    PortablePluginVerificationReportV2,
};
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
