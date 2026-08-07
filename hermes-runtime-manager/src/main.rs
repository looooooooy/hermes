use hermes_runtime_manager::platform::{DefaultInstallLayout, FailClosedServiceManager};
#[cfg(unix)]
use hermes_runtime_manager::ports::InstallLayout;
use hermes_runtime_manager::{
    run_blank_machine_toolchain_gate, PrivateToolchainInstaller, RuntimeManager,
};
use std::path::PathBuf;
use std::sync::Arc;

fn main() {
    let args = std::env::args().collect::<Vec<_>>();
    let command = args.get(1).map(String::as_str).unwrap_or("status");

    if command == "install-toolchain" {
        install_toolchain_command(&args);
        return;
    }
    if command == "blank-machine-toolchain-gate" {
        blank_machine_toolchain_gate_command(&args);
        return;
    }

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

    match command {
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
            eprintln!(
                "supported commands: status, doctor, serve-read-only, install-toolchain, blank-machine-toolchain-gate, version"
            );
            std::process::exit(64);
        }
    }
}

fn install_toolchain_command(args: &[String]) {
    if args.len() != 4 {
        eprintln!("usage: hermes-runtime-manager install-toolchain <bundle-root> <toolchains-root>");
        std::process::exit(64);
    }
    let source = PathBuf::from(&args[2]);
    let destination = PathBuf::from(&args[3]);
    match PrivateToolchainInstaller::install(&source, &destination) {
        Ok(manifest) => println!(
            "{}",
            serde_json::to_string_pretty(&manifest).expect("toolchain manifest is serializable")
        ),
        Err(error) => {
            eprintln!("runtime_manager_toolchain_install_error: {error}");
            std::process::exit(7);
        }
    }
}

fn blank_machine_toolchain_gate_command(args: &[String]) {
    if args.len() != 4 {
        eprintln!(
            "usage: hermes-runtime-manager blank-machine-toolchain-gate <qualified-bundle-root> <fresh-sandbox-root>"
        );
        std::process::exit(64);
    }
    let source = PathBuf::from(&args[2]);
    let sandbox = PathBuf::from(&args[3]);
    match run_blank_machine_toolchain_gate(&source, &sandbox) {
        Ok(report) => println!(
            "{}",
            serde_json::to_string_pretty(&report).expect("blank-machine report is serializable")
        ),
        Err(error) => {
            eprintln!("runtime_manager_blank_machine_gate_error: {error}");
            std::process::exit(8);
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
        Ok(root) => root.join("rm.sock"),
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
    manager: Arc<RuntimeManager>,
    _layout: Arc<DefaultInstallLayout>,
) {
    use hermes_runtime_manager::windows_pipe::{
        current_user_pipe_name, ReadOnlyWindowsPipeServer,
    };

    let pipe_name = match current_user_pipe_name() {
        Ok(name) => name,
        Err(error) => {
            eprintln!("runtime_manager_pipe_identity_error: {error}");
            std::process::exit(4);
        }
    };
    let server = match ReadOnlyWindowsPipeServer::new(&pipe_name, manager) {
        Ok(server) => server,
        Err(error) => {
            eprintln!("runtime_manager_pipe_bind_error: {error}");
            std::process::exit(5);
        }
    };
    eprintln!("runtime_manager_read_only_pipe_ready: {pipe_name}");
    loop {
        if let Err(error) = server.serve_once() {
            eprintln!("runtime_manager_pipe_request_error: {error}");
        }
    }
}
