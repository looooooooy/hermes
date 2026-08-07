#![cfg(windows)]

use crate::ports::PortError;
use crate::update_safe_window::{HostUpdateSafetySnapshotV1, HostUpdateSafetySource};
use std::env;
use std::ffi::c_void;
use std::ptr::{null, null_mut};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::Security::{
    EqualSid, GetTokenInformation, TokenUser, TOKEN_QUERY, TOKEN_USER,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, ReadFile, WriteFile, FILE_GENERIC_READ, FILE_GENERIC_WRITE, OPEN_EXISTING,
};
use windows_sys::Win32::System::Pipes::{
    GetNamedPipeServerProcessId, PeekNamedPipe, WaitNamedPipeW,
};
use windows_sys::Win32::System::Threading::{
    GetCurrentProcess, OpenProcess, OpenProcessToken, PROCESS_QUERY_LIMITED_INFORMATION,
};

const MAX_PIPE_NAME_CHARS: usize = 240;
const MAX_RESPONSE_BYTES: usize = 8_192;
const REQUEST: &[u8] = b"{\"method\":\"update-safety.snapshot\",\"schema_version\":1}\n";
const ERROR_FILE_NOT_FOUND_CODE: u32 = 2;
const ERROR_PIPE_BUSY_CODE: u32 = 231;
const ERROR_BROKEN_PIPE_CODE: u32 = 109;

