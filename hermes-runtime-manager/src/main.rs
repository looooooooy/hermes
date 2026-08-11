use hermes_runtime_manager::platform::DefaultInstallLayout;
#[cfg(target_os = "linux")]
use hermes_runtime_manager::platform::FailClosedServiceManager;
#[cfg(unix)]
use hermes_runtime_manager::ports::InstallLayout;
#[cfg(windows)]
use hermes_runtime_manager::WindowsTaskServiceManager;
use hermes_runtime_manager::{
    pack_managed_payload, run_blank_machine_toolchain_gate, verify_portable_plugin_signature,
    verify_release_control_files, PrivatePythonManagedReleaseStager, PrivateToolchainInstaller,
    RuntimeManager,
};
#[cfg(target_os = "macos")]
use hermes_runtime_manager::{
    ports::{ConnectorLaunchConfigV1, PortError, ServiceManager},
    InitialReleaseActivator, MacOSLaunchAgentServiceManager, ServiceManagerInitialReadinessProbe,
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
    if command == "verify-plugin-signature" {
        verify_plugin_signature_command(&args);
        return;
    }
    if command == "verify-release-control" {
        verify_release_control_command(&args);
        return;
    }
    if command == "pack-managed-payload" {
        pack_managed_payload_command(&args);
        return;
    }
    if command == "stage-managed-payload" {
        stage_managed_payload_command(&args);
        return;
    }

    let layout = match DefaultInstallLayout::discover() {
        Ok(layout) => Arc::new(layout),
        Err(error) => {
            eprintln!("runtime_manager_layout_error: {error}");
            std::process::exit(2);
        }
    };

    if command == "ipc-endpoint" {
        print_read_only_ipc_endpoint(&layout);
        return;
    }

    #[cfg(target_os = "macos")]
    let (manager, macos_service_manager) = {
        let application_root = match layout.application_root() {
            Ok(path) => path,
            Err(error) => {
                eprintln!("runtime_manager_layout_error: {error}");
                std::process::exit(2);
            }
        };
        let logs_root = match layout.logs_root() {
            Ok(path) => path,
            Err(error) => {
                eprintln!("runtime_manager_layout_error: {error}");
                std::process::exit(2);
            }
        };
        let connector_config = if command == "activate-initial-release" {
            match connector_config_from_activation_args(&args, &application_root) {
                Ok(config) => Some(config),
                Err(error) => {
                    eprintln!("runtime_manager_activation_configuration_error: {error}");
                    std::process::exit(64);
                }
            }
        } else {
            None
        };
        let service_manager = match MacOSLaunchAgentServiceManager::new(
            application_root,
            logs_root,
            connector_config,
        ) {
            Ok(manager) => manager,
            Err(error) => {
                eprintln!("runtime_manager_macos_service_error: {error}");
                std::process::exit(2);
            }
        };
        let service_manager = Arc::new(service_manager);
        match RuntimeManager::new_persistent(service_manager.clone(), layout.clone()) {
            Ok(manager) => (manager, service_manager),
            Err(error) => {
                eprintln!("runtime_manager_persistent_state_error: {error}");
                std::process::exit(2);
            }
        }
    };
    #[cfg(windows)]
    let manager = match RuntimeManager::new_persistent(
        Arc::new(WindowsTaskServiceManager::new()),
        layout.clone(),
    ) {
        Ok(manager) => manager,
        Err(error) => {
            eprintln!("runtime_manager_persistent_state_error: {error}");
            std::process::exit(2);
        }
    };
    #[cfg(target_os = "linux")]
    let manager = RuntimeManager::new(Arc::new(FailClosedServiceManager), layout.clone());
    let manager = Arc::new(manager);

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
            #[cfg(target_os = "macos")]
            println!("Platform adapter status: macOS LaunchAgent service projection selected.");
            #[cfg(windows)]
            println!(
                "Platform adapter status: Windows Task Scheduler service projection selected."
            );
            #[cfg(target_os = "linux")]
            println!("Platform adapter status: fail-closed until a verified adapter is selected.");
        }
        "serve-read-only" => serve_read_only(manager, layout),
        #[cfg(target_os = "macos")]
        "activate-initial-release" => {
            activate_initial_release_command(&args, manager, macos_service_manager, layout)
        }
        "--version" | "version" => println!("{}", env!("CARGO_PKG_VERSION")),
        other => {
            eprintln!("unsupported command: {other}");
            eprintln!(
                "supported commands: status, doctor, serve-read-only, ipc-endpoint, install-toolchain, blank-machine-toolchain-gate, verify-plugin-signature, verify-release-control, pack-managed-payload, stage-managed-payload, version"
            );
            std::process::exit(64);
        }
    }
}

