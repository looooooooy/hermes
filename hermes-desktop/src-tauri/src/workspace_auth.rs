use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use rand::RngCore;
use reqwest::blocking::Client;
use reqwest::{StatusCode, Url};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener};
use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const CALLBACK_PATH: &str = "/oauth/callback";
const KEYCHAIN_SERVICE: &str = "com.hermes.desktop.workspace-auth.v1";
const KEYCHAIN_ACCOUNT: &str = "current";
const AUTH_TIMEOUT: Duration = Duration::from_secs(600);
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);
const POLL_INTERVAL: Duration = Duration::from_millis(300);
const REFRESH_BEFORE_SECONDS: u64 = 30;

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct WorkspaceAuthStatus {
    pub authenticated: bool,
    pub endpoint: Option<String>,
    pub user_id: Option<String>,
    pub provider: Option<String>,
    pub expires_at_epoch_seconds: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
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

#[derive(Debug, Deserialize)]
struct TokenPayload {
    access_token: String,
    refresh_token: String,
    token_type: String,
    expires_at: u64,
    provider: String,
    user_id: String,
}

#[derive(Debug, Deserialize)]
struct PollPayload {
    code: String,
}

struct PkceCredentials {
    verifier: String,
    challenge: String,
    state: String,
    request_id: String,
}

pub fn status() -> WorkspaceAuthStatus {
    let stored = match load_tokens() {
        Ok(Some(value)) => value,
        _ => return disconnected_status(),
    };

    if access_session_active(&stored) {
        return status_from_tokens(stored, true);
    }

    match refresh_stored_tokens(&stored) {
        Ok(refreshed) => status_from_tokens(refreshed, true),
        Err(_) => status_from_tokens(stored, false),
    }
}

pub fn connect(endpoint: &str) -> Result<WorkspaceAuthStatus, String> {
    let endpoint = normalize_endpoint(endpoint)?;
    let credentials = generate_pkce();

    // Keep a unique RFC 8252 loopback redirect in the authorization request for
    // backwards compatibility with existing native clients. Desktop no longer
    // depends on the browser reaching this listener: it finishes through the
    // authenticated PKCE polling channel below.
    let listener = TcpListener::bind(SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0))
        .map_err(|_| "Could not reserve the secure local sign-in callback.".to_owned())?;
    let port = listener
        .local_addr()
        .map_err(|_| "Could not read the secure local sign-in callback address.".to_owned())?
        .port();
    let redirect_uri = format!("http://127.0.0.1:{port}{CALLBACK_PATH}");

    let authorization_url = build_authorization_url(
        &endpoint,
        &redirect_uri,
        &credentials.challenge,
        &credentials.state,
        &credentials.request_id,
    )?;
    open_browser(&authorization_url)?;

    let code = wait_for_authorization(&endpoint, &credentials)?;
    let tokens = exchange_code(&endpoint, &code, &credentials.verifier)?;
    let stored = stored_from_payload(&endpoint, tokens);
    validate_tokens(&stored)?;
    store_tokens(&stored)?;
    Ok(status_from_tokens(stored, true))
}

fn status_from_tokens(tokens: StoredWorkspaceTokens, authenticated: bool) -> WorkspaceAuthStatus {
    WorkspaceAuthStatus {
        authenticated,
        endpoint: Some(tokens.endpoint),
        user_id: Some(tokens.user_id),
        provider: Some(tokens.provider),
        expires_at_epoch_seconds: Some(tokens.expires_at),
    }
}

fn disconnected_status() -> WorkspaceAuthStatus {
    WorkspaceAuthStatus {
        authenticated: false,
        endpoint: None,
        user_id: None,
        provider: None,
        expires_at_epoch_seconds: None,
    }
}

fn normalize_endpoint(raw: &str) -> Result<Url, String> {
    let raw = raw.trim();
    if raw.is_empty() || raw.len() > 2048 {
        return Err("Enter the Hermes Cloud HTTPS address.".to_owned());
    }
    let mut endpoint = Url::parse(raw).map_err(|_| "Hermes Cloud address is invalid.".to_owned())?;
    let secure = endpoint.scheme() == "https";
    let loopback_dev = endpoint.scheme() == "http"
        && matches!(endpoint.host_str(), Some("127.0.0.1") | Some("localhost"));
    if !secure && !loopback_dev {
        return Err("Hermes Cloud must use HTTPS (loopback HTTP is allowed only for development).".to_owned());
    }
    if endpoint.username() != "" || endpoint.password().is_some() || endpoint.fragment().is_some() {
        return Err("Hermes Cloud address must not contain credentials or fragments.".to_owned());
    }
    endpoint.set_query(None);
    endpoint.set_fragment(None);
    if !endpoint.path().ends_with('/') {
        let path = format!("{}/", endpoint.path());
        endpoint.set_path(&path);
    }
    Ok(endpoint)
}

