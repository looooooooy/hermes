#![cfg(target_os = "linux")]

use crate::ports::{PortError, SecretStore};
use keyring::{Entry, Error as KeyringError};

const SERVICE_PREFIX: &str = "com.hermes.runtime";
const MAX_SECRET_BYTES: usize = 5 * 512;
const MAX_NAMESPACE_BYTES: usize = 64;
const MAX_ACCOUNT_BYTES: usize = 160;

#[derive(Debug, Default, Clone, Copy)]
pub struct LinuxSecretServiceStore;

impl LinuxSecretServiceStore {
    pub fn new() -> Self {
        Self
    }

    fn entry(namespace: &str, account: &str) -> Result<Entry, PortError> {
        validate_key_component("namespace", namespace, MAX_NAMESPACE_BYTES)?;
        validate_key_component("account", account, MAX_ACCOUNT_BYTES)?;
        let service = format!("{SERVICE_PREFIX}.{namespace}");
        Entry::new(&service, account)
            .map_err(|error| keyring_operation("create Secret Service entry", error))
    }
}

impl SecretStore for LinuxSecretServiceStore {
    fn put(&self, namespace: &str, account: &str, secret: &[u8]) -> Result<(), PortError> {
        validate_secret(secret)?;
        Self::entry(namespace, account)?
            .set_secret(secret)
            .map_err(|error| keyring_operation("write Secret Service item", error))
    }

    fn get(&self, namespace: &str, account: &str) -> Result<Option<Vec<u8>>, PortError> {
        match Self::entry(namespace, account)?.get_secret() {
            Ok(secret) => Ok(Some(secret)),
            Err(KeyringError::NoEntry) => Ok(None),
            Err(error) => Err(keyring_operation("read Secret Service item", error)),
        }
    }

    fn delete(&self, namespace: &str, account: &str) -> Result<(), PortError> {
        match Self::entry(namespace, account)?.delete_credential() {
            Ok(()) | Err(KeyringError::NoEntry) => Ok(()),
            Err(error) => Err(keyring_operation("delete Secret Service item", error)),
        }
    }
}

fn validate_secret(secret: &[u8]) -> Result<(), PortError> {
    if secret.is_empty() {
        return Err(PortError::Operation(
            "Secret Service secret must not be empty".to_owned(),
        ));
    }
    if secret.len() > MAX_SECRET_BYTES {
        return Err(PortError::Operation(format!(
            "Secret Service secret exceeds cross-platform credential limit of {MAX_SECRET_BYTES} bytes"
        )));
    }
    Ok(())
}

fn validate_key_component(label: &str, value: &str, max_bytes: usize) -> Result<(), PortError> {
    let valid = !value.is_empty()
        && value.len() <= max_bytes
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b':')
        });
    if valid {
        Ok(())
    } else {
        Err(PortError::Operation(format!(
            "invalid Secret Service {label} component"
        )))
    }
}

fn keyring_operation(context: &str, error: KeyringError) -> PortError {
    PortError::Operation(format!("{context} failed: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secret_limit_matches_windows_generic_credential_contract() {
        assert!(validate_secret(&[1]).is_ok());
        assert!(validate_secret(&vec![7; MAX_SECRET_BYTES]).is_ok());
        assert!(validate_secret(&[]).is_err());
        assert!(validate_secret(&vec![7; MAX_SECRET_BYTES + 1]).is_err());
    }

    #[test]
    fn key_components_are_bounded_and_never_accept_path_or_shell_material() {
        assert!(validate_key_component("namespace", "cloud.device", MAX_NAMESPACE_BYTES).is_ok());
        assert!(validate_key_component("account", "device:01", MAX_ACCOUNT_BYTES).is_ok());
        assert!(validate_key_component("account", "../escape", MAX_ACCOUNT_BYTES).is_err());
        assert!(validate_key_component("account", "bad value", MAX_ACCOUNT_BYTES).is_err());
        assert!(validate_key_component("account", "bad$var", MAX_ACCOUNT_BYTES).is_err());
    }
}
