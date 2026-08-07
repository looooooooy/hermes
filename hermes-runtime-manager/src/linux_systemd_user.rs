#![cfg(target_os = "linux")]

use crate::ports::PortError;
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::thread;
use std::time::Duration;

const SYSTEMCTL: &str = "/usr/bin/systemctl";
const UNIT_PREFIX: &str = "hermes-runtime-manager";
const WAIT_ATTEMPTS: usize = 150;
const WAIT_INTERVAL: Duration = Duration::from_millis(100);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LinuxSystemdUserStatus {
    pub load_state: String,
    pub active_state: String,
    pub sub_state: String,
    pub main_pid: u32,
}

impl LinuxSystemdUserStatus {
    pub fn ready(&self) -> bool {
        self.load_state == "loaded"
            && self.active_state == "active"
            && self.sub_state == "running"
            && self.main_pid > 0
    }
}

#[derive(Debug, Clone)]
pub struct LinuxSystemdUserBootstrap {
    unit_dir: PathBuf,
    systemctl: PathBuf,
}

impl LinuxSystemdUserBootstrap {
    pub fn discover() -> Result<Self, PortError> {
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .ok_or_else(|| PortError::Operation("HOME is unavailable for systemd user units".to_owned()))?;
        let config_home = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| home.join(".config"));
        let systemctl = PathBuf::from(SYSTEMCTL);
        if systemctl.is_symlink() || !systemctl.is_file() {
            return Err(PortError::Unavailable(
                "systemctl is unavailable at /usr/bin/systemctl",
            ));
        }
        Ok(Self {
            unit_dir: config_home.join("systemd/user"),
            systemctl,
        })
    }

    pub fn register_runtime_manager(
        &self,
        unit_name: &str,
        runtime_manager: &Path,
    ) -> Result<PathBuf, PortError> {
        validate_unit_name(unit_name)?;
        validate_runtime_manager(runtime_manager)?;
        fs::create_dir_all(&self.unit_dir)?;
        fs::set_permissions(&self.unit_dir, fs::Permissions::from_mode(0o700))?;

        let unit_path = self.unit_path(unit_name);
        if unit_path.is_symlink() {
            return Err(PortError::Operation(
                "refusing to replace a symlinked systemd user unit".to_owned(),
            ));
        }
        let payload = render_runtime_manager_unit(runtime_manager)?;
        atomic_write_private(&unit_path, payload.as_bytes())?;

        self.systemctl(&["--user", "daemon-reload"])?;
        if let Err(error) = self.systemctl(&["--user", "enable", "--now", unit_name]) {
            let _ = self.remove_runtime_manager(unit_name);
            return Err(error);
        }
        let _ = self.wait_ready(unit_name, None)?;
        Ok(unit_path)
    }

    pub fn restart(&self, unit_name: &str) -> Result<LinuxSystemdUserStatus, PortError> {
        validate_unit_name(unit_name)?;
        let before = self.status(unit_name)?;
        self.systemctl(&["--user", "restart", unit_name])?;
        self.wait_ready(unit_name, Some(before.main_pid))
    }

    pub fn wait_ready(
        &self,
        unit_name: &str,
        previous_pid: Option<u32>,
    ) -> Result<LinuxSystemdUserStatus, PortError> {
        validate_unit_name(unit_name)?;
        let mut last = None;
        for _ in 0..WAIT_ATTEMPTS {
            match self.status(unit_name) {
                Ok(status)
                    if status.ready()
                        && previous_pid.map_or(true, |pid| status.main_pid != pid) =>
                {
                    return Ok(status)
                }
                Ok(status) => last = Some(status),
                Err(_) => {}
            }
            thread::sleep(WAIT_INTERVAL);
        }
        Err(PortError::Operation(format!(
            "systemd user unit did not become ready with a fresh PID: {unit_name}; last={last:?}"
        )))
    }

    pub fn status(&self, unit_name: &str) -> Result<LinuxSystemdUserStatus, PortError> {
        validate_unit_name(unit_name)?;
        let output = self.systemctl(&[
            "--user",
            "show",
            unit_name,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            "--no-pager",
        ])?;
        parse_status(&String::from_utf8_lossy(&output.stdout))
    }

    pub fn kill_main_for_recovery_proof(&self, unit_name: &str) -> Result<u32, PortError> {
        let status = self.status(unit_name)?;
        if !status.ready() {
            return Err(PortError::Operation(format!(
                "cannot kill a non-ready systemd user unit: {unit_name}: {status:?}"
            )));
        }
        let pid = i32::try_from(status.main_pid)
            .map_err(|_| PortError::Operation("systemd MainPID exceeds i32".to_owned()))?;
        // SAFETY: kill is called with a positive PID obtained directly from systemd.
        if unsafe { libc::kill(pid, libc::SIGKILL) } != 0 {
            return Err(PortError::Operation(format!(
                "SIGKILL of systemd MainPID {} failed: {}",
                status.main_pid,
                std::io::Error::last_os_error()
            )));
        }
        Ok(status.main_pid)
    }

    pub fn unit_contents(&self, unit_name: &str) -> Result<String, PortError> {
        validate_unit_name(unit_name)?;
        let path = self.unit_path(unit_name);
        let metadata = fs::symlink_metadata(&path)?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(PortError::Operation(
                "systemd user unit is missing or symlinked".to_owned(),
            ));
        }
        fs::read_to_string(path).map_err(PortError::from)
    }

    pub fn remove_runtime_manager(&self, unit_name: &str) -> Result<(), PortError> {
        validate_unit_name(unit_name)?;
        let _ = self.systemctl_allow_failure(&["--user", "disable", "--now", unit_name]);
        let unit_path = self.unit_path(unit_name);
        if let Ok(metadata) = fs::symlink_metadata(&unit_path) {
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err(PortError::Operation(
                    "refusing to remove a non-regular systemd user unit".to_owned(),
                ));
            }
            fs::remove_file(&unit_path)?;
        }
        self.systemctl(&["--user", "daemon-reload"])?;
        let _ = self.systemctl_allow_failure(&["--user", "reset-failed", unit_name]);
        Ok(())
    }

    pub fn unit_path(&self, unit_name: &str) -> PathBuf {
        self.unit_dir.join(unit_name)
    }

    fn systemctl(&self, args: &[&str]) -> Result<Output, PortError> {
        let output = Command::new(&self.systemctl)
            .args(args)
            .env("SYSTEMD_PAGER", "cat")
            .env("SYSTEMD_COLORS", "0")
            .output()?;
        if !output.status.success() {
            return Err(command_error("systemctl", args, &output));
        }
        Ok(output)
    }

    fn systemctl_allow_failure(&self, args: &[&str]) -> Result<Output, PortError> {
        Command::new(&self.systemctl)
            .args(args)
            .env("SYSTEMD_PAGER", "cat")
            .env("SYSTEMD_COLORS", "0")
            .output()
            .map_err(PortError::from)
    }
}

