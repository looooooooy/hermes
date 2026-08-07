use crate::ports::PortError;
use crate::update_safe_window::{HostUpdateSafetySnapshotV1, HostUpdateSafetySource};
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::os::unix::net::UnixStream;
use std::path::{Component, Path, PathBuf};
use std::time::Duration;

const MAX_UDS_PATH_BYTES: usize = 103;
const MAX_RESPONSE_BYTES: usize = 8_192;
const REQUEST: &[u8] = b"{\"method\":\"update-safety.snapshot\",\"schema_version\":1}\n";

#[derive(Debug, Clone)]
pub struct UnixHostUpdateSafetySource {
    endpoint: PathBuf,
    timeout: Duration,
}

impl UnixHostUpdateSafetySource {
    pub fn discover() -> Result<Self, PortError> {
        Self::new(default_update_safety_endpoint()?)
    }

    pub fn new(endpoint: PathBuf) -> Result<Self, PortError> {
        validate_endpoint_shape(&endpoint)?;
        Ok(Self {
            endpoint,
            timeout: Duration::from_secs(1),
        })
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Result<Self, PortError> {
        if timeout.is_zero() || timeout > Duration::from_secs(10) {
            return Err(PortError::Operation(
                "Host update-safety timeout is invalid".to_owned(),
            ));
        }
        self.timeout = timeout;
        Ok(self)
    }

    pub fn endpoint(&self) -> &Path {
        &self.endpoint
    }

    fn read_snapshot(&self) -> Result<HostUpdateSafetySnapshotV1, PortError> {
        validate_endpoint_trust(&self.endpoint)?;
        let mut stream = UnixStream::connect(&self.endpoint).map_err(|_| {
            PortError::Operation("Host update-safety endpoint is unavailable".to_owned())
        })?;
        verify_peer_user(&stream)?;
        stream
            .set_read_timeout(Some(self.timeout))
            .map_err(|_| PortError::Operation("Host update-safety timeout setup failed".to_owned()))?;
        stream
            .set_write_timeout(Some(self.timeout))
            .map_err(|_| PortError::Operation("Host update-safety timeout setup failed".to_owned()))?;
        stream.write_all(REQUEST).map_err(|_| {
            PortError::Operation("Host update-safety request failed".to_owned())
        })?;
        stream.flush().map_err(|_| {
            PortError::Operation("Host update-safety request failed".to_owned())
        })?;
        std::net::Shutdown::Write;
        stream.shutdown(std::net::Shutdown::Write).map_err(|_| {
            PortError::Operation("Host update-safety request shutdown failed".to_owned())
        })?;

        let mut response = Vec::new();
        stream
            .take((MAX_RESPONSE_BYTES + 1) as u64)
            .read_to_end(&mut response)
            .map_err(|_| {
                PortError::Operation("Host update-safety response failed".to_owned())
            })?;
        if response.len() > MAX_RESPONSE_BYTES
            || response.last() != Some(&b'\n')
            || response[..response.len().saturating_sub(1)].contains(&b'\n')
        {
            return Err(PortError::Operation(
                "Host update-safety response framing is invalid".to_owned(),
            ));
        }
        response.pop();
        serde_json::from_slice::<HostUpdateSafetySnapshotV1>(&response).map_err(|_| {
            PortError::Operation("Host update-safety response is invalid".to_owned())
        })
    }
}

impl HostUpdateSafetySource for UnixHostUpdateSafetySource {
    fn snapshot(&self) -> Result<HostUpdateSafetySnapshotV1, PortError> {
        self.read_snapshot()
    }
}

pub fn default_update_safety_endpoint() -> Result<PathBuf, PortError> {
    let endpoint = match env::var_os("HERMES_UPDATE_SAFETY_SOCKET") {
        Some(value) if !value.is_empty() => PathBuf::from(value),
        Some(_) => {
            return Err(PortError::Operation(
                "HERMES_UPDATE_SAFETY_SOCKET is empty".to_owned(),
            ))
        }
        None => {
            let uid = unsafe { libc::geteuid() };
            PathBuf::from(format!("/tmp/hermes-update-safety-{uid}/host.sock"))
        }
    };
    validate_endpoint_shape(&endpoint)?;
    Ok(endpoint)
}

fn validate_endpoint_shape(endpoint: &Path) -> Result<(), PortError> {
    if !endpoint.is_absolute()
        || endpoint.as_os_str().as_bytes().len() > MAX_UDS_PATH_BYTES
        || endpoint.components().any(|component| {
            matches!(component, Component::ParentDir | Component::CurDir)
        })
        || endpoint.file_name().is_none()
    {
        return Err(PortError::Operation(
            "Host update-safety endpoint path is invalid".to_owned(),
        ));
    }
    Ok(())
}

fn validate_endpoint_trust(endpoint: &Path) -> Result<(), PortError> {
    let expected_uid = unsafe { libc::geteuid() };
    let parent = endpoint.parent().ok_or_else(|| {
        PortError::Operation("Host update-safety endpoint path is invalid".to_owned())
    })?;
    let parent_metadata = fs::symlink_metadata(parent).map_err(|_| {
        PortError::Operation("Host update-safety directory is unavailable".to_owned())
    })?;
    if !parent_metadata.file_type().is_dir()
        || parent_metadata.file_type().is_symlink()
        || parent_metadata.uid() != expected_uid
        || parent_metadata.mode() & 0o077 != 0
    {
        return Err(PortError::Operation(
            "Host update-safety directory is untrusted".to_owned(),
        ));
    }
    let socket_metadata = fs::symlink_metadata(endpoint).map_err(|_| {
        PortError::Operation("Host update-safety endpoint is unavailable".to_owned())
    })?;
    if !socket_metadata.file_type().is_socket()
        || socket_metadata.file_type().is_symlink()
        || socket_metadata.uid() != expected_uid
        || socket_metadata.mode() & 0o077 != 0
    {
        return Err(PortError::Operation(
            "Host update-safety endpoint is untrusted".to_owned(),
        ));
    }
    Ok(())
}

fn verify_peer_user(stream: &UnixStream) -> Result<(), PortError> {
    let expected_uid = unsafe { libc::geteuid() };
    let peer_uid = peer_uid(stream).map_err(|_| {
        PortError::Operation("Host update-safety peer identity is unavailable".to_owned())
    })?;
    if peer_uid != expected_uid {
        return Err(PortError::Operation(
            "Host update-safety peer identity is untrusted".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
fn peer_uid(stream: &UnixStream) -> std::io::Result<libc::uid_t> {
    let mut uid: libc::uid_t = 0;
    let mut gid: libc::gid_t = 0;
    let result = unsafe { libc::getpeereid(stream.as_raw_fd(), &mut uid, &mut gid) };
    if result == 0 {
        Ok(uid)
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(target_os = "linux")]
fn peer_uid(stream: &UnixStream) -> std::io::Result<libc::uid_t> {
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut credentials as *mut libc::ucred as *mut libc::c_void,
            &mut length,
        )
    };
    if result == 0 && length as usize == std::mem::size_of::<libc::ucred>() {
        Ok(credentials.uid)
    } else if result == 0 {
        Err(std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "invalid peer credential length",
        ))
    } else {
        Err(std::io::Error::last_os_error())
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
fn peer_uid(_stream: &UnixStream) -> std::io::Result<libc::uid_t> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "peer credentials are unsupported",
    ))
}
