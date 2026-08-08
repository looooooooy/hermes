#![cfg(unix)]

use hermes_runtime_manager::model::{ComponentHealth, LifecycleState, PlatformKind};
use hermes_runtime_manager::ports::{Clock, InstallLayout, PortError, ServiceManager};
use hermes_runtime_manager::release_control::{
    ReleaseChannelV1, ReleaseControlVerificationReportV1,
};
use hermes_runtime_manager::{
    compose_managed_update_safe_window, ArtifactDownloadReceiptV1,
    ArtifactDownloadSpecV1, HostUpdateSafetySource, ReleaseArtifactKindV1,
    StagedReleaseV1, UnixHostUpdateSafetySource, UpdateArtifactFetcher,
    UpdateCoordinator, UpdateHealthEvidenceV1, UpdateHealthGate,
    UpdateOutcomeStatusV1, UpdatePhaseV1, UpdatePlanV1, UpdateReleaseActivator,
    UpdateReleaseStager, UpdateRollbackPolicy,
};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;

static NEXT_ID: AtomicU64 = AtomicU64::new(1);

#[derive(Default)]
struct RecordingServices {
    calls: Mutex<Vec<String>>,
}

impl ServiceManager for RecordingServices {
    fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
        Ok(())
    }

    fn start_host(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
        Ok(())
    }

    fn stop_host(&self) -> Result<(), PortError> {
        Ok(())
    }

    fn start_connector(&self, _executable: &Path, release_id: &str) -> Result<(), PortError> {
        self.calls
            .lock()
            .unwrap()
            .push(format!("start:{release_id}"));
        Ok(())
    }

    fn stop_connector(&self) -> Result<(), PortError> {
        self.calls.lock().unwrap().push("stop".to_owned());
        Ok(())
    }

    fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
        Ok(vec![])
    }
}

struct Layout(PathBuf);

impl InstallLayout for Layout {
    fn platform(&self) -> PlatformKind {
        current_platform()
    }
    fn application_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.0.clone())
    }
    fn releases_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.0.join("releases"))
    }
    fn toolchains_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.0.join("toolchains"))
    }
    fn state_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.0.join("state"))
    }
    fn logs_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.0.join("logs"))
    }
}

struct FakeFetcher(PathBuf);

impl UpdateArtifactFetcher for FakeFetcher {
    fn fetch(&self, spec: &ArtifactDownloadSpecV1) -> Result<ArtifactDownloadReceiptV1, PortError> {
        Ok(ArtifactDownloadReceiptV1 {
            schema_version: 1,
            release_id: spec.release_id.clone(),
            release_generation: spec.release_generation,
            target: spec.target.clone(),
            kind: spec.kind,
            object_key: spec.object_key.clone(),
            sha256: spec.sha256.clone(),
            size_bytes: spec.size_bytes,
            final_path: self.0.join("cache").join(&spec.file_name),
            resumed_from_bytes: 0,
            downloaded_bytes: spec.size_bytes,
            reused_existing: false,
            content_verified: true,
        })
    }
}

struct FakeStager(PathBuf);

impl UpdateReleaseStager for FakeStager {
    fn stage(
        &self,
        _receipt: &ArtifactDownloadReceiptV1,
        release_id: &str,
        release_generation: u64,
    ) -> Result<StagedReleaseV1, PortError> {
        Ok(StagedReleaseV1 {
            release_id: release_id.to_owned(),
            release_generation,
            release_path: self.0.join("releases").join(release_id),
            content_verified: true,
        })
    }
}

#[derive(Default)]
struct FakeActivator {
    activated: Mutex<Vec<String>>,
}

impl UpdateReleaseActivator for FakeActivator {
    fn activate(&self, staged: &StagedReleaseV1) -> Result<(), PortError> {
        self.activated
            .lock()
            .unwrap()
            .push(staged.release_id.clone());
        Ok(())
    }

    fn rollback(&self, release_id: &str) -> Result<(), PortError> {
        self.activated
            .lock()
            .unwrap()
            .push(format!("rollback:{release_id}"));
        Ok(())
    }
}

struct Healthy;

impl UpdateHealthGate for Healthy {
    fn verify(&self, _release_id: &str) -> Result<UpdateHealthEvidenceV1, PortError> {
        Ok(UpdateHealthEvidenceV1 {
            agent_ready: true,
            cloud_connected: true,
            live_session_ok: true,
            components_ready: true,
        })
    }
}

struct AllowRollback;

impl UpdateRollbackPolicy for AllowRollback {
    fn rollback_allowed(&self, _release_id: &str) -> Result<bool, PortError> {
        Ok(true)
    }
}

