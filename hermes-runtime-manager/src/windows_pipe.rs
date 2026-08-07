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
    EqualSid, GetTokenInformation, RevertToSelf, TokenUser, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FlushFileBuffers, ReadFile, WriteFile, FILE_GENERIC_READ, FILE_GENERIC_WRITE,
    OPEN_EXISTING,
};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, GetNamedPipeClientProcessId,
    ImpersonateNamedPipeClient, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE,
    PIPE_WAIT,
};
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, GetCurrentThread, OpenProcessToken, OpenThreadToken,
};

const PIPE_ACCESS_DUPLEX_MODE: u32 = 0x0000_0003;
const SDDL_REVISION_1: u32 = 1;

#[link(name = "advapi32")]
unsafe extern "system" {
    fn ConvertSidToStringSidW(sid: *mut c_void, string_sid: *mut *mut u16) -> i32;
    fn ConvertStringSecurityDescriptorToSecurityDescriptorW(
        string_security_descriptor: *const u16,
        string_sd_revision: u32,
        security_descriptor: *mut *mut c_void,
        security_descriptor_size: *mut u32,
    ) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

#[repr(C)]
struct RawSecurityAttributes {
    n_length: u32,
    security_descriptor: *mut c_void,
    inherit_handle: i32,
}

struct PipeSecurity {
    descriptor: *mut c_void,
    attributes: RawSecurityAttributes,
    user_sid: String,
}

impl PipeSecurity {
    fn current_user() -> Result<Self, WindowsPipeError> {
        let token = open_process_token()?;
        let user_buffer = token_user_buffer(token.raw())?;
        let user = unsafe { &*(user_buffer.as_ptr().cast::<TOKEN_USER>()) };
        if user.User.Sid.is_null() {
            return Err(WindowsPipeError::Identity("TokenUser SID is null"));
        }
        let user_sid = sid_to_string(user.User.Sid)?;
        let sddl = format!("D:P(A;;GA;;;{user_sid})");
        let sddl_wide = wide(&sddl);
        let mut descriptor = null_mut();
        let mut descriptor_size = 0u32;
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl_wide.as_ptr(),
                SDDL_REVISION_1,
                &mut descriptor,
                &mut descriptor_size,
            )
        } == 0
        {
            return Err(last_error());
        }
        if descriptor.is_null() || descriptor_size == 0 {
            if !descriptor.is_null() {
                unsafe { LocalFree(descriptor) };
            }
            return Err(WindowsPipeError::Identity(
                "current-user pipe security descriptor is empty",
            ));
        }
        Ok(Self {
            descriptor,
            attributes: RawSecurityAttributes {
                n_length: std::mem::size_of::<RawSecurityAttributes>() as u32,
                security_descriptor: descriptor,
                inherit_handle: 0,
            },
            user_sid,
        })
    }

    fn attributes_ptr(&self) -> *const RawSecurityAttributes {
        &self.attributes
    }
}

impl Drop for PipeSecurity {
    fn drop(&mut self) {
        if !self.descriptor.is_null() {
            unsafe { LocalFree(self.descriptor) };
            self.descriptor = null_mut();
        }
    }
}

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
            unsafe { CloseHandle(self.0) };
        }
    }
}

pub fn current_user_pipe_name() -> Result<String, WindowsPipeError> {
    let security = PipeSecurity::current_user()?;
    Ok(format!(r"\\.\pipe\HermesRuntimeManager-{}", security.user_sid))
}

pub struct ReadOnlyWindowsPipeServer {
    pipe: OwnedHandle,
    manager: Arc<RuntimeManager>,
}

impl ReadOnlyWindowsPipeServer {
    pub fn new(name: &str, manager: Arc<RuntimeManager>) -> Result<Self, WindowsPipeError> {
        if !name.starts_with(r"\\.\pipe\") || name.len() > 240 || name.contains('\0') {
            return Err(WindowsPipeError::Identity("invalid Named Pipe name"));
        }
        let name = wide(name);
        let pipe = create_pipe(&name)?;
        Ok(Self { pipe, manager })
    }

    pub fn serve_once(&self) -> Result<WindowsPipePeerIdentity, WindowsPipeError> {
        connect_pipe(self.pipe.raw())?;
        let pid = client_pid(self.pipe.raw())?;
        let envelope: ManagerEnvelopeV1<ManagerRequestV1> = read_envelope(self.pipe.raw())?;
        let same_user = verify_client_same_user(self.pipe.raw())?;
        if !same_user {
            unsafe { DisconnectNamedPipe(self.pipe.raw()) };
            return Err(WindowsPipeError::Identity(
                "Named Pipe client SID does not match Runtime Manager user SID",
            ));
        }
        let response = dispatch_read_only(&self.manager, envelope.body);
        write_envelope(
            self.pipe.raw(),
            &ManagerEnvelopeV1::new(envelope.request_id, response),
        )?;
        // Flush on the server end before disconnecting. Windows documents this as the
        // synchronous handoff that prevents the client from observing ERROR_NO_DATA
        // after a response has been written but the server disconnects too quickly.
        if unsafe { FlushFileBuffers(self.pipe.raw()) } == 0 {
            unsafe { DisconnectNamedPipe(self.pipe.raw()) };
            return Err(last_error());
        }
        unsafe { DisconnectNamedPipe(self.pipe.raw()) };
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
    let security = PipeSecurity::current_user()?;
    let handle = unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            PIPE_ACCESS_DUPLEX_MODE,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            (MAX_MANAGER_FRAME_BYTES + 4) as u32,
            (MAX_MANAGER_FRAME_BYTES + 4) as u32,
            0,
            security.attributes_ptr().cast(),
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
    unsafe { GetTokenInformation(token, TokenUser, null_mut(), 0, &mut required) };
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

fn sid_to_string(sid: *mut c_void) -> Result<String, WindowsPipeError> {
    let mut string_sid: *mut u16 = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut string_sid) } == 0 {
        return Err(last_error());
    }
    if string_sid.is_null() {
        return Err(WindowsPipeError::Identity("string SID is null"));
    }
    let result = (|| {
        let mut length = 0usize;
        while length < 256 && unsafe { *string_sid.add(length) } != 0 {
            length += 1;
        }
        if length == 0 || length == 256 {
            return Err(WindowsPipeError::Identity("string SID length is invalid"));
        }
        String::from_utf16(unsafe { std::slice::from_raw_parts(string_sid, length) })
            .map_err(|_| WindowsPipeError::Identity("string SID is invalid UTF-16"))
    })();
    unsafe { LocalFree(string_sid.cast()) };
    result
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
    fn current_user_pipe_name_is_sid_scoped() {
        let name = current_user_pipe_name().expect("pipe name");
        assert!(name.starts_with(r"\\.\pipe\HermesRuntimeManager-S-1-"));
    }

    #[test]
    fn named_pipe_status_round_trip_returns_client_pid_and_same_user() {
        let base = current_user_pipe_name().expect("pipe name");
        let name = format!("{base}-test-{}", std::process::id());
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
