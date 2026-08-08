use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine as _;
use rand::RngCore;
use reqwest::blocking::Client;
use reqwest::Url;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const CALLBACK_PATH: &str = "/oauth/callback";
const KEYCHAIN_SERVICE: &str = "com.hermes.desktop.workspace-auth.v1";
const KEYCHAIN_ACCOUNT: &str = "current";
const CALLBACK_TIMEOUT: Duration = Duration::from_secs(600);
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_REQUEST_LINE_BYTES: usize = 4096;
const MAX_HEADER_LINE_BYTES: usize = 8192;
const MAX_HEADER_LINES: usize = 64;

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

struct PkceCredentials {
    verifier: String,
    challenge: String,
    state: String,
}

struct AuthorizationCallback {
    code: Option<String>,
    state: String,
    error: Option<String>,
}

pub fn status() -> WorkspaceAuthStatus {
    let stored = match load_tokens() {
        Ok(Some(value)) => value,
        _ => return disconnected_status(),
    };
    let now = unix_seconds();
    let authenticated = stored.expires_at > now.saturating_add(30)
        && stored.token_type.eq_ignore_ascii_case("bearer")
        && !stored.access_token.is_empty()
        && !stored.refresh_token.is_empty();
    WorkspaceAuthStatus {
        authenticated,
        endpoint: Some(stored.endpoint),
        user_id: Some(stored.user_id),
        provider: Some(stored.provider),
        expires_at_epoch_seconds: Some(stored.expires_at),
    }
}

pub fn connect(endpoint: &str) -> Result<WorkspaceAuthStatus, String> {
    let endpoint = normalize_endpoint(endpoint)?;
    let credentials = generate_pkce();
    let listener = TcpListener::bind(SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0))
        .map_err(|_| "Could not start the secure local sign-in callback.".to_owned())?;
    listener
        .set_nonblocking(true)
        .map_err(|_| "Could not configure the secure local sign-in callback.".to_owned())?;
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
    )?;
    open_browser(&authorization_url)?;
    let callback = wait_for_callback(&listener, port)?;
    if !constant_time_eq(callback.state.as_bytes(), credentials.state.as_bytes()) {
        return Err("Hermes sign-in response could not be verified.".to_owned());
    }
    if let Some(error) = callback.error {
        return Err(format!("Hermes sign-in was denied ({error})."));
    }
    let code = callback
        .code
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| "Hermes sign-in returned no authorization code.".to_owned())?;
    let tokens = exchange_code(&endpoint, &code, &credentials.verifier)?;
    let stored = StoredWorkspaceTokens {
        schema_version: 1,
        endpoint: endpoint.to_string().trim_end_matches('/').to_owned(),
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
        token_type: tokens.token_type,
        expires_at: tokens.expires_at,
        provider: tokens.provider,
        user_id: tokens.user_id,
    };
    validate_tokens(&stored)?;
    store_tokens(&stored)?;
    Ok(status())
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
    rand::thread_rng().fill_bytes(&mut verifier_bytes);
    rand::thread_rng().fill_bytes(&mut state_bytes);
    let verifier = URL_SAFE_NO_PAD.encode(verifier_bytes);
    let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let state = URL_SAFE_NO_PAD.encode(state_bytes);
    PkceCredentials {
        verifier,
        challenge,
        state,
    }
}

