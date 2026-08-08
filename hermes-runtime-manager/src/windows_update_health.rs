#![cfg(windows)]

use crate::ports::{PortError, ServiceManager};
use crate::update_coordinator::{UpdateHealthEvidenceV1, UpdateHealthGate};
use serde::Deserialize;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

const MAX_STATUS_BYTES: usize = 8_192;
const MAX_LIVE_SESSION_BYTES: usize = 4_096;
const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const DEFAULT_CONVERGENCE_TIMEOUT: Duration = Duration::from_secs(30);
const DEFAULT_POLL_INTERVAL: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsConnectorReadinessEvidence {
    pub release_id: String,
    pub runtime_generation: String,
    pub agent_ready: bool,
    pub cloud_connected: bool,
}

pub trait WindowsHealthCommandRunner: Send + Sync {
    fn run(
        &self,
        executable: &Path,
        arguments: &[&str],
        environment: &HashMap<String, String>,
        timeout: Duration,
    ) -> Result<Output, PortError>;
}

#[derive(Debug, Default)]
pub struct SubprocessWindowsHealthCommandRunner;

impl WindowsHealthCommandRunner for SubprocessWindowsHealthCommandRunner {
    fn run(
        &self,
        executable: &Path,
        arguments: &[&str],
        environment: &HashMap<String, String>,
        timeout: Duration,
    ) -> Result<Output, PortError> {
        if !executable.is_absolute() || executable.is_symlink() || !executable.is_file() {
            return Err(operation("authoritative health executable is unavailable"));
        }
        let mut child = Command::new(executable)
            .args(arguments)
            .envs(environment)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| operation("authoritative health command could not start"))?;
        let deadline = Instant::now() + timeout;
        loop {
            match child
                .try_wait()
                .map_err(|_| operation("authoritative health command status is unavailable"))?
            {
                Some(_) => {
                    return child
                        .wait_with_output()
                        .map_err(|_| operation("authoritative health output is unavailable"));
                }
                None if Instant::now() < deadline => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                None => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(operation("authoritative health command timed out"));
                }
            }
        }
    }
}

#[derive(Clone)]
pub struct WindowsConnectorCommandHealth {
    releases_root: PathBuf,
    hermes_home: PathBuf,
    profile: String,
    config_file: PathBuf,
    runner: Arc<dyn WindowsHealthCommandRunner>,
}

impl WindowsConnectorCommandHealth {
    pub fn new(
        releases_root: PathBuf,
        hermes_home: PathBuf,
        profile: impl Into<String>,
        config_file: PathBuf,
    ) -> Result<Self, PortError> {
        Self::with_runner(
            releases_root,
            hermes_home,
            profile,
            config_file,
            Arc::new(SubprocessWindowsHealthCommandRunner),
        )
    }

    pub fn with_runner(
        releases_root: PathBuf,
        hermes_home: PathBuf,
        profile: impl Into<String>,
        config_file: PathBuf,
        runner: Arc<dyn WindowsHealthCommandRunner>,
    ) -> Result<Self, PortError> {
        let profile = profile.into();
        if !releases_root.is_absolute() || releases_root.is_symlink() {
            return Err(operation("health release root is unsafe"));
        }
        if !hermes_home.is_absolute() || hermes_home.is_symlink() {
            return Err(operation("HERMES_HOME health root is unsafe"));
        }
        if !safe_identity(&profile, 128) {
            return Err(operation("health profile is invalid"));
        }
        if !config_file.is_absolute() || !is_within(&config_file, &hermes_home) {
            return Err(operation("health configuration path is unsafe"));
        }
        Ok(Self {
            releases_root,
            hermes_home,
            profile,
            config_file,
            runner,
        })
    }

    pub fn readiness(
        &self,
        release_id: &str,
    ) -> Result<WindowsConnectorReadinessEvidence, PortError> {
        let executable = self.console_script(release_id, "hermes-connector")?;
        let output = self.runner.run(
            &executable,
            &["status", "--json"],
            &self.environment(),
            COMMAND_TIMEOUT,
        )?;
        if !output.status.success()
            || output.stdout.is_empty()
            || output.stdout.len() > MAX_STATUS_BYTES
        {
            return Ok(not_ready(release_id));
        }
        let status: ConnectorStatusOutput = match serde_json::from_slice(&output.stdout) {
            Ok(value) => value,
            Err(_) => return Ok(not_ready(release_id)),
        };
        let bound = status.release_id == release_id
            && safe_identity(&status.runtime_generation, 128)
            && status.ready;
        Ok(WindowsConnectorReadinessEvidence {
            release_id: status.release_id,
            runtime_generation: status.runtime_generation,
            agent_ready: bound,
            cloud_connected: bound && status.cloud_state == "active",
        })
    }

