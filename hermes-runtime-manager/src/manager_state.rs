use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

const SCHEMA_VERSION: u8 = 1;
const FILE_NAME: &str = "runtime-manager-state-v1.json";
const MAX_STATE_BYTES: u64 = 64 * 1024;
const MAX_RELEASE_ID_BYTES: usize = 160;
const MAX_GENERATION_BYTES: usize = 128;
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, thiserror::Error)]
pub enum ManagerStateError {
    #[error("Runtime Manager state path is unsafe: {0}")]
    UnsafePath(String),
    #[error("Runtime Manager state I/O failed: {0}")]
    Io(#[from] std::io::Error),
    #[error("Runtime Manager state is invalid: {0}")]
    Invalid(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RestoredManagerState {
    pub active_release: Option<String>,
    pub previous_release: Option<String>,
    pub runtime_generation: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ManagerStateFile {
    root: PathBuf,
    path: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ManagerStateV1 {
    schema_version: u8,
    active_release: Option<String>,
    previous_release: Option<String>,
    runtime_generation: Option<String>,
}

impl ManagerStateFile {
    pub fn new(root: PathBuf) -> Result<Self, ManagerStateError> {
        if !root.is_absolute() {
            return Err(ManagerStateError::UnsafePath(
                "state root must be absolute".to_owned(),
            ));
        }
        if root.exists() && root.symlink_metadata()?.file_type().is_symlink() {
            return Err(ManagerStateError::UnsafePath(
                "state root must not be a symlink".to_owned(),
            ));
        }
        Ok(Self {
            path: root.join(FILE_NAME),
            root,
        })
    }

    pub fn load(&self) -> Result<RestoredManagerState, ManagerStateError> {
        if !self.path.exists() {
            return Ok(RestoredManagerState::default());
        }
        let metadata = self.path.symlink_metadata()?;
        if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
            return Err(ManagerStateError::UnsafePath(
                "state file must be a regular non-symlink file".to_owned(),
            ));
        }
        if metadata.len() == 0 || metadata.len() > MAX_STATE_BYTES {
            return Err(ManagerStateError::Invalid(
                "state file size is out of bounds".to_owned(),
            ));
        }
        let mut file = File::open(&self.path)?;
        let mut raw = Vec::with_capacity(metadata.len() as usize);
        file.take(MAX_STATE_BYTES + 1).read_to_end(&mut raw)?;
        if raw.len() as u64 > MAX_STATE_BYTES {
            return Err(ManagerStateError::Invalid(
                "state file exceeds the bounded size".to_owned(),
            ));
        }
        let decoded: ManagerStateV1 = serde_json::from_slice(&raw)
            .map_err(|error| ManagerStateError::Invalid(error.to_string()))?;
        validate_state(&decoded)?;
        Ok(RestoredManagerState {
            active_release: decoded.active_release,
            previous_release: decoded.previous_release,
            runtime_generation: decoded.runtime_generation,
        })
    }

    pub fn store(&self, state: &RestoredManagerState) -> Result<(), ManagerStateError> {
        let encoded = ManagerStateV1 {
            schema_version: SCHEMA_VERSION,
            active_release: state.active_release.clone(),
            previous_release: state.previous_release.clone(),
            runtime_generation: state.runtime_generation.clone(),
        };
        validate_state(&encoded)?;
        let payload = serde_json::to_vec(&encoded)
            .map_err(|error| ManagerStateError::Invalid(error.to_string()))?;
        if payload.is_empty() || payload.len() as u64 > MAX_STATE_BYTES {
            return Err(ManagerStateError::Invalid(
                "serialized state size is out of bounds".to_owned(),
            ));
        }
        self.prepare_root()?;
        if self.path.exists() {
            let metadata = self.path.symlink_metadata()?;
            if metadata.file_type().is_symlink() || !metadata.file_type().is_file() {
                return Err(ManagerStateError::UnsafePath(
                    "existing state destination is unsafe".to_owned(),
                ));
            }
        }
        let temp = self.temp_path();
        let result = (|| -> Result<(), ManagerStateError> {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(unix)]
            {
                use std::os::unix::fs::OpenOptionsExt;
                options.mode(0o600);
            }
            let mut file = options.open(&temp)?;
            file.write_all(&payload)?;
            file.sync_all()?;
            drop(file);
            replace_file(&temp, &self.path)?;
            sync_parent(&self.root)?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(&temp);
        }
        result
    }

    #[cfg(test)]
    pub fn path(&self) -> &Path {
        &self.path
    }

    fn prepare_root(&self) -> Result<(), ManagerStateError> {
        fs::create_dir_all(&self.root)?;
        let metadata = self.root.symlink_metadata()?;
        if metadata.file_type().is_symlink() || !metadata.file_type().is_dir() {
            return Err(ManagerStateError::UnsafePath(
                "state root must be a regular directory".to_owned(),
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))?;
        }
        Ok(())
    }

    fn temp_path(&self) -> PathBuf {
        let nonce = TEMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        self.root.join(format!(
            ".{FILE_NAME}.tmp-{}-{nonce}",
            std::process::id()
        ))
    }
}

fn validate_state(state: &ManagerStateV1) -> Result<(), ManagerStateError> {
    if state.schema_version != SCHEMA_VERSION {
        return Err(ManagerStateError::Invalid(
            "unsupported state schema version".to_owned(),
        ));
    }
    match state.active_release.as_deref() {
        Some(value) => validate_release_id(value)?,
        None => {
            if state.previous_release.is_some() || state.runtime_generation.is_some() {
                return Err(ManagerStateError::Invalid(
                    "state without an active release cannot carry previous/generation"
                        .to_owned(),
                ));
            }
        }
    }
    if let Some(previous) = state.previous_release.as_deref() {
        validate_release_id(previous)?;
        if state.active_release.as_deref() == Some(previous) {
            return Err(ManagerStateError::Invalid(
                "active and previous releases must differ".to_owned(),
            ));
        }
    }
    if let Some(generation) = state.runtime_generation.as_deref() {
        if generation.is_empty()
            || generation.len() > MAX_GENERATION_BYTES
            || !generation
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+'))
        {
            return Err(ManagerStateError::Invalid(
                "runtime generation is invalid".to_owned(),
            ));
        }
    }
    Ok(())
}

fn validate_release_id(value: &str) -> Result<(), ManagerStateError> {
    if value.is_empty()
        || value.len() > MAX_RELEASE_ID_BYTES
        || value == "."
        || value == ".."
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+'))
    {
        return Err(ManagerStateError::Invalid(
            "release identity is invalid".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn replace_file(source: &Path, destination: &Path) -> Result<(), ManagerStateError> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    let source_wide: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let destination_wide: Vec<u16> = destination.as_os_str().encode_wide().chain(Some(0)).collect();
    let result = unsafe {
        MoveFileExW(
            source_wide.as_ptr(),
            destination_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        return Err(ManagerStateError::Io(std::io::Error::last_os_error()));
    }
    Ok(())
}

#[cfg(not(windows))]
fn replace_file(source: &Path, destination: &Path) -> Result<(), ManagerStateError> {
    fs::rename(source, destination)?;
    Ok(())
}

#[cfg(unix)]
fn sync_parent(root: &Path) -> Result<(), ManagerStateError> {
    File::open(root)?.sync_all()?;
    Ok(())
}

#[cfg(not(unix))]
fn sync_parent(_root: &Path) -> Result<(), ManagerStateError> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_COUNTER: AtomicU64 = AtomicU64::new(1);

    fn temp_root() -> PathBuf {
        let id = TEST_COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "hermes-manager-state-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn state_round_trip_preserves_only_release_identity() {
        let root = temp_root();
        let file = ManagerStateFile::new(root.clone()).unwrap();
        let state = RestoredManagerState {
            active_release: Some("1.0.1+win".to_owned()),
            previous_release: Some("1.0.0+win".to_owned()),
            runtime_generation: Some("101".to_owned()),
        };

        file.store(&state).unwrap();
        assert_eq!(file.load().unwrap(), state);
        assert!(file.path().is_file());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn malformed_or_unknown_state_fails_closed() {
        let root = temp_root();
        let file = ManagerStateFile::new(root.clone()).unwrap();
        fs::write(
            file.path(),
            br#"{"schema_version":1,"active_release":"1.0.0","previous_release":null,"runtime_generation":"100","unknown":true}"#,
        )
        .unwrap();
        assert!(matches!(file.load(), Err(ManagerStateError::Invalid(_))));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn state_without_active_release_cannot_claim_previous_or_generation() {
        let root = temp_root();
        let file = ManagerStateFile::new(root.clone()).unwrap();
        let invalid = RestoredManagerState {
            active_release: None,
            previous_release: Some("1.0.0".to_owned()),
            runtime_generation: None,
        };
        assert!(matches!(
            file.store(&invalid),
            Err(ManagerStateError::Invalid(_))
        ));
        let _ = fs::remove_dir_all(root);
    }
}
