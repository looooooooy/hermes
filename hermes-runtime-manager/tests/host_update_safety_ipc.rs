#![cfg(unix)]

use hermes_runtime_manager::{HostUpdateSafetySource, UnixHostUpdateSafetySource};
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::Duration;

const REQUEST: &[u8] = b"{\"method\":\"update-safety.snapshot\",\"schema_version\":1}\n";
static NEXT_ID: AtomicU64 = AtomicU64::new(1);

fn private_root() -> PathBuf {
    let id = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let root = PathBuf::from(format!("/tmp/hus-{}-{id}", std::process::id()));
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    root
}

fn listener(root: &PathBuf) -> (PathBuf, UnixListener) {
    let endpoint = root.join("host.sock");
    let listener = UnixListener::bind(&endpoint).unwrap();
    fs::set_permissions(&endpoint, fs::Permissions::from_mode(0o600)).unwrap();
    (endpoint, listener)
}

fn spawn_one_response(
    listener: UnixListener,
    response: Vec<u8>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut request = Vec::new();
        let mut byte = [0_u8; 1];
        while request.last() != Some(&b'\n') {
            stream.read_exact(&mut byte).unwrap();
            request.push(byte[0]);
            assert!(request.len() <= 1_024);
        }
        assert_eq!(request, REQUEST);
        stream.write_all(&response).unwrap();
    })
}

fn cleanup(root: PathBuf) {
    let endpoint = root.join("host.sock");
    let _ = fs::remove_file(endpoint);
    let _ = fs::remove_dir(root);
}

#[test]
fn reads_strict_shared_snapshot_from_same_user_peer() {
    let root = private_root();
    let (endpoint, listener) = listener(&root);
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
    let server = spawn_one_response(listener, response);

    let snapshot = UnixHostUpdateSafetySource::new(endpoint)
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
    cleanup(root);
}

#[test]
fn body_free_error_response_fails_closed() {
    let root = private_root();
    let (endpoint, listener) = listener(&root);
    let server = spawn_one_response(
        listener,
        b"{\"error\":\"unavailable\",\"schema_version\":1}\n".to_vec(),
    );

    let error = UnixHostUpdateSafetySource::new(endpoint)
        .unwrap()
        .snapshot()
        .unwrap_err();

    assert!(error.to_string().contains("response is invalid"));
    server.join().unwrap();
    cleanup(root);
}

#[test]
fn unknown_response_fields_are_rejected() {
    let root = private_root();
    let (endpoint, listener) = listener(&root);
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
    let server = spawn_one_response(listener, response);

    let error = UnixHostUpdateSafetySource::new(endpoint)
        .unwrap()
        .snapshot()
        .unwrap_err();

    assert!(error.to_string().contains("response is invalid"));
    server.join().unwrap();
    cleanup(root);
}

#[test]
fn oversized_response_is_rejected_before_json_parsing() {
    let root = private_root();
    let (endpoint, listener) = listener(&root);
    let mut response = vec![b'x'; 8_193];
    response.push(b'\n');
    let server = spawn_one_response(listener, response);

    let error = UnixHostUpdateSafetySource::new(endpoint)
        .unwrap()
        .snapshot()
        .unwrap_err();

    assert!(error.to_string().contains("framing is invalid"));
    server.join().unwrap();
    cleanup(root);
}

#[test]
fn world_accessible_parent_is_rejected_before_connect() {
    let root = private_root();
    let (endpoint, listener) = listener(&root);
    fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();

    let error = UnixHostUpdateSafetySource::new(endpoint)
        .unwrap()
        .snapshot()
        .unwrap_err();

    assert!(error.to_string().contains("directory is untrusted"));
    drop(listener);
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    cleanup(root);
}

#[test]
fn relative_or_parent_traversal_endpoint_is_rejected() {
    assert!(UnixHostUpdateSafetySource::new(PathBuf::from("host.sock")).is_err());
    assert!(UnixHostUpdateSafetySource::new(PathBuf::from("/tmp/a/../host.sock")).is_err());
}
