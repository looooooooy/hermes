#![cfg(windows)]

use crate::ports::{PortError, SecretStore};
use std::ffi::c_void;
use std::ptr::{null_mut};
use windows_sys::Win32::Foundation::{GetLastError, FILETIME};

const CRED_TYPE_GENERIC: u32 = 1;
const CRED_PERSIST_LOCAL_MACHINE: u32 = 2;
const CRED_MAX_CREDENTIAL_BLOB_SIZE: usize = 5 * 512;
const ERROR_NOT_FOUND: u32 = 1168;
const MAX_KEY_COMPONENT_BYTES: usize = 128;
const TARGET_PREFIX: &str = "HermesRuntimeManager";

#[repr(C)]
struct RawCredentialW {
    flags: u32,
    credential_type: u32,
    target_name: *mut u16,
    comment: *mut u16,
    last_written: FILETIME,
    credential_blob_size: u32,
    credential_blob: *mut u8,
    persist: u32,
    attribute_count: u32,
    attributes: *mut c_void,
    target_alias: *mut u16,
    user_name: *mut u16,
}

#[link(name = "advapi32")]
unsafe extern "system" {
    fn CredWriteW(credential: *const RawCredentialW, flags: u32) -> i32;
    fn CredReadW(
        target_name: *const u16,
        credential_type: u32,
        flags: u32,
        credential: *mut *mut RawCredentialW,
    ) -> i32;
    fn CredDeleteW(target_name: *const u16, credential_type: u32, flags: u32) -> i32;
    fn CredFree(buffer: *mut c_void);
}

/// Windows Credential Manager-backed secret store for the current OS user.
///
/// Generic credentials are stored in the credential set associated with the current
/// token. `CRED_PERSIST_LOCAL_MACHINE` keeps them available to subsequent logon
/// sessions for the same user on the same computer. No secret material is encoded in
/// the target name, user name, process arguments, environment, or filesystem paths.
#[derive(Debug, Default, Clone, Copy)]
pub struct WindowsCredentialSecretStore;

impl WindowsCredentialSecretStore {
    pub fn new() -> Self {
        Self
    }
}

impl SecretStore for WindowsCredentialSecretStore {
    fn put(&self, namespace: &str, account: &str, secret: &[u8]) -> Result<(), PortError> {
        if secret.len() > CRED_MAX_CREDENTIAL_BLOB_SIZE {
            return Err(PortError::Operation(format!(
                "Windows Credential Manager secret exceeds {}-byte generic credential limit",
                CRED_MAX_CREDENTIAL_BLOB_SIZE
            )));
        }
        let mut target = wide_target(namespace, account)?;
        let credential_blob = if secret.is_empty() {
            null_mut()
        } else {
            secret.as_ptr().cast_mut()
        };
        let credential = RawCredentialW {
            flags: 0,
            credential_type: CRED_TYPE_GENERIC,
            target_name: target.as_mut_ptr(),
            comment: null_mut(),
            last_written: FILETIME {
                dwLowDateTime: 0,
                dwHighDateTime: 0,
            },
            credential_blob_size: secret.len() as u32,
            credential_blob,
            persist: CRED_PERSIST_LOCAL_MACHINE,
            attribute_count: 0,
            attributes: null_mut(),
            target_alias: null_mut(),
            user_name: null_mut(),
        };
        if unsafe { CredWriteW(&credential, 0) } == 0 {
            return Err(win32_operation("CredWriteW"));
        }
        Ok(())
    }

    fn get(&self, namespace: &str, account: &str) -> Result<Option<Vec<u8>>, PortError> {
        let target = wide_target(namespace, account)?;
        let mut raw: *mut RawCredentialW = null_mut();
        if unsafe { CredReadW(target.as_ptr(), CRED_TYPE_GENERIC, 0, &mut raw) } == 0 {
            let code = unsafe { GetLastError() };
            if code == ERROR_NOT_FOUND {
                return Ok(None);
            }
            return Err(win32_operation_with_code("CredReadW", code));
        }
        if raw.is_null() {
            return Err(PortError::Operation(
                "CredReadW returned success with a null credential".to_owned(),
            ));
        }
        let guard = OwnedCredential(raw);
        let credential = unsafe { &*guard.0 };
        if credential.credential_type != CRED_TYPE_GENERIC {
            return Err(PortError::Operation(
                "Credential Manager returned an unexpected credential type".to_owned(),
            ));
        }
        let size = credential.credential_blob_size as usize;
        if size > CRED_MAX_CREDENTIAL_BLOB_SIZE {
            return Err(PortError::Operation(
                "Credential Manager returned an oversized generic credential".to_owned(),
            ));
        }
        if size == 0 {
            return Ok(Some(Vec::new()));
        }
        if credential.credential_blob.is_null() {
            return Err(PortError::Operation(
                "Credential Manager returned a null non-empty credential blob".to_owned(),
            ));
        }
        let bytes = unsafe { std::slice::from_raw_parts(credential.credential_blob, size) };
        Ok(Some(bytes.to_vec()))
    }

