use crate::manager::{ManagerError, RuntimeManager};
use crate::model::LifecycleState;
use crate::ports::{Clock, PortError};
use crate::release_control::ReleaseControlVerificationReportV1;
use crate::update_download::{ArtifactDownloadReceiptV1, ArtifactDownloadSpecV1};
use serde::{Deserialize, Serialize};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use thiserror::Error;

const JOURNAL_FILE: &str = "update-journal-v1.json";
const MAX_JOURNAL_BYTES: u64 = 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpdatePhaseV1 {
    Checking,
    Downloading,
    Verifying,
    Staging,
    WaitingSafeWindow,
    Draining,
    Activating,
    HealthGate,
    Committing,
    RollingBack,
    Completed,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpdateOutcomeStatusV1 {
    Updated,
    Deferred,
    RolledBack,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateSafeWindowEvidenceV1 {
    pub active_tasks: u32,
    pub pending_approvals: u32,
    pub pending_clarifications: u32,
    pub connector_inflight_commands: u32,
}

impl UpdateSafeWindowEvidenceV1 {
    pub fn safe_to_update(&self) -> bool {
        self.active_tasks == 0
            && self.pending_approvals == 0
            && self.pending_clarifications == 0
            && self.connector_inflight_commands == 0
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StagedReleaseV1 {
    pub release_id: String,
    pub release_generation: u64,
    pub release_path: PathBuf,
    pub content_verified: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateHealthEvidenceV1 {
    pub agent_ready: bool,
    pub cloud_connected: bool,
    pub live_session_ok: bool,
    pub components_ready: bool,
}

impl UpdateHealthEvidenceV1 {
    pub fn healthy(&self) -> bool {
        self.agent_ready && self.cloud_connected && self.live_session_ok && self.components_ready
    }
}

#[derive(Debug, Clone)]
pub struct UpdatePlanV1 {
    pub transaction_id: String,
    pub target_release_id: String,
    pub target_release_generation: u64,
    pub release_control: ReleaseControlVerificationReportV1,
    pub download_spec: ArtifactDownloadSpecV1,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UpdateTransactionV1 {
    pub schema_version: u8,
    pub transaction_id: String,
    pub target_release_id: String,
    pub target_release_generation: u64,
    pub previous_release_id: String,
    pub previous_runtime_generation: Option<String>,
    pub phase: UpdatePhaseV1,
    pub downloaded_artifact_path: Option<PathBuf>,
    pub staged_release_path: Option<PathBuf>,
    pub rollback_performed: bool,
    pub failure_code: Option<String>,
    pub updated_at_unix_ms: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpdateOutcomeV1 {
    pub status: UpdateOutcomeStatusV1,
    pub transaction: UpdateTransactionV1,
    pub health: Option<UpdateHealthEvidenceV1>,
}

pub trait UpdateArtifactFetcher: Send + Sync {
    fn fetch(&self, spec: &ArtifactDownloadSpecV1) -> Result<ArtifactDownloadReceiptV1, PortError>;
}

pub trait UpdateReleaseStager: Send + Sync {
    fn stage(
        &self,
        receipt: &ArtifactDownloadReceiptV1,
        release_id: &str,
        release_generation: u64,
    ) -> Result<StagedReleaseV1, PortError>;
}

pub trait UpdateSafeWindowProbe: Send + Sync {
    fn inspect(&self) -> Result<UpdateSafeWindowEvidenceV1, PortError>;
}

pub trait UpdateConnectorLane: Send + Sync {
    fn drain(&self) -> Result<(), PortError>;
    fn reconcile(&self) -> Result<(), PortError>;
}

pub trait UpdateReleaseActivator: Send + Sync {
    fn activate(&self, staged: &StagedReleaseV1) -> Result<(), PortError>;
    fn rollback(&self, release_id: &str) -> Result<(), PortError>;
}

pub trait UpdateHealthGate: Send + Sync {
    fn verify(&self, release_id: &str) -> Result<UpdateHealthEvidenceV1, PortError>;
}

pub trait UpdateRollbackPolicy: Send + Sync {
    fn rollback_allowed(&self, release_id: &str) -> Result<bool, PortError>;
}

#[derive(Debug, Error)]
pub enum UpdateCoordinatorError {
    #[error("update plan is invalid: {0}")]
    InvalidPlan(String),
    #[error("update transaction journal is invalid: {0}")]
    InvalidJournal(String),
    #[error("update recovery state is ambiguous: {0}")]
    AmbiguousRecovery(String),
    #[error("automatic rollback target is unavailable or blocked")]
    RollbackUnavailable,
    #[error("update health gate failed and rollback also failed")]
    RollbackFailed,
    #[error(transparent)]
    Manager(#[from] ManagerError),
    #[error(transparent)]
    Port(#[from] PortError),
    #[error("update journal I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("update journal JSON failed: {0}")]
    Json(#[from] serde_json::Error),
}

pub struct UpdateCoordinator {
    manager: Arc<RuntimeManager>,
    fetcher: Arc<dyn UpdateArtifactFetcher>,
    stager: Arc<dyn UpdateReleaseStager>,
    safe_window: Arc<dyn UpdateSafeWindowProbe>,
    connector_lane: Arc<dyn UpdateConnectorLane>,
    activator: Arc<dyn UpdateReleaseActivator>,
    health_gate: Arc<dyn UpdateHealthGate>,
    rollback_policy: Arc<dyn UpdateRollbackPolicy>,
    clock: Arc<dyn Clock>,
    journal_root: PathBuf,
}

impl UpdateCoordinator {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        manager: Arc<RuntimeManager>,
        fetcher: Arc<dyn UpdateArtifactFetcher>,
        stager: Arc<dyn UpdateReleaseStager>,
        safe_window: Arc<dyn UpdateSafeWindowProbe>,
        connector_lane: Arc<dyn UpdateConnectorLane>,
        activator: Arc<dyn UpdateReleaseActivator>,
        health_gate: Arc<dyn UpdateHealthGate>,
        rollback_policy: Arc<dyn UpdateRollbackPolicy>,
        clock: Arc<dyn Clock>,
        journal_root: PathBuf,
    ) -> Result<Self, UpdateCoordinatorError> {
        prepare_private_journal_root(&journal_root)?;
        Ok(Self {
            manager,
            fetcher,
            stager,
            safe_window,
            connector_lane,
            activator,
            health_gate,
            rollback_policy,
            clock,
            journal_root,
        })
    }

    pub fn execute(&self, plan: &UpdatePlanV1) -> Result<UpdateOutcomeV1, UpdateCoordinatorError> {
        self.validate_plan(plan)?;
        if self.journal_path().exists() || self.journal_path().is_symlink() {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "pending update transaction already exists; recover it first".to_owned(),
            ));
        }
        let snapshot = self.manager.snapshot()?;
        if snapshot.state != LifecycleState::Ready {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "Runtime Manager must be Ready before a new update".to_owned(),
            ));
        }
        let previous_release_id = snapshot.active_release.ok_or_else(|| {
            UpdateCoordinatorError::InvalidPlan("active release is missing".to_owned())
        })?;
        if previous_release_id == plan.target_release_id {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "target release is already active".to_owned(),
            ));
        }
        let mut transaction = UpdateTransactionV1 {
            schema_version: 1,
            transaction_id: plan.transaction_id.clone(),
            target_release_id: plan.target_release_id.clone(),
            target_release_generation: plan.target_release_generation,
            previous_release_id,
            previous_runtime_generation: snapshot.runtime_generation,
            phase: UpdatePhaseV1::Checking,
            downloaded_artifact_path: None,
            staged_release_path: None,
            rollback_performed: false,
            failure_code: None,
            updated_at_unix_ms: self.clock.unix_ms(),
        };
        self.persist(&transaction)?;
        self.run(plan, &mut transaction)
    }

    pub fn recover(&self, plan: &UpdatePlanV1) -> Result<UpdateOutcomeV1, UpdateCoordinatorError> {
        self.validate_plan(plan)?;
        let mut transaction = self.load_journal()?;
        self.validate_transaction(plan, &transaction)?;
        let snapshot = self.manager.snapshot()?;

        match transaction.phase {
            UpdatePhaseV1::Completed => Ok(UpdateOutcomeV1 {
                status: if transaction.rollback_performed {
                    UpdateOutcomeStatusV1::RolledBack
                } else {
                    UpdateOutcomeStatusV1::Updated
                },
                transaction,
                health: None,
            }),
            UpdatePhaseV1::Failed => Err(UpdateCoordinatorError::AmbiguousRecovery(
                transaction
                    .failure_code
                    .clone()
                    .unwrap_or_else(|| "previous update transaction failed".to_owned()),
            )),
            UpdatePhaseV1::Activating
            | UpdatePhaseV1::HealthGate
            | UpdatePhaseV1::Committing => {
                if snapshot.active_release.as_deref() == Some(plan.target_release_id.as_str()) {
                    if snapshot.state != LifecycleState::Updating {
                        return Err(UpdateCoordinatorError::AmbiguousRecovery(
                            "target is active but manager is not Updating".to_owned(),
                        ));
                    }
                    self.set_phase(&mut transaction, UpdatePhaseV1::HealthGate)?;
                    let health = self.health_gate.verify(&plan.target_release_id)?;
                    if health.healthy() {
                        return self.commit(&mut transaction, health);
                    }
                    self.rollback(&mut transaction, Some(health))
                } else if snapshot.active_release.as_deref()
                    == Some(transaction.previous_release_id.as_str())
                {
                    if snapshot.state == LifecycleState::Updating {
                        self.manager.transition(LifecycleState::Ready)?;
                    }
                    transaction.rollback_performed = true;
                    self.set_phase(&mut transaction, UpdatePhaseV1::Completed)?;
                    Ok(UpdateOutcomeV1 {
                        status: UpdateOutcomeStatusV1::RolledBack,
                        transaction,
                        health: None,
                    })
                } else {
                    Err(UpdateCoordinatorError::AmbiguousRecovery(
                        "active release matches neither target nor previous release".to_owned(),
                    ))
                }
            }
            UpdatePhaseV1::RollingBack => {
                if snapshot.active_release.as_deref()
                    != Some(transaction.previous_release_id.as_str())
                {
                    return Err(UpdateCoordinatorError::AmbiguousRecovery(
                        "rollback journal exists but previous release is not active".to_owned(),
                    ));
                }
                let health = self.health_gate.verify(&transaction.previous_release_id)?;
                if !health.healthy() {
                    self.mark_failed(&mut transaction, "rollback_health_failed")?;
                    let _ = self.manager.transition(LifecycleState::Failed);
                    return Err(UpdateCoordinatorError::RollbackFailed);
                }
                self.connector_lane.reconcile()?;
                if snapshot.state == LifecycleState::RollingBack {
                    self.manager.transition(LifecycleState::Ready)?;
                }
                transaction.rollback_performed = true;
                self.set_phase(&mut transaction, UpdatePhaseV1::Completed)?;
                Ok(UpdateOutcomeV1 {
                    status: UpdateOutcomeStatusV1::RolledBack,
                    transaction,
                    health: Some(health),
                })
            }
            _ => {
                if snapshot.active_release.as_deref()
                    != Some(transaction.previous_release_id.as_str())
                {
                    return Err(UpdateCoordinatorError::AmbiguousRecovery(
                        "pre-activation recovery requires previous release to remain active"
                            .to_owned(),
                    ));
                }
                if snapshot.state == LifecycleState::Updating {
                    self.manager.transition(LifecycleState::Ready)?;
                } else if snapshot.state != LifecycleState::Ready {
                    return Err(UpdateCoordinatorError::AmbiguousRecovery(
                        "pre-activation recovery requires Ready or Updating manager state"
                            .to_owned(),
                    ));
                }
                self.remove_journal()?;
                self.execute(plan)
            }
        }
    }

    pub fn load_journal(&self) -> Result<UpdateTransactionV1, UpdateCoordinatorError> {
        let path = self.journal_path();
        if path.is_symlink() || !path.is_file() {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal is missing, non-regular, or symlinked".to_owned(),
            ));
        }
        let metadata = fs::metadata(&path)?;
        if metadata.len() == 0 || metadata.len() > MAX_JOURNAL_BYTES {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal size is outside the bounded limit".to_owned(),
            ));
        }
        let bytes = fs::read(&path)?;
        let transaction: UpdateTransactionV1 = serde_json::from_slice(&bytes)?;
        if transaction.schema_version != 1 || !safe_identifier(&transaction.transaction_id, 128) {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal schema or transaction identity is invalid".to_owned(),
            ));
        }
        Ok(transaction)
    }

    fn run(
        &self,
        plan: &UpdatePlanV1,
        transaction: &mut UpdateTransactionV1,
    ) -> Result<UpdateOutcomeV1, UpdateCoordinatorError> {
        self.manager.transition(LifecycleState::Updating)?;

        self.set_phase(transaction, UpdatePhaseV1::Downloading)?;
        let receipt = self.fetcher.fetch(&plan.download_spec)?;
        self.validate_download_receipt(plan, &receipt)?;
        transaction.downloaded_artifact_path = Some(receipt.final_path.clone());
        self.persist(transaction)?;

        self.set_phase(transaction, UpdatePhaseV1::Verifying)?;
        if !receipt.content_verified {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "download receipt is not content-verified".to_owned(),
            ));
        }

        self.set_phase(transaction, UpdatePhaseV1::Staging)?;
        let staged = self.stager.stage(
            &receipt,
            &plan.target_release_id,
            plan.target_release_generation,
        )?;
        self.validate_staged_release(plan, &staged)?;
        transaction.staged_release_path = Some(staged.release_path.clone());
        self.persist(transaction)?;

        self.set_phase(transaction, UpdatePhaseV1::WaitingSafeWindow)?;
        let safety = self.safe_window.inspect()?;
        if !safety.safe_to_update() {
            self.manager.transition(LifecycleState::Ready)?;
            return Ok(UpdateOutcomeV1 {
                status: UpdateOutcomeStatusV1::Deferred,
                transaction: transaction.clone(),
                health: None,
            });
        }

        self.set_phase(transaction, UpdatePhaseV1::Draining)?;
        self.connector_lane.drain()?;

        self.set_phase(transaction, UpdatePhaseV1::Activating)?;
        self.activator.activate(&staged)?;
        self.manager.record_activation(
            &plan.target_release_id,
            plan.target_release_generation.to_string(),
        )?;

        self.set_phase(transaction, UpdatePhaseV1::HealthGate)?;
        let health = self.health_gate.verify(&plan.target_release_id)?;
        if health.healthy() {
            self.commit(transaction, health)
        } else {
            self.rollback(transaction, Some(health))
        }
    }

    fn commit(
        &self,
        transaction: &mut UpdateTransactionV1,
        health: UpdateHealthEvidenceV1,
    ) -> Result<UpdateOutcomeV1, UpdateCoordinatorError> {
        self.set_phase(transaction, UpdatePhaseV1::Committing)?;
        self.connector_lane.reconcile()?;
        if self.manager.state()? == LifecycleState::Updating {
            self.manager.transition(LifecycleState::Ready)?;
        }
        self.set_phase(transaction, UpdatePhaseV1::Completed)?;
        Ok(UpdateOutcomeV1 {
            status: UpdateOutcomeStatusV1::Updated,
            transaction: transaction.clone(),
            health: Some(health),
        })
    }

    fn rollback(
        &self,
        transaction: &mut UpdateTransactionV1,
        failed_health: Option<UpdateHealthEvidenceV1>,
    ) -> Result<UpdateOutcomeV1, UpdateCoordinatorError> {
        if !self
            .rollback_policy
            .rollback_allowed(&transaction.previous_release_id)?
        {
            self.mark_failed(transaction, "rollback_target_blocked")?;
            let _ = self.manager.transition(LifecycleState::Failed);
            return Err(UpdateCoordinatorError::RollbackUnavailable);
        }
        if self.manager.state()? == LifecycleState::Updating {
            self.manager.transition(LifecycleState::RollingBack)?;
        }
        self.set_phase(transaction, UpdatePhaseV1::RollingBack)?;
        self.activator.rollback(&transaction.previous_release_id)?;
        self.manager.record_rollback(
            &transaction.previous_release_id,
            &transaction.target_release_id,
            transaction.previous_runtime_generation.clone(),
        )?;
        let rollback_health = self.health_gate.verify(&transaction.previous_release_id)?;
        if !rollback_health.healthy() {
            self.mark_failed(transaction, "rollback_health_failed")?;
            let _ = self.manager.transition(LifecycleState::Failed);
            return Err(UpdateCoordinatorError::RollbackFailed);
        }
        self.connector_lane.reconcile()?;
        if self.manager.state()? == LifecycleState::RollingBack {
            self.manager.transition(LifecycleState::Ready)?;
        }
        transaction.rollback_performed = true;
        self.set_phase(transaction, UpdatePhaseV1::Completed)?;
        Ok(UpdateOutcomeV1 {
            status: UpdateOutcomeStatusV1::RolledBack,
            transaction: transaction.clone(),
            health: failed_health.or(Some(rollback_health)),
        })
    }

    fn validate_plan(&self, plan: &UpdatePlanV1) -> Result<(), UpdateCoordinatorError> {
        if !safe_identifier(&plan.transaction_id, 128)
            || !safe_identifier(&plan.target_release_id, 160)
            || plan.target_release_generation == 0
        {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "transaction/release identity is invalid".to_owned(),
            ));
        }
        if !plan.release_control.eligible || !plan.release_control.signatures_verified {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "release control must already be signature-verified and eligible".to_owned(),
            ));
        }
        if plan.release_control.release_id != plan.target_release_id
            || plan.release_control.release_generation != plan.target_release_generation
            || plan.download_spec.release_id != plan.target_release_id
            || plan.download_spec.release_generation != plan.target_release_generation
        {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "release control/download identity does not match update target".to_owned(),
            ));
        }
        Ok(())
    }

    fn validate_transaction(
        &self,
        plan: &UpdatePlanV1,
        transaction: &UpdateTransactionV1,
    ) -> Result<(), UpdateCoordinatorError> {
        if transaction.transaction_id != plan.transaction_id
            || transaction.target_release_id != plan.target_release_id
            || transaction.target_release_generation != plan.target_release_generation
        {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal does not match requested update plan".to_owned(),
            ));
        }
        Ok(())
    }

    fn validate_download_receipt(
        &self,
        plan: &UpdatePlanV1,
        receipt: &ArtifactDownloadReceiptV1,
    ) -> Result<(), UpdateCoordinatorError> {
        if receipt.release_id != plan.target_release_id
            || receipt.release_generation != plan.target_release_generation
            || receipt.object_key != plan.download_spec.object_key
            || receipt.sha256 != plan.download_spec.sha256
            || receipt.size_bytes != plan.download_spec.size_bytes
            || !receipt.content_verified
        {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "download receipt does not match signed update target".to_owned(),
            ));
        }
        Ok(())
    }

    fn validate_staged_release(
        &self,
        plan: &UpdatePlanV1,
        staged: &StagedReleaseV1,
    ) -> Result<(), UpdateCoordinatorError> {
        if staged.release_id != plan.target_release_id
            || staged.release_generation != plan.target_release_generation
            || !staged.content_verified
            || !staged.release_path.is_absolute()
            || staged.release_path.is_symlink()
        {
            return Err(UpdateCoordinatorError::InvalidPlan(
                "staged release evidence is invalid".to_owned(),
            ));
        }
        Ok(())
    }

    fn set_phase(
        &self,
        transaction: &mut UpdateTransactionV1,
        phase: UpdatePhaseV1,
    ) -> Result<(), UpdateCoordinatorError> {
        transaction.phase = phase;
        transaction.updated_at_unix_ms = self.clock.unix_ms();
        self.persist(transaction)
    }

    fn mark_failed(
        &self,
        transaction: &mut UpdateTransactionV1,
        code: &str,
    ) -> Result<(), UpdateCoordinatorError> {
        transaction.failure_code = Some(code.to_owned());
        self.set_phase(transaction, UpdatePhaseV1::Failed)
    }

    fn persist(&self, transaction: &UpdateTransactionV1) -> Result<(), UpdateCoordinatorError> {
        prepare_private_journal_root(&self.journal_root)?;
        let path = self.journal_path();
        if path.is_symlink() {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal path is symlinked".to_owned(),
            ));
        }
        let bytes = serde_json::to_vec(transaction)?;
        if bytes.is_empty() || bytes.len() as u64 > MAX_JOURNAL_BYTES {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal serialization exceeded bounded size".to_owned(),
            ));
        }
        let temporary = self.journal_root.join(format!(".{JOURNAL_FILE}.new"));
        if temporary.exists() || temporary.is_symlink() {
            fs::remove_file(&temporary)?;
        }
        let mut options = OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(&temporary)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, &path)?;
        Ok(())
    }

    fn remove_journal(&self) -> Result<(), UpdateCoordinatorError> {
        let path = self.journal_path();
        if path.is_symlink() {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal path is symlinked".to_owned(),
            ));
        }
        if path.exists() {
            fs::remove_file(path)?;
        }
        Ok(())
    }

    fn journal_path(&self) -> PathBuf {
        self.journal_root.join(JOURNAL_FILE)
    }
}