#[cfg(target_os = "macos")]
fn connector_config_from_activation_args(
    args: &[String],
    application_root: &std::path::Path,
) -> Result<ConnectorLaunchConfigV1, PortError> {
    if args.len() != 7 {
        return Err(PortError::Operation(
            "usage: hermes-runtime-manager activate-initial-release RELEASE_ID GENERATION API_ENDPOINT WSS_ENDPOINT DISPLAY_NAME"
                .to_owned(),
        ));
    }
    let state_directory = application_root.join("connector/profiles/default/state");
    let config = ConnectorLaunchConfigV1 {
        cloud_api_endpoint: args[4].clone(),
        cloud_endpoint: args[5].clone(),
        display_name: args[6].clone(),
        profile: "default".to_owned(),
        connector_version: env!("CARGO_PKG_VERSION").to_owned(),
        application_root: application_root.to_path_buf(),
        database_file: state_directory.join("connector.sqlite3"),
        lock_file: state_directory.join("connector.lock"),
        state_directory,
    };
    config.validate()?;
    Ok(config)
}

#[cfg(target_os = "macos")]
fn activate_initial_release_command(
    args: &[String],
    manager: Arc<RuntimeManager>,
    service_manager: Arc<MacOSLaunchAgentServiceManager>,
    layout: Arc<DefaultInstallLayout>,
) {
    let releases_root = match layout.releases_root() {
        Ok(path) => path,
        Err(error) => {
            eprintln!("runtime_manager_activation_error: {error}");
            std::process::exit(9);
        }
    };
    let services: Arc<dyn ServiceManager> = service_manager;
    let readiness = Arc::new(ServiceManagerInitialReadinessProbe::new(services.clone()));
    let activator = match InitialReleaseActivator::new(
        manager,
        services,
        readiness,
        releases_root,
        layout.platform(),
    ) {
        Ok(activator) => activator,
        Err(error) => {
            eprintln!("runtime_manager_activation_error: {error}");
            std::process::exit(9);
        }
    };
    match activator.activate(&args[2], &args[3]) {
        Ok(snapshot) => println!(
            "{}",
            serde_json::to_string_pretty(&snapshot).expect("snapshot is serializable")
        ),
        Err(error) => {
            eprintln!("runtime_manager_activation_error: {error}");
            std::process::exit(9);
        }
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use std::path::Path;

    #[test]
    fn activation_cli_derives_managed_connector_paths() {
        let args = vec![
            "hermes-runtime-manager".to_owned(),
            "activate-initial-release".to_owned(),
            "desktop-0.1.0+macos-aarch64".to_owned(),
            "1".to_owned(),
            "https://api.example.test/hermes".to_owned(),
            "wss://api.example.test/hermes/internal/connector/ws".to_owned(),
            "Hermes workstation".to_owned(),
        ];
        let root = PathBuf::from("/Applications/Hermes/managed");

        let config = connector_config_from_activation_args(&args, &root).unwrap();

        assert_eq!(config.profile, "default");
        assert_eq!(config.application_root, root);
        assert_eq!(
            config.state_directory,
            PathBuf::from("/Applications/Hermes/managed/connector/profiles/default/state")
        );
        assert_eq!(
            config.database_file,
            config.state_directory.join("connector.sqlite3")
        );
        assert_eq!(
            config.lock_file,
            config.state_directory.join("connector.lock")
        );
    }

    #[test]
    fn activation_cli_rejects_incomplete_configuration() {
        let args = vec![
            "hermes-runtime-manager".to_owned(),
            "activate-initial-release".to_owned(),
            "desktop-0.1.0+macos-aarch64".to_owned(),
        ];
        assert!(connector_config_from_activation_args(
            &args,
            Path::new("/Applications/Hermes/managed")
        )
        .is_err());
    }
}

fn install_toolchain_command(args: &[String]) {
    if args.len() != 4 {
        eprintln!(
            "usage: hermes-runtime-manager install-toolchain <bundle-root> <toolchains-root>"
        );
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

fn verify_plugin_signature_command(args: &[String]) {
    if args.len() != 5 {
        eprintln!(
            "usage: hermes-runtime-manager verify-plugin-signature <portable-manifest.json> <trust-store.json> <plugin-wheel>"
        );
        std::process::exit(64);
    }
    let manifest = PathBuf::from(&args[2]);
    let trust_store = PathBuf::from(&args[3]);
    let wheel = PathBuf::from(&args[4]);
    match verify_portable_plugin_signature(&manifest, &trust_store, &wheel) {
        Ok(report) => println!(
            "{}",
            serde_json::to_string_pretty(&report).expect("plugin verification report serializable")
        ),
        Err(error) => {
            eprintln!("runtime_manager_plugin_signature_error: {error}");
            std::process::exit(9);
        }
    }
}

fn verify_release_control_command(args: &[String]) {
    if args.len() != 7 {
        eprintln!(
            "usage: hermes-runtime-manager verify-release-control <release-envelope.json> <channel-envelope.json> <block-envelope.json> <release-trust-store.json> <observed-state.json>"
        );
        std::process::exit(64);
    }
    let release = PathBuf::from(&args[2]);
    let channel = PathBuf::from(&args[3]);
    let block = PathBuf::from(&args[4]);
    let trust_store = PathBuf::from(&args[5]);
    let observed = PathBuf::from(&args[6]);
    match verify_release_control_files(&release, &channel, &block, &trust_store, &observed) {
        Ok(report) => println!(
            "{}",
            serde_json::to_string_pretty(&report).expect("release control report serializable")
        ),
        Err(error) => {
            eprintln!("runtime_manager_release_control_error: {error}");
            std::process::exit(10);
        }
    }
}

fn pack_managed_payload_command(args: &[String]) {
    if args.len() != 4 {
        eprintln!(
            "usage: hermes-runtime-manager pack-managed-payload <portable-payload-root> <output.tar.zst>"
        );
        std::process::exit(64);
    }
    let payload = PathBuf::from(&args[2]);
    let output = PathBuf::from(&args[3]);
    match pack_managed_payload(&payload, &output) {
        Ok(receipt) => println!(
            "{{\"schema_version\":1,\"files\":{},\"expanded_bytes\":{},\"archive\":{}}}",
            receipt.files,
            receipt.expanded_bytes,
            serde_json::to_string(&output).expect("archive path is serializable")
        ),
        Err(error) => {
            eprintln!("runtime_manager_managed_payload_pack_error: {error}");
            std::process::exit(11);
        }
    }
}

fn stage_managed_payload_command(args: &[String]) {
    if args.len() != 12 {
        eprintln!(
            "usage: hermes-runtime-manager stage-managed-payload <archive.tar.zst> <private-python> <installer.pyz> <runtime-manager> <qualified-toolchain-root> <releases-root> <staging-root> <release-id> <release-generation> <target>"
        );
        std::process::exit(64);
    }
    let release_id = &args[9];
    let release_generation = match args[10].parse::<u64>() {
        Ok(value) if value > 0 => value,
        _ => {
            eprintln!("runtime_manager_managed_payload_stage_error: release generation is invalid");
            std::process::exit(64);
        }
    };
    let stager = match PrivatePythonManagedReleaseStager::new(
        PathBuf::from(&args[3]),
        PathBuf::from(&args[4]),
        PathBuf::from(&args[5]),
        PathBuf::from(&args[6]),
        PathBuf::from(&args[7]),
        PathBuf::from(&args[8]),
        args[11].clone(),
    ) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("runtime_manager_managed_payload_stage_error: {error}");
            std::process::exit(12);
        }
    };
    match stager.stage_archive(&PathBuf::from(&args[2]), release_id, release_generation) {
        Ok(staged) => println!(
            "{{\"schema_version\":1,\"release_id\":{},\"release_generation\":{},\"release_path\":{},\"content_verified\":true}}",
            serde_json::to_string(&staged.release_id).expect("release id serializable"),
            staged.release_generation,
            serde_json::to_string(&staged.release_path).expect("release path serializable")
        ),
        Err(error) => {
            eprintln!("runtime_manager_managed_payload_stage_error: {error}");
            std::process::exit(12);
        }
    }
}

#[cfg(unix)]
fn print_read_only_ipc_endpoint(layout: &DefaultInstallLayout) {
    match layout.state_root() {
        Ok(root) => println!("{}", root.join("rm.sock").display()),
        Err(error) => {
            eprintln!("runtime_manager_ipc_layout_error: {error}");
            std::process::exit(4);
        }
    }
}

#[cfg(windows)]
fn print_read_only_ipc_endpoint(_layout: &DefaultInstallLayout) {
    match hermes_runtime_manager::windows_pipe::current_user_pipe_name() {
        Ok(name) => println!("{name}"),
        Err(error) => {
            eprintln!("runtime_manager_pipe_identity_error: {error}");
            std::process::exit(4);
        }
    }
}

#[cfg(unix)]
fn serve_read_only(manager: Arc<RuntimeManager>, layout: Arc<DefaultInstallLayout>) {
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
    eprintln!(
        "runtime_manager_read_only_ipc_ready: {}",
        endpoint.display()
    );
    loop {
        if let Err(error) = server.serve_once() {
            eprintln!("runtime_manager_ipc_request_error: {error}");
        }
    }
}

#[cfg(windows)]
fn serve_read_only(manager: Arc<RuntimeManager>, _layout: Arc<DefaultInstallLayout>) {
    use hermes_runtime_manager::windows_pipe::{current_user_pipe_name, ReadOnlyWindowsPipeServer};

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
