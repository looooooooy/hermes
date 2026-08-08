use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::Path;
use thiserror::Error;
use time::{format_description::well_known::Rfc3339, Duration, OffsetDateTime};

const PRODUCT: &str = "hermes-desktop";
const SIGNATURE_ALGORITHM: &str = "ed25519";
const MAX_CONTROL_FILE_BYTES: u64 = 1024 * 1024;
const MAX_TRUST_STORE_BYTES: u64 = 128 * 1024;
const MAX_RELEASE_ARTIFACT_BYTES: u64 = 8 * 1024 * 1024 * 1024;
const ALLOWED_TARGETS: [&str; 5] = [
    "macos-aarch64",
    "macos-x86_64",
    "windows-x86_64",
    "linux-x86_64",
    "linux-aarch64",
];
const REQUIRED_COMPONENTS: [&str; 7] = [
    "desktop",
    "runtime_manager",
    "private_python",
    "uv",
    "core",
    "plugin",
    "connector",
];
const REQUIRED_CONTRACTS: [&str; 4] = [
    "runtime",
    "host_spi",
    "local_protocol",
    "cloud_protocol",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ReleaseChannelV1 {
    Canary,
    Beta,
    Stable,
    Enterprise,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseSourceV1 {
    pub repository: String,
    pub git_commit: String,
    pub workflow_run_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseArtifactV1 {
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub platform_signature: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProductTargetV1 {
    pub minimum_os: String,
    pub installer: ReleaseArtifactV1,
    pub bootstrap_payload: ReleaseArtifactV1,
    pub managed_release_payload: ReleaseArtifactV1,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseSecurityPolicyV1 {
    pub security_critical: bool,
    pub minimum_safe_release_generation: u64,
    pub mandatory_after: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProductReleaseManifestV1 {
    pub schema_version: u8,
    pub product: String,
    pub product_version: String,
    pub release_id: String,
    pub release_generation: u64,
    pub published_at: String,
    pub source: ReleaseSourceV1,
    pub components: BTreeMap<String, String>,
    pub contracts: BTreeMap<String, u32>,
    pub targets: BTreeMap<String, ProductTargetV1>,
    pub security: ReleaseSecurityPolicyV1,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RollbackAuthorizationV1 {
    pub from_release_id: String,
    pub from_release_generation: u64,
    pub to_release_id: String,
    pub to_release_generation: u64,
    pub reason_code: String,
    pub expires_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChannelManifestV1 {
    pub schema_version: u8,
    pub channel: ReleaseChannelV1,
    pub channel_generation: u64,
    pub release_id: String,
    pub release_generation: u64,
    pub published_at: String,
    pub minimum_safe_release_generation: u64,
    pub mandatory_after: Option<String>,
    pub rollback_authorization: Option<RollbackAuthorizationV1>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlockedReleaseV1 {
    pub release_id: String,
    pub release_generation: u64,
    pub reason_code: String,
    pub blocked_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BlockManifestV1 {
    pub schema_version: u8,
    pub block_generation: u64,
    pub published_at: String,
    pub minimum_safe_release_generation: u64,
    pub blocked_releases: Vec<BlockedReleaseV1>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignedReleaseEnvelopeV1<T> {
    pub schema_version: u8,
    pub key_id: String,
    pub signature_algorithm: String,
    pub signed_at: String,
    pub payload: T,
    pub signature: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseTrustStoreV1 {
    pub schema_version: u8,
    pub keys: Vec<ReleaseTrustKeyV1>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseTrustKeyV1 {
    pub key_id: String,
    pub signature_algorithm: String,
    pub public_key: String,
    pub not_before: String,
    pub not_after: String,
    pub revoked: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseControlObservedStateV1 {
    pub schema_version: u8,
    pub active_release_id: Option<String>,
    pub active_release_generation: u64,
    pub highest_release_generation: u64,
    pub highest_channel_generation: u64,
    pub highest_block_generation: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReleaseControlVerificationReportV1 {
    pub schema_version: u8,
    pub product_version: String,
    pub release_id: String,
    pub release_generation: u64,
    pub channel: ReleaseChannelV1,
    pub channel_generation: u64,
    pub block_generation: u64,
    pub effective_minimum_safe_generation: u64,
    pub rollback_authorized: bool,
    pub decision: String,
    pub signatures_verified: bool,
    pub eligible: bool,
}

#[derive(Debug, Error)]
pub enum ReleaseControlError {
    #[error("release control input is missing, symlinked, oversized, or not a regular file: {0}")]
    InvalidInput(String),
    #[error("release control JSON is invalid: {0}")]
    InvalidJson(String),
    #[error("release control trust store is invalid: {0}")]
    InvalidTrustStore(String),
    #[error("release manifest is invalid: {0}")]
    InvalidRelease(String),
    #[error("channel manifest is invalid: {0}")]
    InvalidChannel(String),
    #[error("block manifest is invalid: {0}")]
    InvalidBlock(String),
    #[error("release control signature is invalid")]
    InvalidSignature,
    #[error("release control signature validity is invalid: {0}")]
    InvalidValidity(String),
    #[error("release control anti-rollback check failed: {0}")]
    AntiRollback(String),
    #[error("release is blocked: {0}")]
    Blocked(String),
    #[error("release control relationship is invalid: {0}")]
    InvalidRelationship(String),
    #[error("release control I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

pub fn verify_release_control_files(
    release_path: &Path,
    channel_path: &Path,
    block_path: &Path,
    trust_store_path: &Path,
    observed_state_path: &Path,
) -> Result<ReleaseControlVerificationReportV1, ReleaseControlError> {
    verify_release_control_files_at(
        release_path,
        channel_path,
        block_path,
        trust_store_path,
        observed_state_path,
        OffsetDateTime::now_utc(),
    )
}

pub fn verify_release_control_files_at(
    release_path: &Path,
    channel_path: &Path,
    block_path: &Path,
    trust_store_path: &Path,
    observed_state_path: &Path,
    now: OffsetDateTime,
) -> Result<ReleaseControlVerificationReportV1, ReleaseControlError> {
    let release: SignedReleaseEnvelopeV1<ProductReleaseManifestV1> =
        read_json_bounded(release_path, MAX_CONTROL_FILE_BYTES, "release manifest")?;
    let channel: SignedReleaseEnvelopeV1<ChannelManifestV1> =
        read_json_bounded(channel_path, MAX_CONTROL_FILE_BYTES, "channel manifest")?;
    let block: SignedReleaseEnvelopeV1<BlockManifestV1> =
        read_json_bounded(block_path, MAX_CONTROL_FILE_BYTES, "block manifest")?;
    let trust_store: ReleaseTrustStoreV1 =
        read_json_bounded(trust_store_path, MAX_TRUST_STORE_BYTES, "release trust store")?;
    let observed: ReleaseControlObservedStateV1 =
        read_json_bounded(observed_state_path, MAX_CONTROL_FILE_BYTES, "observed release state")?;

    verify_release_control(&release, &channel, &block, &trust_store, &observed, now)
}

pub fn verify_release_control(
    release: &SignedReleaseEnvelopeV1<ProductReleaseManifestV1>,
    channel: &SignedReleaseEnvelopeV1<ChannelManifestV1>,
    block: &SignedReleaseEnvelopeV1<BlockManifestV1>,
    trust_store: &ReleaseTrustStoreV1,
    observed: &ReleaseControlObservedStateV1,
    now: OffsetDateTime,
) -> Result<ReleaseControlVerificationReportV1, ReleaseControlError> {
    validate_trust_store(trust_store)?;
    verify_envelope(release, trust_store, now)?;
    verify_envelope(channel, trust_store, now)?;
    verify_envelope(block, trust_store, now)?;

    validate_product_release(&release.payload)?;
    validate_channel_manifest(&channel.payload)?;
    validate_block_manifest(&block.payload)?;
    validate_observed_state(observed)?;

    if channel.payload.release_id != release.payload.release_id
        || channel.payload.release_generation != release.payload.release_generation
    {
        return Err(ReleaseControlError::InvalidRelationship(
            "channel target does not match Product Release identity".to_owned(),
        ));
    }
    if channel.payload.channel_generation < observed.highest_channel_generation {
        return Err(ReleaseControlError::AntiRollback(
            "channel_generation moved backwards".to_owned(),
        ));
    }
    if block.payload.block_generation < observed.highest_block_generation {
        return Err(ReleaseControlError::AntiRollback(
            "block_generation moved backwards".to_owned(),
        ));
    }

    let release_generation = release.payload.release_generation;
    let effective_minimum = release
        .payload
        .security
        .minimum_safe_release_generation
        .max(channel.payload.minimum_safe_release_generation)
        .max(block.payload.minimum_safe_release_generation);
    if release_generation < effective_minimum {
        return Err(ReleaseControlError::Blocked(format!(
            "release generation {release_generation} is below minimum safe generation {effective_minimum}"
        )));
    }
    if block
        .payload
        .blocked_releases
        .iter()
        .any(|item| item.release_id == release.payload.release_id || item.release_generation == release_generation)
    {
        return Err(ReleaseControlError::Blocked(
            "release identity appears in the signed block manifest".to_owned(),
        ));
    }

    let same_as_active = observed.active_release_id.as_deref() == Some(&release.payload.release_id)
        && observed.active_release_generation == release_generation;
    let mut rollback_authorized = false;
    let decision = if same_as_active {
        "already_active"
    } else if release_generation >= observed.highest_release_generation {
        "forward_update"
    } else {
        let authorization = channel.payload.rollback_authorization.as_ref().ok_or_else(|| {
            ReleaseControlError::AntiRollback(
                "historical release requires explicit signed rollback authorization".to_owned(),
            )
        })?;
        let active_id = observed.active_release_id.as_deref().ok_or_else(|| {
            ReleaseControlError::AntiRollback(
                "rollback authorization requires an observed active release".to_owned(),
            )
        })?;
        if authorization.from_release_id != active_id
            || authorization.from_release_generation != observed.active_release_generation
            || authorization.to_release_id != release.payload.release_id
            || authorization.to_release_generation != release_generation
        {
            return Err(ReleaseControlError::AntiRollback(
                "rollback authorization does not bind active and target releases".to_owned(),
            ));
        }
        if channel.payload.channel_generation <= observed.highest_channel_generation {
            return Err(ReleaseControlError::AntiRollback(
                "rollback requires a fresh channel_generation".to_owned(),
            ));
        }
        let expires_at = parse_utc(&authorization.expires_at, "rollback expires_at")?;
        if now >= expires_at {
            return Err(ReleaseControlError::AntiRollback(
                "rollback authorization is expired".to_owned(),
            ));
        }
        rollback_authorized = true;
        "authorized_rollback"
    };

    Ok(ReleaseControlVerificationReportV1 {
        schema_version: 1,
        product_version: release.payload.product_version.clone(),
        release_id: release.payload.release_id.clone(),
        release_generation,
        channel: channel.payload.channel,
        channel_generation: channel.payload.channel_generation,
        block_generation: block.payload.block_generation,
        effective_minimum_safe_generation: effective_minimum,
        rollback_authorized,
        decision: decision.to_owned(),
        signatures_verified: true,
        eligible: true,
    })
}

fn verify_envelope<T: Serialize>(
    envelope: &SignedReleaseEnvelopeV1<T>,
    trust_store: &ReleaseTrustStoreV1,
    now: OffsetDateTime,
) -> Result<(), ReleaseControlError> {
    if envelope.schema_version != 1
        || envelope.signature_algorithm != SIGNATURE_ALGORITHM
        || !canonical_identifier(&envelope.key_id, 96)
        || envelope.signature.is_empty()
        || envelope.signature.len() > 256
    {
        return Err(ReleaseControlError::InvalidSignature);
    }
    let signed_at = parse_utc(&envelope.signed_at, "signed_at")?;
    if signed_at > now + Duration::minutes(5) {
        return Err(ReleaseControlError::InvalidValidity(
            "signed_at is too far in the future".to_owned(),
        ));
    }
    let matching = trust_store
        .keys
        .iter()
        .filter(|key| key.key_id == envelope.key_id)
        .collect::<Vec<_>>();
    if matching.len() != 1 {
        return Err(ReleaseControlError::InvalidTrustStore(
            "key_id must reference exactly one trusted release key".to_owned(),
        ));
    }
    let key = matching[0];
    if key.revoked {
        return Err(ReleaseControlError::InvalidTrustStore(
            "release signing key is revoked".to_owned(),
        ));
    }
    let not_before = parse_utc(&key.not_before, "not_before")?;
    let not_after = parse_utc(&key.not_after, "not_after")?;
    if signed_at < not_before || signed_at >= not_after {
        return Err(ReleaseControlError::InvalidValidity(
            "signed_at is outside trusted key validity".to_owned(),
        ));
    }
    let public_key = decode_exact::<32>(&key.public_key, "public_key")?;
    let signature_bytes = decode_exact::<64>(&envelope.signature, "signature")?;
    let verifying_key =
        VerifyingKey::from_bytes(&public_key).map_err(|_| ReleaseControlError::InvalidSignature)?;
    let signature = Signature::from_bytes(&signature_bytes);
    let canonical = canonical_envelope_bytes(envelope)?;
    verifying_key
        .verify(&canonical, &signature)
        .map_err(|_| ReleaseControlError::InvalidSignature)
}

fn validate_trust_store(store: &ReleaseTrustStoreV1) -> Result<(), ReleaseControlError> {
    if store.schema_version != 1 || store.keys.is_empty() || store.keys.len() > 32 {
        return Err(ReleaseControlError::InvalidTrustStore(
            "schema_version/keys are invalid".to_owned(),
        ));
    }
    let mut seen = BTreeSet::new();
    for key in &store.keys {
        if !canonical_identifier(&key.key_id, 96) || !seen.insert(key.key_id.as_str()) {
            return Err(ReleaseControlError::InvalidTrustStore(
                "key_id is invalid or duplicated".to_owned(),
            ));
        }
        if key.signature_algorithm != SIGNATURE_ALGORITHM {
            return Err(ReleaseControlError::InvalidTrustStore(
                "signature_algorithm is invalid".to_owned(),
            ));
        }
        let _ = decode_exact::<32>(&key.public_key, "public_key")?;
        let before = parse_utc(&key.not_before, "not_before")?;
        let after = parse_utc(&key.not_after, "not_after")?;
        if before >= after {
            return Err(ReleaseControlError::InvalidTrustStore(
                "trusted key validity window is invalid".to_owned(),
            ));
        }
    }
    Ok(())
}

fn validate_product_release(manifest: &ProductReleaseManifestV1) -> Result<(), ReleaseControlError> {
    if manifest.schema_version != 1 || manifest.product != PRODUCT {
        return invalid_release("schema_version/product is invalid");
    }
    if !valid_semver(&manifest.product_version) {
        return invalid_release("product_version is not canonical SemVer");
    }
    if !valid_release_id(&manifest.release_id, &manifest.product_version) {
        return invalid_release("release_id is invalid or not bound to product_version");
    }
    if manifest.release_generation == 0 {
        return invalid_release("release_generation must be positive");
    }
    let _ = parse_utc(&manifest.published_at, "release published_at")?;
    if !canonical_text(&manifest.source.repository, 128)
        || !lower_hex(&manifest.source.git_commit, 40)
        || !canonical_identifier(&manifest.source.workflow_run_id, 64)
    {
        return invalid_release("source provenance is invalid");
    }
    let component_keys = manifest.components.keys().map(String::as_str).collect::<BTreeSet<_>>();
    if component_keys != REQUIRED_COMPONENTS.into_iter().collect::<BTreeSet<_>>()
        || manifest
            .components
            .values()
            .any(|value| !canonical_text(value, 128))
    {
        return invalid_release("component matrix is incomplete or invalid");
    }
    let contract_keys = manifest.contracts.keys().map(String::as_str).collect::<BTreeSet<_>>();
    if contract_keys != REQUIRED_CONTRACTS.into_iter().collect::<BTreeSet<_>>()
        || manifest.contracts.values().any(|value| *value == 0)
    {
        return invalid_release("contract matrix is incomplete or invalid");
    }
    if manifest.targets.is_empty() || manifest.targets.len() > ALLOWED_TARGETS.len() {
        return invalid_release("target matrix is empty or too large");
    }
    for (target_name, target) in &manifest.targets {
        if !ALLOWED_TARGETS.contains(&target_name.as_str()) || !canonical_text(&target.minimum_os, 128) {
            return invalid_release("target identity/minimum_os is invalid");
        }
        validate_artifact(&target.installer, true)?;
        validate_artifact(&target.bootstrap_payload, false)?;
        validate_artifact(&target.managed_release_payload, false)?;
    }
    if manifest.security.minimum_safe_release_generation > manifest.release_generation {
        return invalid_release("release minimum_safe_generation exceeds its own generation");
    }
    if let Some(value) = &manifest.security.mandatory_after {
        let _ = parse_utc(value, "release mandatory_after")?;
    }
    Ok(())
}

fn validate_channel_manifest(manifest: &ChannelManifestV1) -> Result<(), ReleaseControlError> {
    if manifest.schema_version != 1 || manifest.channel_generation == 0 || manifest.release_generation == 0 {
        return invalid_channel("schema_version/generations are invalid");
    }
    if !canonical_text(&manifest.release_id, 128) {
        return invalid_channel("release_id is invalid");
    }
    let _ = parse_utc(&manifest.published_at, "channel published_at")?;
    if let Some(value) = &manifest.mandatory_after {
        let _ = parse_utc(value, "channel mandatory_after")?;
    }
    if let Some(authorization) = &manifest.rollback_authorization {
        if !canonical_text(&authorization.from_release_id, 128)
            || !canonical_text(&authorization.to_release_id, 128)
            || authorization.from_release_generation == 0
            || authorization.to_release_generation == 0
            || !canonical_identifier(&authorization.reason_code, 96)
        {
            return invalid_channel("rollback authorization identity is invalid");
        }
        let _ = parse_utc(&authorization.expires_at, "rollback expires_at")?;
    }
    Ok(())
}

fn validate_block_manifest(manifest: &BlockManifestV1) -> Result<(), ReleaseControlError> {
    if manifest.schema_version != 1 || manifest.block_generation == 0 || manifest.blocked_releases.len() > 4096 {
        return invalid_block("schema_version/block_generation/entry count is invalid");
    }
    let _ = parse_utc(&manifest.published_at, "block published_at")?;
    let mut ids = BTreeSet::new();
    let mut generations = BTreeSet::new();
    for item in &manifest.blocked_releases {
        if !canonical_text(&item.release_id, 128)
            || item.release_generation == 0
            || !canonical_identifier(&item.reason_code, 96)
            || !ids.insert(item.release_id.as_str())
            || !generations.insert(item.release_generation)
        {
            return invalid_block("blocked release identity is invalid or duplicated");
        }
        let _ = parse_utc(&item.blocked_at, "blocked_at")?;
    }
    Ok(())
}

fn validate_observed_state(state: &ReleaseControlObservedStateV1) -> Result<(), ReleaseControlError> {
    if state.schema_version != 1 || state.active_release_generation > state.highest_release_generation {
        return Err(ReleaseControlError::AntiRollback(
            "observed release state is internally inconsistent".to_owned(),
        ));
    }
    match &state.active_release_id {
        Some(value) if state.active_release_generation > 0 && canonical_text(value, 128) => Ok(()),
        None if state.active_release_generation == 0 => Ok(()),
        _ => Err(ReleaseControlError::AntiRollback(
            "observed active release identity/generation is invalid".to_owned(),
        )),
    }
}

fn validate_artifact(artifact: &ReleaseArtifactV1, require_platform_signature: bool) -> Result<(), ReleaseControlError> {
    if !lower_hex(&artifact.sha256, 64) || artifact.size_bytes == 0 || artifact.size_bytes > MAX_RELEASE_ARTIFACT_BYTES {
        return invalid_release("artifact digest/size is invalid");
    }
    validate_content_addressed_object_key(&artifact.object_key, &artifact.sha256)?;
    if require_platform_signature {
        match &artifact.platform_signature {
            Some(value) if canonical_text(value, 128) => {}
            _ => return invalid_release("installer platform signature declaration is missing"),
        }
    } else if let Some(value) = &artifact.platform_signature {
        if !canonical_text(value, 128) {
            return invalid_release("artifact platform signature declaration is invalid");
        }
    }
    Ok(())
}

fn validate_content_addressed_object_key(object_key: &str, sha256: &str) -> Result<(), ReleaseControlError> {
    if !object_key.is_ascii() || object_key.len() > 512 || object_key.starts_with('/') || object_key.contains("..") {
        return invalid_release("artifact object_key is invalid");
    }
    let parts = object_key.split('/').collect::<Vec<_>>();
    if parts.len() != 6
        || parts[0] != "artifacts"
        || parts[1] != "v1"
        || parts[2] != "sha256"
        || parts[3] != &sha256[..2]
        || parts[4] != sha256
        || !safe_filename(parts[5])
    {
        return invalid_release("artifact object_key is not content-addressed by SHA-256");
    }
    Ok(())
}

fn canonical_envelope_bytes<T: Serialize>(
    envelope: &SignedReleaseEnvelopeV1<T>,
) -> Result<Vec<u8>, ReleaseControlError> {
    let mut value = serde_json::to_value(envelope)
        .map_err(|error| ReleaseControlError::InvalidJson(error.to_string()))?;
    let object = value.as_object_mut().ok_or_else(|| {
        ReleaseControlError::InvalidJson("signed envelope is not an object".to_owned())
    })?;
    object.remove("signature");
    let mut output = String::new();
    write_canonical_json(&value, &mut output)?;
    Ok(output.into_bytes())
}

fn write_canonical_json(value: &Value, output: &mut String) -> Result<(), ReleaseControlError> {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
        Value::Number(value) => output.push_str(&value.to_string()),
        Value::String(value) => output.push_str(
            &serde_json::to_string(value).map_err(|error| ReleaseControlError::InvalidJson(error.to_string()))?,
        ),
        Value::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                write_canonical_json(value, output)?;
            }
            output.push(']');
        }
        Value::Object(values) => {
            output.push('{');
            let mut keys = values.keys().collect::<Vec<_>>();
            keys.sort();
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    output.push(',');
                }
                output.push_str(
                    &serde_json::to_string(key)
                        .map_err(|error| ReleaseControlError::InvalidJson(error.to_string()))?,
                );
                output.push(':');
                write_canonical_json(&values[key], output)?;
            }
            output.push('}');
        }
    }
    Ok(())
}

fn read_json_bounded<T: DeserializeOwned>(
    path: &Path,
    maximum_bytes: u64,
    label: &str,
) -> Result<T, ReleaseControlError> {
    let bytes = read_regular_bounded(path, maximum_bytes, label)?;
    serde_json::from_slice(&bytes).map_err(|error| ReleaseControlError::InvalidJson(error.to_string()))
}

fn read_regular_bounded(path: &Path, maximum_bytes: u64, label: &str) -> Result<Vec<u8>, ReleaseControlError> {
    if path.is_symlink() || !path.is_file() {
        return Err(ReleaseControlError::InvalidInput(label.to_owned()));
    }
    let before = fs::metadata(path)?;
    if before.len() == 0 || before.len() > maximum_bytes {
        return Err(ReleaseControlError::InvalidInput(label.to_owned()));
    }
    let bytes = fs::read(path)?;
    let after = fs::metadata(path)?;
    if before.len() != after.len() || before.modified()? != after.modified()? || bytes.len() as u64 != before.len() {
        return Err(ReleaseControlError::InvalidInput(format!("{label} changed during verification")));
    }
    Ok(bytes)
}

fn parse_utc(value: &str, label: &str) -> Result<OffsetDateTime, ReleaseControlError> {
    let parsed = OffsetDateTime::parse(value, &Rfc3339)
        .map_err(|_| ReleaseControlError::InvalidValidity(format!("{label} is not RFC3339")))?;
    if parsed.offset() != time::UtcOffset::UTC {
        return Err(ReleaseControlError::InvalidValidity(format!("{label} must be UTC")));
    }
    Ok(parsed)
}

fn decode_exact<const N: usize>(value: &str, label: &str) -> Result<[u8; N], ReleaseControlError> {
    let decoded = BASE64
        .decode(value)
        .map_err(|_| ReleaseControlError::InvalidTrustStore(format!("{label} is invalid base64")))?;
    decoded
        .try_into()
        .map_err(|_| ReleaseControlError::InvalidTrustStore(format!("{label} length is invalid")))
}

fn valid_semver(value: &str) -> bool {
    if !canonical_text(value, 64) {
        return false;
    }
    let without_build = value.split_once('+').map(|item| item.0).unwrap_or(value);
    let core = without_build.split_once('-').map(|item| item.0).unwrap_or(without_build);
    let parts = core.split('.').collect::<Vec<_>>();
    parts.len() == 3 && parts.into_iter().all(valid_semver_number)
}

fn valid_semver_number(value: &str) -> bool {
    !value.is_empty()
        && value.bytes().all(|value| value.is_ascii_digit())
        && (value == "0" || !value.starts_with('0'))
}

fn valid_release_id(value: &str, product_version: &str) -> bool {
    value.len() <= 128
        && value.starts_with(&format!("{product_version}+"))
        && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'+' | b'-'))
}

fn canonical_identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn canonical_text(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value == value.trim()
        && !value.chars().any(char::is_control)
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length && value.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn safe_filename(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 255
        && value != "."
        && value != ".."
        && !value.contains('/')
        && !value.contains('\\')
        && value.bytes().all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'+' | b'-'))
}

fn invalid_release<T>(message: &str) -> Result<T, ReleaseControlError> {
    Err(ReleaseControlError::InvalidRelease(message.to_owned()))
}

fn invalid_channel<T>(message: &str) -> Result<T, ReleaseControlError> {
    Err(ReleaseControlError::InvalidChannel(message.to_owned()))
}

fn invalid_block<T>(message: &str) -> Result<T, ReleaseControlError> {
    Err(ReleaseControlError::InvalidBlock(message.to_owned()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    const NOW: &str = "2026-08-07T14:00:00Z";

    #[test]
    fn forward_update_requires_three_valid_signed_manifests() {
        let fixture = fixture(1042, 82, 5);
        let report = verify_release_control(
            &fixture.release,
            &fixture.channel,
            &fixture.block,
            &fixture.trust,
            &observed("1.4.1+20260801.1.g11111111", 1041, 1041, 81, 5),
            parse(NOW),
        )
        .expect("forward update should verify");
        assert_eq!(report.decision, "forward_update");
        assert!(!report.rollback_authorized);
        assert_eq!(report.release_generation, 1042);
        assert!(report.signatures_verified && report.eligible);
    }

    #[test]
    fn historical_release_fails_without_fresh_rollback_authorization() {
        let fixture = fixture(1042, 82, 5);
        let error = verify_release_control(
            &fixture.release,
            &fixture.channel,
            &fixture.block,
            &fixture.trust,
            &observed("1.4.3+20260808.1.g22222222", 1043, 1043, 82, 5),
            parse(NOW),
        )
        .expect_err("rollback without authorization must fail");
        assert!(matches!(error, ReleaseControlError::AntiRollback(_)));
    }

    #[test]
    fn signed_fresh_channel_can_authorize_business_rollback() {
        let mut fixture = fixture(1042, 83, 5);
        fixture.channel.payload.rollback_authorization = Some(RollbackAuthorizationV1 {
            from_release_id: "1.4.3+20260808.1.g22222222".to_owned(),
            from_release_generation: 1043,
            to_release_id: fixture.release.payload.release_id.clone(),
            to_release_generation: 1042,
            reason_code: "regression-rollback".to_owned(),
            expires_at: "2026-08-08T14:00:00Z".to_owned(),
        });
        fixture.channel = sign(fixture.channel.payload.clone(), &fixture.signing_key, 1, "release-key-1");
        let report = verify_release_control(
            &fixture.release,
            &fixture.channel,
            &fixture.block,
            &fixture.trust,
            &observed("1.4.3+20260808.1.g22222222", 1043, 1043, 82, 5),
            parse(NOW),
        )
        .expect("fresh signed rollback should verify");
        assert_eq!(report.decision, "authorized_rollback");
        assert!(report.rollback_authorized);
    }

    #[test]
    fn block_manifest_overrides_channel_pointer() {
        let mut fixture = fixture(1042, 82, 6);
        fixture.block.payload.blocked_releases.push(BlockedReleaseV1 {
            release_id: fixture.release.payload.release_id.clone(),
            release_generation: 1042,
            reason_code: "security-block".to_owned(),
            blocked_at: NOW.to_owned(),
        });
        fixture.block = sign(fixture.block.payload.clone(), &fixture.signing_key, 1, "release-key-1");
        let error = verify_release_control(
            &fixture.release,
            &fixture.channel,
            &fixture.block,
            &fixture.trust,
            &observed("1.4.1+20260801.1.g11111111", 1041, 1041, 81, 5),
            parse(NOW),
        )
        .expect_err("blocked release must fail");
        assert!(matches!(error, ReleaseControlError::Blocked(_)));
    }

    #[test]
    fn stale_channel_generation_fails_closed() {
        let fixture = fixture(1042, 81, 5);
        let error = verify_release_control(
            &fixture.release,
            &fixture.channel,
            &fixture.block,
            &fixture.trust,
            &observed("1.4.1+20260801.1.g11111111", 1041, 1041, 82, 5),
            parse(NOW),
        )
        .expect_err("stale channel must fail");
        assert!(matches!(error, ReleaseControlError::AntiRollback(_)));
    }

    #[test]
    fn tampered_release_payload_breaks_signature() {
        let mut fixture = fixture(1042, 82, 5);
        fixture.release.payload.release_generation = 9999;
        let error = verify_release_control(
            &fixture.release,
            &fixture.channel,
            &fixture.block,
            &fixture.trust,
            &observed("1.4.1+20260801.1.g11111111", 1041, 1041, 81, 5),
            parse(NOW),
        )
        .expect_err("tampering must invalidate signature");
        assert!(matches!(error, ReleaseControlError::InvalidSignature));
    }

    #[test]
    fn artifact_key_must_bind_full_sha256() {
        let mut fixture = fixture(1042, 82, 5);
        fixture.release.payload.targets.get_mut("macos-aarch64").unwrap().installer.object_key =
            "artifacts/v1/sha256/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/Hermes.dmg".to_owned();
        fixture.release = sign(fixture.release.payload.clone(), &fixture.signing_key, 1, "release-key-1");
        let error = verify_release_control(
            &fixture.release,
            &fixture.channel,
            &fixture.block,
            &fixture.trust,
            &observed("1.4.1+20260801.1.g11111111", 1041, 1041, 81, 5),
            parse(NOW),
        )
        .expect_err("object key must be content addressed");
        assert!(matches!(error, ReleaseControlError::InvalidRelease(_)));
    }

    struct Fixture {
        signing_key: SigningKey,
        trust: ReleaseTrustStoreV1,
        release: SignedReleaseEnvelopeV1<ProductReleaseManifestV1>,
        channel: SignedReleaseEnvelopeV1<ChannelManifestV1>,
        block: SignedReleaseEnvelopeV1<BlockManifestV1>,
    }

    fn fixture(release_generation: u64, channel_generation: u64, block_generation: u64) -> Fixture {
        let signing_key = SigningKey::from_bytes(&[7u8; 32]);
        let public_key = BASE64.encode(signing_key.verifying_key().as_bytes());
        let trust = ReleaseTrustStoreV1 {
            schema_version: 1,
            keys: vec![ReleaseTrustKeyV1 {
                key_id: "release-key-1".to_owned(),
                signature_algorithm: SIGNATURE_ALGORITHM.to_owned(),
                public_key,
                not_before: "2026-08-01T00:00:00Z".to_owned(),
                not_after: "2027-08-01T00:00:00Z".to_owned(),
                revoked: false,
            }],
        };
        let digest = "ab".repeat(32);
        let object = |name: &str, signature: Option<&str>| ReleaseArtifactV1 {
            object_key: format!("artifacts/v1/sha256/ab/{digest}/{name}"),
            sha256: digest.clone(),
            size_bytes: 1234,
            platform_signature: signature.map(str::to_owned),
        };
        let release_id = "1.4.2+20260807.3.g9839a049".to_owned();
        let release_payload = ProductReleaseManifestV1 {
            schema_version: 1,
            product: PRODUCT.to_owned(),
            product_version: "1.4.2".to_owned(),
            release_id: release_id.clone(),
            release_generation,
            published_at: NOW.to_owned(),
            source: ReleaseSourceV1 {
                repository: "looooooooy/hermes".to_owned(),
                git_commit: "9".repeat(40),
                workflow_run_id: "31185703341".to_owned(),
            },
            components: REQUIRED_COMPONENTS
                .into_iter()
                .map(|key| (key.to_owned(), "1.0.0".to_owned()))
                .collect(),
            contracts: REQUIRED_CONTRACTS
                .into_iter()
                .map(|key| (key.to_owned(), 1))
                .collect(),
            targets: BTreeMap::from([(
                "macos-aarch64".to_owned(),
                ProductTargetV1 {
                    minimum_os: "macOS 14".to_owned(),
                    installer: object("Hermes-1.4.2-arm64.dmg", Some("apple-developer-id")),
                    bootstrap_payload: object("bootstrap.tar.zst", None),
                    managed_release_payload: object("managed-release.tar.zst", None),
                },
            )]),
            security: ReleaseSecurityPolicyV1 {
                security_critical: false,
                minimum_safe_release_generation: 1000,
                mandatory_after: None,
            },
        };
        let channel_payload = ChannelManifestV1 {
            schema_version: 1,
            channel: ReleaseChannelV1::Stable,
            channel_generation,
            release_id: release_id.clone(),
            release_generation,
            published_at: NOW.to_owned(),
            minimum_safe_release_generation: 1000,
            mandatory_after: None,
            rollback_authorization: None,
        };
        let block_payload = BlockManifestV1 {
            schema_version: 1,
            block_generation,
            published_at: NOW.to_owned(),
            minimum_safe_release_generation: 1000,
            blocked_releases: vec![],
        };
        Fixture {
            release: sign(release_payload, &signing_key, 1, "release-key-1"),
            channel: sign(channel_payload, &signing_key, 1, "release-key-1"),
            block: sign(block_payload, &signing_key, 1, "release-key-1"),
            signing_key,
            trust,
        }
    }

    fn sign<T: Serialize + Clone>(
        payload: T,
        key: &SigningKey,
        schema_version: u8,
        key_id: &str,
    ) -> SignedReleaseEnvelopeV1<T> {
        let mut envelope = SignedReleaseEnvelopeV1 {
            schema_version,
            key_id: key_id.to_owned(),
            signature_algorithm: SIGNATURE_ALGORITHM.to_owned(),
            signed_at: NOW.to_owned(),
            payload,
            signature: String::new(),
        };
        let canonical = canonical_envelope_bytes(&envelope).expect("canonical envelope");
        envelope.signature = BASE64.encode(key.sign(&canonical).to_bytes());
        envelope
    }

    fn observed(
        active_release_id: &str,
        active_release_generation: u64,
        highest_release_generation: u64,
        highest_channel_generation: u64,
        highest_block_generation: u64,
    ) -> ReleaseControlObservedStateV1 {
        ReleaseControlObservedStateV1 {
            schema_version: 1,
            active_release_id: Some(active_release_id.to_owned()),
            active_release_generation,
            highest_release_generation,
            highest_channel_generation,
            highest_block_generation,
        }
    }

    fn parse(value: &str) -> OffsetDateTime {
        OffsetDateTime::parse(value, &Rfc3339).expect("valid test time")
    }
}
