use crate::ports::{PortError, SecretStore};
use std::fmt;
use std::sync::Arc;
use zeroize::{Zeroize, Zeroizing};

pub const PROVIDER_SERVICE: &str = "com.hermes.runtime.provider.v1";
pub const PROVIDER_ACCOUNT: &str = "work:deepseek";
const MAX_SECRET_BYTES: usize = 64 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AdapterError {
    Operation,
}

struct AdapterSecret {
    bytes: Vec<u8>,
    #[cfg(test)]
    zeroized_observer: Option<Arc<std::sync::Mutex<Vec<u8>>>>,
}

impl AdapterSecret {
    fn new(bytes: Vec<u8>) -> Self {
        Self {
            bytes,
            #[cfg(test)]
            zeroized_observer: None,
        }
    }

    #[cfg(test)]
    fn tracked(bytes: Vec<u8>, observer: Arc<std::sync::Mutex<Vec<u8>>>) -> Self {
        Self {
            bytes,
            zeroized_observer: Some(observer),
        }
    }
}

impl Drop for AdapterSecret {
    fn drop(&mut self) {
        self.bytes.as_mut_slice().zeroize();
        #[cfg(test)]
        if let Some(observer) = &self.zeroized_observer {
            *observer.lock().expect("zeroize observer lock") = self.bytes.clone();
        }
        self.bytes.clear();
    }
}

trait PasswordAdapter: Send + Sync {
    fn put(&self, service: &str, account: &str, secret: &[u8]) -> Result<(), AdapterError>;
    fn get(&self, service: &str, account: &str) -> Result<Option<AdapterSecret>, AdapterError>;
    fn delete(&self, service: &str, account: &str) -> Result<(), AdapterError>;
}

struct SecurityFrameworkPasswordAdapter;

impl PasswordAdapter for SecurityFrameworkPasswordAdapter {
    fn put(&self, service: &str, account: &str, secret: &[u8]) -> Result<(), AdapterError> {
        security_framework::passwords::set_generic_password(service, account, secret)
            .map_err(|_| AdapterError::Operation)
    }

    fn get(&self, service: &str, account: &str) -> Result<Option<AdapterSecret>, AdapterError> {
        match security_framework::passwords::get_generic_password(service, account) {
            Ok(bytes) => Ok(Some(AdapterSecret::new(bytes))),
            Err(error) if error.code() == security_framework_sys::base::errSecItemNotFound => {
                Ok(None)
            }
            Err(_) => Err(AdapterError::Operation),
        }
    }

    fn delete(&self, service: &str, account: &str) -> Result<(), AdapterError> {
        match security_framework::passwords::delete_generic_password(service, account) {
            Ok(()) => Ok(()),
            Err(error) if error.code() == security_framework_sys::base::errSecItemNotFound => {
                Ok(())
            }
            Err(_) => Err(AdapterError::Operation),
        }
    }
}

pub struct MacOSKeychainSecretStore {
    adapter: Arc<dyn PasswordAdapter>,
}

impl MacOSKeychainSecretStore {
    pub fn new() -> Self {
        Self {
            adapter: Arc::new(SecurityFrameworkPasswordAdapter),
        }
    }

    #[cfg(test)]
    fn with_adapter(adapter: Arc<dyn PasswordAdapter>) -> Self {
        Self { adapter }
    }

    fn validate_reference(namespace: &str, account: &str) -> Result<(), PortError> {
        if namespace != PROVIDER_SERVICE || account != PROVIDER_ACCOUNT {
            return Err(PortError::Operation(
                "provider Keychain reference is invalid".to_owned(),
            ));
        }
        Ok(())
    }

    fn operation_error() -> PortError {
        PortError::Operation("macOS provider Keychain operation failed".to_owned())
    }
}

impl Default for MacOSKeychainSecretStore {
    fn default() -> Self {
        Self::new()
    }
}

impl fmt::Debug for MacOSKeychainSecretStore {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("MacOSKeychainSecretStore")
            .finish_non_exhaustive()
    }
}

impl SecretStore for MacOSKeychainSecretStore {
    fn put(&self, namespace: &str, account: &str, secret: &[u8]) -> Result<(), PortError> {
        Self::validate_reference(namespace, account)?;
        if secret.is_empty() || secret.len() > MAX_SECRET_BYTES {
            return Err(PortError::Operation(
                "provider credential length is invalid".to_owned(),
            ));
        }
        let secret = Zeroizing::new(secret.to_vec());
        self.adapter
            .put(namespace, account, secret.as_slice())
            .map_err(|_| Self::operation_error())
    }

