use crate::ports::PortError;
use crate::update_coordinator::{
    UpdateConnectorLane, UpdateSafeWindowEvidenceV1, UpdateSafeWindowProbe,
};
use std::sync::Arc;

const MAX_OBSERVED_COUNT: u32 = 1_000_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostUpdateSafetySnapshotV1 {
    pub active_tasks: u32,
    pub pending_approvals: u32,
    pub pending_clarifications: u32,
    pub evidence_complete: bool,
}

pub trait HostUpdateSafetySource: Send + Sync {
    fn snapshot(&self) -> Result<HostUpdateSafetySnapshotV1, PortError>;
}

pub struct DrainingSafeWindowProbe {
    connector_lane: Arc<dyn UpdateConnectorLane>,
    host: Arc<dyn HostUpdateSafetySource>,
}

impl DrainingSafeWindowProbe {
    pub fn new(
        connector_lane: Arc<dyn UpdateConnectorLane>,
        host: Arc<dyn HostUpdateSafetySource>,
    ) -> Self {
        Self {
            connector_lane,
            host,
        }
    }

    fn restore_connector(&self) -> Result<(), PortError> {
        self.connector_lane.reconcile()
    }
}

impl UpdateSafeWindowProbe for DrainingSafeWindowProbe {
    fn inspect(&self) -> Result<UpdateSafeWindowEvidenceV1, PortError> {
        self.connector_lane.drain()?;
        let snapshot = match self.host.snapshot() {
            Ok(snapshot) => snapshot,
            Err(error) => {
                if self.restore_connector().is_err() {
                    return Err(PortError::Operation(
                        "Host safety evidence failed and Connector reconciliation also failed"
                            .to_owned(),
                    ));
                }
                return Err(error);
            }
        };

        if !snapshot.evidence_complete
            || snapshot.active_tasks > MAX_OBSERVED_COUNT
            || snapshot.pending_approvals > MAX_OBSERVED_COUNT
            || snapshot.pending_clarifications > MAX_OBSERVED_COUNT
        {
            self.restore_connector()?;
            return Err(PortError::Operation(
                "Host update-safety evidence is incomplete or out of bounds".to_owned(),
            ));
        }

        let evidence = UpdateSafeWindowEvidenceV1 {
            active_tasks: snapshot.active_tasks,
            pending_approvals: snapshot.pending_approvals,
            pending_clarifications: snapshot.pending_clarifications,
            connector_inflight_commands: 0,
        };
        if !evidence.safe_to_update() {
            self.restore_connector()?;
        }
        Ok(evidence)
    }
}