fn build_authorization_url(
    endpoint: &Url,
    redirect_uri: &str,
    challenge: &str,
    state: &str,
) -> Result<Url, String> {
    let mut url = endpoint
        .join("auth/native/authorize")
        .map_err(|_| "Hermes Cloud authorization URL could not be created.".to_owned())?;
    url.query_pairs_mut()
        .append_pair("code_challenge", challenge)
        .append_pair("code_challenge_method", "S256")
        .append_pair("redirect_uri", redirect_uri)
        .append_pair("state", state);
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

fn wait_for_callback(listener: &TcpListener, port: u16) -> Result<AuthorizationCallback, String> {
    let deadline = std::time::Instant::now() + CALLBACK_TIMEOUT;
    loop {
        match listener.accept() {
            Ok((mut stream, peer)) => {
                if !peer.ip().is_loopback() {
                    continue;
                }
                stream
                    .set_read_timeout(Some(Duration::from_secs(5)))
                    .map_err(|_| "Hermes sign-in callback could not be configured.".to_owned())?;
                let callback = read_callback(&mut stream, port)?;
                write_browser_response(&mut stream, callback.error.is_none());
                return Ok(callback);
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                if std::time::Instant::now() >= deadline {
                    return Err("Hermes sign-in timed out. Try again.".to_owned());
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => return Err("Hermes sign-in callback failed.".to_owned()),
        }
    }
}

fn read_callback(stream: &mut TcpStream, port: u16) -> Result<AuthorizationCallback, String> {
    let mut reader = BufReader::new(stream.try_clone().map_err(|_| "Hermes sign-in callback failed.".to_owned())?);
    let request_line = read_limited_line(&mut reader, MAX_REQUEST_LINE_BYTES)?;
    for _ in 0..MAX_HEADER_LINES {
        let line = read_limited_line(&mut reader, MAX_HEADER_LINE_BYTES)?;
        if line.is_empty() {
            break;
        }
    }
    let mut parts = request_line.splitn(3, ' ');
    if parts.next() != Some("GET") {
        return Err("Hermes sign-in callback request is invalid.".to_owned());
    }
    let target = parts
        .next()
        .ok_or_else(|| "Hermes sign-in callback request is invalid.".to_owned())?;
    if !target.starts_with('/') || target.starts_with("//") {
        return Err("Hermes sign-in callback target is invalid.".to_owned());
    }
    let url = Url::parse(&format!("http://127.0.0.1:{port}{target}"))
        .map_err(|_| "Hermes sign-in callback URL is invalid.".to_owned())?;
    if url.path() != CALLBACK_PATH {
        return Err("Hermes sign-in callback path is invalid.".to_owned());
    }
    let mut code = None;
    let mut state = String::new();
    let mut error = None;
    for (key, value) in url.query_pairs() {
        match key.as_ref() {
            "code" => code = Some(value.into_owned()),
            "state" => state = value.into_owned(),
            "error" => error = Some(value.into_owned()),
            _ => {}
        }
    }
    Ok(AuthorizationCallback { code, state, error })
}

fn read_limited_line<R: BufRead>(reader: &mut R, maximum: usize) -> Result<String, String> {
    let mut buffer = Vec::new();
    let bytes = reader
        .read_until(b'\n', &mut buffer)
        .map_err(|_| "Hermes sign-in callback could not be read.".to_owned())?;
    if bytes == 0 || buffer.len() > maximum {
        return Err("Hermes sign-in callback line is invalid.".to_owned());
    }
    while matches!(buffer.last(), Some(b'\n' | b'\r')) {
        buffer.pop();
    }
    String::from_utf8(buffer).map_err(|_| "Hermes sign-in callback is not ASCII/UTF-8.".to_owned())
}

fn write_browser_response(stream: &mut TcpStream, success: bool) {
    let title = if success {
        "Hermes sign-in received"
    } else {
        "Hermes sign-in could not be verified"
    };
    let body = format!(
        "<!doctype html><meta charset=\"utf-8\"><title>{title}</title><p>{title}. You can return to Hermes Desktop.</p>"
    );
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: {}\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();
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
    let payload: TokenPayload = response
        .json()
        .map_err(|_| "Hermes Cloud returned an invalid sign-in response.".to_owned())?;
    Ok(payload)
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

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0u8;
    for (&a, &b) in left.iter().zip(right.iter()) {
        diff |= a ^ b;
    }
    diff == 0
}

#[cfg(target_os = "macos")]
fn load_tokens() -> Result<Option<StoredWorkspaceTokens>, String> {
    use security_framework::passwords::get_generic_password;
    match get_generic_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT) {
        Ok(bytes) => {
            let value = serde_json::from_slice::<StoredWorkspaceTokens>(&bytes)
                .map_err(|_| "Stored Hermes workspace credentials are invalid.".to_owned())?;
            validate_tokens(&value)?;
            Ok(Some(value))
        }
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