    pub fn live_session_ok(
        &self,
        release_id: &str,
        expected_runtime_generation: &str,
    ) -> Result<bool, PortError> {
        if !safe_identity(expected_runtime_generation, 128) {
            return Ok(false);
        }
        let executable = self.console_script(release_id, "hermes-connector-live-session")?;
        let output = self.runner.run(
            &executable,
            &["--json"],
            &self.environment(),
            COMMAND_TIMEOUT,
        )?;
        if !output.status.success()
            || output.stdout.is_empty()
            || output.stdout.len() > MAX_LIVE_SESSION_BYTES
        {
            return Ok(false);
        }
        let evidence: LiveSessionOutput = match serde_json::from_slice(&output.stdout) {
            Ok(value) => value,
            Err(_) => return Ok(false),
        };
        Ok(evidence.live_session_ok
            && evidence.runtime_generation.as_deref() == Some(expected_runtime_generation))
    }

    fn console_script(&self, release_id: &str, name: &str) -> Result<PathBuf, PortError> {
        if !safe_identity(release_id, 160) {
            return Err(operation("health release identity is invalid"));
        }
        let release = self.releases_root.join(release_id);
        if release.is_symlink() || !release.is_dir() {
            return Err(operation("health release directory is unavailable"));
        }
        let canonical_release = release
            .canonicalize()
            .map_err(|_| operation("health release directory is unavailable"))?;
        let canonical_root = self
            .releases_root
            .canonicalize()
            .map_err(|_| operation("health release root is unavailable"))?;
        if canonical_release.parent() != Some(canonical_root.as_path())
            || canonical_release.file_name() != Some(release_id.as_ref())
        {
            return Err(operation("health release escaped release root"));
        }
        let executable = canonical_release
            .join("connector")
            .join("venv")
            .join("Scripts")
            .join(format!("{name}.exe"));
        if executable.is_symlink() || !executable.is_file() {
            return Err(operation("health console entrypoint is unavailable"));
        }
        Ok(executable)
    }

    fn environment(&self) -> HashMap<String, String> {
        HashMap::from([
            (
                "HERMES_HOME".to_owned(),
                self.hermes_home.to_string_lossy().into_owned(),
            ),
            (
                "HERMES_CONNECTOR_CONFIG_FILE".to_owned(),
                self.config_file.to_string_lossy().into_owned(),
            ),
            ("HERMES_CONNECTOR_PROFILE".to_owned(), self.profile.clone()),
        ])
    }
}

pub struct WindowsAuthoritativeUpdateHealthGate {
    service_manager: Arc<dyn ServiceManager>,
    connector: Arc<WindowsConnectorCommandHealth>,
    convergence_timeout: Duration,
    poll_interval: Duration,
}

impl WindowsAuthoritativeUpdateHealthGate {
    pub fn new(
        service_manager: Arc<dyn ServiceManager>,
        connector: Arc<WindowsConnectorCommandHealth>,
    ) -> Self {
        Self::with_policy(
            service_manager,
            connector,
            DEFAULT_CONVERGENCE_TIMEOUT,
            DEFAULT_POLL_INTERVAL,
        )
    }

    pub fn with_policy(
        service_manager: Arc<dyn ServiceManager>,
        connector: Arc<WindowsConnectorCommandHealth>,
        convergence_timeout: Duration,
        poll_interval: Duration,
    ) -> Self {
        Self {
            service_manager,
            connector,
            convergence_timeout,
            poll_interval,
        }
    }
}

