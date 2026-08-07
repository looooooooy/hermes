use crate::model::{ComponentHealth, PlatformKind};
use crate::ports::{InstallLayout, PortError, ServiceManager};
use std::path::{Path, PathBuf};

#[derive(Debug, Default)]
pub struct FailClosedServiceManager;

impl ServiceManager for FailClosedServiceManager {
    fn install_bootstrap(&self, _runtime_manager: &Path) -> Result<(), PortError> {
        Err(PortError::Unavailable("platform ServiceManager adapter is not implemented"))
    }

    fn start_host(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
        Err(PortError::Unavailable("platform Host service adapter is not implemented"))
    }

    fn stop_host(&self) -> Result<(), PortError> {
        Err(PortError::Unavailable("platform Host service adapter is not implemented"))
    }

    fn start_connector(&self, _executable: &Path, _release_id: &str) -> Result<(), PortError> {
        Err(PortError::Unavailable("platform Connector service adapter is not implemented"))
    }

    fn stop_connector(&self) -> Result<(), PortError> {
        Err(PortError::Unavailable("platform Connector service adapter is not implemented"))
    }

    fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
        Ok(["Hermes Core", "Agent Plugin", "Connector", "Hermes Cloud"]
            .into_iter()
            .map(|name| ComponentHealth {
                name: name.to_owned(),
                ready: false,
                detail: "platform adapter not connected".to_owned(),
                process: None,
            })
            .collect())
    }
}

#[derive(Debug, Clone)]
pub struct DefaultInstallLayout {
    platform: PlatformKind,
    root: PathBuf,
}

impl DefaultInstallLayout {
    pub fn discover() -> Result<Self, PortError> {
        let platform = current_platform();
        let root = match platform {
            PlatformKind::Macos => home_dir()?.join("Library/Application Support/Hermes"),
            PlatformKind::Windows => std::env::var_os("LOCALAPPDATA")
                .map(PathBuf::from)
                .ok_or_else(|| PortError::Operation("LOCALAPPDATA is unavailable".to_owned()))?
                .join("Hermes"),
            PlatformKind::Linux => std::env::var_os("XDG_DATA_HOME")
                .map(PathBuf::from)
                .unwrap_or(home_dir()?.join(".local/share"))
                .join("hermes"),
        };
        Ok(Self { platform, root })
    }
}

impl InstallLayout for DefaultInstallLayout {
    fn platform(&self) -> PlatformKind {
        self.platform
    }

    fn application_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.root.clone())
    }

    fn releases_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.root.join("releases"))
    }

    fn toolchains_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.root.join("toolchains"))
    }

    fn state_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.root.join("state"))
    }

    fn logs_root(&self) -> Result<PathBuf, PortError> {
        Ok(self.root.join("logs"))
    }
}

fn home_dir() -> Result<PathBuf, PortError> {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .ok_or_else(|| PortError::Operation("user home directory is unavailable".to_owned()))
}

pub const fn current_platform() -> PlatformKind {
    #[cfg(target_os = "macos")]
    {
        PlatformKind::Macos
    }
    #[cfg(target_os = "windows")]
    {
        PlatformKind::Windows
    }
    #[cfg(target_os = "linux")]
    {
        PlatformKind::Linux
    }
    #[cfg(not(any(target_os = "macos", target_os = "windows", target_os = "linux")))]
    compile_error!("Hermes Runtime Manager supports only macOS, Windows, and Linux");
}
