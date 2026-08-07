#![cfg(target_os = "linux")]

use hermes_runtime_manager::ipc::{ManagerRequestV1, ManagerResponseV1};
use hermes_runtime_manager::local_ipc::request_read_only;
use hermes_runtime_manager::platform::DefaultInstallLayout;
use hermes_runtime_manager::ports::InstallLayout;
use hermes_runtime_manager::LinuxSystemdUserBootstrap;
use std::path::PathBuf;
use std::thread;
use std::time::{Duration, Instant};

const IPC_WAIT: Duration = Duration::from_secs(15);
const POLL: Duration = Duration::from_millis(100);

struct UnitCleanup {
    bootstrap: LinuxSystemdUserBootstrap,
    unit_name: String,
}

impl Drop for UnitCleanup {
    fn drop(&mut self) {
        let _ = self.bootstrap.remove_runtime_manager(&self.unit_name);
    }
}

#[test]
fn systemd_user_bootstrap_recovers_runtime_manager_and_uds() {
    let runtime_manager = PathBuf::from(env!("CARGO_BIN_EXE_hermes-runtime-manager"));
    assert!(runtime_manager.is_absolute());
    assert!(runtime_manager.is_file());

    let bootstrap = LinuxSystemdUserBootstrap::discover()
        .expect("discover real systemd --user bootstrap");
    let unit_name = format!("hermes-runtime-manager-ci-{}.service", std::process::id());
    let cleanup = UnitCleanup {
        bootstrap: bootstrap.clone(),
        unit_name: unit_name.clone(),
    };

    let unit_path = bootstrap
        .register_runtime_manager(&unit_name, &runtime_manager)
        .expect("register and start Runtime Manager systemd --user unit");
    assert!(unit_path.is_file());

    let unit = bootstrap
        .unit_contents(&unit_name)
        .expect("read registered unit");
    assert!(unit.contains("Restart=on-failure"));
    assert!(unit.contains("RestartSec=1s"));
    assert!(unit.contains("NoNewPrivileges=true"));
    assert!(unit.contains("UMask=0077"));
    assert!(unit.contains("serve-read-only"));
    assert!(!unit.contains("python"));
    assert!(!unit.contains("connector"));

    let initial = bootstrap.status(&unit_name).expect("initial systemd status");
    assert!(initial.ready());
    let endpoint = DefaultInstallLayout::discover()
        .expect("Linux install layout")
        .state_root()
        .expect("Linux state root")
        .join("rm.sock");
    wait_for_snapshot(&endpoint, "systemd-initial");

    let killed_pid = bootstrap
        .kill_main_for_recovery_proof(&unit_name)
        .expect("SIGKILL systemd MainPID");
    assert_eq!(killed_pid, initial.main_pid);

    let recovered = bootstrap
        .wait_ready(&unit_name, Some(killed_pid))
        .expect("Restart=on-failure should recover a fresh Runtime Manager PID");
    assert_ne!(recovered.main_pid, killed_pid);
    wait_for_snapshot(&endpoint, "systemd-recovered");

    let restarted = bootstrap
        .restart(&unit_name)
        .expect("systemctl --user restart should produce another fresh Runtime Manager PID");
    assert_ne!(restarted.main_pid, recovered.main_pid);
    wait_for_snapshot(&endpoint, "systemd-restarted");

    bootstrap
        .remove_runtime_manager(&unit_name)
        .expect("disable and remove Runtime Manager user unit");
    assert!(!unit_path.exists());
    std::mem::forget(cleanup);
}

fn wait_for_snapshot(endpoint: &std::path::Path, request_id: &str) {
    let deadline = Instant::now() + IPC_WAIT;
    let mut last_error = None;
    while Instant::now() < deadline {
        match request_read_only(endpoint, request_id, ManagerRequestV1::Status) {
            Ok(ManagerResponseV1::Snapshot(snapshot)) => {
                assert_eq!(snapshot.schema_version, 1);
                return;
            }
            Ok(other) => last_error = Some(format!("unexpected response: {other:?}")),
            Err(error) => last_error = Some(error.to_string()),
        }
        thread::sleep(POLL);
    }
    panic!(
        "Runtime Manager UDS did not return Status after systemd transition: endpoint={} last_error={:?}",
        endpoint.display(),
        last_error
    );
}
