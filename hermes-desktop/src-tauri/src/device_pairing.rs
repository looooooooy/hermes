use hermes_runtime_manager::platform::DefaultInstallLayout;
use hermes_runtime_manager::ports::InstallLayout;
use rand::RngCore;
use reqwest::blocking::{Client, Response};
use reqwest::header::{ACCEPT, AUTHORIZATION, CACHE_CONTROL, CONTENT_TYPE};
use reqwest::{StatusCode, Url};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Output};
use std::thread;
use std::time::Duration;

use crate::workspace_session;

const MAX_JSON_BYTES: usize = 64 * 1024;
const PAIRING_STATE_MAX_BYTES: u64 = 16 * 1024;
const HTTP_TIMEOUT: Duration = Duration::from_secs(15);
const PAIRING_POLL_ATTEMPTS: usize = 30;
const PAIRING_POLL_INTERVAL: Duration = Duration::from_millis(300);
const DEFAULT_PROFILE: &str = "default";

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DevicePairingStatus {
    pub paired: bool,
    pub state: String,
    pub activation_state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub credential_fingerprint: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub workspace_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent_key: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PairedProjection {
    version: u8,
    tenant_id: String,
    device_id: String,
    credential_id: String,
    agent_id: String,
    scopes: Vec<String>,
    key_handle: String,
    credential_fingerprint: String,
    token_expires_at: String,
    lifecycle_state: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PairingContextResponse {
    targets: Vec<PairingTarget>,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct PairingTarget {
    workspace_id: String,
    workspace_key: String,
    workspace_display_name: String,
    agent_id: String,
    agent_key: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnerPairingResponse {
    pairing_offer_id: String,
    pairing_session_id: String,
    state: String,
    activation_state: String,
    binding: OwnerPairingBinding,
    display_name: String,
    platform_family: String,
    connector_version: String,
    key_algorithm: String,
    credential_fingerprint: String,
    expires_at: String,
    revision: u64,
    device_revision: u64,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OwnerPairingBinding {
    tenant_id: String,
    user_id: String,
    workspace_id: String,
    agent_id: String,
    device_id: String,
    credential_id: String,
    scopes: Vec<String>,
}

#[derive(Debug)]
struct PairStartOutput {
    pairing_code: String,
    credential_fingerprint: String,
    expires_at: String,
}

#[derive(Debug)]
struct PairStatusOutput {
    state: String,
    activation_state: String,
    credential_fingerprint: String,
    expires_at: String,
}

#[derive(Debug)]
enum PairingHelper {
    Installed {
        python: PathBuf,
        application_root: PathBuf,
        state_root: PathBuf,
        api_endpoint: String,
        cloud_endpoint: String,
        connector_version: String,
    },
    #[cfg(debug_assertions)]
    DevelopmentPython {
        python: PathBuf,
        project_root: PathBuf,
        application_root: PathBuf,
        state_root: PathBuf,
        api_endpoint: String,
        cloud_endpoint: String,
        connector_version: String,
    },
    #[cfg(debug_assertions)]
    DevelopmentUv {
        uv: PathBuf,
        project_root: PathBuf,
        application_root: PathBuf,
        state_root: PathBuf,
        api_endpoint: String,
        cloud_endpoint: String,
        connector_version: String,
    },
}

#[derive(Debug, Deserialize)]
struct BootstrapPayloadManifest {
    platform: String,
    components: BootstrapComponents,
}

#[derive(Debug, Deserialize)]
struct BootstrapComponents {
    toolchain: BootstrapToolchain,
    pairing_bootstrap: BootstrapPairing,
}

#[derive(Debug, Deserialize)]
struct BootstrapToolchain {
    python_path: String,
    uv_path: String,
}

#[derive(Debug, Deserialize)]
struct BootstrapPairing {
    root: String,
    manifest_path: String,
    manifest_sha256: String,
    connector_version: String,
    network_dependency_install: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PairingBootstrapManifest {
    schema_version: u8,
    scope: String,
    target: String,
    platform: String,
    architecture: String,
    python_tag: String,
    connector_version: String,
    connector_lock_sha256: String,
    connector_wheel: String,
    entrypoint_module: String,
    allowed_actions: Vec<String>,
    credential_authority: String,
    network_dependency_install: bool,
    artifacts: Vec<PairingArtifact>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PairingArtifact {
    filename: String,
    sha256: String,
    size_bytes: u64,
}

pub(crate) fn evidence() -> DevicePairingStatus {
    match paired_projection_path().and_then(|path| read_paired_projection(&path)) {
        Ok(Some(projection)) if projection.lifecycle_state == "active" => DevicePairingStatus {
            paired: true,
            state: "paired".to_owned(),
            activation_state: "active".to_owned(),
            credential_fingerprint: Some(projection.credential_fingerprint),
            expires_at: Some(projection.token_expires_at),
            workspace_name: None,
            agent_key: None,
        },
        _ => unpaired_status(),
    }
}

pub(crate) fn pair() -> Result<DevicePairingStatus, String> {
    if evidence().paired {
        return Ok(evidence());
    }
    let workspace = workspace_session::load()?;
    let endpoint = workspace_root(&workspace.endpoint)?;
    let client = Client::builder()
        .connect_timeout(HTTP_TIMEOUT)
        .timeout(HTTP_TIMEOUT)
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Hermes secure pairing HTTP client could not be initialized.".to_owned())?;
    let target = load_pairing_target(&client, &endpoint, &workspace.access_token)?;
    let helper = PairingHelper::prepare(&endpoint)?;
    let started = helper.start()?;

    let result = (|| {
        let claimed = claim_pairing(
            &client,
            &endpoint,
            &workspace.access_token,
            &target,
            &started,
        )?;
        validate_claim(&claimed, &target, &started)?;
        let confirmed = confirm_pairing(
            &client,
            &endpoint,
            &workspace.access_token,
            &claimed,
            &started.credential_fingerprint,
        )?;
        validate_confirm(&confirmed, &target, &started)?;

        let mut last = None;
        for _ in 0..PAIRING_POLL_ATTEMPTS {
            let status = helper.status()?;
            if status.credential_fingerprint != started.credential_fingerprint {
                return Err("Hermes device fingerprint changed during pairing.".to_owned());
            }
            if status.activation_state == "active" {
                let paired = evidence();
                if !paired.paired {
                    return Err(
                        "Hermes pairing completed but the local paired evidence is missing."
                            .to_owned(),
                    );
                }
                return Ok(DevicePairingStatus {
                    workspace_name: Some(target.workspace_display_name.clone()),
                    agent_key: Some(target.agent_key.clone()),
                    ..paired
                });
            }
            if status.activation_state == "blocked"
                || matches!(status.state.as_str(), "cancelled" | "expired")
            {
                return Err("Hermes Cloud blocked or expired this device pairing.".to_owned());
            }
            last = Some(status);
            thread::sleep(PAIRING_POLL_INTERVAL);
        }
        let detail = last
            .map(|status| format!("{} / {}", status.state, status.activation_state))
            .unwrap_or_else(|| "no status".to_owned());
        Err(format!("Hermes device pairing did not activate in time ({detail})."))
    })();

    if result.is_err() {
        let _ = helper.cancel();
    }
    result
}

pub(crate) fn cancel() -> Result<DevicePairingStatus, String> {
    let workspace = workspace_session::load()?;
    let endpoint = workspace_root(&workspace.endpoint)?;
    let helper = PairingHelper::prepare(&endpoint)?;
    helper.cancel()?;
    Ok(unpaired_status())
}

impl PairingHelper {
    fn prepare(endpoint: &Url) -> Result<Self, String> {
        let layout = DefaultInstallLayout::discover().map_err(|error| error.to_string())?;
        let application_root = layout.application_root().map_err(|error| error.to_string())?;
        let state_root = connector_state_root(&application_root);
        let api_endpoint = endpoint.as_str().to_owned();
        let cloud_endpoint = connector_cloud_endpoint(endpoint)?;

        let installed = application_root.join("bootstrap/current");
        if installed.is_dir() {
            return prepare_installed_helper(
                &installed,
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
            );
        }

        #[cfg(debug_assertions)]
        {
            return prepare_development_helper(
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
            );
        }
        #[cfg(not(debug_assertions))]
        Err("Hermes pairing bootstrap payload is not installed.".to_owned())
    }

    fn start(&self) -> Result<PairStartOutput, String> {
        let fields = parse_helper_fields(self.execute("start")?)?;
        let pairing_code = required_field(&fields, "pairing_code")?;
        let credential_fingerprint = required_field(&fields, "credential_fingerprint")?;
        let expires_at = required_field(&fields, "expires_at")?;
        if !valid_pairing_code(&pairing_code) || !valid_fingerprint(&credential_fingerprint) {
            return Err("Hermes pairing helper returned invalid pairing evidence.".to_owned());
        }
        Ok(PairStartOutput {
            pairing_code,
            credential_fingerprint,
            expires_at,
        })
    }

    fn status(&self) -> Result<PairStatusOutput, String> {
        let fields = parse_helper_fields(self.execute("status")?)?;
        let state = required_field(&fields, "pairing_state")?;
        let activation_state = required_field(&fields, "activation_state")?;
        let credential_fingerprint = required_field(&fields, "credential_fingerprint")?;
        let expires_at = required_field(&fields, "expires_at")?;
        if !valid_fingerprint(&credential_fingerprint) {
            return Err("Hermes pairing helper returned invalid device evidence.".to_owned());
        }
        Ok(PairStatusOutput {
            state,
            activation_state,
            credential_fingerprint,
            expires_at,
        })
    }

    fn cancel(&self) -> Result<(), String> {
        let fields = parse_helper_fields(self.execute("cancel")?)?;
        let state = required_field(&fields, "pairing_state")?;
        if state != "cancelled" {
            return Err("Hermes pairing helper did not cancel the pending pairing.".to_owned());
        }
        Ok(())
    }

    fn execute(&self, action: &str) -> Result<Output, String> {
        match self {
            Self::Installed {
                python,
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
                connector_version,
            } => run_python_helper(
                python,
                None,
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
                connector_version,
                action,
            ),
            #[cfg(debug_assertions)]
            Self::DevelopmentPython {
                python,
                project_root,
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
                connector_version,
            } => run_python_helper(
                python,
                Some(project_root),
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
                connector_version,
                action,
            ),
            #[cfg(debug_assertions)]
            Self::DevelopmentUv {
                uv,
                project_root,
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
                connector_version,
            } => run_uv_helper(
                uv,
                project_root,
                application_root,
                state_root,
                api_endpoint,
                cloud_endpoint,
                connector_version,
                action,
            ),
        }
    }
}

fn prepare_installed_helper(
    bootstrap_root: &Path,
    application_root: PathBuf,
    state_root: PathBuf,
    api_endpoint: String,
    cloud_endpoint: String,
) -> Result<PairingHelper, String> {
    let manifest_path = bootstrap_root.join("BOOTSTRAP-PAYLOAD.json");
    let manifest: BootstrapPayloadManifest = read_json_file(&manifest_path, MAX_JSON_BYTES as u64)?;
    if manifest.platform != std::env::consts::OS {
        return Err("Hermes pairing bootstrap platform does not match this device.".to_owned());
    }
    if manifest.components.pairing_bootstrap.network_dependency_install {
        return Err("Hermes pairing bootstrap attempted to enable network dependency installation.".to_owned());
    }
    let pairing_manifest_path = safe_join(
        bootstrap_root,
        &manifest.components.pairing_bootstrap.manifest_path,
    )?;
    verify_sha256(
        &pairing_manifest_path,
        &manifest.components.pairing_bootstrap.manifest_sha256,
    )?;
    let pairing_manifest: PairingBootstrapManifest =
        read_json_file(&pairing_manifest_path, MAX_JSON_BYTES as u64)?;
    validate_pairing_bootstrap(&pairing_manifest, bootstrap_root)?;
    if pairing_manifest.connector_version != manifest.components.pairing_bootstrap.connector_version {
        return Err("Hermes pairing bootstrap Connector version binding is invalid.".to_owned());
    }

    let private_python = safe_join(bootstrap_root, &manifest.components.toolchain.python_path)?;
    let private_uv = safe_join(bootstrap_root, &manifest.components.toolchain.uv_path)?;
    require_regular_file(&private_python)?;
    require_regular_file(&private_uv)?;
    let pairing_root = safe_join(bootstrap_root, &manifest.components.pairing_bootstrap.root)?;
    let wheels = pairing_root.join("wheels");
    let environment_root = application_root.join("bootstrap/pairing-env");
    let environment_python = pairing_environment_python(&environment_root);
    fs::create_dir_all(
        environment_root
            .parent()
            .ok_or_else(|| "Hermes pairing environment path is invalid.".to_owned())?,
    )
    .map_err(|_| "Hermes pairing environment directory could not be created.".to_owned())?;

    let mut venv = Command::new(&private_uv);
    venv.args([
        "venv",
        "--python",
        private_python
            .to_str()
            .ok_or_else(|| "Hermes Private Python path is invalid.".to_owned())?,
        environment_root
            .to_str()
            .ok_or_else(|| "Hermes pairing environment path is invalid.".to_owned())?,
    ]);
    apply_bootstrap_process_environment(&mut venv);
    let created = venv
        .output()
        .map_err(|_| "Hermes pairing environment could not be created.".to_owned())?;
    if !created.status.success() {
        return Err("Hermes pairing environment could not be created.".to_owned());
    }

    let mut install = Command::new(&private_uv);
    install.args([
        "pip",
        "install",
        "--python",
        environment_python
            .to_str()
            .ok_or_else(|| "Hermes pairing Python path is invalid.".to_owned())?,
        "--no-index",
        "--find-links",
        wheels
            .to_str()
            .ok_or_else(|| "Hermes pairing wheelhouse path is invalid.".to_owned())?,
        &format!("hermes-connector=={}", pairing_manifest.connector_version),
    ]);
    apply_bootstrap_process_environment(&mut install);
    let installed = install
        .output()
        .map_err(|_| "Hermes pairing helper could not be installed offline.".to_owned())?;
    if !installed.status.success() {
        return Err("Hermes pairing helper could not be installed offline.".to_owned());
    }
    require_regular_file(&environment_python)?;

    Ok(PairingHelper::Installed {
        python: environment_python,
        application_root,
        state_root,
        api_endpoint,
        cloud_endpoint,
        connector_version: pairing_manifest.connector_version,
    })
}

#[cfg(debug_assertions)]
fn prepare_development_helper(
    application_root: PathBuf,
    state_root: PathBuf,
    api_endpoint: String,
    cloud_endpoint: String,
) -> Result<PairingHelper, String> {
    let project_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../hermes-connector")
        .canonicalize()
        .map_err(|_| "Hermes Connector development project could not be found.".to_owned())?;
    let connector_version = development_connector_version(&project_root)?;
    let python = project_root.join(".venv/bin/python");
    if python.is_file() {
        return Ok(PairingHelper::DevelopmentPython {
            python,
            project_root,
            application_root,
            state_root,
            api_endpoint,
            cloud_endpoint,
            connector_version,
        });
    }
    let uv = development_uv_path();
    Ok(PairingHelper::DevelopmentUv {
        uv,
        project_root,
        application_root,
        state_root,
        api_endpoint,
        cloud_endpoint,
        connector_version,
    })
}

#[cfg(debug_assertions)]
fn development_connector_version(project_root: &Path) -> Result<String, String> {
    let pyproject = fs::read_to_string(project_root.join("pyproject.toml"))
        .map_err(|_| "Hermes Connector development metadata could not be read.".to_owned())?;
    let prefix = "version = \"";
    pyproject
        .lines()
        .find_map(|line| line.trim().strip_prefix(prefix))
        .and_then(|value| value.strip_suffix('"'))
        .filter(|value| !value.is_empty() && value.len() <= 64)
        .map(str::to_owned)
        .ok_or_else(|| "Hermes Connector development version is invalid.".to_owned())
}

#[cfg(debug_assertions)]
fn development_uv_path() -> PathBuf {
    if let Some(path) = std::env::var_os("HERMES_DESKTOP_DEV_UV") {
        return PathBuf::from(path);
    }
    if let Some(home) = std::env::var_os("HOME") {
        let home = PathBuf::from(home);
        for candidate in [home.join(".local/bin/uv"), home.join(".cargo/bin/uv")] {
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    for candidate in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv"] {
        let path = PathBuf::from(candidate);
        if path.is_file() {
            return path;
        }
    }
    PathBuf::from("uv")
}

fn run_python_helper(
    python: &Path,
    project_root: Option<&PathBuf>,
    application_root: &Path,
    state_root: &Path,
    api_endpoint: &str,
    cloud_endpoint: &str,
    connector_version: &str,
    action: &str,
) -> Result<Output, String> {
    let mut command = Command::new(python);
    command.args(["-I", "-m", "hermes_connector.cli", "pair", action]);
    if let Some(project) = project_root {
        command.current_dir(project);
    }
    apply_connector_environment(
        &mut command,
        application_root,
        state_root,
        api_endpoint,
        cloud_endpoint,
        connector_version,
    );
    run_pairing_process(command)
}

#[cfg(debug_assertions)]
fn run_uv_helper(
    uv: &Path,
    project_root: &Path,
    application_root: &Path,
    state_root: &Path,
    api_endpoint: &str,
    cloud_endpoint: &str,
    connector_version: &str,
    action: &str,
) -> Result<Output, String> {
    let mut command = Command::new(uv);
    command
        .args(["run", "--locked", "python", "-I", "-m", "hermes_connector.cli", "pair", action])
        .current_dir(project_root);
    apply_connector_environment(
        &mut command,
        application_root,
        state_root,
        api_endpoint,
        cloud_endpoint,
        connector_version,
    );
    run_pairing_process(command)
}

fn run_pairing_process(mut command: Command) -> Result<Output, String> {
    let output = command
        .output()
        .map_err(|_| "Hermes Connector pairing helper could not be started.".to_owned())?;
    if !output.status.success() {
        return Err("Hermes Connector pairing helper failed safely.".to_owned());
    }
    Ok(output)
}

fn apply_connector_environment(
    command: &mut Command,
    application_root: &Path,
    state_root: &Path,
    api_endpoint: &str,
    cloud_endpoint: &str,
    connector_version: &str,
) {
    let home = std::env::var_os("HOME").unwrap_or_default();
    command.env_clear();
    command.env("HOME", home);
    command.env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin");
    command.env("HERMES_HOME", application_root);
    command.env("HERMES_CONNECTOR_API_ENDPOINT", api_endpoint);
    command.env("HERMES_CONNECTOR_CLOUD_ENDPOINT", cloud_endpoint);
    command.env("HERMES_CONNECTOR_DISPLAY_NAME", local_device_name());
    command.env("HERMES_CONNECTOR_PROFILE", DEFAULT_PROFILE);
    command.env("HERMES_CONNECTOR_VERSION", connector_version);
    command.env("HERMES_CONNECTOR_STATE_DIR", state_root);
    command.env("HERMES_CONNECTOR_DATABASE_FILE", state_root.join("connector.sqlite3"));
    command.env("HERMES_CONNECTOR_LOCK_FILE", state_root.join("connector.lock"));
    command.env("HERMES_CONNECTOR_CREDENTIAL_STORE", "keychain");
}

fn apply_bootstrap_process_environment(command: &mut Command) {
    let home = std::env::var_os("HOME").unwrap_or_default();
    command.env_clear();
    command.env("HOME", home);
    command.env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin");
    command.env("UV_NO_INDEX", "1");
    command.env("UV_NO_PROGRESS", "1");
}

fn load_pairing_target(
    client: &Client,
    endpoint: &Url,
    access_token: &str,
) -> Result<PairingTarget, String> {
    let url = endpoint
        .join("api/onboarding/pairing-context")
        .map_err(|_| "Hermes pairing context URL could not be created.".to_owned())?;
    let response = client
        .get(url)
        .header(ACCEPT, "application/json")
        .header(AUTHORIZATION, format!("Bearer {access_token}"))
        .send()
        .map_err(|_| "Hermes Cloud could not be reached for device pairing.".to_owned())?;
    if response.status() != StatusCode::OK {
        return Err(format!(
            "Hermes Cloud pairing context is unavailable (HTTP {}).",
            response.status().as_u16()
        ));
    }
    if response
        .headers()
        .get(CACHE_CONTROL)
        .and_then(|value| value.to_str().ok())
        != Some("no-store")
    {
        return Err("Hermes Cloud pairing context was not marked private.".to_owned());
    }
    let body: PairingContextResponse = read_json_response(response)?;
    match body.targets.as_slice() {
        [target] => {
            validate_target(target)?;
            Ok(target.clone())
        }
        [] => Err("No active Hermes workspace Agent is available for this account.".to_owned()),
        _ => Err(
            "This account has multiple Hermes pairing targets; target selection is required."
                .to_owned(),
        ),
    }
}

fn claim_pairing(
    client: &Client,
    endpoint: &Url,
    access_token: &str,
    target: &PairingTarget,
    started: &PairStartOutput,
) -> Result<OwnerPairingResponse, String> {
    let url = endpoint
        .join("api/device-pairing/claims")
        .map_err(|_| "Hermes pairing claim URL could not be created.".to_owned())?;
    let response = client
        .post(url)
        .header(ACCEPT, "application/json")
        .header(CONTENT_TYPE, "application/json")
        .header(AUTHORIZATION, format!("Bearer {access_token}"))
        .header("Idempotency-Key", uuid_v4())
        .json(&serde_json::json!({
            "pairing_code": started.pairing_code,
            "workspace_id": target.workspace_id,
            "agent_id": target.agent_id,
            "device_display_name": local_device_name(),
            "scopes": ["session.observe", "session.control.request"],
            "expected_revision": 1
        }))
        .send()
        .map_err(|_| "Hermes Cloud could not claim this device pairing.".to_owned())?;
    require_success(response, "Hermes Cloud rejected the device pairing claim")
}

fn confirm_pairing(
    client: &Client,
    endpoint: &Url,
    access_token: &str,
    claimed: &OwnerPairingResponse,
    fingerprint: &str,
) -> Result<OwnerPairingResponse, String> {
    let url = endpoint
        .join(&format!(
            "api/device-pairing/sessions/{}/confirm",
            claimed.pairing_session_id
        ))
        .map_err(|_| "Hermes pairing confirmation URL could not be created.".to_owned())?;
    let response = client
        .post(url)
        .header(ACCEPT, "application/json")
        .header(CONTENT_TYPE, "application/json")
        .header(AUTHORIZATION, format!("Bearer {access_token}"))
        .header("Idempotency-Key", uuid_v4())
        .json(&serde_json::json!({
            "credential_fingerprint": fingerprint,
            "expected_revision": claimed.revision
        }))
        .send()
        .map_err(|_| "Hermes Cloud could not confirm this device pairing.".to_owned())?;
    require_success(response, "Hermes Cloud rejected the device pairing confirmation")
}

fn validate_claim(
    response: &OwnerPairingResponse,
    target: &PairingTarget,
    started: &PairStartOutput,
) -> Result<(), String> {
    validate_owner_response(response, target, started)?;
    if response.state != "claimed" || response.activation_state != "waiting_owner_confirmation" {
        return Err("Hermes Cloud returned an unexpected pairing claim state.".to_owned());
    }
    Ok(())
}

fn validate_confirm(
    response: &OwnerPairingResponse,
    target: &PairingTarget,
    started: &PairStartOutput,
) -> Result<(), String> {
    validate_owner_response(response, target, started)?;
    if response.state != "confirmed" || response.activation_state != "awaiting_proof" {
        return Err("Hermes Cloud returned an unexpected pairing confirmation state.".to_owned());
    }
    Ok(())
}

fn validate_owner_response(
    response: &OwnerPairingResponse,
    target: &PairingTarget,
    started: &PairStartOutput,
) -> Result<(), String> {
    if response.credential_fingerprint != started.credential_fingerprint
        || response.binding.workspace_id != target.workspace_id
        || response.binding.agent_id != target.agent_id
        || response.key_algorithm != "Ed25519"
        || response.revision == 0
        || response.device_revision == 0
        || !valid_uuid(&response.pairing_offer_id)
        || !valid_uuid(&response.pairing_session_id)
        || !valid_uuid(&response.binding.tenant_id)
        || !valid_uuid(&response.binding.user_id)
        || !valid_uuid(&response.binding.device_id)
        || !valid_uuid(&response.binding.credential_id)
        || response.display_name.is_empty()
        || response.platform_family != "macos"
        || response.connector_version.is_empty()
        || response.expires_at != started.expires_at
        || response.binding.scopes.is_empty()
    {
        return Err("Hermes Cloud pairing binding did not match this device.".to_owned());
    }
    Ok(())
}

fn require_success<T: for<'de> Deserialize<'de>>(
    response: Response,
    label: &str,
) -> Result<T, String> {
    if !response.status().is_success() {
        return Err(format!("{label} (HTTP {}).", response.status().as_u16()));
    }
    read_json_response(response)
}

fn read_json_response<T: for<'de> Deserialize<'de>>(mut response: Response) -> Result<T, String> {
    let content_type = response
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if content_type != "application/json" {
        return Err("Hermes Cloud returned a non-JSON pairing response.".to_owned());
    }
    if let Some(length) = response.content_length() {
        if length > MAX_JSON_BYTES as u64 {
            return Err("Hermes Cloud pairing response exceeded the size limit.".to_owned());
        }
    }
    let mut limited = response.by_ref().take(MAX_JSON_BYTES as u64 + 1);
    let mut bytes = Vec::new();
    limited
        .read_to_end(&mut bytes)
        .map_err(|_| "Hermes Cloud pairing response could not be read.".to_owned())?;
    if bytes.is_empty() || bytes.len() > MAX_JSON_BYTES {
        return Err("Hermes Cloud pairing response is invalid.".to_owned());
    }
    serde_json::from_slice(&bytes)
        .map_err(|_| "Hermes Cloud pairing response did not match the expected contract.".to_owned())
}

fn workspace_root(raw: &str) -> Result<Url, String> {
    let mut text = raw.trim().to_owned();
    if !text.ends_with('/') {
        text.push('/');
    }
    let url = Url::parse(&text).map_err(|_| "Stored Hermes workspace URL is invalid.".to_owned())?;
    if url.scheme() != "https"
        || url.host_str().is_none()
        || url.username() != ""
        || url.password().is_some()
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return Err("Stored Hermes workspace URL is unsafe.".to_owned());
    }
    Ok(url)
}

fn connector_cloud_endpoint(endpoint: &Url) -> Result<String, String> {
    let mut url = endpoint
        .join("internal/connector/ws")
        .map_err(|_| "Hermes Connector Cloud URL could not be created.".to_owned())?;
    url.set_scheme("wss")
        .map_err(|_| "Hermes Connector Cloud URL could not be secured.".to_owned())?;
    Ok(url.to_string())
}

fn connector_state_root(application_root: &Path) -> PathBuf {
    application_root
        .join("connector/profiles")
        .join(DEFAULT_PROFILE)
        .join("state")
}

fn paired_projection_path() -> Result<PathBuf, String> {
    let layout = DefaultInstallLayout::discover().map_err(|error| error.to_string())?;
    let application_root = layout.application_root().map_err(|error| error.to_string())?;
    Ok(connector_state_root(&application_root).join("paired.json"))
}

fn read_paired_projection(path: &Path) -> Result<Option<PairedProjection>, String> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err("Hermes paired device evidence could not be inspected.".to_owned()),
    };
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() > PAIRING_STATE_MAX_BYTES {
        return Err("Hermes paired device evidence is unsafe.".to_owned());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o777 != 0o600 {
            return Err("Hermes paired device evidence permissions are unsafe.".to_owned());
        }
    }
    let bytes = fs::read(path).map_err(|_| "Hermes paired device evidence could not be read.".to_owned())?;
    let projection: PairedProjection = serde_json::from_slice(&bytes)
        .map_err(|_| "Hermes paired device evidence is invalid.".to_owned())?;
    validate_paired_projection(&projection)?;
    Ok(Some(projection))
}

fn validate_paired_projection(projection: &PairedProjection) -> Result<(), String> {
    let scopes_valid = !projection.scopes.is_empty()
        && projection.scopes.len() <= 2
        && projection.scopes.iter().all(|scope| {
            matches!(scope.as_str(), "session.observe" | "session.control.request")
        });
    if projection.version != 1
        || !valid_uuid(&projection.tenant_id)
        || !valid_uuid(&projection.device_id)
        || !valid_uuid(&projection.credential_id)
        || !valid_uuid(&projection.agent_id)
        || !scopes_valid
        || !projection.key_handle.starts_with("hermes-device-key:v1:")
        || !valid_fingerprint(&projection.credential_fingerprint)
        || !projection.token_expires_at.ends_with('Z')
        || !matches!(
            projection.lifecycle_state.as_str(),
            "active" | "auth_blocked" | "suspended" | "revoked"
        )
    {
        return Err("Hermes paired device evidence is invalid.".to_owned());
    }
    Ok(())
}

fn validate_target(target: &PairingTarget) -> Result<(), String> {
    if !valid_uuid(&target.workspace_id)
        || !valid_uuid(&target.agent_id)
        || target.workspace_key.is_empty()
        || target.workspace_key.len() > 128
        || target.workspace_display_name.is_empty()
        || target.workspace_display_name.len() > 256
        || target.agent_key.is_empty()
        || target.agent_key.len() > 128
    {
        return Err("Hermes Cloud returned an invalid pairing target.".to_owned());
    }
    Ok(())
}

fn parse_helper_fields(output: Output) -> Result<BTreeMap<String, String>, String> {
    let mut fields = BTreeMap::new();
    for raw in [&output.stdout[..], &output.stderr[..]] {
        let text = std::str::from_utf8(raw)
            .map_err(|_| "Hermes pairing helper returned invalid text.".to_owned())?;
        for line in text.lines() {
            let Some((key, value)) = line.split_once('=') else {
                continue;
            };
            if matches!(
                key,
                "pairing_code"
                    | "credential_fingerprint"
                    | "expires_at"
                    | "pairing_state"
                    | "activation_state"
            ) {
                if fields.insert(key.to_owned(), value.to_owned()).is_some() {
                    return Err("Hermes pairing helper returned duplicate evidence.".to_owned());
                }
            }
        }
    }
    Ok(fields)
}

fn required_field(fields: &BTreeMap<String, String>, key: &str) -> Result<String, String> {
    fields
        .get(key)
        .filter(|value| !value.is_empty() && value.len() <= 512)
        .cloned()
        .ok_or_else(|| format!("Hermes pairing helper did not return {key}."))
}

fn unpaired_status() -> DevicePairingStatus {
    DevicePairingStatus {
        paired: false,
        state: "not-paired".to_owned(),
        activation_state: "inactive".to_owned(),
        credential_fingerprint: None,
        expires_at: None,
        workspace_name: None,
        agent_key: None,
    }
}

fn local_device_name() -> String {
    std::env::var("COMPUTERNAME")
        .or_else(|_| std::env::var("HOSTNAME"))
        .unwrap_or_else(|_| "Hermes Desktop".to_owned())
        .chars()
        .filter(|character| !character.is_control())
        .take(128)
        .collect::<String>()
        .trim()
        .to_owned()
        .pipe(|value| if value.is_empty() { "Hermes Desktop".to_owned() } else { value })
}

trait Pipe: Sized {
    fn pipe<T>(self, operation: impl FnOnce(Self) -> T) -> T {
        operation(self)
    }
}
impl<T> Pipe for T {}

fn uuid_v4() -> String {
    let mut bytes = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
    )
}

fn valid_uuid(value: &str) -> bool {
    if value.len() != 36 {
        return false;
    }
    value.bytes().enumerate().all(|(index, byte)| match index {
        8 | 13 | 18 | 23 => byte == b'-',
        _ => byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase(),
    })
}

fn valid_pairing_code(value: &str) -> bool {
    value.len() == 9
        && value.as_bytes().get(4) == Some(&b'-')
        && value
            .bytes()
            .enumerate()
            .all(|(index, byte)| index == 4 || byte.is_ascii_digit() || (b'A'..=b'Z').contains(&byte))
}

fn valid_fingerprint(value: &str) -> bool {
    value.len() == 50
        && value.starts_with("SHA256:")
        && value[7..]
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn safe_join(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let path = Path::new(relative);
    if relative.is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err("Hermes bootstrap manifest contains an unsafe path.".to_owned());
    }
    Ok(root.join(path))
}

fn read_json_file<T: for<'de> Deserialize<'de>>(path: &Path, maximum: u64) -> Result<T, String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| "Hermes bootstrap manifest could not be inspected.".to_owned())?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() == 0 || metadata.len() > maximum {
        return Err("Hermes bootstrap manifest is unsafe.".to_owned());
    }
    let bytes = fs::read(path).map_err(|_| "Hermes bootstrap manifest could not be read.".to_owned())?;
    serde_json::from_slice(&bytes).map_err(|_| "Hermes bootstrap manifest is invalid.".to_owned())
}

fn validate_pairing_bootstrap(
    manifest: &PairingBootstrapManifest,
    bootstrap_root: &Path,
) -> Result<(), String> {
    let expected_platform = std::env::consts::OS;
    if manifest.schema_version != 1
        || manifest.scope != "hermes_desktop_pairing_bootstrap"
        || manifest.platform != expected_platform
        || manifest.architecture != std::env::consts::ARCH
        || manifest.python_tag != "cp313"
        || manifest.connector_version.is_empty()
        || !valid_sha256(&manifest.connector_lock_sha256)
        || manifest.entrypoint_module != "hermes_connector.cli"
        || manifest.allowed_actions != ["pair start", "pair status", "pair cancel"]
        || manifest.network_dependency_install
        || manifest.artifacts.is_empty()
        || manifest.target.is_empty()
        || manifest.credential_authority.is_empty()
    {
        return Err("Hermes pairing bootstrap manifest is invalid.".to_owned());
    }
    let pairing_root = bootstrap_root.join("pairing/wheels");
    let mut connector_found = false;
    for artifact in &manifest.artifacts {
        if artifact.filename.is_empty()
            || artifact.filename.contains('/')
            || artifact.filename.contains('\\')
            || !artifact.filename.ends_with(".whl")
            || !valid_sha256(&artifact.sha256)
            || artifact.size_bytes == 0
        {
            return Err("Hermes pairing bootstrap artifact declaration is invalid.".to_owned());
        }
        let path = pairing_root.join(&artifact.filename);
        let metadata = fs::symlink_metadata(&path)
            .map_err(|_| "Hermes pairing bootstrap artifact is missing.".to_owned())?;
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.len() != artifact.size_bytes
        {
            return Err("Hermes pairing bootstrap artifact is unsafe.".to_owned());
        }
        verify_sha256(&path, &artifact.sha256)?;
        if artifact.filename == manifest.connector_wheel {
            connector_found = true;
        }
    }
    if !connector_found {
        return Err("Hermes pairing bootstrap Connector wheel is missing.".to_owned());
    }
    Ok(())
}

fn verify_sha256(path: &Path, expected: &str) -> Result<(), String> {
    if !valid_sha256(expected) {
        return Err("Hermes bootstrap digest is invalid.".to_owned());
    }
    let bytes = fs::read(path).map_err(|_| "Hermes bootstrap artifact could not be read.".to_owned())?;
    let actual = format!("{:x}", Sha256::digest(bytes));
    if actual != expected {
        return Err("Hermes bootstrap artifact digest did not match.".to_owned());
    }
    Ok(())
}

fn valid_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn require_regular_file(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| "Hermes bootstrap executable is missing.".to_owned())?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err("Hermes bootstrap executable is unsafe.".to_owned());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o111 == 0 {
            return Err("Hermes bootstrap executable is not executable.".to_owned());
        }
    }
    Ok(())
}

fn pairing_environment_python(root: &Path) -> PathBuf {
    #[cfg(windows)]
    {
        root.join("Scripts/python.exe")
    }
    #[cfg(not(windows))]
    {
        root.join("bin/python")
    }
}