fn generate_pkce() -> PkceCredentials {
    let mut verifier_bytes = [0u8; 32];
    let mut state_bytes = [0u8; 24];
    let mut request_bytes = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut verifier_bytes);
    rand::thread_rng().fill_bytes(&mut state_bytes);
    rand::thread_rng().fill_bytes(&mut request_bytes);
    let verifier = URL_SAFE_NO_PAD.encode(verifier_bytes);
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let state = URL_SAFE_NO_PAD.encode(state_bytes);
    let request_id = URL_SAFE_NO_PAD.encode(request_bytes);
    PkceCredentials {
        verifier,
        challenge,
        state,
        request_id,
    }
}

fn build_authorization_url(
    endpoint: &Url,
    redirect_uri: &str,
    challenge: &str,
    state: &str,
    request_id: &str,
) -> Result<Url, String> {
    let mut url = endpoint
        .join("auth/native/authorize")
        .map_err(|_| "Hermes Cloud authorization URL could not be created.".to_owned())?;
    url.query_pairs_mut()
        .append_pair("code_challenge", challenge)
        .append_pair("code_challenge_method", "S256")
        .append_pair("redirect_uri", redirect_uri)
        .append_pair("state", state)
        .append_pair("request_id", request_id);
    Ok(url)
}

#[cfg(target_os = "macos")]
fn open_browser(url: &Url) -> Result<(), String> {
    let status = Command::new("/usr/bin/open")
        .arg(url.as_str())
        .status()
        .map_err(|_| "Could not open the browser for Hermes sign-in.".to_owned())?;
    if status.success() {
        Ok(())
    } else {
        Err("Could not open the browser for Hermes sign-in.".to_owned())
    }
}

#[cfg(not(target_os = "macos"))]
fn open_browser(_url: &Url) -> Result<(), String> {
    Err("Native workspace sign-in is currently enabled only on macOS.".to_owned())
}