    fn get(&self, namespace: &str, account: &str) -> Result<Option<Vec<u8>>, PortError> {
        Self::validate_reference(namespace, account)?;
        let secret = self
            .adapter
            .get(namespace, account)
            .map_err(|_| Self::operation_error())?;
        Ok(secret.map(|value| value.bytes.clone()))
    }

    fn delete(&self, namespace: &str, account: &str) -> Result<(), PortError> {
        Self::validate_reference(namespace, account)?;
        self.adapter
            .delete(namespace, account)
            .map_err(|_| Self::operation_error())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ports::SecretStore;
    use std::collections::BTreeMap;
    use std::sync::{Arc, Mutex};

    #[derive(Default)]
    struct FakePasswordAdapter {
        values: Mutex<BTreeMap<(String, String), Vec<u8>>>,
        fail: Mutex<bool>,
        zeroized_read: Arc<Mutex<Vec<u8>>>,
    }

    impl PasswordAdapter for FakePasswordAdapter {
        fn put(&self, service: &str, account: &str, secret: &[u8]) -> Result<(), AdapterError> {
            if *self.fail.lock().unwrap() {
                return Err(AdapterError::Operation);
            }
            self.values
                .lock()
                .unwrap()
                .insert((service.to_owned(), account.to_owned()), secret.to_vec());
            Ok(())
        }

        fn get(
            &self,
            service: &str,
            account: &str,
        ) -> Result<Option<AdapterSecret>, AdapterError> {
            if *self.fail.lock().unwrap() {
                return Err(AdapterError::Operation);
            }
            Ok(self
                .values
                .lock()
                .unwrap()
                .get(&(service.to_owned(), account.to_owned()))
                .cloned()
                .map(|bytes| AdapterSecret::tracked(bytes, self.zeroized_read.clone())))
        }

        fn delete(&self, service: &str, account: &str) -> Result<(), AdapterError> {
            if *self.fail.lock().unwrap() {
                return Err(AdapterError::Operation);
            }
            self.values
                .lock()
                .unwrap()
                .remove(&(service.to_owned(), account.to_owned()));
            Ok(())
        }
    }

    fn store() -> (MacOSKeychainSecretStore, Arc<FakePasswordAdapter>) {
        let adapter = Arc::new(FakePasswordAdapter::default());
        (
            MacOSKeychainSecretStore::with_adapter(adapter.clone()),
            adapter,
        )
    }

    #[test]
    fn namespace_and_account_must_be_canonical() {
        let (store, _) = store();
        assert!(store.put("other", PROVIDER_ACCOUNT, b"valid-secret-value").is_err());
        assert!(store
            .put(PROVIDER_SERVICE, "other", b"valid-secret-value")
            .is_err());
        assert!(store
            .put(PROVIDER_SERVICE, PROVIDER_ACCOUNT, b"")
            .is_err());
    }

    #[test]
    fn create_read_update_delete_uses_the_fixed_keychain_item() {
        let (store, adapter) = store();
        store
            .put(PROVIDER_SERVICE, PROVIDER_ACCOUNT, b"first-secret-value")
            .unwrap();
        assert_eq!(
            store.get(PROVIDER_SERVICE, PROVIDER_ACCOUNT).unwrap(),
            Some(b"first-secret-value".to_vec())
        );
        store
            .put(PROVIDER_SERVICE, PROVIDER_ACCOUNT, b"second-secret-value")
            .unwrap();
        assert_eq!(
            store.get(PROVIDER_SERVICE, PROVIDER_ACCOUNT).unwrap(),
            Some(b"second-secret-value".to_vec())
        );
        store.delete(PROVIDER_SERVICE, PROVIDER_ACCOUNT).unwrap();
        assert_eq!(store.get(PROVIDER_SERVICE, PROVIDER_ACCOUNT).unwrap(), None);
        assert!(adapter.values.lock().unwrap().is_empty());
    }

    #[test]
    fn adapter_errors_are_redacted_and_read_buffer_is_zeroized() {
        let (store, adapter) = store();
        store
            .put(PROVIDER_SERVICE, PROVIDER_ACCOUNT, b"zeroize-secret-value")
            .unwrap();
        let result = store.get(PROVIDER_SERVICE, PROVIDER_ACCOUNT).unwrap();
        assert_eq!(result, Some(b"zeroize-secret-value".to_vec()));
        assert_eq!(
            *adapter.zeroized_read.lock().unwrap(),
            vec![0; b"zeroize-secret-value".len()]
        );

        *adapter.fail.lock().unwrap() = true;
        let error = store
            .get(PROVIDER_SERVICE, PROVIDER_ACCOUNT)
            .unwrap_err()
            .to_string();
        assert_eq!(error, "platform operation failed: macOS provider Keychain operation failed");
        assert!(!error.contains("zeroize-secret-value"));
    }
}
