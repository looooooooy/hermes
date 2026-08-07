#![cfg(windows)]

use hermes_runtime_manager::{HostUpdateSafetySource, WindowsHostUpdateSafetySource};
use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::Duration;
use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_PIPE_CONNECTED, HANDLE, INVALID_HANDLE_VALUE,
};
use windows_sys::Win32::Storage::FileSystem::{
    FlushFileBuffers, ReadFile, WriteFile,
};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, PIPE_READMODE_BYTE,
    PIPE_REJECT_REMOTE_CLIENTS, PIPE_TYPE_BYTE, PIPE_WAIT,
};

const PIPE_ACCESS_DUPLEX: u32 = 0x0000_0003;
const REQUEST: &[u8] = b"{\"method\":\"update-safety.snapshot\",\"schema_version\":1}\n";
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

struct OwnedHandle(HANDLE);

unsafe impl Send for OwnedHandle {}

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_null() && self.0 != INVALID_HANDLE_VALUE {
            unsafe { CloseHandle(self.0) };
        }
    }
}

fn pipe_name(label: &str) -> String {
    let id = NEXT_ID.fetch_add(1, Ordering::SeqCst);
    format!(
        r"\\.\pipe\HermesUpdateSafetyRustTest-{}-{id}-{label}",
        std::process::id()
    )
}

fn spawn_server(name: String, response: Vec<u8>) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let wide_name = wide(&name);
        let raw = unsafe {
            CreateNamedPipeW(
                wide_name.as_ptr(),
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                1,
                8_192,
                1_024,
                1_000,
                null(),
            )
        };
        assert!(!raw.is_null() && raw != INVALID_HANDLE_VALUE);
        let pipe = OwnedHandle(raw);
        let connected = unsafe { ConnectNamedPipe(pipe.0, null_mut()) };
        if connected == 0 {
            assert_eq!(unsafe { GetLastError() }, ERROR_PIPE_CONNECTED);
        }
        let mut request = vec![0u8; REQUEST.len()];
        read_exact(pipe.0, &mut request);
        assert_eq!(request, REQUEST);
        write_all(pipe.0, &response);
        assert_ne!(unsafe { FlushFileBuffers(pipe.0) }, 0);
        unsafe { DisconnectNamedPipe(pipe.0) };
    })
}

#[test]
fn reads_strict_snapshot_from_same_user_server() {
    let name = pipe_name("valid");
    let response = concat!(
        "{\"schema_version\":1,",
        "\"profile\":\"default\",",
        "\"runtime_generation\":\"generation-42\",",
        "\"active_tasks\":2,",
        "\"pending_approvals\":1,",
        "\"pending_clarifications\":3,",
        "\"evidence_complete\":true}\n"
    )
    .as_bytes()
    .to_vec();
    let server = spawn_server(name.clone(), response);
    thread::sleep(Duration::from_millis(50));

    let snapshot = WindowsHostUpdateSafetySource::new(name)
        .unwrap()
        .snapshot()
        .unwrap();

    assert_eq!(snapshot.profile, "default");
    assert_eq!(snapshot.runtime_generation, "generation-42");
    assert_eq!(snapshot.active_tasks, 2);
    assert_eq!(snapshot.pending_approvals, 1);
    assert_eq!(snapshot.pending_clarifications, 3);
    assert!(snapshot.evidence_complete);
    server.join().unwrap();
}

#[test]
fn generic_error_frame_fails_closed() {
    let name = pipe_name("error");
    let server = spawn_server(
        name.clone(),
        b"{\"error\":\"unavailable\",\"schema_version\":1}\n".to_vec(),
    );
    thread::sleep(Duration::from_millis(50));

    let error = WindowsHostUpdateSafetySource::new(name)
        .unwrap()
        .snapshot()
        .unwrap_err();

    assert!(error.to_string().contains("response is invalid"));
    server.join().unwrap();
}

#[test]
fn unknown_response_fields_are_rejected() {
    let name = pipe_name("unknown");
    let response = concat!(
        "{\"schema_version\":1,",
        "\"profile\":\"default\",",
        "\"runtime_generation\":\"generation-42\",",
        "\"active_tasks\":0,",
        "\"pending_approvals\":0,",
        "\"pending_clarifications\":0,",
        "\"evidence_complete\":true,",
        "\"session_key\":\"must-not-cross-boundary\"}\n"
    )
    .as_bytes()
    .to_vec();
    let server = spawn_server(name.clone(), response);
    thread::sleep(Duration::from_millis(50));

    let error = WindowsHostUpdateSafetySource::new(name)
        .unwrap()
        .snapshot()
        .unwrap_err();

    assert!(error.to_string().contains("response is invalid"));
    server.join().unwrap();
}

#[test]
fn invalid_or_missing_pipe_fails_closed() {
    assert!(WindowsHostUpdateSafetySource::new("relative-pipe".to_owned()).is_err());
    let name = pipe_name("missing");
    let error = WindowsHostUpdateSafetySource::new(name)
        .unwrap()
        .with_timeout(Duration::from_millis(50))
        .unwrap()
        .snapshot()
        .unwrap_err();
    assert!(error.to_string().contains("timed out"));
}

#[test]
fn python_relay_cross_language_contract_when_configured() {
    let Ok(name) = std::env::var("HERMES_UPDATE_SAFETY_CROSS_LANGUAGE_PIPE") else {
        return;
    };
    let snapshot = WindowsHostUpdateSafetySource::new(name)
        .unwrap()
        .snapshot()
        .unwrap();
    assert_eq!(snapshot.profile, "default");
    assert_eq!(snapshot.runtime_generation, "generation-python-42");
    assert_eq!(snapshot.active_tasks, 2);
    assert_eq!(snapshot.pending_approvals, 1);
    assert_eq!(snapshot.pending_clarifications, 3);
    assert!(snapshot.evidence_complete);
}

fn read_exact(pipe: HANDLE, buffer: &mut [u8]) {
    let mut offset = 0usize;
    while offset < buffer.len() {
        let mut read = 0u32;
        let remaining = (buffer.len() - offset) as u32;
        assert_ne!(
            unsafe {
                ReadFile(
                    pipe,
                    buffer[offset..].as_mut_ptr().cast(),
                    remaining,
                    &mut read,
                    null_mut(),
                )
            },
            0
        );
        assert!(read > 0);
        offset += read as usize;
    }
}

fn write_all(pipe: HANDLE, buffer: &[u8]) {
    let mut offset = 0usize;
    while offset < buffer.len() {
        let mut written = 0u32;
        let remaining = (buffer.len() - offset) as u32;
        assert_ne!(
            unsafe {
                WriteFile(
                    pipe,
                    buffer[offset..].as_ptr().cast(),
                    remaining,
                    &mut written,
                    null_mut(),
                )
            },
            0
        );
        assert!(written > 0);
        offset += written as usize;
    }
}

fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}
