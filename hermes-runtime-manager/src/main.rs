use hermes_runtime_manager::platform::{DefaultInstallLayout, FailClosedServiceManager};
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
    let manager = RuntimeManager::new(Arc::new(FailClosedServiceManager), layout);

    match command.as_str() {
        "status" | "--status-json" => match manager.snapshot() {
            Ok(snapshot) => println!("{}", serde_json::to_string_pretty(&snapshot).expect("snapshot is serializable")),
            Err(error) => {
                eprintln!("runtime_manager_status_error: {error}");
                std::process::exit(3);
            }
        },
        "doctor" => {
            println!("Hermes Runtime Manager foundation is installed.");
            println!("Platform adapter status: fail-closed until a verified adapter is selected.");
        }
        "--version" | "version" => println!("{}", env!("CARGO_PKG_VERSION")),
        other => {
            eprintln!("unsupported command: {other}");
            eprintln!("supported commands: status, doctor, version");
            std::process::exit(64);
        }
    }
}
