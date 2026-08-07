use crate::ipc::{decode_frame, encode_frame, DoctorCheckV1, IpcCodecError, ManagerEnvelopeV1, ManagerRequestV1, ManagerResponseV1, MAX_MANAGER_FRAME_BYTES};
use crate::RuntimeManager;
use std::sync::Arc;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum LocalIpcError {
    #[error(transparent)]
    Codec(#[from] IpcCodecError),
    #[error("local IPC unavailable: {0}")]
    Unavailable(&'static str),
    #[error("local IPC I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("runtime manager request failed: {0}")]
    Manager(String),
}

pub fn dispatch_read_only(manager: &RuntimeManager, request: ManagerRequestV1) -> ManagerResponseV1 {
    match request {
        ManagerRequestV1::Status => match manager.snapshot() {
            Ok(snapshot) => ManagerResponseV1::Snapshot(snapshot),
            Err(error) => ManagerResponseV1::Error {
                code: "status_failed".to_owned(),
                message: error.to_string(),
            },
        },
        ManagerRequestV1::Doctor => match manager.snapshot() {
            Ok(snapshot) => {
                let checks = snapshot
                    .components
                    .iter()
                    .map(|component| DoctorCheckV1 {
                        name: component.name.clone(),
                        passed: component.ready,
                        detail: component.detail.clone(),
                    })
                    .collect::<Vec<_>>();
                ManagerResponseV1::Doctor {
                    passed: !checks.is_empty() && checks.iter().all(|check| check.passed),
                    checks,
                }
            }
            Err(error) => ManagerResponseV1::Error {
                code: "doctor_failed".to_owned(),
                message: error.to_string(),
            },
        },
        ManagerRequestV1::Start
        | ManagerRequestV1::Stop
        | ManagerRequestV1::CheckForUpdates
        | ManagerRequestV1::StageUpdate { .. }
        | ManagerRequestV1::Rollback => ManagerResponseV1::Error {
            code: "read_only_transport".to_owned(),
            message: "control requests are disabled on the foundation local IPC transport"
                .to_owned(),
        },
    }
}

#[cfg(unix)]
mod unix_transport {
    use super::*;
    use std::fs;
    use std::io::{Read, Write};
    use std::os::unix::fs::{FileTypeExt, PermissionsExt};
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::{Path, PathBuf};

    pub struct ReadOnlyUnixServer {
        endpoint: PathBuf,
        listener: UnixListener,
        manager: Arc<RuntimeManager>,
    }

    impl ReadOnlyUnixServer {
        pub fn bind(endpoint: impl Into<PathBuf>, manager: Arc<RuntimeManager>) -> Result<Self, LocalIpcError> {
            let endpoint = endpoint.into();
            if !endpoint.is_absolute() {
                return Err(LocalIpcError::Unavailable("Unix IPC endpoint must be absolute"));
            }
            let parent = endpoint
                .parent()
                .ok_or(LocalIpcError::Unavailable("Unix IPC endpoint has no parent"))?;
            fs::create_dir_all(parent)?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;

            if let Ok(metadata) = fs::symlink_metadata(&endpoint) {
                if metadata.file_type().is_symlink() || !metadata.file_type().is_socket() {
                    return Err(LocalIpcError::Unavailable("refusing to replace non-socket IPC endpoint"));
                }
                fs::remove_file(&endpoint)?;
            }

            let listener = UnixListener::bind(&endpoint)?;
            fs::set_permissions(&endpoint, fs::Permissions::from_mode(0o600))?;
            Ok(Self {
                endpoint,
                listener,
                manager,
            })
        }

        pub fn serve_once(&self) -> Result<(), LocalIpcError> {
            let (mut stream, _) = self.listener.accept()?;
            let envelope: ManagerEnvelopeV1<ManagerRequestV1> = read_envelope(&mut stream)?;
            let response = dispatch_read_only(&self.manager, envelope.body);
            let response_envelope = ManagerEnvelopeV1::new(envelope.request_id, response);
            write_envelope(&mut stream, &response_envelope)
        }

        pub fn endpoint(&self) -> &Path {
            &self.endpoint
        }
    }

    impl Drop for ReadOnlyUnixServer {
        fn drop(&mut self) {
            if let Ok(metadata) = fs::symlink_metadata(&self.endpoint) {
                if metadata.file_type().is_socket() {
                    let _ = fs::remove_file(&self.endpoint);
                }
            }
        }
    }

    pub fn request(
        endpoint: &Path,
        request_id: &str,
        request: ManagerRequestV1,
    ) -> Result<ManagerResponseV1, LocalIpcError> {
        let mut stream = UnixStream::connect(endpoint)?;
        let envelope = ManagerEnvelopeV1::new(request_id, request);
        write_envelope(&mut stream, &envelope)?;
        let response: ManagerEnvelopeV1<ManagerResponseV1> = read_envelope(&mut stream)?;
        if response.request_id != request_id {
            return Err(LocalIpcError::Unavailable("IPC response request_id mismatch"));
        }
        Ok(response.body)
    }

    fn read_envelope<T: serde::de::DeserializeOwned>(
        stream: &mut UnixStream,
    ) -> Result<ManagerEnvelopeV1<T>, LocalIpcError> {
        let mut prefix = [0u8; 4];
        stream.read_exact(&mut prefix)?;
        let payload_len = u32::from_be_bytes(prefix) as usize;
        if payload_len > MAX_MANAGER_FRAME_BYTES {
            return Err(LocalIpcError::Codec(IpcCodecError::TooLarge));
        }
        let mut frame = Vec::with_capacity(4 + payload_len);
        frame.extend_from_slice(&prefix);
        let mut payload = vec![0u8; payload_len];
        stream.read_exact(&mut payload)?;
        frame.extend_from_slice(&payload);
        Ok(decode_frame(&frame)?)
    }

    fn write_envelope<T: serde::Serialize>(
        stream: &mut UnixStream,
        envelope: &ManagerEnvelopeV1<T>,
    ) -> Result<(), LocalIpcError> {
        let frame = encode_frame(envelope)?;
        stream.write_all(&frame)?;
        stream.flush()?;
        Ok(())
    }
}

#[cfg(unix)]
pub use unix_transport::{request as request_read_only, ReadOnlyUnixServer};

#[cfg(windows)]
pub fn request_read_only(
    _endpoint: &std::path::Path,
    _request_id: &str,
    _request: ManagerRequestV1,
) -> Result<ManagerResponseV1, LocalIpcError> {
    Err(LocalIpcError::Unavailable(
        "Windows Named Pipe transport is not implemented yet",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::platform::{DefaultInstallLayout, FailClosedServiceManager};
    use std::sync::Arc;

    #[test]
    fn control_requests_fail_closed_on_read_only_dispatch() {
        let layout = Arc::new(DefaultInstallLayout::discover().expect("layout"));
        let manager = RuntimeManager::new(Arc::new(FailClosedServiceManager), layout);
        let response = dispatch_read_only(&manager, ManagerRequestV1::Start);
        assert!(matches!(
            response,
            ManagerResponseV1::Error { ref code, .. } if code == "read_only_transport"
        ));
    }

    #[cfg(unix)]
    #[test]
    fn unix_status_round_trip_uses_real_socket_and_shared_framing() {
        use std::fs;
        use std::thread;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let root = std::env::temp_dir().join(format!(
            "hermes-runtime-manager-ipc-{}-{unique}",
            std::process::id()
        ));
        let endpoint = root.join("runtime-manager.sock");
        let layout = Arc::new(DefaultInstallLayout::discover().expect("layout"));
        let manager = Arc::new(RuntimeManager::new(
            Arc::new(FailClosedServiceManager),
            layout,
        ));
        let server = ReadOnlyUnixServer::bind(endpoint.clone(), manager).expect("bind");
        assert_eq!(
            fs::metadata(server.endpoint())
                .expect("socket metadata")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );

        let server_thread = thread::spawn(move || server.serve_once().expect("serve once"));
        let response = request_read_only(&endpoint, "status-test", ManagerRequestV1::Status)
            .expect("status request");
        server_thread.join().expect("server thread");

        assert!(matches!(response, ManagerResponseV1::Snapshot(_)));
        let _ = fs::remove_dir_all(root);
    }
}