fn wait_for_authorization(endpoint: &Url, credentials: &PkceCredentials) -> Result<String, String> {
    let client = Client::builder()
        .connect_timeout(HTTP_TIMEOUT)
        .timeout(HTTP_TIMEOUT)
        .build()
        .map_err(|_| "Hermes secure sign-in client could not be initialized.".to_owned())?;
    let deadline = Instant::now() + AUTH_TIMEOUT;
    loop {
        if let Some(code) = poll_authorization(&client, endpoint, credentials)? {
            return Ok(code);
        }
        if Instant::now() >= deadline {
            return Err("Hermes sign-in timed out. Try again.".to_owned());
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

fn poll_authorization(
    client: &Client,
    endpoint: &Url,
    credentials: &PkceCredentials,
) -> Result<Option<String>, String> {
    let poll_url = endpoint
        .join("auth/native/poll")
        .map_err(|_| "Hermes Cloud polling URL could not be created.".to_owned())?;
    let response = client
        .post(poll_url)
        .header("Accept", "application/json")
        .json(&serde_json::json!({
            "request_id": credentials.request_id,
            "code_verifier": credentials.verifier,
            "state": credentials.state,
        }))
        .send()
        .map_err(|_| "Hermes Cloud could not be reached while waiting for browser sign-in.".to_owned())?;
    if response.status() == StatusCode::ACCEPTED {
        return Ok(None);
    }
    if !response.status().is_success() {
        return Err(format!(
            "Hermes Cloud native sign-in polling is unavailable (HTTP {}).",
            response.status().as_u16()
        ));
    }
    let payload: PollPayload = response
        .json()
        .map_err(|_| "Hermes Cloud returned an invalid browser sign-in result.".to_owned())?;
    if payload.code.trim().is_empty() || payload.code.len() > 256 {
        return Err("Hermes Cloud returned an invalid authorization code.".to_owned());
    }
    Ok(Some(payload.code))
}

fn exchange_code(endpoint: &Url, code: &str, verifier: &str) -> Result<TokenPayload, String> {
    let token_url = endpoint
        .join("auth/native/token")
        .map_err(|_| "Hermes Cloud token URL could not be created.".to_owned())?;
    let client = Client::builder()
        .connect_timeout(HTTP_TIMEOUT)
        .timeout(HTTP_TIMEOUT)
        .build()
        .map_err(|_| "Hermes secure HTTP client could not be initialized.".to_owned())?;
    let response = client
        .post(token_url)
        .header("Accept", "application/json")
        .json(&serde_json::json!({ "code": code, "code_verifier": verifier }))
        .send()
        .map_err(|_| "Hermes Cloud could not be reached to finish sign-in.".to_owned())?;
    if !response.status().is_success() {
        return Err(format!("Hermes Cloud rejected sign-in (HTTP {}).", response.status().as_u16()));
    }
    response
        .json()
        .map_err(|_| "Hermes Cloud returned an invalid sign-in response.".to_owned())
}

fn refresh_stored_tokens(current: &StoredWorkspaceTokens) -> Result<StoredWorkspaceTokens, String> {
    if current.schema_version != 1
        || current.endpoint.is_empty()
        || current.refresh_token.is_empty()
        || current.provider.is_empty()
        || current.user_id.is_empty()
    {
        return Err("Stored Hermes workspace refresh credentials are invalid.".to_owned());
    }
    let endpoint = normalize_endpoint(&current.endpoint)?;
    let refresh_url = endpoint
        .join("auth/native/refresh")
        .map_err(|_| "Hermes Cloud refresh URL could not be created.".to_owned())?;
    let client = Client::builder()
        .connect_timeout(HTTP_TIMEOUT)
        .timeout(HTTP_TIMEOUT)
        .build()
        .map_err(|_| "Hermes secure refresh client could not be initialized.".to_owned())?;
    let response = client
        .post(refresh_url)
        .header("Accept", "application/json")
        .json(&serde_json::json!({
            "refresh_token": current.refresh_token,
            "provider": current.provider,
        }))
        .send()
        .map_err(|_| "Hermes Cloud could not be reached to refresh the workspace session.".to_owned())?;
    if !response.status().is_success() {
        return Err(format!(
            "Hermes Cloud rejected workspace session refresh (HTTP {}).",
            response.status().as_u16()
        ));
    }
    let payload: TokenPayload = response
        .json()
        .map_err(|_| "Hermes Cloud returned an invalid workspace refresh response.".to_owned())?;
    let refreshed = stored_from_payload(&endpoint, payload);
    validate_tokens(&refreshed)?;
    store_tokens(&refreshed)?;
    Ok(refreshed)
}

fn stored_from_payload(endpoint: &Url, tokens: TokenPayload) -> StoredWorkspaceTokens {
    StoredWorkspaceTokens {
        schema_version: 1,
        endpoint: endpoint.to_string().trim_end_matches('/').to_owned(),
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
        token_type: tokens.token_type,
        expires_at: tokens.expires_at,
        provider: tokens.provider,
        user_id: tokens.user_id,
    }
}

fn access_session_active(tokens: &StoredWorkspaceTokens) -> bool {
    tokens.schema_version == 1
        && !tokens.endpoint.is_empty()
        && !tokens.access_token.is_empty()
        && !tokens.refresh_token.is_empty()
        && tokens.token_type.eq_ignore_ascii_case("bearer")
        && tokens.expires_at > unix_seconds().saturating_add(REFRESH_BEFORE_SECONDS)
        && !tokens.provider.is_empty()
        && !tokens.user_id.is_empty()
}

fn validate_tokens(tokens: &StoredWorkspaceTokens) -> Result<(), String> {
    if tokens.schema_version != 1
        || tokens.endpoint.is_empty()
        || tokens.access_token.is_empty()
        || tokens.refresh_token.is_empty()
        || !tokens.token_type.eq_ignore_ascii_case("bearer")
        || tokens.expires_at <= unix_seconds()
        || tokens.provider.is_empty()
        || tokens.user_id.is_empty()
    {
        return Err("Hermes Cloud returned incomplete workspace credentials.".to_owned());
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
    Ok(None)
}

#[cfg(target_os = "macos")]
fn store_tokens(tokens: &StoredWorkspaceTokens) -> Result<(), String> {
    use security_framework::passwords::set_generic_password;
    let bytes = serde_json::to_vec(tokens)
        .map_err(|_| "Hermes workspace credentials could not be encoded.".to_owned())?;
    set_generic_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, &bytes)
        .map_err(|_| "Hermes workspace credentials could not be stored in Keychain.".to_owned())
}

#[cfg(not(target_os = "macos"))]
fn store_tokens(_tokens: &StoredWorkspaceTokens) -> Result<(), String> {
    Err("Native workspace credential storage is currently enabled only on macOS.".to_owned())
}
