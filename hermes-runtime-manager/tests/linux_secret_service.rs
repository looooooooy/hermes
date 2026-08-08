#![cfg(target_os = "linux")]

use hermes_runtime_manager::ports::SecretStore;
use hermes_runtime_manager::LinuxSecretServiceStore;

#[test]
#[ignore = "requires a live Secret Service session; CI runs this explicitly inside GNOME Keyring"]
fn secret_service_round_trip_supports_binary_secret_overwrite_and_delete() {
    let store = LinuxSecretServiceStore::new();
    let namespace = format!("ci.{}", std::process::id());
    let account = "device-credential";
    let first = [0x00, 0x01, 0x7f, 0x80, 0xff, 0x42];
    let second = b"replacement-secret-without-plaintext-file-fallback";

    let _ = store.delete(&namespace, account);
    assert_eq!(store.get(&namespace, account).expect("initial lookup"), None);

    store.put(&namespace, account, &first).expect("put binary secret");
    assert_eq!(
        store.get(&namespace, account).expect("get binary secret"),
        Some(first.to_vec())
    );

    store.put(&namespace, account, second).expect("overwrite secret");
    assert_eq!(
        store.get(&namespace, account).expect("get overwritten secret"),
        Some(second.to_vec())
    );

    store.delete(&namespace, account).expect("delete secret");
    assert_eq!(store.get(&namespace, account).expect("post-delete lookup"), None);
    store.delete(&namespace, account).expect("idempotent delete");
}
