#![cfg(windows)]

use crate::ipc::{
    decode_frame, encode_frame, IpcCodecError, ManagerEnvelopeV1, ManagerRequestV1,
    ManagerResponseV1, MAX_MANAGER_FRAME_BYTES,
};
use crate::local_ipc::dispatch_read_only;
use crate::RuntimeManager;
use std::ptr::{null, null_mut};
use std::sync::Arc;
use thiserror::Error;
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_PIPE_CONNECTED, HANDLE, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::Security::{
    EqualSid, GetTokenInformation, RevertToSelf, TokenUser, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, WriteFile, FILE_GENERIC_READ, FILE_GENERIC_WRITE, OPEN_EXISTING,
};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, GetNamedPipeClientProcessId,
    ImpersonateNamedPipeClient, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE,
    PIPE_WAIT,
};
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, GetCurrentThread, OpenProcessToken, OpenThreadToken,
};

// Win32 `PIPE_ACCESS_DUPLEX` is defined as 0x00000003. Keeping this one open-mode
// constant local avoids depending on a windows-sys re-export location that has moved
// across generated projections while preserving the documented Win32 contract.
const PIPE_ACCESS_DUPLEX_MODE: u32 = 0x0000_0003;

#[derive(Debug, Error)]
pub enum WindowsPipeError {
    #[error("Windows Named Pipe API failed: {0}")]
    Win32(u32),
    #[error(transparent)]
    Codec(#[from] IpcCodecError),
    #[error("Windows Named Pipe peer identity failed: {0}")]
    Identity(&'static str),
    #[error("Windows Named Pipe response request id mismatch")]
    RequestId,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WindowsPipePeerIdentity {
    pub pid: u32,
    pub same_user: bool,
}

struct OwnedHandle(HANDLE);

unsafe impl Send for OwnedHandle {}
unsafe impl Sync for OwnedHandle {}

impl OwnedHandle {
    fn new(handle: HANDLE) -> Result<Self, WindowsPipeError> {
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            Err(last_error())
        } else {
            Ok(Self(handle))
        }
    }

    fn raw(&self) -> HANDLE {
        self.0
    }
}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe {
                CloseHandle(self.0);
            }
        }
    }
}

pub struct ReadOnlyWindowsPipeServer {
    pipe: OwnedHandle,
    manager: Arc<RuntimeManager>,
}

impl ReadOnlyWindowsPipeServer {
    pub fn new(name: &str, manager: Arc<RuntimeManager>) -> Result<Self, WindowsPipeError> {
        if !name.starts_with(r"\\.\pipe\") || name.len() > 220 || name.contains('\0') {
            return Err(WindowsPipeError::Identity("invalid Named Pipe name"));
        }
        let name = wide(name);
        // Create the server instance synchronously here rather than inside
        // `serve_once`. This removes a real client/server startup race: after `new`
        // succeeds the named pipe namespace entry already exists, so a client may
        // connect immediately while the serving thread enters ConnectNamedPipe.
        let pipe = create_pipe(&name)?;
        Ok(Self { pipe, manager })
    }

    pub fn serve_once(&self) -> Result<WindowsPipePeerIdentity, WindowsPipeError> {
        connect_pipe(self.pipe.raw())?;
        let pid = client_pid(self.pipe.raw())?;
        let envelope: ManagerEnvelopeV1<ManagerRequestV1> = read_envelope(self.pipe.raw())?;
        let same_user = verify_client_same_user(self.pipe.raw())?;
        if !same_user {
            unsafe {
                DisconnectNamedPipe(self.pipe.raw());
            }
            return Err(WindowsPipeError::Identity(
                "Named Pipe client SID does not match Runtime Manager user SID",
            ));
        }
        let response = dispatch_read_only(&self.manager, envelope.body);
        write_envelope(
            self.pipe.raw(),
            &ManagerEnvelopeV1::new(envelope.request_id, response),
        )?;
        unsafe {
            DisconnectNamedPipe(self.pipe.raw());
        }
        Ok(WindowsPipePeerIdentity { pid, same_user })
    }
}