fn prepare_private_journal_root(path: &Path) -> Result<(), UpdateCoordinatorError> {
    if !path.is_absolute() || path.is_symlink() {
        return Err(UpdateCoordinatorError::InvalidJournal(
            "journal root must be absolute and non-symlinked".to_owned(),
        ));
    }
    if path.exists() {
        if !path.is_dir() {
            return Err(UpdateCoordinatorError::InvalidJournal(
                "journal root is not a directory".to_owned(),
            ));
        }
    } else {
        fs::create_dir_all(path)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn safe_identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+' | b':'))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{ComponentHealth, PlatformKind, ProcessEvidence};
    use crate::ports::{InstallLayout, ServiceManager};
    use crate::release_control::ReleaseChannelV1;
    use crate::update_download::ReleaseArtifactKindV1;
    use std::collections::BTreeMap;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Mutex;

    static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

    struct FakeServiceManager;

    impl ServiceManager for FakeServiceManager {
        fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
            Ok(())
        }
        fn start_host(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
            Ok(())
        }
        fn stop_host(&self) -> Result<(), PortError> {
            Ok(())
        }
        fn start_connector(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
            Ok(())
        }
        fn stop_connector(&self) -> Result<(), PortError> {
            Ok(())
        }
        fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
            Ok(Vec::new())
        }
    }

    struct FakeLayout {
        root: PathBuf,
    }

    impl InstallLayout for FakeLayout {
        fn platform(&self) -> PlatformKind {
            PlatformKind::Linux
        }
        fn application_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.clone())
        }
        fn releases_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("releases"))
        }
        fn toolchains_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("toolchains"))
        }
        fn state_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("state"))
        }
        fn logs_root(&self) -> Result<PathBuf, PortError> {
            Ok(self.root.join("logs"))
        }
    }

    struct FakeFetcher {
        root: PathBuf,
    }

    impl UpdateArtifactFetcher for FakeFetcher {
        fn fetch(&self, spec: &ArtifactDownloadSpecV1) -> Result<ArtifactDownloadReceiptV1, PortError> {
            Ok(ArtifactDownloadReceiptV1 {
                schema_version: 1,
                release_id: spec.release_id.clone(),
                release_generation: spec.release_generation,
                target: spec.target.clone(),
                kind: spec.kind,
                object_key: spec.object_key.clone(),
                sha256: spec.sha256.clone(),
                size_bytes: spec.size_bytes,
                final_path: self.root.join("cache").join(&spec.file_name),
                resumed_from_bytes: 0,
                downloaded_bytes: spec.size_bytes,
                reused_existing: false,
                content_verified: true,
            })
        }
    }

    struct FakeStager {
        root: PathBuf,
    }

    impl UpdateReleaseStager for FakeStager {
        fn stage(
            &self,
            _receipt: &ArtifactDownloadReceiptV1,
            release_id: &str,
            release_generation: u64,
        ) -> Result<StagedReleaseV1, PortError> {
            Ok(StagedReleaseV1 {
                release_id: release_id.to_owned(),
                release_generation,
                release_path: self.root.join("releases").join(release_id),
                content_verified: true,
            })
        }
    }

    struct FakeSafeWindow {
        evidence: Mutex<UpdateSafeWindowEvidenceV1>,
    }

    impl UpdateSafeWindowProbe for FakeSafeWindow {
        fn inspect(&self) -> Result<UpdateSafeWindowEvidenceV1, PortError> {
            Ok(self.evidence.lock().unwrap().clone())
        }
    }

    #[derive(Default)]
    struct FakeLane {
        drains: Mutex<u32>,
        reconciles: Mutex<u32>,
    }

    impl UpdateConnectorLane for FakeLane {
        fn drain(&self) -> Result<(), PortError> {
            *self.drains.lock().unwrap() += 1;
            Ok(())
        }
        fn reconcile(&self) -> Result<(), PortError> {
            *self.reconciles.lock().unwrap() += 1;
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakeActivator {
        active: Mutex<Option<String>>,
        rollbacks: Mutex<Vec<String>>,
    }

    impl UpdateReleaseActivator for FakeActivator {
        fn activate(&self, staged: &StagedReleaseV1) -> Result<(), PortError> {
            *self.active.lock().unwrap() = Some(staged.release_id.clone());
            Ok(())
        }
        fn rollback(&self, release_id: &str) -> Result<(), PortError> {
            *self.active.lock().unwrap() = Some(release_id.to_owned());
            self.rollbacks.lock().unwrap().push(release_id.to_owned());
            Ok(())
        }
    }

    struct FakeHealthGate {
        target_healthy: bool,
    }

    impl UpdateHealthGate for FakeHealthGate {
        fn verify(&self, release_id: &str) -> Result<UpdateHealthEvidenceV1, PortError> {
            let healthy = if release_id.starts_with("1.0.1") {
                self.target_healthy
            } else {
                true
            };
            Ok(UpdateHealthEvidenceV1 {
                agent_ready: healthy,
                cloud_connected: healthy,
                live_session_ok: healthy,
                components_ready: healthy,
            })
        }
    }

    struct FakeRollbackPolicy {
        allowed: bool,
    }

    impl UpdateRollbackPolicy for FakeRollbackPolicy {
        fn rollback_allowed(&self, _release_id: &str) -> Result<bool, PortError> {
            Ok(self.allowed)
        }
    }

    struct TestClock(AtomicU64);

    impl Clock for TestClock {
        fn unix_ms(&self) -> u64 {
            self.0.fetch_add(1, Ordering::SeqCst)
        }
    }

    fn temp_root() -> PathBuf {
        let id = TEMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "hermes-update-coordinator-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn manager(root: &Path) -> Arc<RuntimeManager> {
        let manager = Arc::new(RuntimeManager::new(
            Arc::new(FakeServiceManager),
            Arc::new(FakeLayout {
                root: root.to_path_buf(),
            }),
        ));
        manager.transition(LifecycleState::Installing).unwrap();
        manager.transition(LifecycleState::Stopped).unwrap();
        manager.transition(LifecycleState::Starting).unwrap();
        manager.transition(LifecycleState::Ready).unwrap();
        manager.record_activation("1.0.0+20260801.1.g00000000", "100").unwrap();
        manager
    }

    fn plan() -> UpdatePlanV1 {
        let digest = "a".repeat(64);
        UpdatePlanV1 {
            transaction_id: "update-101".to_owned(),
            target_release_id: "1.0.1+20260808.1.g11111111".to_owned(),
            target_release_generation: 101,
            release_control: ReleaseControlVerificationReportV1 {
                schema_version: 1,
                product_version: "1.0.1".to_owned(),
                release_id: "1.0.1+20260808.1.g11111111".to_owned(),
                release_generation: 101,
                channel: ReleaseChannelV1::Stable,
                channel_generation: 10,
                block_generation: 2,
                effective_minimum_safe_generation: 100,
                rollback_authorized: false,
                decision: "forward_update".to_owned(),
                signatures_verified: true,
                eligible: true,
            },
            download_spec: ArtifactDownloadSpecV1 {
                schema_version: 1,
                release_id: "1.0.1+20260808.1.g11111111".to_owned(),
                release_generation: 101,
                target: "linux-x86_64".to_owned(),
                kind: ReleaseArtifactKindV1::ManagedReleasePayload,
                object_key: format!("artifacts/v1/sha256/aa/{digest}/runtime.tar.zst"),
                file_name: "runtime.tar.zst".to_owned(),
                sha256: digest,
                size_bytes: 4096,
                platform_signature: None,
            },
        }
    }

    fn coordinator(
        root: &Path,
        manager: Arc<RuntimeManager>,
        safe: bool,
        target_healthy: bool,
        rollback_allowed: bool,
    ) -> UpdateCoordinator {
        UpdateCoordinator::new(
            manager,
            Arc::new(FakeFetcher {
                root: root.to_path_buf(),
            }),
            Arc::new(FakeStager {
                root: root.to_path_buf(),
            }),
            Arc::new(FakeSafeWindow {
                evidence: Mutex::new(UpdateSafeWindowEvidenceV1 {
                    active_tasks: if safe { 0 } else { 1 },
                    pending_approvals: 0,
                    pending_clarifications: 0,
                    connector_inflight_commands: 0,
                }),
            }),
            Arc::new(FakeLane::default()),
            Arc::new(FakeActivator::default()),
            Arc::new(FakeHealthGate { target_healthy }),
            Arc::new(FakeRollbackPolicy {
                allowed: rollback_allowed,
            }),
            Arc::new(TestClock(AtomicU64::new(1_000))),
            root.join("state"),
        )
        .unwrap()
    }

    #[test]
    fn healthy_update_commits_only_after_safe_window_and_health_gate() {
        let root = temp_root();
        let manager = manager(&root);
        let coordinator = coordinator(&root, manager.clone(), true, true, true);

        let outcome = coordinator.execute(&plan()).unwrap();

        assert_eq!(outcome.status, UpdateOutcomeStatusV1::Updated);
        assert_eq!(outcome.transaction.phase, UpdatePhaseV1::Completed);
        assert!(!outcome.transaction.rollback_performed);
        assert!(outcome.health.unwrap().healthy());
        let snapshot = manager.snapshot().unwrap();
        assert_eq!(snapshot.state, LifecycleState::Ready);
        assert_eq!(
            snapshot.active_release.as_deref(),
            Some("1.0.1+20260808.1.g11111111")
        );
        assert_eq!(
            snapshot.previous_release.as_deref(),
            Some("1.0.0+20260801.1.g00000000")
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn busy_agent_defers_without_mutating_active_release() {
        let root = temp_root();
        let manager = manager(&root);
        let coordinator = coordinator(&root, manager.clone(), false, true, true);

        let outcome = coordinator.execute(&plan()).unwrap();

        assert_eq!(outcome.status, UpdateOutcomeStatusV1::Deferred);
        assert_eq!(outcome.transaction.phase, UpdatePhaseV1::WaitingSafeWindow);
        let snapshot = manager.snapshot().unwrap();
        assert_eq!(snapshot.state, LifecycleState::Ready);
        assert_eq!(
            snapshot.active_release.as_deref(),
            Some("1.0.0+20260801.1.g00000000")
        );
        assert_eq!(
            coordinator.load_journal().unwrap().phase,
            UpdatePhaseV1::WaitingSafeWindow
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn failed_target_health_rolls_back_to_previous_known_good_release() {
        let root = temp_root();
        let manager = manager(&root);
        let coordinator = coordinator(&root, manager.clone(), true, false, true);

        let outcome = coordinator.execute(&plan()).unwrap();

        assert_eq!(outcome.status, UpdateOutcomeStatusV1::RolledBack);
        assert!(outcome.transaction.rollback_performed);
        assert_eq!(outcome.transaction.phase, UpdatePhaseV1::Completed);
        let snapshot = manager.snapshot().unwrap();
        assert_eq!(snapshot.state, LifecycleState::Ready);
        assert_eq!(
            snapshot.active_release.as_deref(),
            Some("1.0.0+20260801.1.g00000000")
        );
        assert_eq!(
            snapshot.previous_release.as_deref(),
            Some("1.0.1+20260808.1.g11111111")
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn blocked_previous_release_prevents_automatic_rollback() {
        let root = temp_root();
        let manager = manager(&root);
        let coordinator = coordinator(&root, manager.clone(), true, false, false);

        let error = coordinator.execute(&plan()).unwrap_err();

        assert!(matches!(error, UpdateCoordinatorError::RollbackUnavailable));
        assert_eq!(manager.state().unwrap(), LifecycleState::Failed);
        assert_eq!(coordinator.load_journal().unwrap().phase, UpdatePhaseV1::Failed);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn recovery_after_activation_rechecks_health_and_commits() {
        let root = temp_root();
        let manager = manager(&root);
        let coordinator = coordinator(&root, manager.clone(), true, true, true);
        let update_plan = plan();
        manager.transition(LifecycleState::Updating).unwrap();
        manager
            .record_activation(
                &update_plan.target_release_id,
                update_plan.target_release_generation.to_string(),
            )
            .unwrap();
        let transaction = UpdateTransactionV1 {
            schema_version: 1,
            transaction_id: update_plan.transaction_id.clone(),
            target_release_id: update_plan.target_release_id.clone(),
            target_release_generation: update_plan.target_release_generation,
            previous_release_id: "1.0.0+20260801.1.g00000000".to_owned(),
            previous_runtime_generation: Some("100".to_owned()),
            phase: UpdatePhaseV1::Activating,
            downloaded_artifact_path: Some(root.join("cache/runtime.tar.zst")),
            staged_release_path: Some(root.join("releases").join(&update_plan.target_release_id)),
            rollback_performed: false,
            failure_code: None,
            updated_at_unix_ms: 1_001,
        };
        coordinator.persist(&transaction).unwrap();

        let outcome = coordinator.recover(&update_plan).unwrap();

        assert_eq!(outcome.status, UpdateOutcomeStatusV1::Updated);
        assert_eq!(manager.state().unwrap(), LifecycleState::Ready);
        assert_eq!(outcome.transaction.phase, UpdatePhaseV1::Completed);
        let _ = fs::remove_dir_all(root);
    }

    #[allow(dead_code)]
    fn _keep_imports(_map: BTreeMap<String, String>, _process: ProcessEvidence) {}
}
