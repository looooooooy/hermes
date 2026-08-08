#![cfg(target_os = "macos")]

use crate::model::ComponentHealth;
use crate::ports::{PortError, ServiceManager};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

const BOOTSTRAP_LABEL: &str = "com.hermes.runtime-manager";
const HOST_LABEL: &str = "com.hermes.runtime-manager.host";
const CONNECTOR_LABEL: &str = "com.hermes.runtime-manager.connector";
const PROJECTION_STATE_SCHEMA: u8 = 1;
const MAX_STATE_BYTES: u64 = 64 * 1024;
const MAX_RELEASE_ID_BYTES: usize = 128;

const CONNECTOR_ENV_ALLOWLIST: &[&str] = &[
    "HERMES_CONNECTOR_CONFIG_FILE",
    "HERMES_CONNECTOR_CLOUD_ENDPOINT",
    "HERMES_CONNECTOR_API_ENDPOINT",
    "HERMES_CONNECTOR_DISPLAY_NAME",
    "HERMES_CONNECTOR_PROFILE",
    "HERMES_CONNECTOR_VERSION",
    "HERMES_CONNECTOR_LOCAL_GATEWAY_REGISTRY_DIR",
    "HERMES_CONNECTOR_LOCAL_GATEWAY_SOCKET_DIR",
    "HERMES_CONNECTOR_CONTROL_REGISTRY_DIR",
    "HERMES_CONNECTOR_CONTROL_SOCKET_DIR",
    "HERMES_CONNECTOR_OBSERVER_REGISTRY_DIR",
    "HERMES_CONNECTOR_OBSERVER_SOCKET_DIR",
    "HERMES_CONNECTOR_STATE_DIR",
    "HERMES_CONNECTOR_DATABASE_FILE",
    "HERMES_CONNECTOR_LOCK_FILE",
    "HERMES_CONNECTOR_CREDENTIAL_STORE",
    "HERMES_HOME",
];

