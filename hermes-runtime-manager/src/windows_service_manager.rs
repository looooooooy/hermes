#![cfg(windows)]

use crate::model::ComponentHealth;
use crate::ports::{PortError, ServiceManager};
use crate::windows_task_scheduler::WindowsTaskSchedulerBootstrap;
use std::ffi::c_void;
use std::path::Path;
use std::ptr::null_mut;
use std::thread;
use std::time::Duration;
use windows::core::{Error as WindowsError, BSTR};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
};
use windows::Win32::System::TaskScheduler::{
    IRegisteredTask, ITaskFolder, ITaskService, TaskScheduler, TASK_CREATE_OR_UPDATE,
    TASK_LOGON_INTERACTIVE_TOKEN, TASK_STATE, TASK_STATE_QUEUED, TASK_STATE_RUNNING,
};
use windows::Win32::System::Variant::VARIANT;
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE};
use windows_sys::Win32::Security::{GetTokenInformation, TokenUser, TOKEN_QUERY, TOKEN_USER};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

const HOST_TASK: &str = "HermesRuntimeManager-Host";
const CONNECTOR_TASK: &str = "HermesRuntimeManager-Connector";
const TASK_NAME_PREFIX: &str = "HermesRuntimeManager-";
const MAX_TASK_NAME_BYTES: usize = 180;
const MAX_ARGUMENT_BYTES: usize = 1024;
const WAIT_ATTEMPTS: usize = 150;
const WAIT_INTERVAL: Duration = Duration::from_millis(100);
const SCHED_E_TASK_NOT_FOUND: i32 = 0x8004_130f_u32 as i32;
const HRESULT_FILE_NOT_FOUND: i32 = 0x8007_0002_u32 as i32;
const ERROR_INSUFFICIENT_BUFFER: u32 = 122;

