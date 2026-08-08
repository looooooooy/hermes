#![cfg(windows)]

use crate::ports::PortError;
use crate::startup_reconcile::StartupReadinessProbe;
use crate::windows_update_health::WindowsStartupReadinessProbe;

impl StartupReadinessProbe for WindowsStartupReadinessProbe {
    fn ready(&self, release_id: &str) -> Result<bool, PortError> {
        WindowsStartupReadinessProbe::ready(self, release_id)
    }
}