#[derive(Debug, Clone)]
pub struct MacOSLaunchAgentServiceManager {
    launch_agents_dir: PathBuf,
    state_root: PathBuf,
    logs_root: PathBuf,
    domain_target: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProjectionStateV1 {
    schema_version: u8,
    host: Option<ProjectionRecordV1>,
    connector: Option<ProjectionRecordV1>,
}

impl Default for ProjectionStateV1 {
    fn default() -> Self {
        Self {
            schema_version: PROJECTION_STATE_SCHEMA,
            host: None,
            connector: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProjectionRecordV1 {
    label: String,
    executable: PathBuf,
    release_id: String,
    environment: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct ConnectorStatusProjection {
    ready: bool,
    release_id: String,
    cloud_state: String,
}

impl MacOSLaunchAgentServiceManager {
    pub fn new(application_root: PathBuf, logs_root: PathBuf) -> Result<Self, PortError> {
        validate_absolute_root(&application_root, "application root")?;
        validate_absolute_root(&logs_root, "logs root")?;
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .ok_or_else(|| PortError::Operation("HOME is unavailable".to_owned()))?;
        if !home.is_absolute() {
            return Err(PortError::Operation("HOME must be absolute".to_owned()));
        }
        let uid = unsafe { libc::geteuid() };
        Ok(Self {
            launch_agents_dir: home.join("Library/LaunchAgents"),
            state_root: application_root.join("state"),
            logs_root,
            domain_target: format!("gui/{uid}"),
        })
    }

    pub fn projection_state_path(&self) -> PathBuf {
        self.state_root.join("macos-service-projection-v1.json")
    }

    fn plist_path(&self, label: &str) -> PathBuf {
        self.launch_agents_dir.join(format!("{label}.plist"))
    }

    fn ensure_private_directories(&self) -> Result<(), PortError> {
        ensure_private_directory(&self.state_root)?;
        ensure_private_directory(&self.logs_root)?;
        fs::create_dir_all(&self.launch_agents_dir)?;
        let metadata = fs::symlink_metadata(&self.launch_agents_dir)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() || metadata.uid() != unsafe { libc::geteuid() } {
            return Err(PortError::Operation(
                "macOS LaunchAgents directory is unavailable or not owned by the current user"
                    .to_owned(),
            ));
        }
        Ok(())
    }

    fn connector_environment(&self) -> BTreeMap<String, String> {
        CONNECTOR_ENV_ALLOWLIST
            .iter()
            .filter_map(|key| {
                std::env::var(key)
                    .ok()
                    .filter(|value| !value.is_empty() && !value.contains('\0'))
                    .map(|value| ((*key).to_owned(), value))
            })
            .collect()
    }

    fn replace_projection(
        &self,
        label: &str,
        executable: &Path,
        arguments: &[String],
        environment: &BTreeMap<String, String>,
        stdout_name: &str,
        stderr_name: &str,
        run_at_load: bool,
        keep_alive_on_failure: bool,
    ) -> Result<(), PortError> {
        self.ensure_private_directories()?;
        validate_executable(executable)?;
        let plist = render_launch_agent(
            label,
            executable,
            arguments,
            environment,
            &self.logs_root.join(stdout_name),
            &self.logs_root.join(stderr_name),
            run_at_load,
            keep_alive_on_failure,
        );
        let plist_path = self.plist_path(label);
        write_private_atomic(&plist_path, plist.as_bytes())?;
        self.bootout_if_loaded(label)?;
        run_launchctl(&[
            "bootstrap",
            self.domain_target.as_str(),
            plist_path
                .to_str()
                .ok_or_else(|| PortError::Operation("LaunchAgent path is not UTF-8".to_owned()))?,
        ])?;
        run_launchctl(&[
            "kickstart",
            "-k",
            &format!("{}/{}", self.domain_target, label),
        ])?;
        Ok(())
    }

    fn bootout_if_loaded(&self, label: &str) -> Result<(), PortError> {
        if !self.is_loaded(label)? {
            return Ok(());
        }
        run_launchctl(&[
            "bootout",
            &format!("{}/{}", self.domain_target, label),
        ])?;
        Ok(())
    }

    fn is_loaded(&self, label: &str) -> Result<bool, PortError> {
        let output = launchctl_output(&[
            "print",
            &format!("{}/{}", self.domain_target, label),
        ])?;
        Ok(output.status.success())
    }

    fn is_running(&self, label: &str) -> Result<bool, PortError> {
        let output = launchctl_output(&[
            "print",
            &format!("{}/{}", self.domain_target, label),
        ])?;
        if !output.status.success() {
            return Ok(false);
        }
        let text = String::from_utf8_lossy(&output.stdout).to_ascii_lowercase();
        Ok(text.contains("state = running") || text.contains("pid ="))
    }

    fn load_state(&self) -> Result<ProjectionStateV1, PortError> {
        let path = self.projection_state_path();
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(ProjectionStateV1::default())
            }
            Err(error) => return Err(error.into()),
        };
        if metadata.file_type().is_symlink()
            || !metadata.is_file()
            || metadata.uid() != unsafe { libc::geteuid() }
            || metadata.permissions().mode() & 0o777 != 0o600
            || metadata.len() > MAX_STATE_BYTES
        {
            return Err(PortError::Operation(
                "macOS service projection state is unsafe".to_owned(),
            ));
        }
        let raw = fs::read(&path)?;
        let state: ProjectionStateV1 = serde_json::from_slice(&raw).map_err(|error| {
            PortError::Operation(format!("macOS service projection state is invalid: {error}"))
        })?;
        if state.schema_version != PROJECTION_STATE_SCHEMA {
            return Err(PortError::Operation(
                "macOS service projection state schema is unsupported".to_owned(),
            ));
        }
        Ok(state)
    }

    fn store_state(&self, state: &ProjectionStateV1) -> Result<(), PortError> {
        if state.schema_version != PROJECTION_STATE_SCHEMA {
            return Err(PortError::Operation(
                "macOS service projection state schema is invalid".to_owned(),
            ));
        }
        self.ensure_private_directories()?;
        let payload = serde_json::to_vec_pretty(state).map_err(|error| {
            PortError::Operation(format!("macOS service projection state encode failed: {error}"))
        })?;
        if payload.len() as u64 > MAX_STATE_BYTES {
            return Err(PortError::Operation(
                "macOS service projection state is too large".to_owned(),
            ));
        }
        write_private_atomic(&self.projection_state_path(), &payload)
    }

    fn connector_status(
        &self,
        record: Option<&ProjectionRecordV1>,
        connector_running: bool,
    ) -> Option<ConnectorStatusProjection> {
        let record = record?;
        if !connector_running || validate_executable(&record.executable).is_err() {
            return None;
        }
        let mut command = Command::new(&record.executable);
        command.args(["status", "--json"]);
        command.env_clear();
        command.env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin");
        command.env("HOME", std::env::var_os("HOME")?);
        for (key, value) in &record.environment {
            command.env(key, value);
        }
        let output = command.output().ok()?;
        if !output.status.success() && output.status.code() != Some(2) {
            return None;
        }
        serde_json::from_slice(&output.stdout).ok()
    }
}

impl ServiceManager for MacOSLaunchAgentServiceManager {
    fn install_bootstrap(&self, runtime_manager: &Path) -> Result<(), PortError> {
        self.replace_projection(
            BOOTSTRAP_LABEL,
            runtime_manager,
            &["serve-read-only".to_owned()],
            &BTreeMap::new(),
            "runtime-manager.stdout.log",
            "runtime-manager.stderr.log",
            true,
            true,
        )
    }

    fn start_host(&self, executable: &Path, release_id: &str) -> Result<(), PortError> {
        let release_root = validate_managed_release_executable(executable, release_id, "host", "hermes")?;
        let manifest = release_root
            .join("plugin/metadata/signed-plugin-manifest.json");
        let trust_store = release_root.join("plugin/metadata/trust-store.json");
        validate_regular_file(&manifest, "Plugin Store manifest")?;
        validate_regular_file(&trust_store, "Plugin Store trust store")?;
        let environment = BTreeMap::from([
            (
                "HERMES_PLUGIN_STORE_MANIFEST".to_owned(),
                manifest.to_string_lossy().into_owned(),
            ),
            (
                "HERMES_PLUGIN_STORE_TRUST_STORE".to_owned(),
                trust_store.to_string_lossy().into_owned(),
            ),
        ]);
        self.replace_projection(
            HOST_LABEL,
            executable,
            &[],
            &environment,
            "host.stdout.log",
            "host.stderr.log",
            false,
            false,
        )?;
        let mut state = self.load_state()?;
        state.host = Some(ProjectionRecordV1 {
            label: HOST_LABEL.to_owned(),
            executable: executable.to_path_buf(),
            release_id: release_id.to_owned(),
            environment,
        });
        self.store_state(&state)
    }

    fn stop_host(&self) -> Result<(), PortError> {
        self.bootout_if_loaded(HOST_LABEL)
    }

    fn start_connector(&self, executable: &Path, release_id: &str) -> Result<(), PortError> {
        validate_managed_release_executable(executable, release_id, "connector", "hermes-connector")?;
        let environment = self.connector_environment();
        self.replace_projection(
            CONNECTOR_LABEL,
            executable,
            &["run".to_owned(), "--release-id".to_owned(), release_id.to_owned()],
            &environment,
            "connector.stdout.log",
            "connector.stderr.log",
            false,
            false,
        )?;
        let mut state = self.load_state()?;
        state.connector = Some(ProjectionRecordV1 {
            label: CONNECTOR_LABEL.to_owned(),
            executable: executable.to_path_buf(),
            release_id: release_id.to_owned(),
            environment,
        });
        self.store_state(&state)
    }

    fn stop_connector(&self) -> Result<(), PortError> {
        self.bootout_if_loaded(CONNECTOR_LABEL)
    }

    fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
        let state = self.load_state()?;
        let host_running = self.is_running(HOST_LABEL)?;
        let connector_running = self.is_running(CONNECTOR_LABEL)?;
        let connector_status = self.connector_status(state.connector.as_ref(), connector_running);
        let connector_ready = connector_status.as_ref().is_some_and(|status| {
            status.ready
                && state
                    .connector
                    .as_ref()
                    .is_some_and(|record| status.release_id == record.release_id)
        });
        let cloud_ready = connector_ready
            && connector_status
                .as_ref()
                .is_some_and(|status| status.cloud_state == "active");
        let local_authority_ready = host_running && connector_ready;
        Ok(vec![
            health(
                "Hermes Core",
                local_authority_ready,
                if local_authority_ready {
                    "Host LaunchAgent is running and Connector readiness confirms local authority"
                } else if host_running {
                    "Host LaunchAgent is running; waiting for Connector local-authority evidence"
                } else {
                    "Host LaunchAgent is not running"
                },
            ),
            health(
                "Agent Plugin",
                local_authority_ready,
                if local_authority_ready {
                    "Connector readiness confirms the managed Agent authority"
                } else {
                    "Agent authority has not been confirmed by Connector readiness"
                },
            ),
            health(
                "Connector",
                connector_ready,
                if connector_ready {
                    "Connector status receipt is ready for the projected exact release"
                } else if connector_running {
                    "Connector LaunchAgent is running but has not published a ready receipt"
                } else {
                    "Connector LaunchAgent is not running"
                },
            ),
            health(
                "Hermes Cloud",
                cloud_ready,
                if cloud_ready {
                    "Connector status receipt reports active Cloud transport"
                } else {
                    "Cloud transport is not active"
                },
            ),
        ])
    }
}

fn health(name: &str, ready: bool, detail: &str) -> ComponentHealth {
    ComponentHealth {
        name: name.to_owned(),
        ready,
        detail: detail.to_owned(),
        process: None,
    }
}

fn validate_absolute_root(path: &Path, label: &str) -> Result<(), PortError> {
    if !path.is_absolute() || path.components().any(|part| matches!(part, std::path::Component::ParentDir)) {
        return Err(PortError::Operation(format!("{label} must be an absolute canonical path")));
    }
    Ok(())
}

fn ensure_private_directory(path: &Path) -> Result<(), PortError> {
    fs::create_dir_all(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink()
        || !metadata.is_dir()
        || metadata.uid() != unsafe { libc::geteuid() }
    {
        return Err(PortError::Operation(
            "private macOS service directory is unsafe".to_owned(),
        ));
    }
    Ok(())
}

fn validate_release_id(release_id: &str) -> Result<(), PortError> {
    if release_id.is_empty()
        || release_id.len() > MAX_RELEASE_ID_BYTES
        || release_id == "."
        || release_id == ".."
        || release_id.starts_with('.')
        || !release_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+'))
    {
        return Err(PortError::Operation("release id is invalid".to_owned()));
    }
    Ok(())
}

fn validate_managed_release_executable(
    executable: &Path,
    release_id: &str,
    component: &str,
    executable_name: &str,
) -> Result<PathBuf, PortError> {
    validate_release_id(release_id)?;
    validate_executable(executable)?;
    let bin = executable
        .parent()
        .ok_or_else(|| PortError::Operation("managed executable has no bin directory".to_owned()))?;
    let venv = bin
        .parent()
        .ok_or_else(|| PortError::Operation("managed executable has no venv directory".to_owned()))?;
    let component_root = venv
        .parent()
        .ok_or_else(|| PortError::Operation("managed executable has no component directory".to_owned()))?;
    let release_root = component_root
        .parent()
        .ok_or_else(|| PortError::Operation("managed executable has no release directory".to_owned()))?;
    if executable.file_name().and_then(|value| value.to_str()) != Some(executable_name)
        || bin.file_name().and_then(|value| value.to_str()) != Some("bin")
        || venv.file_name().and_then(|value| value.to_str()) != Some("venv")
        || component_root.file_name().and_then(|value| value.to_str()) != Some(component)
        || release_root.file_name().and_then(|value| value.to_str()) != Some(release_id)
        || release_root.parent().and_then(|value| value.file_name()).and_then(|value| value.to_str())
            != Some("releases")
    {
        return Err(PortError::Operation(
            "service executable is not confined to the exact managed release".to_owned(),
        ));
    }
    Ok(release_root.to_path_buf())
}

fn validate_executable(path: &Path) -> Result<(), PortError> {
    if !path.is_absolute() {
        return Err(PortError::Operation("service executable must be absolute".to_owned()));
    }
    for candidate in path.ancestors() {
        let metadata = match fs::symlink_metadata(candidate) {
            Ok(metadata) => metadata,
            Err(error) if candidate != path && error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error.into()),
        };
        if metadata.file_type().is_symlink() {
            return Err(PortError::Operation(
                "service executable path must not contain symlinks".to_owned(),
            ));
        }
        if candidate == path
            && (!metadata.is_file() || metadata.permissions().mode() & 0o111 == 0)
        {
            return Err(PortError::Operation(
                "service executable is not a regular executable file".to_owned(),
            ));
        }
    }
    Ok(())
}

fn validate_regular_file(path: &Path, label: &str) -> Result<(), PortError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| {
        PortError::Operation(format!("{label} is unavailable for the managed release"))
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(PortError::Operation(format!("{label} is unsafe")));
    }
    Ok(())
}

fn run_launchctl(arguments: &[&str]) -> Result<(), PortError> {
    let output = launchctl_output(arguments)?;
    if output.status.success() {
        return Ok(());
    }
    Err(PortError::Operation(format!(
        "launchctl {} failed with status {:?}: {}",
        arguments.first().copied().unwrap_or("operation"),
        output.status.code(),
        String::from_utf8_lossy(&output.stderr).trim()
    )))
}

fn launchctl_output(arguments: &[&str]) -> Result<Output, PortError> {
    Command::new("/bin/launchctl")
        .args(arguments)
        .env_clear()
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        .output()
        .map_err(PortError::Io)
}

fn write_private_atomic(path: &Path, payload: &[u8]) -> Result<(), PortError> {
    let parent = path
        .parent()
        .ok_or_else(|| PortError::Operation("private output has no parent".to_owned()))?;
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().and_then(|value| value.to_str()).unwrap_or("hermes"),
        std::process::id()
    ));
    let _ = fs::remove_file(&temporary);
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(payload)?;
    file.sync_all()?;
    fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
    fs::rename(&temporary, path)?;
    Ok(())
}

