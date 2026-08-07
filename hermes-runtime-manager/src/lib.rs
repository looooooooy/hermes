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
pub mod release_control;
pub mod toolchain;
pub mod update_download;
pub mod update_http;
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
pub use release_control::{
    verify_release_control, verify_release_control_files, verify_release_control_files_at,
    BlockManifestV1, BlockedReleaseV1, ChannelManifestV1, ProductReleaseManifestV1,
    ProductTargetV1, ReleaseArtifactV1, ReleaseChannelV1, ReleaseControlError,
    ReleaseControlObservedStateV1, ReleaseControlVerificationReportV1,
    ReleaseSecurityPolicyV1, ReleaseSourceV1, ReleaseTrustKeyV1, ReleaseTrustStoreV1,
    RollbackAuthorizationV1, SignedReleaseEnvelopeV1,
};
pub use toolchain::{PrivateToolchainBundleV1, PrivateToolchainInstaller, ToolchainInstallError};
pub use update_download::{
    download_spec_from_verified_release, download_verified_artifact, ArtifactDownloadReceiptV1,
    ArtifactDownloadSpecV1, ArtifactRangeSource, DownloadChunkV1, ReleaseArtifactKindV1,
    UpdateDownloadError,
};
pub use update_http::{HttpDownloadGrantV1, HttpRangeSource};
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