fn render_runtime_manager_unit(runtime_manager: &Path) -> Result<String, PortError> {
    let exec = quote_systemd_exec_path(runtime_manager)?;
    Ok(format!(
        "[Unit]\nDescription=Hermes Runtime Manager\n\n[Service]\nType=simple\nExecStart={exec} serve-read-only\nRestart=on-failure\nRestartSec=1s\nTimeoutStartSec=15s\nTimeoutStopSec=10s\nKillSignal=SIGTERM\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n\n[Install]\nWantedBy=default.target\n"
    ))
}

fn quote_systemd_exec_path(path: &Path) -> Result<String, PortError> {
    let value = path
        .to_str()
        .ok_or_else(|| PortError::Operation("Runtime Manager path is not UTF-8".to_owned()))?;
    if value.contains('\0') || value.contains('\n') || value.contains('\r') {
        return Err(PortError::Operation(
            "Runtime Manager path contains forbidden control characters".to_owned(),
        ));
    }
    let escaped = value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('%', "%%")
        .replace('$', "$$");
    Ok(format!("\"{escaped}\""))
}

fn validate_runtime_manager(path: &Path) -> Result<(), PortError> {
    if !path.is_absolute() || path.is_symlink() || !path.is_file() {
        return Err(PortError::Operation(
            "systemd user Runtime Manager action must be an absolute regular file".to_owned(),
        ));
    }
    Ok(())
}

