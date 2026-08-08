#![cfg(windows)]

use hermes_runtime_manager::{
    run_registered_task_and_wait_for_fresh_completion, WindowsScheduledAction,
    WindowsTaskSchedulerBootstrap,
};
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

fn unique_task_name() -> String {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_nanos();
    format!("HermesRuntimeManager-CI-{}-{nonce}", std::process::id())
}

#[test]
fn current_user_logon_task_registers_runs_runtime_manager_and_cleans_up() {
    let scheduler = WindowsTaskSchedulerBootstrap::new();
    let task_name = unique_task_name();
    let runtime_manager = PathBuf::from(env!("CARGO_BIN_EXE_hermes-runtime-manager"));
    assert!(runtime_manager.is_absolute());
    assert!(runtime_manager.is_file());

    scheduler.delete_task(&task_name).expect("pre-clean task");
    let registration = scheduler
        .register_current_user_logon_task(
            &task_name,
            &runtime_manager,
            WindowsScheduledAction::VersionProbe,
        )
        .expect("register current-user Task Scheduler task");

    let result = (|| {
        assert_eq!(registration.task_name, task_name);
        assert!(registration.user_sid.starts_with("S-1-"));
        assert_eq!(registration.arguments, "version");
        assert_eq!(registration.executable, runtime_manager);

        let xml = scheduler
            .task_xml(&task_name)
            .expect("read task XML")
            .expect("registered task exists");
        assert!(xml.contains("<LogonTrigger>"));
        assert!(xml.contains(&registration.user_sid));
        assert!(xml.contains("<LogonType>InteractiveToken</LogonType>"));
        assert!(xml.contains("<RestartOnFailure>"));
        assert!(xml.contains("<Interval>PT1M</Interval>"));
        assert!(xml.contains("<Count>3</Count>"));
        assert!(xml.contains("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>"));
        assert!(xml.contains("version"));
        assert!(!xml.to_ascii_lowercase().contains("<password>"));
        assert!(!xml.to_ascii_lowercase().contains("highestavailable"));
        // Least-privilege semantics are verified by the registration adapter through
        // TaskDefinition Principal.RunLevel == TASK_RUNLEVEL_LUA. The service may omit
        // default-valued RunLevel from the serialized XML.

        let evidence = run_registered_task_and_wait_for_fresh_completion(&task_name)
            .expect("fresh scheduled Runtime Manager run must complete");
        assert!(evidence.last_run_time_after > evidence.last_run_time_before);
        assert_eq!(evidence.last_task_result, 0);
        Ok::<(), Box<dyn std::error::Error>>(())
    })();

    let cleanup = scheduler.delete_task(&task_name);
    if let Err(error) = result {
        cleanup.expect("cleanup after failure");
        panic!("Task Scheduler proof failed: {error}");
    }
    cleanup.expect("delete registered task");
    assert_eq!(scheduler.task_xml(&task_name).expect("post-delete lookup"), None);
    scheduler.delete_task(&task_name).expect("idempotent delete");
}