struct TestClock(AtomicU64);

impl Clock for TestClock {
    fn unix_ms(&self) -> u64 {
        self.0.fetch_add(1, Ordering::SeqCst)
    }
}

#[test]
fn pending_approval_defers_and_restores_previous_connector_before_activation() {
    let root = temp_root();
    let services = Arc::new(RecordingServices::default());
    let manager = ready_manager(&root, services.clone());
    let (source, server) = one_snapshot_source(&root, snapshot_json(0, 1, 0));
    let composition = compose_managed_update_safe_window(
        manager.clone(),
        services.clone(),
        root.join("releases"),
        current_platform(),
        source,
    )
    .unwrap();
    let activator = Arc::new(FakeActivator::default());
    let coordinator = coordinator(
        &root,
        manager.clone(),
        composition.safe_window(),
        composition.connector_lane(),
        activator.clone(),
    );

    let outcome = coordinator.execute(&plan()).unwrap();

    assert_eq!(outcome.status, UpdateOutcomeStatusV1::Deferred);
    assert_eq!(outcome.transaction.phase, UpdatePhaseV1::WaitingSafeWindow);
    assert!(activator.activated.lock().unwrap().is_empty());
    assert_eq!(
        services.calls.lock().unwrap().as_slice(),
        ["stop", "start:1.0.0+20260801.1.g00000000"]
    );
    assert_eq!(manager.state().unwrap(), LifecycleState::Ready);
    assert_eq!(
        manager.snapshot().unwrap().active_release.as_deref(),
        Some("1.0.0+20260801.1.g00000000")
    );
    server.join().unwrap();
    cleanup(root);
}

#[test]
fn safe_authoritative_snapshot_enters_activation_and_reconciles_target_connector_once() {
    let root = temp_root();
    let services = Arc::new(RecordingServices::default());
    let manager = ready_manager(&root, services.clone());
    let (source, server) = one_snapshot_source(&root, snapshot_json(0, 0, 0));
    let composition = compose_managed_update_safe_window(
        manager.clone(),
        services.clone(),
        root.join("releases"),
        current_platform(),
        source,
    )
    .unwrap();
    let activator = Arc::new(FakeActivator::default());
    let coordinator = coordinator(
        &root,
        manager.clone(),
        composition.safe_window(),
        composition.connector_lane(),
        activator.clone(),
    );

    let outcome = coordinator.execute(&plan()).unwrap();

    assert_eq!(outcome.status, UpdateOutcomeStatusV1::Updated);
    assert_eq!(outcome.transaction.phase, UpdatePhaseV1::Completed);
    assert_eq!(
        activator.activated.lock().unwrap().as_slice(),
        ["1.0.1+20260808.1.g11111111"]
    );
    assert_eq!(
        services.calls.lock().unwrap().as_slice(),
        ["stop", "start:1.0.1+20260808.1.g11111111"]
    );
    assert_eq!(manager.state().unwrap(), LifecycleState::Ready);
    assert_eq!(
        manager.snapshot().unwrap().active_release.as_deref(),
        Some("1.0.1+20260808.1.g11111111")
    );
    server.join().unwrap();
    cleanup(root);
}

fn coordinator(
    root: &Path,
    manager: Arc<hermes_runtime_manager::RuntimeManager>,
    safe_window: Arc<dyn hermes_runtime_manager::UpdateSafeWindowProbe>,
    connector_lane: Arc<dyn hermes_runtime_manager::UpdateConnectorLane>,
    activator: Arc<FakeActivator>,
) -> UpdateCoordinator {
    UpdateCoordinator::new(
        manager,
        Arc::new(FakeFetcher(root.to_path_buf())),
        Arc::new(FakeStager(root.to_path_buf())),
        safe_window,
        connector_lane,
        activator,
        Arc::new(Healthy),
        Arc::new(AllowRollback),
        Arc::new(TestClock(AtomicU64::new(1_000))),
        root.join("journal"),
    )
    .unwrap()
}

fn ready_manager(
    root: &Path,
    services: Arc<RecordingServices>,
) -> Arc<hermes_runtime_manager::RuntimeManager> {
    let previous = "1.0.0+20260801.1.g00000000";
    let target = "1.0.1+20260808.1.g11111111";
    make_release(&root.join("releases"), previous);
    make_release(&root.join("releases"), target);
    let manager = Arc::new(hermes_runtime_manager::RuntimeManager::new(
        services,
        Arc::new(Layout(root.to_path_buf())),
    ));
    manager.transition(LifecycleState::Installing).unwrap();
    manager.transition(LifecycleState::Stopped).unwrap();
    manager.transition(LifecycleState::Starting).unwrap();
    manager.transition(LifecycleState::Ready).unwrap();
    manager.record_activation(previous, "100").unwrap();
    manager
}

