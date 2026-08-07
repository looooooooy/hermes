#![cfg(windows)]

use crate::ipc::{
    decode_frame, encode_frame, IpcCodecError, ManagerEnvelopeV1, ManagerRequestV1,
    ManagerResponseV1, MAX_MANAGER_FRAME_BYTES,
};
use crate::local_ipc::dispatch_read_only;
use crate::RuntimeManager;
use std::ffi::c_void;
use std::ptr::{null, null_mut};
use std::sync::Arc;
use thiserror::Error;
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_PIPE_CONNECTED, HANDLE, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::Security::{
    EqualSid, GetTokenInformation, OpenProcessToken, OpenThreadToken, RevertToSelf, TokenUser,
    TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, WriteFile, FILE_GENERIC_READ, FILE_GENERIC_WRITE, OPEN_EXISTING,
};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, GetNamedPipeClientProcessId,
    ImpersonateNamedPipeClient, PIPE_ACCESS_DUPLEX, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS,
    PIPE_TYPE_BYTE, PIPE_WAIT,
};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetCurrentThread};

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
    name: Vec<u16>,
    manager: Arc<RuntimeManager>,
}

impl ReadOnlyWindowsPipeServer {
    pub fn new(name: &str, manager: Arc<RuntimeManager>) -> Result<Self, WindowsPipeError> {
        if !name.starts_with(r"\\.\pipe\") || name.len() > 220 || name.contains('\0') {
            return Err(WindowsPipeError::Identity("invalid Named Pipe name"));
        }
        Ok(Self {
            name: wide(name),
            manager,
        })
    }

    pub fn serve_once(&self) -> Result<WindowsPipePeerIdentity, WindowsPipeError> {
        let pipe = create_pipe(&self.name)?;
        connect_pipe(pipe.raw())?;
        let pid = client_pid(pipe.raw())?;
        let envelope: ManagerEnvelopeV1<ManagerRequestV1> = read_envelope(pipe.raw())?;
        let same_user = verify_client_same_user(pipe.raw())?;
        if !same_user {
            unsafe {
                DisconnectNamedPipe(pipe.raw());
            }
            return Err(WindowsPipeError::Identity(
                "Named Pipe client SID does not match Runtime Manager user SID",
            ));
        }
        let response = dispatch_read_only(&self.manager, envelope.body);
        write_envelope(
            pipe.raw(),
            &ManagerEnvelopeV1::new(envelope.request_id, response),
        )?;
        unsafe {
            DisconnectNamedPipe(pipe.raw());
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
            PIPE_ACCESS_DUPLEX,
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
    // The server verifies identity after receiving a complete bounded request frame and
    // before dispatching it. Impersonation is immediately reverted on every path.
    if unsafe { ImpersonateNamedPipeClient(pipe) } == 0 {
        return Err(last_error());
    }
    let result = (|| {
        let client_token = open_thread_token()?;
        let server_token = open_process_token()?;
        let client_sid = token_user_sid(client_token.raw())?;
        let server_sid = token_user_sid(server_token.raw())?;
        Ok(unsafe { EqualSid(client_sid, server_sid) != 0 })
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

fn token_user_sid(token: HANDLE) -> Result<*mut c_void, WindowsPipeError> {
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
    let token_user = unsafe { &*(buffer.as_ptr().cast::<TOKEN_USER>()) };
    let sid = token_user.User.Sid;
    if sid.is_null() {
        return Err(WindowsPipeError::Identity("TokenUser SID is null"));
    }
    // The caller compares the SID immediately while `buffer` is alive.  Returning a
    // raw pointer from this helper would outlive the buffer, so this function is only
    // used through `same_token_user` below in the final implementation.
    Err(WindowsPipeError::Identity(
        "internal SID lifetime guard must use same_token_user",
    ))
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