pub fn request_read_only(
    name: &str,
    request_id: &str,
    request: ManagerRequestV1,
) -> Result<ManagerResponseV1, WindowsPipeError> {
    let name = wide(name);
    let handle = unsafe {
        CreateFileW(
            name.as_ptr(),
            FILE_GENERIC_READ | FILE_GENERIC_WRITE,
            0,
            null(),
            OPEN_EXISTING,
            0,
            null_mut(),
        )
    };
    let pipe = OwnedHandle::new(handle)?;
    write_envelope(
        pipe.raw(),
        &ManagerEnvelopeV1::new(request_id, request),
    )?;
    let response: ManagerEnvelopeV1<ManagerResponseV1> = read_envelope(pipe.raw())?;
    if response.request_id != request_id {
        return Err(WindowsPipeError::RequestId);
    }
    Ok(response.body)
}

fn create_pipe(name: &[u16]) -> Result<OwnedHandle, WindowsPipeError> {
    let handle = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_DUPLEX_MODE,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            (MAX_MANAGER_FRAME_BYTES + 4) as u32,
            (MAX_MANAGER_FRAME_BYTES + 4) as u32,
            0,
            null(),
        )
    };
    OwnedHandle::new(handle)
}

fn connect_pipe(pipe: HANDLE) -> Result<(), WindowsPipeError> {
    let connected = unsafe { ConnectNamedPipe(pipe, null_mut()) };
    if connected != 0 {
        return Ok(());
    }
    let error = unsafe { GetLastError() };
    if error == ERROR_PIPE_CONNECTED {
        Ok(())
    } else {
        Err(WindowsPipeError::Win32(error))
    }
}

fn client_pid(pipe: HANDLE) -> Result<u32, WindowsPipeError> {
    let mut pid = 0u32;
    if unsafe { GetNamedPipeClientProcessId(pipe, &mut pid) } == 0 {
        Err(last_error())
    } else if pid == 0 {
        Err(WindowsPipeError::Identity("Named Pipe client PID is zero"))
    } else {
        Ok(pid)
    }
}

fn verify_client_same_user(pipe: HANDLE) -> Result<bool, WindowsPipeError> {
    // Identity is checked after a complete bounded request frame is read but before
    // any request is dispatched. The impersonation is reverted on every exit path.
    if unsafe { ImpersonateNamedPipeClient(pipe) } == 0 {
        return Err(last_error());
    }
    let result = (|| {
        let client_token = open_thread_token()?;
        let server_token = open_process_token()?;
        same_token_user(client_token.raw(), server_token.raw())
    })();
    let reverted = unsafe { RevertToSelf() };
    if reverted == 0 {
        return Err(last_error());
    }
    result
}

fn open_thread_token() -> Result<OwnedHandle, WindowsPipeError> {
    let mut token = null_mut();
    if unsafe { OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, 1, &mut token) } == 0 {
        Err(last_error())
    } else {
        OwnedHandle::new(token)
    }
}

fn open_process_token() -> Result<OwnedHandle, WindowsPipeError> {
    let mut token = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        Err(last_error())
    } else {
        OwnedHandle::new(token)
    }
}

fn same_token_user(left: HANDLE, right: HANDLE) -> Result<bool, WindowsPipeError> {
    let left_buffer = token_user_buffer(left)?;
    let right_buffer = token_user_buffer(right)?;
    let left_user = unsafe { &*(left_buffer.as_ptr().cast::<TOKEN_USER>()) };
    let right_user = unsafe { &*(right_buffer.as_ptr().cast::<TOKEN_USER>()) };
    if left_user.User.Sid.is_null() || right_user.User.Sid.is_null() {
        return Err(WindowsPipeError::Identity("TokenUser SID is null"));
    }
    Ok(unsafe { EqualSid(left_user.User.Sid, right_user.User.Sid) != 0 })
}

