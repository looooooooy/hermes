use hermes_runtime_manager::ipc::{ManagerRequestV1, ManagerResponseV1};
#[cfg(unix)]
use hermes_runtime_manager::local_ipc::request_read_only;
use hermes_runtime_manager::model::{LifecycleState, ManagerSnapshotV1};
#[cfg(unix)]
use hermes_runtime_manager::platform::DefaultInstallLayout;
#[cfg(unix)]
use hermes_runtime_manager::ports::InstallLayout;
#[cfg(windows)]
use hermes_runtime_manager::windows_pipe::{
    current_user_pipe_name, request_read_only as request_windows_pipe,
};
use serde::Serialize;
#[cfg(unix)]
use std::path::Path;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeComponent {
    id: String,
    name: String,
    detail: String,
    state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    latency_ms: Option<u16>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ProviderStatus {
    name: String,
    model: String,
    state: String,
    note: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeEvent {
    id: String,
    at: String,
    title: String,
    detail: String,
    tone: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeSnapshot {
    device_name: String,
    profile_name: String,
    platform: String,
    architecture: String,
    desktop_version: String,
    runtime_version: String,
    runtime_generation: String,
    state: String,
    cloud_connected: bool,
    agent_ready: bool,
    active_sessions: u8,
    running_tasks: u8,
    update_available: bool,
    update_version: Option<String>,
    last_checked: String,
    components: Vec<RuntimeComponent>,
    providers: Vec<ProviderStatus>,
    events: Vec<RuntimeEvent>,
}

fn local_device_name() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "Hermes workstation".to_owned())
}

#[tauri::command]
fn runtime_snapshot() -> RuntimeSnapshot {
    match fetch_manager_snapshot() {
        Ok(snapshot) => from_manager_snapshot(snapshot),
        Err(error) => offline_snapshot(error),
    }
}

#[cfg(unix)]
fn fetch_manager_snapshot() -> Result<ManagerSnapshotV1, String> {
    let layout = DefaultInstallLayout::discover().map_err(|error| error.to_string())?;
    let state_root = layout.state_root().map_err(|error| error.to_string())?;
    fetch_manager_snapshot_from(&state_root.join("rm.sock"))
}

#[cfg(windows)]
fn fetch_manager_snapshot() -> Result<ManagerSnapshotV1, String> {
    let pipe_name = current_user_pipe_name().map_err(|error| error.to_string())?;
    fetch_manager_snapshot_from_pipe(&pipe_name)
}

#[cfg(unix)]
fn fetch_manager_snapshot_from(endpoint: &Path) -> Result<ManagerSnapshotV1, String> {
    let request_id = format!("desktop-status-{}", std::process::id());
    match request_read_only(endpoint, &request_id, ManagerRequestV1::Status)
        .map_err(|error| error.to_string())?
    {
        ManagerResponseV1::Snapshot(snapshot) => Ok(snapshot),
        ManagerResponseV1::Error { code, message } => Err(format!("{code}: {message}")),
        other => Err(format!("unexpected Runtime Manager response: {other:?}")),
    }
}

#[cfg(windows)]
fn fetch_manager_snapshot_from_pipe(pipe_name: &str) -> Result<ManagerSnapshotV1, String> {
    let request_id = format!("desktop-status-{}", std::process::id());
    match request_windows_pipe(pipe_name, &request_id, ManagerRequestV1::Status)
        .map_err(|error| error.to_string())?
    {
        ManagerResponseV1::Snapshot(snapshot) => Ok(snapshot),
        ManagerResponseV1::Error { code, message } => Err(format!("{code}: {message}")),
        other => Err(format!("unexpected Runtime Manager response: {other:?}")),
    }
}

fn from_manager_snapshot(snapshot: ManagerSnapshotV1) -> RuntimeSnapshot {
    let cloud_connected = component_ready(&snapshot, "Hermes Cloud");
    let core_ready = component_ready(&snapshot, "Hermes Core");
    let plugin_ready = component_ready(&snapshot, "Agent Plugin");
    let agent_ready = core_ready && plugin_ready;
    let components = snapshot
        .components
        .iter()
        .map(|component| RuntimeComponent {
            id: component_id(&component.name).to_owned(),
            name: component.name.clone(),
            detail: component.detail.clone(),
            state: if component.ready { "healthy" } else { "offline" }.to_owned(),
            latency_ms: None,
        })
        .collect();

    RuntimeSnapshot {
        device_name: local_device_name(),
        profile_name: "Work".to_owned(),
        platform: std::env::consts::OS.to_owned(),
        architecture: std::env::consts::ARCH.to_owned(),
        desktop_version: env!("CARGO_PKG_VERSION").to_owned(),
        runtime_version: snapshot
            .active_release
            .clone()
            .unwrap_or_else(|| "not-installed".to_owned()),
        runtime_generation: snapshot
            .runtime_generation
            .clone()
            .unwrap_or_else(|| "manager-connected".to_owned()),
        state: lifecycle_name(snapshot.state).to_owned(),
        cloud_connected,
        agent_ready,
        active_sessions: 0,
        running_tasks: 0,
        update_available: false,
        update_version: None,
        last_checked: "live local IPC".to_owned(),
        components,
        providers: provider_slots(),
        events: vec![RuntimeEvent {
            id: "runtime-manager-connected".to_owned(),
            at: "now".to_owned(),
            title: "Runtime Manager connected".to_owned(),
            detail: "Desktop received an evidence snapshot over the read-only local IPC transport."
                .to_owned(),
            tone: if agent_ready && cloud_connected {
                "success"
            } else {
                "neutral"
            }
            .to_owned(),
        }],
    }
}

fn offline_snapshot(error: String) -> RuntimeSnapshot {
    RuntimeSnapshot {
        device_name: local_device_name(),
        profile_name: "Work".to_owned(),
        platform: std::env::consts::OS.to_owned(),
        architecture: std::env::consts::ARCH.to_owned(),
        desktop_version: env!("CARGO_PKG_VERSION").to_owned(),
        runtime_version: "not-connected".to_owned(),
        runtime_generation: "manager-not-connected".to_owned(),
        state: "offline".to_owned(),
        cloud_connected: false,
        agent_ready: false,
        active_sessions: 0,
        running_tasks: 0,
        update_available: false,
        update_version: None,
        last_checked: "native shell".to_owned(),
        components: [
            ("cloud", "Hermes Cloud"),
            ("connector", "Connector"),
            ("plugin", "Agent Plugin"),
            ("core", "Hermes Core"),
        ]
        .into_iter()
        .map(|(id, name)| RuntimeComponent {
            id: id.to_owned(),
            name: name.to_owned(),
            detail: "Waiting for Runtime Manager evidence".to_owned(),
            state: "offline".to_owned(),
            latency_ms: None,
        })
        .collect(),
        providers: provider_slots(),
        events: vec![RuntimeEvent {
            id: "runtime-manager-offline".to_owned(),
            at: "now".to_owned(),
            title: "Runtime Manager unavailable".to_owned(),
            detail: error,
            tone: "attention".to_owned(),
        }],
    }
}

fn provider_slots() -> Vec<ProviderStatus> {
    ["DeepSeek", "Kimi"]
        .into_iter()
        .map(|name| ProviderStatus {
            name: name.to_owned(),
            model: "Provider slot".to_owned(),
            state: "not-configured".to_owned(),
            note: "Local secret store".to_owned(),
        })
        .collect()
}

fn component_ready(snapshot: &ManagerSnapshotV1, name: &str) -> bool {
    snapshot
        .components
        .iter()
        .find(|component| component.name == name)
        .is_some_and(|component| component.ready)
}

fn component_id(name: &str) -> &'static str {
    match name {
        "Hermes Cloud" => "cloud",
        "Connector" => "connector",
        "Agent Plugin" => "plugin",
        "Hermes Core" => "core",
        _ => "component",
    }
}

