use crate::model::ManagerSnapshotV1;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "method", content = "params", rename_all = "snake_case")]
pub enum ManagerRequestV1 {
    Status,
    Doctor,
    Start,
    Stop,
    CheckForUpdates,
    StageUpdate { release_id: String },
    Rollback,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "type", content = "payload", rename_all = "snake_case")]
pub enum ManagerResponseV1 {
    Snapshot(ManagerSnapshotV1),
    Accepted { operation_id: String },
    Doctor { passed: bool, checks: Vec<DoctorCheckV1> },
    Error { code: String, message: String },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DoctorCheckV1 {
    pub name: String,
    pub passed: bool,
    pub detail: String,
}