fn plan() -> UpdatePlanV1 {
    let release_id = "1.0.1+20260808.1.g11111111";
    let digest = "a".repeat(64);
    UpdatePlanV1 {
        transaction_id: "update-101-authoritative".to_owned(),
        target_release_id: release_id.to_owned(),
        target_release_generation: 101,
        release_control: ReleaseControlVerificationReportV1 {
            schema_version: 1,
            product_version: "1.0.1".to_owned(),
            release_id: release_id.to_owned(),
            release_generation: 101,
            channel: ReleaseChannelV1::Stable,
            channel_generation: 10,
            block_generation: 2,
            effective_minimum_safe_generation: 100,
            rollback_authorized: false,
            decision: "forward_update".to_owned(),
            signatures_verified: true,
            eligible: true,
        },
        download_spec: ArtifactDownloadSpecV1 {
            schema_version: 1,
            release_id: release_id.to_owned(),
            release_generation: 101,
            target: current_target().to_owned(),
            kind: ReleaseArtifactKindV1::ManagedReleasePayload,
            object_key: format!("artifacts/v1/sha256/aa/{digest}/runtime.tar.zst"),
            file_name: "runtime.tar.zst".to_owned(),
            sha256: digest,
            size_bytes: 4_096,
            platform_signature: None,
        },
    }
}

fn one_snapshot_source(
    root: &Path,
    response: Vec<u8>,
) -> (
    Arc<dyn HostUpdateSafetySource>,
    thread::JoinHandle<()>,
) {
    let directory = root.join("host-safety");
    fs::create_dir(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700)).unwrap();
    let endpoint = directory.join("host.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    fs::set_permissions(&endpoint, fs::Permissions::from_mode(0o600)).unwrap();
    let server = thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        let mut request = Vec::new();
        let mut byte = [0_u8; 1];
        while request.last() != Some(&b'\n') {
            stream.read_exact(&mut byte).unwrap();
            request.push(byte[0]);
            assert!(request.len() <= 1_024);
        }
        assert_eq!(
            request,
            b"{\"method\":\"update-safety.snapshot\",\"schema_version\":1}\n"
        );
        stream.write_all(&response).unwrap();
    });
    (
        Arc::new(UnixHostUpdateSafetySource::new(endpoint).unwrap()),
        server,
    )
}

fn snapshot_json(active: u32, approvals: u32, clarifications: u32) -> Vec<u8> {
    format!(
        concat!(
            "{{\"schema_version\":1,",
            "\"profile\":\"default\",",
            "\"runtime_generation\":\"generation-42\",",
            "\"active_tasks\":{},",
            "\"pending_approvals\":{},",
            "\"pending_clarifications\":{},",
            "\"evidence_complete\":true}}\n"
        ),
        active,
        approvals,
        clarifications,
    )
    .into_bytes()
}

fn make_release(root: &Path, release_id: &str) {
    let release = root.join(release_id);
    let connector = connector_executable(&release);
    fs::create_dir_all(connector.parent().unwrap()).unwrap();
    fs::write(&connector, b"connector").unwrap();
    fs::set_permissions(&connector, fs::Permissions::from_mode(0o700)).unwrap();
}

fn connector_executable(release: &Path) -> PathBuf {
    release
        .join("connector")
        .join("venv")
        .join("bin")
        .join("hermes-connector")
}

fn current_platform() -> PlatformKind {
    #[cfg(target_os = "macos")]
    {
        PlatformKind::Macos
    }
    #[cfg(target_os = "linux")]
    {
        PlatformKind::Linux
    }
}

fn current_target() -> &'static str {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        "macos-aarch64"
    }
    #[cfg(all(target_os = "macos", target_arch = "x86_64"))]
    {
        "macos-x86_64"
    }
    #[cfg(all(target_os = "linux", target_arch = "aarch64"))]
    {
        "linux-aarch64"
    }
    #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
    {
        "linux-x86_64"
    }
}

fn temp_root() -> PathBuf {
    let id = NEXT_ID.fetch_add(1, Ordering::SeqCst);
    let root = PathBuf::from(format!("/tmp/husc-{}-{id}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).unwrap();
    root
}

fn cleanup(root: PathBuf) {
    let _ = fs::remove_dir_all(root);
}