impl UpdateHealthGate for WindowsAuthoritativeUpdateHealthGate {
    fn verify(&self, release_id: &str) -> Result<UpdateHealthEvidenceV1, PortError> {
        let deadline = Instant::now() + self.convergence_timeout;
        loop {
            let components = self.service_manager.component_health()?;
            let components_ready =
                !components.is_empty() && components.iter().all(|item| item.ready);
            let readiness = self.connector.readiness(release_id)?;
            let live_session_ok = readiness.agent_ready
                && readiness.cloud_connected
                && self
                    .connector
                    .live_session_ok(release_id, &readiness.runtime_generation)?;
            let evidence = UpdateHealthEvidenceV1 {
                agent_ready: readiness.agent_ready,
                cloud_connected: readiness.cloud_connected,
                live_session_ok,
                components_ready,
            };
            if evidence.healthy() || Instant::now() >= deadline {
                return Ok(evidence);
            }
            sleep_bounded(self.poll_interval, deadline);
        }
    }
}

pub struct WindowsStartupReadinessProbe {
    connector: Arc<WindowsConnectorCommandHealth>,
    convergence_timeout: Duration,
    poll_interval: Duration,
}

impl WindowsStartupReadinessProbe {
    pub fn new(connector: Arc<WindowsConnectorCommandHealth>) -> Self {
        Self::with_policy(
            connector,
            DEFAULT_CONVERGENCE_TIMEOUT,
            DEFAULT_POLL_INTERVAL,
        )
    }

    pub fn with_policy(
        connector: Arc<WindowsConnectorCommandHealth>,
        convergence_timeout: Duration,
        poll_interval: Duration,
    ) -> Self {
        Self {
            connector,
            convergence_timeout,
            poll_interval,
        }
    }