    fn delete(&self, namespace: &str, account: &str) -> Result<(), PortError> {
        let target = wide_target(namespace, account)?;
        if unsafe { CredDeleteW(target.as_ptr(), CRED_TYPE_GENERIC, 0) } == 0 {
            let code = unsafe { GetLastError() };
            if code != ERROR_NOT_FOUND {
                return Err(win32_operation_with_code("CredDeleteW", code));
            }
        }
        Ok(())
    }
}

struct OwnedCredential(*mut RawCredentialW);

impl Drop for OwnedCredential {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { CredFree(self.0.cast()) };
            self.0 = null_mut();
        }
    }
}

fn wide_target(namespace: &str, account: &str) -> Result<Vec<u16>, PortError> {
    validate_component(namespace, "namespace")?;
    validate_component(account, "account")?;
    Ok(format!("{TARGET_PREFIX}/{namespace}/{account}")
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect())
}

fn validate_component(value: &str, label: &str) -> Result<(), PortError> {
    if value.is_empty()
        || value.len() > MAX_KEY_COMPONENT_BYTES
        || !value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
        })
    {
        return Err(PortError::Operation(format!(
            "invalid Windows Credential Manager {label}"
        )));
    }
    Ok(())
}

fn win32_operation(operation: &str) -> PortError {
    win32_operation_with_code(operation, unsafe { GetLastError() })
}

fn win32_operation_with_code(operation: &str, code: u32) -> PortError {
    PortError::Operation(format!("{operation} failed with Win32 error {code}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn unique_account() -> String {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        format!("ci-{}-{nonce}", std::process::id())
    }

    #[test]
    fn credential_manager_round_trip_supports_binary_secret_and_delete() {
        let store = WindowsCredentialSecretStore::new();
        let namespace = "runtime-manager-ci";
        let account = unique_account();
        let first = b"device\0credential\xffbinary";
        let second = b"replacement\0secret";

        store.delete(namespace, &account).expect("pre-clean");
        assert_eq!(store.get(namespace, &account).expect("initial get"), None);

        store.put(namespace, &account, first).expect("put first");
        assert_eq!(
            store.get(namespace, &account).expect("get first"),
            Some(first.to_vec())
        );

        store.put(namespace, &account, second).expect("replace");
        assert_eq!(
            store.get(namespace, &account).expect("get replacement"),
            Some(second.to_vec())
        );

        store.delete(namespace, &account).expect("delete");
        assert_eq!(store.get(namespace, &account).expect("get deleted"), None);
        store.delete(namespace, &account).expect("idempotent delete");
    }

    #[test]
    fn target_name_never_contains_secret_and_rejects_unsafe_components() {
        let target = wide_target("provider", "primary-account").expect("target");
        let decoded = String::from_utf16(&target[..target.len() - 1]).expect("utf16");
        assert_eq!(decoded, "HermesRuntimeManager/provider/primary-account");
        assert!(!decoded.contains("secret"));
        assert!(wide_target("provider/escape", "account").is_err());
        assert!(wide_target("provider", "account with spaces").is_err());
        assert!(wide_target("", "account").is_err());
    }

    #[test]
    fn generic_credential_blob_limit_is_enforced_before_win32_call() {
        let store = WindowsCredentialSecretStore::new();
        let oversized = vec![0u8; CRED_MAX_CREDENTIAL_BLOB_SIZE + 1];
        let error = store
            .put("runtime-manager-ci", "oversized", &oversized)
            .expect_err("oversized secret must fail closed");
        assert!(error.to_string().contains("exceeds"));
    }
}