fn token_user_buffer(token: HANDLE) -> Result<Vec<u8>, WindowsPipeError> {
    let mut required = 0u32;
    unsafe {
        GetTokenInformation(token, TokenUser, null_mut(), 0, &mut required);
    }
    if required == 0 || required > 64 * 1024 {
        return Err(WindowsPipeError::Identity("invalid TokenUser size"));
    }
    let mut buffer = vec![0u8; required as usize];
    if unsafe {
        GetTokenInformation(
            token,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            required,
            &mut required,
        )
    } == 0
    {
        return Err(last_error());
    }
    Ok(buffer)
}

fn read_envelope<T: serde::de::DeserializeOwned>(
    pipe: HANDLE,
) -> Result<ManagerEnvelopeV1<T>, WindowsPipeError> {
    let mut prefix = [0u8; 4];
    read_exact(pipe, &mut prefix)?;
    let payload_len = u32::from_be_bytes(prefix) as usize;
    if payload_len > MAX_MANAGER_FRAME_BYTES {
        return Err(WindowsPipeError::Codec(IpcCodecError::TooLarge));
    }
    let mut payload = vec![0u8; payload_len];
    read_exact(pipe, &mut payload)?;
    let mut frame = Vec::with_capacity(4 + payload_len);
    frame.extend_from_slice(&prefix);
    frame.extend_from_slice(&payload);
    Ok(decode_frame(&frame)?)
}

fn write_envelope<T: serde::Serialize>(
    pipe: HANDLE,
    envelope: &ManagerEnvelopeV1<T>,
) -> Result<(), WindowsPipeError> {
    let frame = encode_frame(envelope)?;
    write_all(pipe, &frame)
}

fn read_exact(pipe: HANDLE, buffer: &mut [u8]) -> Result<(), WindowsPipeError> {
    let mut offset = 0usize;
    while offset < buffer.len() {
        let mut read = 0u32;
        let remaining = u32::try_from(buffer.len() - offset)
            .map_err(|_| WindowsPipeError::Identity("read size overflow"))?;
        if unsafe {
            ReadFile(
                pipe,
                buffer[offset..].as_mut_ptr().cast(),
                remaining,
                &mut read,
                null_mut(),
            )
        } == 0
        {
            return Err(last_error());
        }
        if read == 0 {
            return Err(WindowsPipeError::Identity("unexpected pipe EOF"));
        }
        offset += read as usize;
    }
    Ok(())
}

fn write_all(pipe: HANDLE, buffer: &[u8]) -> Result<(), WindowsPipeError> {
    let mut offset = 0usize;
    while offset < buffer.len() {
        let mut written = 0u32;
        let remaining = u32::try_from(buffer.len() - offset)
            .map_err(|_| WindowsPipeError::Identity("write size overflow"))?;
        if unsafe {
            WriteFile(
                pipe,
                buffer[offset..].as_ptr().cast(),
                remaining,
                &mut written,
                null_mut(),
            )
        } == 0
        {
            return Err(last_error());
        }
        if written == 0 {
            return Err(WindowsPipeError::Identity("zero-byte pipe write"));
        }
        offset += written as usize;
    }
    Ok(())
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

fn last_error() -> WindowsPipeError {
    WindowsPipeError::Win32(unsafe { GetLastError() })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::platform::{DefaultInstallLayout, FailClosedServiceManager};
    use std::thread;

    #[test]
    fn named_pipe_status_round_trip_returns_client_pid_and_same_user() {
        let name = format!(
            r"\\.\pipe\HermesRuntimeManagerTest-{}",
            std::process::id()
        );
        let manager = Arc::new(RuntimeManager::new(
            Arc::new(FailClosedServiceManager),
            Arc::new(DefaultInstallLayout::discover().expect("layout")),
        ));
        // Server creation binds the pipe synchronously, so the client can connect
        // immediately without a scheduler-dependent sleep or retry loop.
        let server = ReadOnlyWindowsPipeServer::new(&name, manager).expect("server");
        let server_thread = thread::spawn(move || server.serve_once().expect("serve"));

        let response = request_read_only(&name, "win-status-1", ManagerRequestV1::Status)
            .expect("Named Pipe request");
        let identity = server_thread.join().expect("server thread");

        assert!(matches!(response, ManagerResponseV1::Snapshot(_)));
        assert_eq!(identity.pid, std::process::id());
        assert!(identity.same_user);
    }
}