#[link(name = "advapi32")]
unsafe extern "system" {
    fn ConvertSidToStringSidW(sid: *mut c_void, string_sid: *mut *mut u16) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

#[derive(Debug, Clone)]
pub struct WindowsHostUpdateSafetySource {
    pipe_name: String,
    timeout: Duration,
}

impl WindowsHostUpdateSafetySource {
    pub fn discover() -> Result<Self, PortError> {
        Self::new(default_update_safety_pipe_name()?)
    }

    pub fn new(pipe_name: String) -> Result<Self, PortError> {
        validate_pipe_name(&pipe_name)?;
        Ok(Self {
            pipe_name,
            timeout: Duration::from_secs(1),
        })
    }

    pub fn with_timeout(mut self, timeout: Duration) -> Result<Self, PortError> {
        if timeout.is_zero() || timeout > Duration::from_secs(10) {
            return Err(operation("Host update-safety timeout is invalid"));
        }
        self.timeout = timeout;
        Ok(self)
    }

    pub fn pipe_name(&self) -> &str {
        &self.pipe_name
    }

    fn read_snapshot(&self) -> Result<HostUpdateSafetySnapshotV1, PortError> {
        let pipe = connect_pipe(&self.pipe_name, self.timeout)?;
        verify_server_same_user(pipe.raw())?;
        write_all(pipe.raw(), REQUEST)?;
        let mut response = read_bounded_line(pipe.raw(), self.timeout)?;
        if response.last() != Some(&b'\n') {
            return Err(operation("Host update-safety response framing is invalid"));
        }
        response.pop();
        serde_json::from_slice::<HostUpdateSafetySnapshotV1>(&response)
            .map_err(|_| operation("Host update-safety response is invalid"))
    }
}

impl HostUpdateSafetySource for WindowsHostUpdateSafetySource {
    fn snapshot(&self) -> Result<HostUpdateSafetySnapshotV1, PortError> {
        self.read_snapshot()
    }
}

pub fn default_update_safety_pipe_name() -> Result<String, PortError> {
    let value = match env::var("HERMES_UPDATE_SAFETY_PIPE") {
        Ok(value) if !value.is_empty() => value,
        Ok(_) => return Err(operation("HERMES_UPDATE_SAFETY_PIPE is empty")),
        Err(env::VarError::NotPresent) => {
            format!(r"\\.\pipe\HermesUpdateSafety-{}", current_user_sid_string()?)
        }
        Err(env::VarError::NotUnicode(_)) => {
            return Err(operation("HERMES_UPDATE_SAFETY_PIPE is not Unicode"))
        }
    };
    validate_pipe_name(&value)?;
    Ok(value)
}

fn validate_pipe_name(value: &str) -> Result<(), PortError> {
    if !value.starts_with(r"\\.\pipe\")
        || value.len() > MAX_PIPE_NAME_CHARS
        || value.contains('\0')
        || value.ends_with('\\')
    {
        return Err(operation("Host update-safety pipe name is invalid"));
    }
    Ok(())
}

struct OwnedHandle(HANDLE);

impl OwnedHandle {
    fn new(handle: HANDLE) -> Result<Self, PortError> {
        if handle.is_null() || handle == INVALID_HANDLE_VALUE {
            Err(operation("Host update-safety pipe is unavailable"))
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

fn connect_pipe(name: &str, timeout: Duration) -> Result<OwnedHandle, PortError> {
    let wide_name = wide(name);
    let deadline = Instant::now() + timeout;
    loop {
        let handle = unsafe {
            CreateFileW(
                wide_name.as_ptr(),
                FILE_GENERIC_READ | FILE_GENERIC_WRITE,
                0,
                null(),
                OPEN_EXISTING,
                0,
                null_mut(),
            )
        };
        if !handle.is_null() && handle != INVALID_HANDLE_VALUE {
            return OwnedHandle::new(handle);
        }
        let error = unsafe { GetLastError() };
        if error != ERROR_PIPE_BUSY_CODE && error != ERROR_FILE_NOT_FOUND_CODE {
            return Err(operation("Host update-safety pipe connect failed"));
        }
        let now = Instant::now();
        if now >= deadline {
            return Err(operation("Host update-safety pipe connect timed out"));
        }
        let remaining_ms = deadline
            .saturating_duration_since(now)
            .as_millis()
            .clamp(1, u32::MAX as u128) as u32;
        unsafe { WaitNamedPipeW(wide_name.as_ptr(), remaining_ms.min(100)) };
        std::thread::sleep(Duration::from_millis(5));
    }
}

fn verify_server_same_user(pipe: HANDLE) -> Result<(), PortError> {
    let mut pid = 0u32;
    if unsafe { GetNamedPipeServerProcessId(pipe, &mut pid) } == 0 || pid == 0 {
        return Err(operation("Host update-safety server identity is unavailable"));
    }
    let process = OwnedHandle::new(unsafe {
        OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    })
    .map_err(|_| operation("Host update-safety server process is unavailable"))?;
    let server_token = open_process_token(process.raw())?;
    let local_token = open_process_token(unsafe { GetCurrentProcess() })?;
    if !same_token_user(server_token.raw(), local_token.raw())? {
        return Err(operation("Host update-safety server identity is untrusted"));
    }
    Ok(())
}

fn open_process_token(process: HANDLE) -> Result<OwnedHandle, PortError> {
    let mut token = null_mut();
    if unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token) } == 0 {
        Err(operation("Host update-safety token identity is unavailable"))
    } else {
        OwnedHandle::new(token)
    }
}

fn same_token_user(left: HANDLE, right: HANDLE) -> Result<bool, PortError> {
    let left_buffer = token_user_buffer(left)?;
    let right_buffer = token_user_buffer(right)?;
    let left_user = unsafe { &*(left_buffer.as_ptr().cast::<TOKEN_USER>()) };
    let right_user = unsafe { &*(right_buffer.as_ptr().cast::<TOKEN_USER>()) };
    if left_user.User.Sid.is_null() || right_user.User.Sid.is_null() {
        return Err(operation("Host update-safety TokenUser SID is null"));
    }
    Ok(unsafe { EqualSid(left_user.User.Sid, right_user.User.Sid) != 0 })
}

fn token_user_buffer(token: HANDLE) -> Result<Vec<u8>, PortError> {
    let mut required = 0u32;
    unsafe { GetTokenInformation(token, TokenUser, null_mut(), 0, &mut required) };
    if required == 0 || required > 64 * 1024 {
        return Err(operation("Host update-safety TokenUser size is invalid"));
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
        return Err(operation("Host update-safety TokenUser read failed"));
    }
    Ok(buffer)
}

fn current_user_sid_string() -> Result<String, PortError> {
    let token = open_process_token(unsafe { GetCurrentProcess() })?;
    let buffer = token_user_buffer(token.raw())?;
    let user = unsafe { &*(buffer.as_ptr().cast::<TOKEN_USER>()) };
    if user.User.Sid.is_null() {
        return Err(operation("Host update-safety TokenUser SID is null"));
    }
    let mut string_sid: *mut u16 = null_mut();
    if unsafe { ConvertSidToStringSidW(user.User.Sid, &mut string_sid) } == 0 || string_sid.is_null()
    {
        return Err(operation("Host update-safety SID conversion failed"));
    }
    let result = (|| {
        let mut length = 0usize;
        while length < 256 && unsafe { *string_sid.add(length) } != 0 {
            length += 1;
        }
        if length == 0 || length == 256 {
            return Err(operation("Host update-safety SID string is invalid"));
        }
        String::from_utf16(unsafe { std::slice::from_raw_parts(string_sid, length) })
            .map_err(|_| operation("Host update-safety SID string is invalid"))
    })();
    unsafe { LocalFree(string_sid.cast()) };
    result
}

fn read_bounded_line(pipe: HANDLE, timeout: Duration) -> Result<Vec<u8>, PortError> {
    let deadline = Instant::now() + timeout;
    let mut response = Vec::new();
    loop {
        if response.contains(&b'\n') {
            let newline = response.iter().position(|byte| *byte == b'\n').unwrap();
            if newline + 1 != response.len() {
                return Err(operation("Host update-safety response framing is invalid"));
            }
            return Ok(response);
        }
        if response.len() >= MAX_RESPONSE_BYTES || Instant::now() >= deadline {
            return Err(operation("Host update-safety response timed out or is oversized"));
        }
        let mut available = 0u32;
        if unsafe { PeekNamedPipe(pipe, null_mut(), 0, null_mut(), &mut available, null_mut()) } == 0
        {
            let error = unsafe { GetLastError() };
            if error == ERROR_BROKEN_PIPE_CODE {
                return Err(operation("Host update-safety response ended early"));
            }
            return Err(operation("Host update-safety response is unavailable"));
        }
        if available == 0 {
            std::thread::sleep(Duration::from_millis(5));
            continue;
        }
        let remaining = MAX_RESPONSE_BYTES - response.len();
        let chunk_len = remaining.min(available as usize).min(512);
        let mut chunk = vec![0u8; chunk_len];
        let mut read = 0u32;
        if unsafe {
            ReadFile(
                pipe,
                chunk.as_mut_ptr().cast(),
                chunk_len as u32,
                &mut read,
                null_mut(),
            )
        } == 0
        {
            return Err(operation("Host update-safety response read failed"));
        }
        if read == 0 {
            return Err(operation("Host update-safety response ended early"));
        }
        response.extend_from_slice(&chunk[..read as usize]);
    }
}

fn write_all(pipe: HANDLE, payload: &[u8]) -> Result<(), PortError> {
    let mut offset = 0usize;
    while offset < payload.len() {
        let mut written = 0u32;
        let remaining = u32::try_from(payload.len() - offset)
            .map_err(|_| operation("Host update-safety request is oversized"))?;
        if unsafe {
            WriteFile(
                pipe,
                payload[offset..].as_ptr().cast(),
                remaining,
                &mut written,
                null_mut(),
            )
        } == 0
        {
            return Err(operation("Host update-safety request failed"));
        }
        if written == 0 {
            return Err(operation("Host update-safety request failed"));
        }
        offset += written as usize;
    }
    Ok(())
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

fn operation(message: &str) -> PortError {
    PortError::Operation(message.to_owned())
}
