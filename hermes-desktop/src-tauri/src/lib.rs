use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeComponent {
    id: &'static str,
    name: &'static str,
    detail: &'static str,
    state: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    latency_ms: Option<u16>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ProviderStatus {
    name: &'static str,
    model: &'static str,
    state: &'static str,
    note: &'static str,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeEvent {
    id: &'static str,
    at: &'static str,
    title: &'static str,
    detail: &'static str,
    tone: &'static str,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct RuntimeSnapshot {
    device_name: String,
    profile_name: &'static str,
    platform: String,
    architecture: &'static str,
    desktop_version: &'static str,
    runtime_version: &'static str,
    runtime_generation: &'static str,
    state: &'static str,
    cloud_connected: bool,
    agent_ready: bool,
    active_sessions: u8,
    running_tasks: u8,
    update_available: bool,
    update_version: Option<&'static str>,
    last_checked: &'static str,
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
    RuntimeSnapshot {
        device_name: local_device_name(),
        profile_name: "Work",
        platform: std::env::consts::OS.to_owned(),
        architecture: std::env::consts::ARCH,
        desktop_version: env!("CARGO_PKG_VERSION"),
        runtime_version: "foundation-dev",
        runtime_generation: "manager-not-connected",
        state: "healthy",
        cloud_connected: false,
        agent_ready: false,
        active_sessions: 0,
        running_tasks: 0,
        update_available: false,
        update_version: None,
        last_checked: "native shell",
        components: vec![
            RuntimeComponent {
                id: "cloud",
                name: "Hermes Cloud",
                detail: "Waiting for Runtime Manager",
                state: "offline",
                latency_ms: None,
            },
            RuntimeComponent {
                id: "connector",
                name: "Connector",
                detail: "Lifecycle not attached yet",
                state: "offline",
                latency_ms: None,
            },
            RuntimeComponent {
                id: "plugin",
                name: "Agent Plugin",
                detail: "Lifecycle not attached yet",
                state: "offline",
                latency_ms: None,
            },
            RuntimeComponent {
                id: "core",
                name: "Hermes Core",
                detail: "Lifecycle not attached yet",
                state: "offline",
                latency_ms: None,
            },
        ],
        providers: vec![
            ProviderStatus {
                name: "DeepSeek",
                model: "Provider slot",
                state: "not-configured",
                note: "Local secret store",
            },
            ProviderStatus {
                name: "Kimi",
                model: "Provider slot",
                state: "not-configured",
                note: "Local secret store",
            },
        ],
        events: vec![RuntimeEvent {
            id: "native-foundation",
            at: "now",
            title: "Desktop shell started",
            detail: "Native Tauri shell is running; Runtime Manager attachment is the next slice.",
            tone: "neutral",
        }],
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![runtime_snapshot])
        .run(tauri::generate_context!())
        .expect("failed to run Hermes Desktop");
}
