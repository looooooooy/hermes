use serde::Deserialize;
use std::time::{SystemTime, UNIX_EPOCH};

const KEYCHAIN_SERVICE: &str = "com.hermes.desktop.workspace-auth.v1";
const KEYCHAIN_ACCOUNT: &str = "current";
pub(crate) const REAUTH_REQUIRED_MESSAGE: &str =
    "Hermes workspace sign-in has expired. Sign in again.";

pub(crate) struct WorkspaceAccessContext {
    pub endpoint: String,
    pub access_token: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredWorkspaceTokens {
    schema_version: u8,
    endpoint: String,
    access_token: String,
    refresh_token: String,
    token_type: String,
    expires_at: u64,
    provider: String,
    user_id: String,
}

pub(crate) fn load() -> Result<WorkspaceAccessContext, String> {
    let stored = load_tokens()?.ok_or_else(|| {
        "Sign in to the Hermes workspace before pairing this device.".to_owned()
    })?;
    validate(&stored)?;
    Ok(WorkspaceAccessContext {
        endpoint: stored.endpoint,
        access_token: stored.access_token,
    })
}

fn validate(tokens: &StoredWorkspaceTokens) -> Result<(), String> {
    let now = unix_seconds();
    if tokens.schema_version != 1
        || tokens.endpoint.is_empty()
        || tokens.access_token.is_empty()
        || tokens.refresh_token.is_empty()
        || !tokens.token_type.eq_ignore_ascii_case("bearer")
        || tokens.expires_at <= now.saturating_add(30)
        || tokens.provider.is_empty()
        || tokens.user_id.is_empty()
    {
        return Err(REAUTH_REQUIRED_MESSAGE.to_owned());
    }
    Ok(())
}

fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or(0)
}

#[cfg(target_os = "macos")]
fn load_tokens() -> Result<Option<StoredWorkspaceTokens>, String> {
    use security_framework::passwords::get_generic_password;
    match get_generic_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) {
        Ok(bytes) => serde_json::from_slice::<StoredWorkspaceTokens>(&bytes)
            .map(Some)
            .map_err(|_| "Stored Hermes workspace credentials are invalid.".to_owned()),
        Err(_) => Ok(None),
    }
}

#[cfg(not(target_os = "macos"))]
fn load_tokens() -> Result<Option<StoredWorkspaceTokens>, String> {
    Err("Native Hermes workspace pairing is currently enabled only on macOS.".to_owned())
}