    pub fn ready(&self, release_id: &str) -> Result<bool, PortError> {
        let deadline = Instant::now() + self.convergence_timeout;
        loop {
            let evidence = self.connector.readiness(release_id)?;
            if evidence.agent_ready && evidence.cloud_connected {
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            sleep_bounded(self.poll_interval, deadline);
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ConnectorStatusOutput {
    cloud_state: String,
    #[serde(rename = "local_authority_identity")]
    _local_authority_identity: serde_json::Value,
    #[serde(rename = "pid")]
    _pid: u32,
    #[serde(rename = "process_executable")]
    _process_executable: String,
    #[serde(rename = "process_executable_device")]
    _process_executable_device: u64,
    #[serde(rename = "process_executable_inode")]
    _process_executable_inode: u64,
    #[serde(rename = "process_start_time_ns")]
    _process_start_time_ns: u64,
    ready: bool,
    release_id: String,
    runtime_generation: String,
    #[serde(rename = "updated_at")]
    _updated_at: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct LiveSessionOutput {
    live_session_ok: bool,
    runtime_generation: Option<String>,
}

fn not_ready(release_id: &str) -> WindowsConnectorReadinessEvidence {
    WindowsConnectorReadinessEvidence {
        release_id: release_id.to_owned(),
        runtime_generation: String::new(),
        agent_ready: false,
        cloud_connected: false,
    }
}

fn sleep_bounded(interval: Duration, deadline: Instant) {
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return;
    }
    std::thread::sleep(interval.min(remaining));
}

fn safe_identity(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value != "."
        && value != ".."
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+'))
}

fn is_within(path: &Path, root: &Path) -> bool {
    path.strip_prefix(root).is_ok()
}

fn operation(message: &str) -> PortError {
    PortError::Operation(message.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::ComponentHealth;
    use std::fs;
    use std::os::windows::process::ExitStatusExt;
    use std::process::ExitStatus;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Mutex;

    static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

    struct RunningServices;
    impl ServiceManager for RunningServices {
        fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
            Ok(())
        }
        fn start_host(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
            Ok(())
        }
        fn stop_host(&self) -> Result<(), PortError> {
            Ok(())
        }
        fn start_connector(
            &self,
            _executable: &Path,
            _release_id: &str,
        ) -> Result<(), PortError> {
            Ok(())
        }
        fn stop_connector(&self) -> Result<(), PortError> {
            Ok(())
        }
        fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
            Ok(vec![
                ComponentHealth {
                    name: "Host Process".to_owned(),
                    ready: true,
                    detail: "running".to_owned(),
                    process: None,
                },
                ComponentHealth {
                    name: "Connector Process".to_owned(),
                    ready: true,
                    detail: "running".to_owned(),
                    process: None,
                },
            ])
        }
    }

    struct FakeRunner {
        outputs: Mutex<Vec<Output>>,
    }
    impl WindowsHealthCommandRunner for FakeRunner {
        fn run(
            &self,
            _executable: &Path,
            _arguments: &[&str],
            _environment: &HashMap<String, String>,
            _timeout: Duration,
        ) -> Result<Output, PortError> {
            let mut outputs = self.outputs.lock().unwrap();
            if outputs.is_empty() {
                return Err(operation("fake health output exhausted"));
            }
            Ok(outputs.remove(0))
        }
    }

    fn output(code: u32, payload: &str) -> Output {
        Output {
            status: ExitStatus::from_raw(code),
            stdout: payload.as_bytes().to_vec(),
            stderr: Vec::new(),
        }
    }

    fn success(payload: &str) -> Output {
        output(0, payload)
    }

    fn setup() -> (PathBuf, PathBuf, PathBuf) {
        let id = TEMP_COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "hermes-windows-health-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        let releases = root.join("releases");
        let release = releases.join("1.0.1+win");
        let scripts = release.join("connector/venv/Scripts");
        fs::create_dir_all(&scripts).unwrap();
        fs::write(scripts.join("hermes-connector.exe"), b"status").unwrap();
        fs::write(
            scripts.join("hermes-connector-live-session.exe"),
            b"live",
        )
        .unwrap();
        let config = root.join("connector/profiles/default/config.json");
        fs::create_dir_all(config.parent().unwrap()).unwrap();
        fs::write(&config, b"{}").unwrap();
        (root, releases, config)
    }

    fn status(generation: &str) -> String {
        format!(
            "{{\"cloud_state\":\"active\",\"local_authority_identity\":{{}},\"pid\":1,\"process_executable\":\"C:\\\\x.exe\",\"process_executable_device\":1,\"process_executable_inode\":1,\"process_start_time_ns\":1,\"ready\":true,\"release_id\":\"1.0.1+win\",\"runtime_generation\":\"{generation}\",\"updated_at\":\"2026-08-08T00:00:00Z\"}}"
        )
    }

    #[test]
    fn update_health_requires_generation_bound_live_session() {
        let (home, releases, config) = setup();
        let runner = Arc::new(FakeRunner {
            outputs: Mutex::new(vec![
                success(&status("gen-1")),
                success("{\"live_session_ok\":true,\"runtime_generation\":\"gen-1\"}"),
            ]),
        });
        let connector = Arc::new(
            WindowsConnectorCommandHealth::with_runner(
                releases,
                home,
                "default",
                config,
                runner,
            )
            .unwrap(),
        );
        let evidence = WindowsAuthoritativeUpdateHealthGate::with_policy(
            Arc::new(RunningServices),
            connector,
            Duration::ZERO,
            Duration::ZERO,
        )
        .verify("1.0.1+win")
        .unwrap();
        assert!(evidence.healthy());
    }

    #[test]
    fn generation_mismatch_fails_closed() {
        let (home, releases, config) = setup();
        let runner = Arc::new(FakeRunner {
            outputs: Mutex::new(vec![
                success(&status("gen-1")),
                success("{\"live_session_ok\":true,\"runtime_generation\":\"gen-2\"}"),
            ]),
        });
        let connector = Arc::new(
            WindowsConnectorCommandHealth::with_runner(
                releases,
                home,
                "default",
                config,
                runner,
            )
            .unwrap(),
        );
        let evidence = WindowsAuthoritativeUpdateHealthGate::with_policy(
            Arc::new(RunningServices),
            connector,
            Duration::ZERO,
            Duration::ZERO,
        )
        .verify("1.0.1+win")
        .unwrap();
        assert!(!evidence.healthy());
        assert!(!evidence.live_session_ok);
    }

    #[test]
    fn startup_readiness_converges_after_initial_not_ready_receipt() {
        let (home, releases, config) = setup();
        let runner = Arc::new(FakeRunner {
            outputs: Mutex::new(vec![
                output(3, "{\"ready\":false}"),
                success(&status("gen-1")),
            ]),
        });
        let connector = Arc::new(
            WindowsConnectorCommandHealth::with_runner(
                releases,
                home,
                "default",
                config,
                runner,
            )
            .unwrap(),
        );
        let probe = WindowsStartupReadinessProbe::with_policy(
            connector,
            Duration::from_secs(1),
            Duration::ZERO,
        );
        assert!(probe.ready("1.0.1+win").unwrap());
    }
}
