pub mod blank_machine;
#[cfg(unix)]
pub mod host_update_safety_ipc;
pub mod ipc;
#[cfg(target_os = "linux")]
pub mod linux_secret_service;
#[cfg(target_os = "linux")]
pub mod linux_systemd_user;
pub mod local_ipc;
pub mod managed_payload_archive;
pub mod managed_release_stager;
pub mod manager;
pub mod model;
pub mod platform;
pub mod portable_plugin_signature;
pub mod ports;
pub mod release_control;
pub mod toolchain;
pub mod update_adapters;
pub mod update_connector_lane;
pub mod update_coordinator;
pub mod update_download;
pub mod update_http;
pub mod update_runtime;
pub mod update_safe_window;
#[cfg(windows)]
pub mod windows_host_update_safety_pipe;
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
#[cfg(unix)]
pub use host_update_safety_ipc::{
    default_update_safety_endpoint, UnixHostUpdateSafetySource,
};
#[cfg(target_os = "linux")]
pub use linux_secret_service::LinuxSecretServiceStore;
#[cfg(target_os = "linux")]
pub use linux_systemd_user::{LinuxSystemdUserBootstrap, LinuxSystemdUserStatus};
pub use managed_payload_archive::{
    pack_managed_payload, unpack_managed_payload, ManagedPayloadArchiveError,
    ManagedPayloadArchiveReceiptV1,
};
pub use managed_release_stager::PrivatePythonManagedReleaseStager;
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
pub use update_adapters::{
    HttpUpdateArtifactFetcher, ServiceManagerReleaseActivator, ServiceManagerUpdateHealthGate,
    SignedBlockRollbackPolicy, SystemClock, UpdateCloudConnectivityProbe, UpdateLiveSessionProbe,
};
pub use update_connector_lane::GracefulServiceManagerConnectorLane;
pub use update_coordinator::{
    StagedReleaseV1, UpdateArtifactFetcher, UpdateConnectorLane, UpdateCoordinator,
    UpdateCoordinatorError, UpdateHealthEvidenceV1, UpdateHealthGate, UpdateOutcomeStatusV1,
    UpdateOutcomeV1, UpdatePhaseV1, UpdatePlanV1, UpdateReleaseActivator,
    UpdateReleaseStager, UpdateRollbackPolicy, UpdateSafeWindowEvidenceV1,
    UpdateSafeWindowProbe, UpdateTransactionV1,
};
pub use update_download::{
    download_spec_from_verified_release, download_verified_artifact, ArtifactDownloadReceiptV1,
    ArtifactDownloadSpecV1, ArtifactRangeSource, DownloadChunkV1, ReleaseArtifactKindV1,
    UpdateDownloadError,
};
pub use update_http::{HttpDownloadGrantV1, HttpRangeSource};
pub use update_runtime::{
    compose_managed_update_safe_window, ManagedUpdateSafeWindow,
};
#[cfg(target_os = "macos")]
pub use update_runtime::compose_macos_authoritative_update_safe_window;
#[cfg(windows)]
pub use update_runtime::compose_windows_authoritative_update_safe_window;
pub use update_safe_window::{
    DrainingSafeWindowProbe, HostUpdateSafetySnapshotV1, HostUpdateSafetySource,
};
#[cfg(windows)]
pub use windows_host_update_safety_pipe::{
    default_update_safety_pipe_name, WindowsHostUpdateSafetySource,
};
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
