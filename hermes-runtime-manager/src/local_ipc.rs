use crate::ipc::{
    decode_frame, encode_frame, DoctorCheckV1, IpcCodecError, ManagerEnvelopeV1,
    ManagerRequestV1, ManagerResponseV1, MAX_MANAGER_FRAME_BYTES,
};
use crate::RuntimeManager;
use std::sync::Arc;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum LocalIpcError {
    #[error(transparent)]
    Codec(#[from] IpcCodecError),
    #[error("local IPC unavailable: {0}")]
    Unavailable(&'static str),
    #[error("local IPC peer rejected: {0}")]
    Peer(String),
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
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{FileTypeExt, PermissionsExt};
    use std::os::unix::net::{UnixListener, UnixStream};
    use std::path::{Path, PathBuf};

    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub struct UnixPeerIdentity {
        pub uid: u32,
        pub pid: Option<u32>,
    }

    pub struct ReadOnlyUnixServer {
        endpoint: PathBuf,
        listener: UnixListener,
        manager: Arc<RuntimeManager>,
    }

    impl ReadOnlyUnixServer {
        pub fn bind(
            endpoint: impl Into<PathBuf>,
            manager: Arc<RuntimeManager>,
        ) -> Result<Self, LocalIpcError> {
            let endpoint = endpoint.into();
            if !endpoint.is_absolute() {
                return Err(LocalIpcError::Unavailable(
                    "Unix IPC endpoint must be absolute",
                ));
            }
            let parent = endpoint
                .parent()
                .ok_or(LocalIpcError::Unavailable("Unix IPC endpoint has no parent"))?;
            fs::create_dir_all(parent)?;
            fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;

            if let Ok(metadata) = fs::symlink_metadata(&endpoint) {
                if metadata.file_type().is_symlink() || !metadata.file_type().is_socket() {
                    return Err(LocalIpcError::Unavailable(
                        "refusing to replace non-socket IPC endpoint",
                    ));
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
            let _peer = verify_same_user(&stream)?;
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
            return Err(LocalIpcError::Unavailable(
                "IPC response request_id mismatch",
            ));
        }
        Ok(response.body)
    }

    pub fn verify_same_user(stream: &UnixStream) -> Result<UnixPeerIdentity, LocalIpcError> {
        let fd = stream.as_raw_fd();

        #[cfg(target_os = "linux")]
        {
            let mut credential = std::mem::MaybeUninit::<libc::ucred>::zeroed();
            let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
            let result = unsafe {
                libc::getsockopt(
                    fd,
                    libc::SOL_SOCKET,
                    libc::SO_PEERCRED,
                    credential.as_mut_ptr().cast(),
                    &mut length,
                )
            };
            if result != 0 {
                return Err(LocalIpcError::Io(std::io::Error::last_os_error()));
            }
            let credential = unsafe { credential.assume_init() };
            let current_uid = unsafe { libc::geteuid() };
            if credential.uid != current_uid {
                return Err(LocalIpcError::Peer(format!(
                    "peer uid {} does not match Runtime Manager uid {}",
                    credential.uid, current_uid
                )));
            }
            return Ok(UnixPeerIdentity {
                uid: credential.uid,
                pid: u32::try_from(credential.pid).ok(),
            });
        }

        #[cfg(target_os = "macos")]
        {
            const SOL_LOCAL_DARWIN: libc::c_int = 0;
            const LOCAL_PEERPID_DARWIN: libc::c_int = 0x002;

            let mut peer_uid: libc::uid_t = 0;
            let mut peer_gid: libc::gid_t = 0;
            let result = unsafe { libc::getpeereid(fd, &mut peer_uid, &mut peer_gid) };
            if result != 0 {
                return Err(LocalIpcError::Io(std::io::Error::last_os_error()));
            }
            let current_uid = unsafe { libc::geteuid() };
            if peer_uid != current_uid {
                return Err(LocalIpcError::Peer(format!(
                    "peer uid {} does not match Runtime Manager uid {}",
                    peer_uid, current_uid
                )));
            }

            let mut peer_pid: libc::pid_t = 0;
            let mut length = std::mem::size_of::<libc::pid_t>() as libc::socklen_t;
            let pid_result = unsafe {
                libc::getsockopt(
                    fd,
                    SOL_LOCAL_DARWIN,
                    LOCAL_PEERPID_DARWIN,
                    (&mut peer_pid as *mut libc::pid_t).cast(),
                    &mut length,
                )
            };
            if pid_result != 0 {
                return Err(LocalIpcError::Io(std::io::Error::last_os_error()));
            }
            let peer_pid = u32::try_from(peer_pid).map_err(|_| {
                LocalIpcError::Peer("macOS peer PID is outside the supported range".to_owned())
            })?;
            if peer_pid == 0 {
                return Err(LocalIpcError::Peer("macOS peer PID is zero".to_owned()));
            }
            return Ok(UnixPeerIdentity {
                uid: peer_uid,
                pid: Some(peer_pid),
            });
        }

        #[cfg(not(any(target_os = "linux", target_os = "macos")))]
        {
            let _ = fd;
            Err(LocalIpcError::Unavailable(
                "Unix peer authentication is not implemented for this OS",
            ))
        }
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

    #[cfg(test)]
    mod peer_tests {
        use super::*;
        use std::thread;
        use std::time::{SystemTime, UNIX_EPOCH};

        #[test]
        fn accepted_peer_is_bound_to_current_os_user_and_process() {
            let unique = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("clock")
                .subsec_nanos();
            let root = Path::new("/tmp").join(format!("hpa-{}-{unique}", std::process::id()));
            fs::create_dir_all(&root).expect("root");
            let endpoint = root.join("p.sock");
            let listener = UnixListener::bind(&endpoint).expect("listener");
            let client_endpoint = endpoint.clone();
            let client = thread::spawn(move || UnixStream::connect(client_endpoint).expect("client"));
            let (server_stream, _) = listener.accept().expect("accept");
            let identity = verify_same_user(&server_stream).expect("peer identity");
            let _client_stream = client.join().expect("client thread");

            assert_eq!(identity.uid, unsafe { libc::geteuid() });
            assert_eq!(identity.pid, Some(std::process::id()));
            let _ = fs::remove_file(endpoint);
            let _ = fs::remove_dir_all(root);
        }
    }
}

#[cfg(unix)]
pub use unix_transport::{request as request_read_only, ReadOnlyUnixServer, UnixPeerIdentity};

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
        use std::os::unix::fs::PermissionsExt;
        use std::path::Path;
        use std::thread;
        use std::time::{SystemTime, UNIX_EPOCH};

        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .subsec_nanos();
        let root = Path::new("/tmp").join(format!("hrm-{}-{unique}", std::process::id()));
        let endpoint = root.join("rm.sock");
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
