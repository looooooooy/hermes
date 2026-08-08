use crate::model::ManagerSnapshotV1;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use thiserror::Error;

pub const MANAGER_IPC_SCHEMA_V1: u8 = 1;
pub const MAX_MANAGER_FRAME_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ManagerEnvelopeV1<T> {
    pub schema_version: u8,
    pub request_id: String,
    pub body: T,
}

impl<T> ManagerEnvelopeV1<T> {
    pub fn new(request_id: impl Into<String>, body: T) -> Self {
        Self {
            schema_version: MANAGER_IPC_SCHEMA_V1,
            request_id: request_id.into(),
            body,
        }
    }
}

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

#[derive(Debug, Error)]
pub enum IpcCodecError {
    #[error("IPC frame is too small")]
    TooSmall,
    #[error("IPC frame length exceeds the configured limit")]
    TooLarge,
    #[error("IPC frame length prefix does not match payload")]
    LengthMismatch,
    #[error("IPC request id is invalid")]
    InvalidRequestId,
    #[error("unsupported IPC schema version: {0}")]
    UnsupportedSchema(u8),
    #[error("IPC JSON is invalid: {0}")]
    InvalidJson(#[from] serde_json::Error),
}

pub fn encode_frame<T: Serialize>(
    envelope: &ManagerEnvelopeV1<T>,
) -> Result<Vec<u8>, IpcCodecError> {
    validate_request_id(&envelope.request_id)?;
    if envelope.schema_version != MANAGER_IPC_SCHEMA_V1 {
        return Err(IpcCodecError::UnsupportedSchema(envelope.schema_version));
    }
    let payload = serde_json::to_vec(envelope)?;
    if payload.len() > MAX_MANAGER_FRAME_BYTES {
        return Err(IpcCodecError::TooLarge);
    }
    let mut frame = Vec::with_capacity(4 + payload.len());
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(&payload);
    Ok(frame)
}

pub fn decode_frame<T: DeserializeOwned>(
    frame: &[u8],
) -> Result<ManagerEnvelopeV1<T>, IpcCodecError> {
    if frame.len() < 4 {
        return Err(IpcCodecError::TooSmall);
    }
    let declared = u32::from_be_bytes(frame[0..4].try_into().expect("four byte slice")) as usize;
    if declared > MAX_MANAGER_FRAME_BYTES {
        return Err(IpcCodecError::TooLarge);
    }
    if frame.len() - 4 != declared {
        return Err(IpcCodecError::LengthMismatch);
    }
    let envelope: ManagerEnvelopeV1<T> = serde_json::from_slice(&frame[4..])?;
    if envelope.schema_version != MANAGER_IPC_SCHEMA_V1 {
        return Err(IpcCodecError::UnsupportedSchema(envelope.schema_version));
    }
    validate_request_id(&envelope.request_id)?;
    Ok(envelope)
}

fn validate_request_id(request_id: &str) -> Result<(), IpcCodecError> {
    let valid = !request_id.is_empty()
        && request_id.len() <= 96
        && request_id
            .bytes()
            .all(|value| value.is_ascii_alphanumeric() || matches!(value, b'-' | b'_' | b'.'));
    if valid {
        Ok(())
    } else {
        Err(IpcCodecError::InvalidRequestId)
    }
}

#[cfg(test)]
mod tests {
    use super::{
        decode_frame, encode_frame, IpcCodecError, ManagerEnvelopeV1, ManagerRequestV1,
        MAX_MANAGER_FRAME_BYTES,
    };

    #[test]
    fn request_round_trip_uses_bounded_length_prefix() {
        let envelope = ManagerEnvelopeV1::new("req-01", ManagerRequestV1::Status);
        let frame = encode_frame(&envelope).expect("frame should encode");
        let decoded: ManagerEnvelopeV1<ManagerRequestV1> =
            decode_frame(&frame).expect("frame should decode");
        assert_eq!(decoded, envelope);
    }

    #[test]
    fn malformed_length_fails_closed() {
        let envelope = ManagerEnvelopeV1::new("req-02", ManagerRequestV1::Doctor);
        let mut frame = encode_frame(&envelope).expect("frame should encode");
        frame[3] = frame[3].wrapping_add(1);
        assert!(matches!(
            decode_frame::<ManagerRequestV1>(&frame),
            Err(IpcCodecError::LengthMismatch)
        ));
    }

    #[test]
    fn oversized_frame_is_rejected_before_json_decode() {
        let mut frame = Vec::from(((MAX_MANAGER_FRAME_BYTES + 1) as u32).to_be_bytes());
        frame.extend(std::iter::repeat_n(b'x', 8));
        assert!(matches!(
            decode_frame::<ManagerRequestV1>(&frame),
            Err(IpcCodecError::TooLarge)
        ));
    }

    #[test]
    fn request_id_is_ascii_and_bounded() {
        let envelope = ManagerEnvelopeV1::new("bad request id", ManagerRequestV1::Status);
        assert!(matches!(
            encode_frame(&envelope),
            Err(IpcCodecError::InvalidRequestId)
        ));
    }
}
