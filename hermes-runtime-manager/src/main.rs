use hermes_runtime_manager::platform::{DefaultInstallLayout, FailClosedServiceManager};
use hermes_runtime_manager::ports::InstallLayout;
use hermes_runtime_manager::RuntimeManager;
use std::sync::Arc;

fn main() {
    let command = std::env::args().nth(1).unwrap_or_else(|| "status".to_owned());
    let layout = match DefaultInstallLayout::discover() {
        Ok(layout) => Arc::new(layout),
        Err(error) => {
            eprintln!("runtime_manager_layout_error: {error}");
            std::process::exit(2);
        }
    };
    let manager = Arc::new(RuntimeManager::new(
        Arc::new(FailClosedServiceManager),
        layout.clone(),
    ));

    match command.as_str() {
        "status" | "--status-json" => match manager.snapshot() {
            Ok(snapshot) => println!(
                "{}",
                serde_json::to_string_pretty(&snapshot).expect("snapshot is serializable")
            ),
            Err(error) => {
                eprintln!("runtime_manager_status_error: {error}");
                std::process::exit(3);
            }
        },
        "doctor" => {
            println!("Hermes Runtime Manager foundation is installed.");
            println!("Platform adapter status: fail-closed until a verified adapter is selected.");
        }
        "serve-read-only" => serve_read_only(manager, layout),
        "--version" | "version" => println!("{}", env!("CARGO_PKG_VERSION")),
        other => {
            eprintln!("unsupported command: {other}");
            eprintln!("supported commands: status, doctor, serve-read-only, version");
            std::process::exit(64);
        }
    }
}

#[cfg(unix)]
fn serve_read_only(
    manager: Arc<RuntimeManager>,
    layout: Arc<DefaultInstallLayout>,
) {
    use hermes_runtime_manager::local_ipc::ReadOnlyUnixServer;

    let endpoint = match layout.state_root() {
        Ok(root) => root.join("runtime-manager.sock"),
        Err(error) => {
            eprintln!("runtime_manager_ipc_layout_error: {error}");
            std::process::exit(4);
        }
    };
    let server = match ReadOnlyUnixServer::bind(endpoint.clone(), manager) {
        Ok(server) => server,
        Err(error) => {
            eprintln!("runtime_manager_ipc_bind_error: {error}");
            std::process::exit(5);
        }
    };
    eprintln!("runtime_manager_read_only_ipc_ready: {}", endpoint.display());
    loop {
        if let Err(error) = server.serve_once() {
            eprintln!("runtime_manager_ipc_request_error: {error}");
        }
    }
}

#[cfg(windows)]
fn serve_read_only(
    _manager: Arc<RuntimeManager>,
    _layout: Arc<DefaultInstallLayout>,
) {
    eprintln!("runtime_manager_ipc_unavailable: Windows Named Pipe transport is not implemented yet");
    std::process::exit(6);
}