#[link(name = "advapi32")]
unsafe extern "system" {
    fn ConvertSidToStringSidW(sid: *mut c_void, string_sid: *mut *mut u16) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct WindowsTaskServiceManager;

impl WindowsTaskServiceManager {
    pub fn new() -> Self {
        Self
    }
}

impl ServiceManager for WindowsTaskServiceManager {
    fn install_bootstrap(&self, runtime_manager: &Path) -> Result<(), PortError> {
        WindowsTaskSchedulerBootstrap::new()
            .register_runtime_manager_bootstrap(runtime_manager)
            .map(|_| ())
    }

    fn start_host(&self, executable: &Path, release_id: &str) -> Result<(), PortError> {
        validate_release_id(release_id)?;
        ServiceProjectionApartment::connect()?.register_and_start(
            HOST_TASK,
            executable,
            "",
            "Hermes Host process projected by Hermes Runtime Manager",
        )
    }

    fn stop_host(&self) -> Result<(), PortError> {
        ServiceProjectionApartment::connect()?.stop(HOST_TASK)
    }

    fn start_connector(&self, executable: &Path, release_id: &str) -> Result<(), PortError> {
        validate_release_id(release_id)?;
        let arguments = connector_arguments(release_id);
        ServiceProjectionApartment::connect()?.register_and_start(
            CONNECTOR_TASK,
            executable,
            &arguments,
            "Hermes Connector process projected by Hermes Runtime Manager",
        )
    }

    fn stop_connector(&self) -> Result<(), PortError> {
        ServiceProjectionApartment::connect()?.stop(CONNECTOR_TASK)
    }

    fn component_health(&self) -> Result<Vec<ComponentHealth>, PortError> {
        let apartment = ServiceProjectionApartment::connect()?;
        let host = apartment.state(HOST_TASK)?;
        let connector = apartment.state(CONNECTOR_TASK)?;
        Ok(vec![
            projection_health("Host Process", host),
            projection_health("Connector Process", connector),
        ])
    }
}

fn projection_health(name: &str, state: Option<TASK_STATE>) -> ComponentHealth {
    let ready = state == Some(TASK_STATE_RUNNING);
    ComponentHealth {
        name: name.to_owned(),
        ready,
        detail: if ready {
            "Task Scheduler running projection".to_owned()
        } else {
            "Task Scheduler projection is not running".to_owned()
        },
        process: None,
    }
}

fn connector_arguments(release_id: &str) -> String {
    format!("run --release-id {release_id}")
}

struct ServiceProjectionApartment {
    service: Option<ITaskService>,
}

impl ServiceProjectionApartment {
    fn connect() -> Result<Self, PortError> {
        unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
            .ok()
            .map_err(|error| windows_operation("CoInitializeEx", error))?;
        let service: ITaskService =
            match unsafe { CoCreateInstance(&TaskScheduler, None, CLSCTX_INPROC_SERVER) } {
                Ok(service) => service,
                Err(error) => {
                    unsafe { CoUninitialize() };
                    return Err(windows_operation("CoCreateInstance(TaskScheduler)", error));
                }
            };
        let empty = VARIANT::default();
        if let Err(error) = unsafe { service.Connect(&empty, &empty, &empty, &empty) } {
            drop(service);
            unsafe { CoUninitialize() };
            return Err(windows_operation("ITaskService::Connect", error));
        }
        Ok(Self {
            service: Some(service),
        })
    }

    fn root_folder(&self) -> Result<ITaskFolder, PortError> {
        let service = self.service.as_ref().ok_or_else(|| {
            PortError::Operation("Task Scheduler service projection is already closed".to_owned())
        })?;
        unsafe { service.GetFolder(&BSTR::from("\\")) }
            .map_err(|error| windows_operation("ITaskService::GetFolder", error))
    }

    fn register_and_start(
        &self,
        task_name: &str,
        executable: &Path,
        arguments: &str,
        description: &str,
    ) -> Result<(), PortError> {
        validate_task_name(task_name)?;
        validate_executable(executable)?;
        validate_arguments(arguments)?;
        let sid = current_user_sid_string()?;
        let xml = render_projection_xml(&sid, executable, arguments, description)?;
        let root = self.root_folder()?;
        let empty = VARIANT::default();
        let user = VARIANT::from(BSTR::from(sid.as_str()));
        let task = unsafe {
            root.RegisterTask(
                &BSTR::from(task_name),
                &BSTR::from(xml.as_str()),
                TASK_CREATE_OR_UPDATE.0,
                &user,
                &empty,
                TASK_LOGON_INTERACTIVE_TOKEN,
                &empty,
            )
        }
        .map_err(|error| windows_operation("ITaskFolder::RegisterTask", error))?;
        validate_registered_projection(&task, &sid, executable, arguments)?;
        self.start_registered(&task)
    }

    fn start_registered(&self, task: &IRegisteredTask) -> Result<(), PortError> {
        let running = unsafe { task.Run(&VARIANT::default()) }
            .map_err(|error| windows_operation("IRegisteredTask::Run", error))?;
        let mut last_registered_state = unsafe { task.State() }
            .map_err(|error| windows_operation("IRegisteredTask::State", error))?;
        let mut last_running_state = unsafe { running.State() }
            .map_err(|error| windows_operation("IRunningTask::State", error))?;
        for _ in 0..WAIT_ATTEMPTS {
            last_registered_state = unsafe { task.State() }
                .map_err(|error| windows_operation("IRegisteredTask::State", error))?;
            last_running_state = unsafe { running.State() }
                .map_err(|error| windows_operation("IRunningTask::State", error))?;
            if last_registered_state == TASK_STATE_RUNNING || last_running_state == TASK_STATE_RUNNING
            {
                return Ok(());
            }
            if last_registered_state != TASK_STATE_QUEUED
                && last_registered_state != TASK_STATE_RUNNING
                && last_running_state != TASK_STATE_QUEUED
                && last_running_state != TASK_STATE_RUNNING
            {
                let result = unsafe { task.LastTaskResult() }
                    .map_err(|error| windows_operation("IRegisteredTask::LastTaskResult", error))?;
                return Err(PortError::Operation(format!(
                    "projected Hermes service exited before reaching running state; task_result=0x{:08x}",
                    result as u32
                )));
            }
            thread::sleep(WAIT_INTERVAL);
        }
        Err(PortError::Operation(format!(
            "projected Hermes service did not reach running state; registered_state={last_registered_state:?}; running_state={last_running_state:?}"
        )))
    }

    fn stop(&self, task_name: &str) -> Result<(), PortError> {
        validate_task_name(task_name)?;
        let root = self.root_folder()?;
        let task = match unsafe { root.GetTask(&BSTR::from(task_name)) } {
            Ok(task) => task,
            Err(error) if is_task_not_found(&error) => return Ok(()),
            Err(error) => return Err(windows_operation("ITaskFolder::GetTask", error)),
        };
        let state = unsafe { task.State() }
            .map_err(|error| windows_operation("IRegisteredTask::State", error))?;
        if state != TASK_STATE_RUNNING && state != TASK_STATE_QUEUED {
            return Ok(());
        }
        unsafe { task.Stop(0) }
            .map_err(|error| windows_operation("IRegisteredTask::Stop", error))?;
        for _ in 0..WAIT_ATTEMPTS {
            let state = unsafe { task.State() }
                .map_err(|error| windows_operation("IRegisteredTask::State", error))?;
            if state != TASK_STATE_RUNNING && state != TASK_STATE_QUEUED {
                return Ok(());
            }
            thread::sleep(WAIT_INTERVAL);
        }
        Err(PortError::Operation(
            "projected Hermes service did not stop within the bounded window".to_owned(),
        ))
    }

    fn state(&self, task_name: &str) -> Result<Option<TASK_STATE>, PortError> {
        validate_task_name(task_name)?;
        let root = self.root_folder()?;
        match unsafe { root.GetTask(&BSTR::from(task_name)) } {
            Ok(task) => unsafe { task.State() }
                .map(Some)
                .map_err(|error| windows_operation("IRegisteredTask::State", error)),
            Err(error) if is_task_not_found(&error) => Ok(None),
            Err(error) => Err(windows_operation("ITaskFolder::GetTask", error)),
        }
    }

    #[cfg(test)]
    fn delete(&self, task_name: &str) -> Result<(), PortError> {
        validate_task_name(task_name)?;
        let root = self.root_folder()?;
        match unsafe { root.DeleteTask(&BSTR::from(task_name), 0) } {
            Ok(()) => Ok(()),
            Err(error) if is_task_not_found(&error) => Ok(()),
            Err(error) => Err(windows_operation("ITaskFolder::DeleteTask", error)),
        }
    }
}

impl Drop for ServiceProjectionApartment {
    fn drop(&mut self) {
        drop(self.service.take());
        unsafe { CoUninitialize() };
    }
}

fn render_projection_xml(
    user_sid: &str,
    executable: &Path,
    arguments: &str,
    description: &str,
) -> Result<String, PortError> {
    let command = executable
        .to_str()
        .ok_or_else(|| PortError::Operation("service executable path is not UTF-8".to_owned()))?;
    let sid = xml_escape(user_sid);
    let command = xml_escape(command);
    let arguments = xml_escape(arguments);
    let description = xml_escape(description);
    Ok(format!(
        r#"<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>Hermes Runtime Manager</Author>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="HermesUser">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="HermesUser">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"#
    ))
}

fn validate_registered_projection(
    task: &IRegisteredTask,
    user_sid: &str,
    executable: &Path,
    arguments: &str,
) -> Result<(), PortError> {
    let xml = unsafe { task.Xml() }
        .map(|value| value.to_string())
        .map_err(|error| windows_operation("IRegisteredTask::Xml", error))?;
    let executable = executable
        .to_str()
        .ok_or_else(|| PortError::Operation("service executable path is not UTF-8".to_owned()))?;
    for required in [
        "<LogonType>InteractiveToken</LogonType>",
        "<AllowStartOnDemand>true</AllowStartOnDemand>",
        user_sid,
        executable,
    ] {
        if !xml.contains(required) && !xml.contains(&xml_escape(required)) {
            return Err(PortError::Operation(format!(
                "registered service projection is missing required contract: {required}"
            )));
        }
    }
    if !arguments.is_empty() && !xml.contains(arguments) && !xml.contains(&xml_escape(arguments)) {
        return Err(PortError::Operation(
            "registered service projection arguments do not match".to_owned(),
        ));
    }
    let lowered = xml.to_ascii_lowercase();
    if lowered.contains("<logontrigger>")
        || lowered.contains("<password>")
        || lowered.contains("highestavailable")
    {
        return Err(PortError::Operation(
            "registered service projection contains autonomous-start or elevation configuration"
                .to_owned(),
        ));
    }
    Ok(())
}

fn current_user_sid_string() -> Result<String, PortError> {
    let mut token: HANDLE = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(win32_operation("OpenProcessToken"));
    }
    let token = OwnedHandle(token);
    let mut needed = 0u32;
    unsafe { GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut needed) };
    let error = unsafe { GetLastError() };
    if needed == 0 || error != ERROR_INSUFFICIENT_BUFFER {
        return Err(win32_operation_with_code(
            "GetTokenInformation(size)",
            error,
        ));
    }
    let mut buffer = vec![0u8; needed as usize];
    if unsafe {
        GetTokenInformation(
            token.0,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    } == 0
    {
        return Err(win32_operation("GetTokenInformation(TokenUser)"));
    }
    let token_user = unsafe { &*(buffer.as_ptr().cast::<TOKEN_USER>()) };
    if token_user.User.Sid.is_null() {
        return Err(PortError::Operation(
            "current Windows token contains a null user SID".to_owned(),
        ));
    }
    let mut wide_sid: *mut u16 = null_mut();
    if unsafe { ConvertSidToStringSidW(token_user.User.Sid, &mut wide_sid) } == 0 {
        return Err(win32_operation("ConvertSidToStringSidW"));
    }
    if wide_sid.is_null() {
        return Err(PortError::Operation(
            "ConvertSidToStringSidW returned a null SID string".to_owned(),
        ));
    }
    let sid = unsafe {
        let mut length = 0usize;
        while *wide_sid.add(length) != 0 {
            length += 1;
            if length > 184 {
                LocalFree(wide_sid.cast());
                return Err(PortError::Operation(
                    "current Windows SID string exceeded the bounded length".to_owned(),
                ));
            }
        }
        let value = String::from_utf16(&std::slice::from_raw_parts(wide_sid, length))
            .map_err(|_| PortError::Operation("current Windows SID is invalid UTF-16".to_owned()))?;
        LocalFree(wide_sid.cast());
        value
    };
    if sid.is_empty() {
        return Err(PortError::Operation(
            "current Windows SID is empty".to_owned(),
        ));
    }
    Ok(sid)
}

struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { CloseHandle(self.0) };
            self.0 = null_mut();
        }
    }
}

fn validate_task_name(value: &str) -> Result<(), PortError> {
    if !value.starts_with(TASK_NAME_PREFIX)
        || value.len() > MAX_TASK_NAME_BYTES
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
    {
        return Err(PortError::Operation(
            "invalid Hermes service projection task name".to_owned(),
        ));
    }
    Ok(())
}

fn validate_executable(path: &Path) -> Result<(), PortError> {
    if !path.is_absolute() || path.is_symlink() || !path.is_file() {
        return Err(PortError::Operation(
            "service projection executable must be an absolute regular file".to_owned(),
        ));
    }
    Ok(())
}

fn validate_release_id(value: &str) -> Result<(), PortError> {
    if value.is_empty()
        || value.len() > 160
        || value == "."
        || value == ".."
        || !value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+')
        })
    {
        return Err(PortError::Operation(
            "service projection release identity is invalid".to_owned(),
        ));
    }
    Ok(())
}

fn validate_arguments(value: &str) -> Result<(), PortError> {
    if value.len() > MAX_ARGUMENT_BYTES
        || value.contains('\0')
        || value.contains('\r')
        || value.contains('\n')
    {
        return Err(PortError::Operation(
            "service projection arguments are invalid".to_owned(),
        ));
    }
    Ok(())
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn is_task_not_found(error: &WindowsError) -> bool {
    matches!(
        error.code().0,
        SCHED_E_TASK_NOT_FOUND | HRESULT_FILE_NOT_FOUND
    )
}

fn windows_operation(operation: &str, error: WindowsError) -> PortError {
    PortError::Operation(format!(
        "{operation} failed with HRESULT 0x{:08x}",
        error.code().0 as u32
    ))
}

fn win32_operation(operation: &str) -> PortError {
    win32_operation_with_code(operation, unsafe { GetLastError() })
}

fn win32_operation_with_code(operation: &str, code: u32) -> PortError {
    PortError::Operation(format!("{operation} failed with Win32 error {code}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn connector_projection_argument_is_exact_release_and_shell_free() {
        let value = connector_arguments("1.2.3+win-ci");
        assert_eq!(value, "run --release-id 1.2.3+win-ci");
        assert!(!value.contains('"'));
        assert!(!value.contains('&'));
        assert!(validate_release_id("../escape").is_err());
    }

    #[test]
    fn service_projection_has_no_autonomous_trigger_or_elevation() {
        let executable = Path::new(r"C:\Hermes\releases\1.2.3\connector.exe");
        let xml = render_projection_xml(
            "S-1-5-21-1-2-3-1001",
            executable,
            "run --release-id 1.2.3",
            "test",
        )
        .expect("render");
        assert!(!xml.contains("<Triggers>"));
        assert!(!xml.contains("<LogonTrigger>"));
        assert!(xml.contains("<AllowStartOnDemand>true</AllowStartOnDemand>"));
        assert!(xml.contains("<RunLevel>LeastPrivilege</RunLevel>"));
        assert!(!xml.to_ascii_lowercase().contains("password"));
    }

    #[test]
    fn real_on_demand_projection_runs_and_stops_without_logon_trigger() {
        let system_root = std::env::var_os("SystemRoot").expect("SystemRoot");
        let ping = PathBuf::from(system_root)
            .join("System32")
            .join("PING.EXE");
        assert!(ping.is_file(), "PING.EXE must exist on Windows test host");
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let task_name = format!("HermesRuntimeManager-CI-{}-{nonce}", std::process::id());
        let apartment = ServiceProjectionApartment::connect().expect("connect Task Scheduler");
        let result = apartment.register_and_start(
            &task_name,
            &ping,
            "-t 127.0.0.1",
            "Hermes CI long-running service projection",
        );
        if let Err(error) = result {
            let _ = apartment.delete(&task_name);
            panic!("projection start failed: {error}");
        }
        assert_eq!(
            apartment.state(&task_name).expect("state"),
            Some(TASK_STATE_RUNNING)
        );
        apartment.stop(&task_name).expect("stop");
        assert_ne!(
            apartment.state(&task_name).expect("stopped state"),
            Some(TASK_STATE_RUNNING)
        );
        apartment.delete(&task_name).expect("delete");
        assert_eq!(apartment.state(&task_name).expect("deleted state"), None);
    }
}