fn validate_unit_name(value: &str) -> Result<(), PortError> {
    if !value.starts_with(UNIT_PREFIX)
        || !value.ends_with(".service")
        || value.len() > 200
        || !value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
        })
    {
        return Err(PortError::Operation(
            "invalid Hermes systemd user unit name".to_owned(),
        ));
    }
    Ok(())
}

fn atomic_write_private(path: &Path, payload: &[u8]) -> Result<(), PortError> {
    let parent = path
        .parent()
        .ok_or_else(|| PortError::Operation("systemd user unit has no parent".to_owned()))?;
    let stage = parent.join(format!(
        ".{}.staging.{}",
        path.file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| PortError::Operation("systemd unit filename is invalid".to_owned()))?,
        std::process::id()
    ));
    if stage.exists() || stage.is_symlink() {
        fs::remove_file(&stage)?;
    }
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&stage)?;
        file.write_all(payload)?;
        file.flush()?;
        file.sync_all()?;
        fs::set_permissions(&stage, fs::Permissions::from_mode(0o600))?;
        fs::rename(&stage, path)?;
        Ok::<(), std::io::Error>(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&stage);
    }
    result.map_err(PortError::from)
}

fn parse_status(value: &str) -> Result<LinuxSystemdUserStatus, PortError> {
    let mut load_state = None;
    let mut active_state = None;
    let mut sub_state = None;
    let mut main_pid = None;
    for line in value.lines() {
        if let Some(value) = line.strip_prefix("LoadState=") {
            load_state = Some(value.to_owned());
        } else if let Some(value) = line.strip_prefix("ActiveState=") {
            active_state = Some(value.to_owned());
        } else if let Some(value) = line.strip_prefix("SubState=") {
            sub_state = Some(value.to_owned());
        } else if let Some(value) = line.strip_prefix("MainPID=") {
            main_pid = value.parse::<u32>().ok();
        }
    }
    Ok(LinuxSystemdUserStatus {
        load_state: load_state.ok_or_else(|| {
            PortError::Operation("systemctl show omitted LoadState".to_owned())
        })?,
        active_state: active_state.ok_or_else(|| {
            PortError::Operation("systemctl show omitted ActiveState".to_owned())
        })?,
        sub_state: sub_state
            .ok_or_else(|| PortError::Operation("systemctl show omitted SubState".to_owned()))?,
        main_pid: main_pid
            .ok_or_else(|| PortError::Operation("systemctl show omitted MainPID".to_owned()))?,
    })
}

fn command_error(command: &str, args: &[&str], output: &Output) -> PortError {
    PortError::Operation(format!(
        "{} {} failed with status {}: {}",
        command,
        args.join(" "),
        output.status,
        String::from_utf8_lossy(&output.stderr).trim()
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unit_contract_keeps_systemd_as_runtime_manager_bootstrap_only() {
        let unit = render_runtime_manager_unit(Path::new("/opt/hermes/hermes-runtime-manager"))
            .expect("unit");
        assert!(unit.contains("ExecStart=\"/opt/hermes/hermes-runtime-manager\" serve-read-only"));
        assert!(unit.contains("Restart=on-failure"));
        assert!(unit.contains("WantedBy=default.target"));
        assert!(unit.contains("UMask=0077"));
        assert!(unit.contains("NoNewPrivileges=true"));
        assert!(!unit.contains("bash"));
        assert!(!unit.contains("python"));
        assert!(!unit.contains("connector"));
    }

    #[test]
    fn status_parser_requires_all_runtime_evidence() {
        let parsed = parse_status(
            "LoadState=loaded\nActiveState=active\nSubState=running\nMainPID=1234\n",
        )
        .expect("status");
        assert!(parsed.ready());
        assert_eq!(parsed.main_pid, 1234);
        assert!(parse_status("ActiveState=active\n").is_err());
    }

    #[test]
    fn unit_name_is_hermes_scoped() {
        assert!(validate_unit_name("hermes-runtime-manager.service").is_ok());
        assert!(validate_unit_name("hermes-runtime-manager-ci-1.service").is_ok());
        assert!(validate_unit_name("other.service").is_err());
        assert!(validate_unit_name("hermes-runtime-manager/escape.service").is_err());
    }
}
