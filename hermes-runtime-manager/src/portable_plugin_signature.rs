use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use thiserror::Error;
use time::{format_description::well_known::Rfc3339, OffsetDateTime};

const PLUGIN_ID: &str = "hermes-agent-plugin";
const SIGNATURE_ALGORITHM: &str = "ed25519";
const ENTRYPOINT_GROUP: &str = "hermes_agent.plugins";
const ENTRYPOINT_NAME: &str = "hermes-agent-plugin";
const ENTRYPOINT_VALUE: &str = "hermes_agent_plugin";
const MAX_MANIFEST_BYTES: u64 = 64 * 1024;
const MAX_TRUST_STORE_BYTES: u64 = 64 * 1024;
const MAX_WHEEL_BYTES: u64 = 128 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortablePluginEntrypointV2 {
    pub group: String,
    pub name: String,
    pub value: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PortablePluginManifestV2 {
    pub schema_version: u8,
    pub plugin_id: String,
    pub version: String,
    pub artifact_filename: String,
    pub wheel_sha256: String,
    pub entrypoint: PortablePluginEntrypointV2,
    pub signature_algorithm: String,
    pub key_id: String,
    pub issued_at: String,
    pub expires_at: String,
    pub signature: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PluginTrustStoreV1 {
    pub schema_version: u8,
    pub keys: Vec<PluginTrustKeyV1>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PluginTrustKeyV1 {
    pub key_id: String,
    pub signature_algorithm: String,
    pub public_key: String,
    pub not_before: String,
    pub not_after: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PortablePluginVerificationReportV2 {
    pub schema_version: u8,
    pub plugin_id: String,
    pub version: String,
    pub artifact_filename: String,
    pub wheel_sha256: String,
    pub key_id: String,
    pub issued_at: String,
    pub expires_at: String,
    pub signature_verified: bool,
}

#[derive(Debug, Error)]
pub enum PortablePluginVerificationError {
    #[error("portable plugin input is missing, symlinked, oversized, or not a regular file: {0}")]
    InvalidInput(String),
    #[error("portable plugin JSON is invalid: {0}")]
    InvalidJson(String),
    #[error("portable plugin manifest contract is invalid: {0}")]
    InvalidManifest(String),
    #[error("portable plugin trust store contract is invalid: {0}")]
    InvalidTrustStore(String),
    #[error("portable plugin artifact digest mismatch")]
    DigestMismatch,
    #[error("portable plugin signature is invalid")]
    InvalidSignature,
    #[error("portable plugin signature validity window is invalid: {0}")]
    InvalidValidity(String),
    #[error("portable plugin I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

pub fn verify_portable_plugin_signature(
    manifest_path: &Path,
    trust_store_path: &Path,
    wheel_path: &Path,
) -> Result<PortablePluginVerificationReportV2, PortablePluginVerificationError> {
    verify_portable_plugin_signature_at(
        manifest_path,
        trust_store_path,
        wheel_path,
        OffsetDateTime::now_utc(),
    )
}

pub fn verify_portable_plugin_signature_at(
    manifest_path: &Path,
    trust_store_path: &Path,
    wheel_path: &Path,
    now: OffsetDateTime,
) -> Result<PortablePluginVerificationReportV2, PortablePluginVerificationError> {
    let manifest_bytes = read_regular_bounded(manifest_path, MAX_MANIFEST_BYTES, "manifest")?;
    let trust_store_bytes =
        read_regular_bounded(trust_store_path, MAX_TRUST_STORE_BYTES, "trust store")?;
    let wheel_bytes = read_regular_bounded(wheel_path, MAX_WHEEL_BYTES, "wheel")?;

    let manifest: PortablePluginManifestV2 = serde_json::from_slice(&manifest_bytes)
        .map_err(|error| PortablePluginVerificationError::InvalidJson(error.to_string()))?;
    let trust_store: PluginTrustStoreV1 = serde_json::from_slice(&trust_store_bytes)
        .map_err(|error| PortablePluginVerificationError::InvalidJson(error.to_string()))?;

    validate_manifest(&manifest, wheel_path)?;
    validate_trust_store(&trust_store)?;

    let observed_wheel_sha = hex_lower(&Sha256::digest(&wheel_bytes));
    if observed_wheel_sha != manifest.wheel_sha256 {
        return Err(PortablePluginVerificationError::DigestMismatch);
    }

    let issued_at = parse_utc(&manifest.issued_at, "issued_at")?;
    let expires_at = parse_utc(&manifest.expires_at, "expires_at")?;
    if issued_at >= expires_at {
        return Err(PortablePluginVerificationError::InvalidValidity(
            "issued_at must precede expires_at".to_owned(),
        ));
    }
    if now < issued_at || now >= expires_at {
        return Err(PortablePluginVerificationError::InvalidValidity(
            "manifest is not currently valid".to_owned(),
        ));
    }

    let matching = trust_store
        .keys
        .iter()
        .filter(|key| key.key_id == manifest.key_id)
        .collect::<Vec<_>>();
    if matching.len() != 1 {
        return Err(PortablePluginVerificationError::InvalidTrustStore(
            "manifest key_id must reference exactly one trusted key".to_owned(),
        ));
    }
    let key = matching[0];
    if key.signature_algorithm != SIGNATURE_ALGORITHM {
        return Err(PortablePluginVerificationError::InvalidTrustStore(
            "trusted key signature algorithm is invalid".to_owned(),
        ));
    }
    let not_before = parse_utc(&key.not_before, "not_before")?;
    let not_after = parse_utc(&key.not_after, "not_after")?;
    if not_before >= not_after {
        return Err(PortablePluginVerificationError::InvalidTrustStore(
            "trusted key validity window is invalid".to_owned(),
        ));
    }
    if issued_at < not_before || expires_at > not_after || now < not_before || now >= not_after {
        return Err(PortablePluginVerificationError::InvalidValidity(
            "manifest validity exceeds trusted key validity".to_owned(),
        ));
    }

    let public_key = decode_exact::<32>(&key.public_key, "public_key")?;
    let signature_bytes = decode_exact::<64>(&manifest.signature, "signature")?;
    let verifying_key = VerifyingKey::from_bytes(&public_key)
        .map_err(|_| PortablePluginVerificationError::InvalidSignature)?;
    let signature = Signature::from_bytes(&signature_bytes);
    let canonical = canonical_manifest_bytes(&manifest)?;
    verifying_key
        .verify(&canonical, &signature)
        .map_err(|_| PortablePluginVerificationError::InvalidSignature)?;

    Ok(PortablePluginVerificationReportV2 {
        schema_version: 2,
        plugin_id: manifest.plugin_id,
        version: manifest.version,
        artifact_filename: manifest.artifact_filename,
        wheel_sha256: manifest.wheel_sha256,
        key_id: manifest.key_id,
        issued_at: manifest.issued_at,
        expires_at: manifest.expires_at,
        signature_verified: true,
    })
}

fn validate_manifest(
    manifest: &PortablePluginManifestV2,
    wheel_path: &Path,
) -> Result<(), PortablePluginVerificationError> {
    if manifest.schema_version != 2 {
        return invalid_manifest("schema_version must be 2");
    }
    if manifest.plugin_id != PLUGIN_ID {
        return invalid_manifest("plugin_id is invalid");
    }
    if !canonical_text(&manifest.version, 64) {
        return invalid_manifest("version is invalid");
    }
    if !safe_filename(&manifest.artifact_filename) || !manifest.artifact_filename.ends_with(".whl") {
        return invalid_manifest("artifact_filename is invalid");
    }
    if wheel_path.file_name().and_then(|value| value.to_str()) != Some(&manifest.artifact_filename) {
        return invalid_manifest("artifact filename does not match wheel path");
    }
    if !lower_sha256(&manifest.wheel_sha256) {
        return invalid_manifest("wheel_sha256 is invalid");
    }
    if manifest.entrypoint.group != ENTRYPOINT_GROUP
        || manifest.entrypoint.name != ENTRYPOINT_NAME
        || manifest.entrypoint.value != ENTRYPOINT_VALUE
    {
        return invalid_manifest("entrypoint is invalid");
    }
    if manifest.signature_algorithm != SIGNATURE_ALGORITHM {
        return invalid_manifest("signature_algorithm is invalid");
    }
    if !canonical_identifier(&manifest.key_id, 96) {
        return invalid_manifest("key_id is invalid");
    }
    if manifest.signature.is_empty() || manifest.signature.len() > 256 {
        return invalid_manifest("signature is invalid");
    }
    if manifest.issued_at.is_empty() || manifest.expires_at.is_empty() {
        return invalid_manifest("validity timestamps are missing");
    }
    Ok(())
}

fn validate_trust_store(store: &PluginTrustStoreV1) -> Result<(), PortablePluginVerificationError> {
    if store.schema_version != 1 || store.keys.is_empty() || store.keys.len() > 32 {
        return Err(PortablePluginVerificationError::InvalidTrustStore(
            "schema_version/keys are invalid".to_owned(),
        ));
    }
    let mut seen = std::collections::BTreeSet::new();
    for key in &store.keys {
        if !canonical_identifier(&key.key_id, 96) || !seen.insert(key.key_id.as_str()) {
            return Err(PortablePluginVerificationError::InvalidTrustStore(
                "key_id is invalid or duplicated".to_owned(),
            ));
        }
        if key.signature_algorithm != SIGNATURE_ALGORITHM {
            return Err(PortablePluginVerificationError::InvalidTrustStore(
                "signature_algorithm is invalid".to_owned(),
            ));
        }
        let _ = decode_exact::<32>(&key.public_key, "public_key")?;
        let _ = parse_utc(&key.not_before, "not_before")?;
        let _ = parse_utc(&key.not_after, "not_after")?;
    }
    Ok(())
}

fn canonical_manifest_bytes(
    manifest: &PortablePluginManifestV2,
) -> Result<Vec<u8>, PortablePluginVerificationError> {
    let mut value = serde_json::to_value(manifest)
        .map_err(|error| PortablePluginVerificationError::InvalidJson(error.to_string()))?;
    let object = value.as_object_mut().ok_or_else(|| {
        PortablePluginVerificationError::InvalidJson("manifest is not an object".to_owned())
    })?;
    object.remove("signature");
    let mut output = String::new();
    write_canonical_json(&value, &mut output)?;
    Ok(output.into_bytes())
}

fn write_canonical_json(
    value: &Value,
    output: &mut String,
) -> Result<(), PortablePluginVerificationError> {
    match value {
        Value::Null => output.push_str("null"),
        Value::Bool(value) => output.push_str(if *value { "true" } else { "false" }),
        Value::Number(value) => output.push_str(&value.to_string()),
        Value::String(value) => output.push_str(
            &serde_json::to_string(value)
                .map_err(|error| PortablePluginVerificationError::InvalidJson(error.to_string()))?,
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
                    &serde_json::to_string(key).map_err(|error| {
                        PortablePluginVerificationError::InvalidJson(error.to_string())
                    })?,
                );
                output.push(':');
                write_canonical_json(&values[key], output)?;
            }
            output.push('}');
        }
    }
    Ok(())
}

fn read_regular_bounded(
    path: &Path,
    maximum: u64,
    label: &str,
) -> Result<Vec<u8>, PortablePluginVerificationError> {
    if !path.is_absolute() || path.is_symlink() {
        return Err(PortablePluginVerificationError::InvalidInput(label.to_owned()));
    }
    let metadata = fs::metadata(path)?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > maximum {
        return Err(PortablePluginVerificationError::InvalidInput(label.to_owned()));
    }
    Ok(fs::read(path)?)
}

fn parse_utc(
    value: &str,
    label: &str,
) -> Result<OffsetDateTime, PortablePluginVerificationError> {
    let parsed = OffsetDateTime::parse(value, &Rfc3339).map_err(|_| {
        PortablePluginVerificationError::InvalidValidity(format!(
            "{label} must be RFC3339"
        ))
    })?;
    if parsed.offset() != time::UtcOffset::UTC {
        return Err(PortablePluginVerificationError::InvalidValidity(format!(
            "{label} must be UTC"
        )));
    }
    Ok(parsed)
}

fn decode_exact<const N: usize>(
    value: &str,
    label: &str,
) -> Result<[u8; N], PortablePluginVerificationError> {
    let decoded = BASE64.decode(value).map_err(|_| {
        PortablePluginVerificationError::InvalidTrustStore(format!(
            "{label} must be base64"
        ))
    })?;
    decoded.try_into().map_err(|_| {
        PortablePluginVerificationError::InvalidTrustStore(format!(
            "{label} has invalid length"
        ))
    })
}

fn canonical_text(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value == value.trim()
        && !value.chars().any(char::is_control)
}

fn canonical_identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn safe_filename(value: &str) -> bool {
    canonical_text(value, 255)
        && !value.contains('/')
        && !value.contains('\\')
        && !matches!(value, "." | "..")
}

fn lower_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
}

fn hex_lower(bytes: &[u8]) -> String {
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        let _ = write!(output, "{byte:02x}");
    }
    output
}

fn invalid_manifest<T>(
    detail: &str,
) -> Result<T, PortablePluginVerificationError> {
    Err(PortablePluginVerificationError::InvalidManifest(
        detail.to_owned(),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn portable_vendor_signature_verifies_and_detects_wheel_tampering() {
        let root = test_root("portable-plugin-signature");
        fs::create_dir_all(&root).expect("root");
        let wheel = root.join("hermes_agent_plugin-0.1.0-py3-none-any.whl");
        fs::write(&wheel, b"portable-plugin-wheel").expect("wheel");
        let wheel_sha256 = hex_lower(&Sha256::digest(b"portable-plugin-wheel"));
        let signing = SigningKey::from_bytes(&[7u8; 32]);
        let key_id = "desktop-ci-vendor-key";
        let mut manifest = PortablePluginManifestV2 {
            schema_version: 2,
            plugin_id: PLUGIN_ID.to_owned(),
            version: "0.1.0".to_owned(),
            artifact_filename: wheel.file_name().unwrap().to_string_lossy().into_owned(),
            wheel_sha256,
            entrypoint: PortablePluginEntrypointV2 {
                group: ENTRYPOINT_GROUP.to_owned(),
                name: ENTRYPOINT_NAME.to_owned(),
                value: ENTRYPOINT_VALUE.to_owned(),
            },
            signature_algorithm: SIGNATURE_ALGORITHM.to_owned(),
            key_id: key_id.to_owned(),
            issued_at: "2026-08-07T00:00:00Z".to_owned(),
            expires_at: "2026-08-08T00:00:00Z".to_owned(),
            signature: String::new(),
        };
        let signature = signing.sign(&canonical_manifest_bytes(&manifest).expect("canonical"));
        manifest.signature = BASE64.encode(signature.to_bytes());
        let trust_store = PluginTrustStoreV1 {
            schema_version: 1,
            keys: vec![PluginTrustKeyV1 {
                key_id: key_id.to_owned(),
                signature_algorithm: SIGNATURE_ALGORITHM.to_owned(),
                public_key: BASE64.encode(signing.verifying_key().to_bytes()),
                not_before: "2026-08-06T00:00:00Z".to_owned(),
                not_after: "2026-08-09T00:00:00Z".to_owned(),
            }],
        };
        let manifest_path = root.join("portable-plugin-manifest.json");
        let trust_path = root.join("trust-store.json");
        fs::write(
            &manifest_path,
            serde_json::to_vec(&manifest).expect("manifest JSON"),
        )
        .expect("manifest");
        fs::write(
            &trust_path,
            serde_json::to_vec(&trust_store).expect("trust JSON"),
        )
        .expect("trust");
        let now = OffsetDateTime::parse("2026-08-07T12:00:00Z", &Rfc3339).expect("now");
        let report = verify_portable_plugin_signature_at(&manifest_path, &trust_path, &wheel, now)
            .expect("signature should verify");
        assert!(report.signature_verified);
        assert_eq!(report.wheel_sha256, manifest.wheel_sha256);

        fs::write(&wheel, b"tampered-wheel").expect("tamper");
        assert!(matches!(
            verify_portable_plugin_signature_at(&manifest_path, &trust_path, &wheel, now),
            Err(PortablePluginVerificationError::DigestMismatch)
        ));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn portable_manifest_rejects_customer_absolute_paths_and_unknown_fields() {
        let value = serde_json::json!({
            "schema_version": 2,
            "plugin_id": PLUGIN_ID,
            "version": "0.1.0",
            "artifact_filename": "plugin.whl",
            "wheel_sha256": "0".repeat(64),
            "entrypoint": {
                "group": ENTRYPOINT_GROUP,
                "name": ENTRYPOINT_NAME,
                "value": ENTRYPOINT_VALUE
            },
            "signature_algorithm": SIGNATURE_ALGORITHM,
            "key_id": "key-1",
            "issued_at": "2026-08-07T00:00:00Z",
            "expires_at": "2026-08-08T00:00:00Z",
            "signature": BASE64.encode([0u8; 64]),
            "wheel_path": "/Users/example/Hermes/plugin.whl"
        });
        let encoded = serde_json::to_vec(&value).expect("JSON");
        assert!(serde_json::from_slice::<PortablePluginManifestV2>(&encoded).is_err());
    }

    fn test_root(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        std::env::temp_dir().join(format!("hermes-{label}-{}-{nonce}", std::process::id()))
    }
}