fn lifecycle_name(state: LifecycleState) -> &'static str {
    match state {
        LifecycleState::Absent => "absent",
        LifecycleState::Installing => "installing",
        LifecycleState::Stopped => "stopped",
        LifecycleState::Starting => "starting",
        LifecycleState::Ready => "ready",
        LifecycleState::Updating => "updating",
        LifecycleState::RollingBack => "rolling_back",
        LifecycleState::Degraded => "degraded",
        LifecycleState::Failed => "failed",
    }
}

#[cfg(test)]
mod tests {
    #[cfg(unix)]
    #[test]
    fn desktop_snapshot_consumes_real_runtime_manager_socket() {
        use super::fetch_manager_snapshot_from;
        use hermes_runtime_manager::local_ipc::ReadOnlyUnixServer;
        use hermes_runtime_manager::platform::{DefaultInstallLayout, FailClosedServiceManager};
        use hermes_runtime_manager::RuntimeManager;
        use std::fs;
        use std::path::Path;
        use std::sync::Arc;
        use std::thread;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .subsec_nanos();
        let root = Path::new("/tmp").join(format!("hdt-{}-{unique}", std::process::id()));
        let endpoint = root.join("rm.sock");
        let manager = Arc::new(RuntimeManager::new(
            Arc::new(FailClosedServiceManager),
            Arc::new(DefaultInstallLayout::discover().expect("layout")),
        ));
        let server = ReadOnlyUnixServer::bind(endpoint.clone(), manager).expect("bind server");
        let server_thread = std::thread::spawn(move || server.serve_once().expect("serve"));

        let snapshot = fetch_manager_snapshot_from(&endpoint).expect("Desktop IPC snapshot");
        server_thread.join().expect("server thread");

        assert_eq!(snapshot.schema_version, 1);
        assert_eq!(snapshot.components.len(), 4);
        assert!(snapshot.components.iter().all(|component| !component.ready));
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn desktop_snapshot_consumes_real_runtime_manager_named_pipe() {
        use super::fetch_manager_snapshot_from_pipe;
        use hermes_runtime_manager::platform::{DefaultInstallLayout, FailClosedServiceManager};
        use hermes_runtime_manager::windows_pipe::{
            current_user_pipe_name, ReadOnlyWindowsPipeServer,
        };
        use hermes_runtime_manager::RuntimeManager;
        use std::sync::Arc;
        use std::thread;

        let base = current_user_pipe_name().expect("pipe name");
        let name = format!("{base}-desktop-test-{}", std::process::id());
        let manager = Arc::new(RuntimeManager::new(
            Arc::new(FailClosedServiceManager),
            Arc::new(DefaultInstallLayout::discover().expect("layout")),
        ));
        let server = ReadOnlyWindowsPipeServer::new(&name, manager).expect("bind server");
        let server_thread = thread::spawn(move || server.serve_once().expect("serve"));

        let snapshot = fetch_manager_snapshot_from_pipe(&name).expect("Desktop pipe snapshot");
        let identity = server_thread.join().expect("server thread");

        assert_eq!(snapshot.schema_version, 1);
        assert_eq!(snapshot.components.len(), 4);
        assert!(snapshot.components.iter().all(|component| !component.ready));
        assert_eq!(identity.pid, std::process::id());
        assert!(identity.same_user);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![runtime_snapshot])
        .run(tauri::generate_context!())
        .expect("failed to run Hermes Desktop");
}
