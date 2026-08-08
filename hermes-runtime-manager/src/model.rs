use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;
use thiserror::Error;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleState {
    Absent,
    Installing,
    Stopped,
    Starting,
    Ready,
    Updating,
    RollingBack,
    Degraded,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlatformKind {
    Macos,
    Windows,
    Linux,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolchainArtifactV1 {
    pub path: PathBuf,
    pub sha256: String,
    pub version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ToolchainManifestV1 {
    pub schema_version: u8,
    pub platform: PlatformKind,
    pub architecture: String,
    pub python: ToolchainArtifactV1,
    pub uv: ToolchainArtifactV1,
    pub offline_only: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManagedReleaseManifestV1 {
    pub schema_version: u8,
    pub release_id: String,
    pub release_sha256: String,
    pub desktop_min_version: String,
    pub runtime_contract: String,
    pub toolchain_sha256: String,
    pub components: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProcessEvidence {
    pub pid: u32,
    pub executable: PathBuf,
    pub started_at_unix_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ComponentHealth {
    pub name: String,
    pub ready: bool,
    pub detail: String,
    pub process: Option<ProcessEvidence>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManagerSnapshotV1 {
    pub schema_version: u8,
    pub state: LifecycleState,
    pub platform: PlatformKind,
    pub active_release: Option<String>,
    pub previous_release: Option<String>,
    pub runtime_generation: Option<String>,
    pub components: Vec<ComponentHealth>,
}

#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum TransitionError {
    #[error("illegal lifecycle transition: {from:?} -> {to:?}")]
    Illegal {
        from: LifecycleState,
        to: LifecycleState,
    },
}

impl LifecycleState {
    pub fn can_transition_to(self, next: Self) -> bool {
        use LifecycleState::*;
        matches!(
            (self, next),
            (Absent, Installing)
                | (Installing, Stopped)
                | (Installing, Failed)
                | (Stopped, Starting)
                | (Starting, Ready)
                | (Starting, Degraded)
                | (Starting, Failed)
                | (Ready, Updating)
                | (Ready, Stopped)
                | (Ready, Degraded)
                | (Updating, Ready)
                | (Updating, RollingBack)
                | (Updating, Failed)
                | (RollingBack, Ready)
                | (RollingBack, Failed)
                | (Degraded, Starting)
                | (Degraded, RollingBack)
                | (Degraded, Stopped)
                | (Failed, RollingBack)
                | (Failed, Stopped)
        )
    }

    pub fn transition(self, next: Self) -> Result<Self, TransitionError> {
        if self == next || self.can_transition_to(next) {
            Ok(next)
        } else {
            Err(TransitionError::Illegal {
                from: self,
                to: next,
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::LifecycleState;

    #[test]
    fn update_must_reach_ready_or_rollback_before_normal_operation() {
        assert!(LifecycleState::Ready
            .transition(LifecycleState::Updating)
            .is_ok());
        assert!(LifecycleState::Updating
            .transition(LifecycleState::RollingBack)
            .is_ok());
        assert!(LifecycleState::RollingBack
            .transition(LifecycleState::Ready)
            .is_ok());
        assert!(LifecycleState::Updating
            .transition(LifecycleState::Stopped)
            .is_err());
    }

    #[test]
    fn absent_runtime_cannot_claim_ready() {
        assert!(LifecycleState::Absent
            .transition(LifecycleState::Ready)
            .is_err());
    }
}
