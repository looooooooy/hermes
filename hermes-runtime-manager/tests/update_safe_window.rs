use hermes_runtime_manager::{
    DrainingSafeWindowProbe, HostUpdateSafetySnapshotV1, HostUpdateSafetySource,
    UpdateConnectorLane, UpdateSafeWindowProbe,
};
use hermes_runtime_manager::ports::PortError;
use std::sync::{Arc, Mutex};

struct StaticHost(Result<HostUpdateSafetySnapshotV1, &'static str>);

impl HostUpdateSafetySource for StaticHost {
    fn snapshot(&self) -> Result<HostUpdateSafetySnapshotV1, PortError> {
        match &self.0 {
            Ok(value) => Ok(value.clone()),
            Err(message) => Err(PortError::Operation((*message).to_owned())),
        }
    }
}

#[derive(Default)]
struct RecordingLane {
    calls: Mutex<Vec<&'static str>>,
    drained: Mutex<bool>,
    fail_reconcile: bool,
}

impl UpdateConnectorLane for RecordingLane {
    fn drain(&self) -> Result<(), PortError> {
        let mut drained = self.drained.lock().unwrap();
        if !*drained {
            self.calls.lock().unwrap().push("drain");
            *drained = true;
        }
        Ok(())
    }

    fn reconcile(&self) -> Result<(), PortError> {
        if self.fail_reconcile {
            return Err(PortError::Operation("reconcile failed".to_owned()));
        }
        let mut drained = self.drained.lock().unwrap();
        if *drained {
            self.calls.lock().unwrap().push("reconcile");
            *drained = false;
        }
        Ok(())
    }
}

#[test]
fn safe_snapshot_leaves_connector_drained_for_cutover() {
    let lane = Arc::new(RecordingLane::default());
    let host = Arc::new(StaticHost(Ok(HostUpdateSafetySnapshotV1 {
        active_tasks: 0,
        pending_approvals: 0,
        pending_clarifications: 0,
        evidence_complete: true,
    })));
    let probe = DrainingSafeWindowProbe::new(lane.clone(), host);

    let evidence = probe.inspect().unwrap();
    assert!(evidence.safe_to_update());
    assert_eq!(*lane.calls.lock().unwrap(), vec!["drain"]);
    assert!(*lane.drained.lock().unwrap());

    lane.drain().unwrap();
    assert_eq!(*lane.calls.lock().unwrap(), vec!["drain"]);
}

#[test]
fn pending_interaction_restores_connector_and_defers() {
    let lane = Arc::new(RecordingLane::default());
    let host = Arc::new(StaticHost(Ok(HostUpdateSafetySnapshotV1 {
        active_tasks: 0,
        pending_approvals: 1,
        pending_clarifications: 0,
        evidence_complete: true,
    })));
    let probe = DrainingSafeWindowProbe::new(lane.clone(), host);

    let evidence = probe.inspect().unwrap();
    assert!(!evidence.safe_to_update());
    assert_eq!(*lane.calls.lock().unwrap(), vec!["drain", "reconcile"]);
    assert!(!*lane.drained.lock().unwrap());
}

#[test]
fn active_task_restores_connector_and_defers() {
    let lane = Arc::new(RecordingLane::default());
    let host = Arc::new(StaticHost(Ok(HostUpdateSafetySnapshotV1 {
        active_tasks: 2,
        pending_approvals: 0,
        pending_clarifications: 0,
        evidence_complete: true,
    })));
    let probe = DrainingSafeWindowProbe::new(lane.clone(), host);

    let evidence = probe.inspect().unwrap();
    assert_eq!(evidence.active_tasks, 2);
    assert_eq!(*lane.calls.lock().unwrap(), vec!["drain", "reconcile"]);
}

#[test]
fn missing_host_evidence_restores_connector_and_fails_closed() {
    let lane = Arc::new(RecordingLane::default());
    let host = Arc::new(StaticHost(Ok(HostUpdateSafetySnapshotV1 {
        active_tasks: 0,
        pending_approvals: 0,
        pending_clarifications: 0,
        evidence_complete: false,
    })));
    let probe = DrainingSafeWindowProbe::new(lane.clone(), host);

    let error = probe.inspect().unwrap_err();
    assert!(error.to_string().contains("incomplete"));
    assert_eq!(*lane.calls.lock().unwrap(), vec!["drain", "reconcile"]);
}

#[test]
fn host_read_error_restores_connector_before_returning_error() {
    let lane = Arc::new(RecordingLane::default());
    let host = Arc::new(StaticHost(Err("host unavailable")));
    let probe = DrainingSafeWindowProbe::new(lane.clone(), host);

    let error = probe.inspect().unwrap_err();
    assert!(error.to_string().contains("host unavailable"));
    assert_eq!(*lane.calls.lock().unwrap(), vec!["drain", "reconcile"]);
}

#[test]
fn reconciliation_failure_is_reported_as_fail_closed() {
    let lane = Arc::new(RecordingLane {
        fail_reconcile: true,
        ..RecordingLane::default()
    });
    let host = Arc::new(StaticHost(Err("host unavailable")));
    let probe = DrainingSafeWindowProbe::new(lane, host);

    let error = probe.inspect().unwrap_err();
    assert!(error.to_string().contains("reconciliation also failed"));
}