fn render_launch_agent(
    label: &str,
    executable: &Path,
    arguments: &[String],
    environment: &BTreeMap<String, String>,
    stdout: &Path,
    stderr: &Path,
    run_at_load: bool,
    keep_alive_on_failure: bool,
) -> String {
    let mut program_arguments = String::new();
    program_arguments.push_str(&format!(
        "<string>{}</string>",
        xml_escape(&executable.to_string_lossy())
    ));
    for argument in arguments {
        program_arguments.push_str(&format!("<string>{}</string>", xml_escape(argument)));
    }
    let mut environment_xml = String::new();
    if !environment.is_empty() {
        environment_xml.push_str("<key>EnvironmentVariables</key><dict>");
        for (key, value) in environment {
            environment_xml.push_str(&format!(
                "<key>{}</key><string>{}</string>",
                xml_escape(key),
                xml_escape(value)
            ));
        }
        environment_xml.push_str("</dict>");
    }
    let keep_alive = if keep_alive_on_failure {
        "<dict><key>SuccessfulExit</key><false/></dict>"
    } else {
        "<false/>"
    };
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\"><dict>\
<key>Label</key><string>{}</string>\
<key>ProgramArguments</key><array>{}</array>\
<key>RunAtLoad</key>{}\
<key>KeepAlive</key>{}\
<key>ProcessType</key><string>Background</string>\
<key>Umask</key><integer>63</integer>\
<key>StandardOutPath</key><string>{}</string>\
<key>StandardErrorPath</key><string>{}</string>\
{}\
</dict></plist>\n",
        xml_escape(label),
        program_arguments,
        if run_at_load { "<true/>" } else { "<false/>" },
        keep_alive,
        xml_escape(&stdout.to_string_lossy()),
        xml_escape(&stderr.to_string_lossy()),
        environment_xml,
    )
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    fn temp_root(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "hermes-macos-service-{name}-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn release_id_rejects_path_like_values() {
        for invalid in ["", "../1.0.0", ".hidden", "a/b", "a b"] {
            assert!(validate_release_id(invalid).is_err(), "{invalid}");
        }
        assert!(validate_release_id("desktop-0.1.0+macos-aarch64").is_ok());
    }

    #[test]
    fn managed_executable_must_be_confined_to_exact_release() {
        let root = temp_root("release");
        let release = root.join("releases/desktop-0.1.0-macos-aarch64");
        let executable = release.join("connector/venv/bin/hermes-connector");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"#!/bin/sh\nexit 0\n").unwrap();
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
        let result = validate_managed_release_executable(
            &executable,
            "desktop-0.1.0-macos-aarch64",
            "connector",
            "hermes-connector",
        )
        .unwrap();
        assert_eq!(result, release);
        assert!(validate_managed_release_executable(
            &executable,
            "desktop-0.1.1-macos-aarch64",
            "connector",
            "hermes-connector",
        )
        .is_err());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn launch_agent_is_shell_free_and_does_not_invent_secret_environment() {
        let environment = BTreeMap::from([
            (
                "HERMES_CONNECTOR_CLOUD_ENDPOINT".to_owned(),
                "wss://cloud.example.test/connector".to_owned(),
            ),
            (
                "HERMES_CONNECTOR_PROFILE".to_owned(),
                "work".to_owned(),
            ),
        ]);
        let plist = render_launch_agent(
            CONNECTOR_LABEL,
            Path::new("/Applications/Hermes/releases/1.0.0/connector/venv/bin/hermes-connector"),
            &["run".to_owned(), "--release-id".to_owned(), "1.0.0".to_owned()],
            &environment,
            Path::new("/tmp/hermes-connector.stdout.log"),
            Path::new("/tmp/hermes-connector.stderr.log"),
            false,
            false,
        );
        assert!(plist.contains("<string>run</string>"));
        assert!(plist.contains("<string>--release-id</string>"));
        assert!(plist.contains("HERMES_CONNECTOR_CLOUD_ENDPOINT"));
        assert!(!plist.to_ascii_lowercase().contains("access_token"));
        assert!(!plist.contains("/bin/sh"));
        assert!(!plist.contains("/bin/bash"));
    }
}
