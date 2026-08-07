#![cfg(windows)]

use crate::ports::PortError;
use std::ffi::c_void;
use std::path::{Path, PathBuf};
use std::ptr::{null_mut};
use std::thread;
use std::time::Duration;
use windows::core::{BSTR, Error as WindowsError};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
};
use windows::Win32::System::TaskScheduler::{
    IRegisteredTask, ITaskFolder, ITaskService, TaskScheduler, TASK_CREATE_OR_UPDATE,
    TASK_LOGON_INTERACTIVE_TOKEN, TASK_STATE_QUEUED, TASK_STATE_RUNNING,
};
use windows::Win32::System::Variant::VARIANT;
use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, HANDLE};
use windows_sys::Win32::Security::{GetTokenInformation, TokenUser, TOKEN_QUERY, TOKEN_USER};
use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

const TASK_NAME_PREFIX: &str = "HermesRuntimeManager-";
const MAX_TASK_NAME_BYTES: usize = 180;
const WAIT_ATTEMPTS: usize = 100;
const WAIT_INTERVAL: Duration = Duration::from_millis(100);
const SCHED_E_TASK_NOT_FOUND: i32 = 0x8004_130f_u32 as i32;
const HRESULT_FILE_NOT_FOUND: i32 = 0x8007_0002_u32 as i32;

#[link(name = "advapi32")]
unsafe extern "system" {
    fn ConvertSidToStringSidW(sid: *mut c_void, string_sid: *mut *mut u16) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WindowsScheduledAction {
    RuntimeBootstrap,
    VersionProbe,
}

impl WindowsScheduledAction {
    fn arguments(self) -> &'static str {
        match self {
            Self::RuntimeBootstrap => "serve-read-only",
            Self::VersionProbe => "version",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WindowsTaskRegistration {
    pub task_name: String,
    pub user_sid: String,
    pub executable: PathBuf,
    pub arguments: String,
    pub xml: String,
}

#[derive(Debug, Default, Clone, Copy)]
pub struct WindowsTaskSchedulerBootstrap;

impl WindowsTaskSchedulerBootstrap {
    pub fn new() -> Self {
        Self
    }

    pub fn register_current_user_logon_task(
        &self,
        task_name: &str,
        runtime_manager: &Path,
        action: WindowsScheduledAction,
    ) -> Result<WindowsTaskRegistration, PortError> {
        validate_task_name(task_name)?;
        validate_runtime_manager(runtime_manager)?;
        let user_sid = current_user_sid_string()?;
        let xml = render_task_xml(&user_sid, runtime_manager, action);
        let apartment = TaskSchedulerApartment::connect()?;
        let root = apartment.root_folder()?;
        let empty = VARIANT::default();
        let user = VARIANT::from(BSTR::from(user_sid.as_str()));
        let registered = unsafe {
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
        let actual_xml = task_xml(&registered)?;
        validate_registered_xml(&actual_xml, &user_sid, runtime_manager, action)?;
        Ok(WindowsTaskRegistration {
            task_name: task_name.to_owned(),
            user_sid,
            executable: runtime_manager.to_path_buf(),
            arguments: action.arguments().to_owned(),
            xml: actual_xml,
        })
    }

    pub fn register_runtime_manager_bootstrap(
        &self,
        runtime_manager: &Path,
    ) -> Result<WindowsTaskRegistration, PortError> {
        self.register_current_user_logon_task(
            "HermesRuntimeManager-Bootstrap",
            runtime_manager,
            WindowsScheduledAction::RuntimeBootstrap,
        )
    }

    pub fn task_xml(&self, task_name: &str) -> Result<Option<String>, PortError> {
        validate_task_name(task_name)?;
        let apartment = TaskSchedulerApartment::connect()?;
        let root = apartment.root_folder()?;
        match unsafe { root.GetTask(&BSTR::from(task_name)) } {
            Ok(task) => Ok(Some(task_xml(&task)?)),
            Err(error) if is_task_not_found(&error) => Ok(None),
            Err(error) => Err(windows_operation("ITaskFolder::GetTask", error)),
        }
    }

    pub fn run_and_wait(&self, task_name: &str) -> Result<(), PortError> {
        validate_task_name(task_name)?;
        let apartment = TaskSchedulerApartment::connect()?;
        let root = apartment.root_folder()?;
        let task = unsafe { root.GetTask(&BSTR::from(task_name)) }
            .map_err(|error| windows_operation("ITaskFolder::GetTask", error))?;
        let running = unsafe { task.Run(&VARIANT::default()) }
            .map_err(|error| windows_operation("IRegisteredTask::Run", error))?;
        for _ in 0..WAIT_ATTEMPTS {
            let state = unsafe { running.State() }
                .map_err(|error| windows_operation("IRunningTask::State", error))?;
            if state != TASK_STATE_RUNNING && state != TASK_STATE_QUEUED {
                let result = unsafe { task.LastTaskResult() }
                    .map_err(|error| windows_operation("IRegisteredTask::LastTaskResult", error))?;
                if result != 0 {
                    return Err(PortError::Operation(format!(
                        "scheduled Runtime Manager probe exited with task result 0x{:08x}",
                        result as u32
                    )));
                }
                return Ok(());
            }
            thread::sleep(WAIT_INTERVAL);
        }
        Err(PortError::Operation(
            "scheduled Runtime Manager probe did not reach a terminal state".to_owned(),
        ))
    }

    pub fn delete_task(&self, task_name: &str) -> Result<(), PortError> {
        validate_task_name(task_name)?;
        let apartment = TaskSchedulerApartment::connect()?;
        let root = apartment.root_folder()?;
        match unsafe { root.DeleteTask(&BSTR::from(task_name), 0) } {
            Ok(()) => Ok(()),
            Err(error) if is_task_not_found(&error) => Ok(()),
            Err(error) => Err(windows_operation("ITaskFolder::DeleteTask", error)),
        }
    }
}

struct TaskSchedulerApartment {
    service: ITaskService,
}

impl TaskSchedulerApartment {
    fn connect() -> Result<Self, PortError> {
        unsafe { CoInitializeEx(None, COINIT_MULTITHREADED) }
            .ok()
            .map_err(|error| windows_operation("CoInitializeEx", error))?;
        let service: ITaskService = match unsafe {
            CoCreateInstance(&TaskScheduler, None, CLSCTX_INPROC_SERVER)
        } {
            Ok(service) => service,
            Err(error) => {
                unsafe { CoUninitialize() };
                return Err(windows_operation("CoCreateInstance(TaskScheduler)", error));
            }
        };
        let empty = VARIANT::default();
        if let Err(error) = unsafe { service.Connect(&empty, &empty, &empty, &empty) } {
            unsafe { CoUninitialize() };
            return Err(windows_operation("ITaskService::Connect", error));
        }
        Ok(Self { service })
    }

    fn root_folder(&self) -> Result<ITaskFolder, PortError> {
        unsafe { self.service.GetFolder(&BSTR::from("\\")) }
            .map_err(|error| windows_operation("ITaskService::GetFolder", error))
    }
}

impl Drop for TaskSchedulerApartment {
    fn drop(&mut self) {
        unsafe { CoUninitialize() };
    }
}

fn task_xml(task: &IRegisteredTask) -> Result<String, PortError> {
    unsafe { task.Xml() }
        .map(|value| value.to_string())
        .map_err(|error| windows_operation("IRegisteredTask::Xml", error))
}

fn render_task_xml(
    user_sid: &str,
    runtime_manager: &Path,
    action: WindowsScheduledAction,
) -> String {
    let sid = xml_escape(user_sid);
    let command = xml_escape(&runtime_manager.display().to_string());
    let arguments = xml_escape(action.arguments());
    format!(
        r#"<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>Hermes Runtime Manager</Author>
    <Description>Hermes current-user Runtime Manager bootstrap.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{sid}</UserId>
    </LogonTrigger>
  </Triggers>
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
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="HermesUser">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"#
    )
}

fn validate_registered_xml(
    xml: &str,
    user_sid: &str,
    runtime_manager: &Path,
    action: WindowsScheduledAction,
) -> Result<(), PortError> {
    let required = [
        "<LogonTrigger>",
        "<LogonType>InteractiveToken</LogonType>",
        "<RunLevel>LeastPrivilege</RunLevel>",
        "<RestartOnFailure>",
        "<Interval>PT1M</Interval>",
        "<Count>3</Count>",
        "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
        user_sid,
        action.arguments(),
    ];
    for value in required {
        if !xml.contains(value) {
            return Err(PortError::Operation(format!(
                "registered Task Scheduler XML is missing required contract: {value}"
            )));
        }
    }
    let executable = runtime_manager.display().to_string();
    if !xml.contains(&xml_escape(&executable)) && !xml.contains(&executable) {
        return Err(PortError::Operation(
            "registered Task Scheduler XML does not contain the Runtime Manager absolute path"
                .to_owned(),
        ));
    }
    let lowered = xml.to_ascii_lowercase();
    if lowered.contains("<password>") || lowered.contains("highestavailable") {
        return Err(PortError::Operation(
            "registered Task Scheduler XML contains forbidden password/elevation configuration"
                .to_owned(),
        ));
    }
    Ok(())
}

fn validate_task_name(value: &str) -> Result<(), PortError> {
    if !value.starts_with(TASK_NAME_PREFIX)
        || value.len() > MAX_TASK_NAME_BYTES
        || !value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
        })
    {
        return Err(PortError::Operation(
            "invalid Hermes Task Scheduler task name".to_owned(),
        ));
    }
    Ok(())
}

fn validate_runtime_manager(path: &Path) -> Result<(), PortError> {
    if !path.is_absolute() || path.is_symlink() || !path.is_file() {
        return Err(PortError::Operation(
            "Runtime Manager Task Scheduler action must be an absolute regular file"
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
    let mut required = 0u32;
    unsafe { GetTokenInformation(token.0, TokenUser, null_mut(), 0, &mut required) };
    if required == 0 || required > 64 * 1024 {
        return Err(PortError::Operation(
            "invalid Windows TokenUser buffer size".to_owned(),
        ));
    }
    let mut buffer = vec![0u8; required as usize];
    if unsafe {
        GetTokenInformation(
            token.0,
            TokenUser,
            buffer.as_mut_ptr().cast(),
            required,
            &mut required,
        )
    } == 0
    {
        return Err(win32_operation("GetTokenInformation(TokenUser)"));
    }
    let user = unsafe { &*(buffer.as_ptr().cast::<TOKEN_USER>()) };
    if user.User.Sid.is_null() {
        return Err(PortError::Operation("TokenUser SID is null".to_owned()));
    }
    sid_to_string(user.User.Sid)
}

fn sid_to_string(sid: *mut c_void) -> Result<String, PortError> {
    let mut string_sid: *mut u16 = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut string_sid) } == 0 {
        return Err(win32_operation("ConvertSidToStringSidW"));
    }
    if string_sid.is_null() {
        return Err(PortError::Operation(
            "ConvertSidToStringSidW returned null".to_owned(),
        ));
    }
    let result = (|| {
        let mut length = 0usize;
        while length < 256 && unsafe { *string_sid.add(length) } != 0 {
            length += 1;
        }
        if length == 0 || length == 256 {
            return Err(PortError::Operation(
                "Windows SID string length is invalid".to_owned(),
            ));
        }
        String::from_utf16(unsafe { std::slice::from_raw_parts(string_sid, length) })
            .map_err(|_| PortError::Operation("Windows SID is invalid UTF-16".to_owned()))
    })();
    unsafe { LocalFree(string_sid.cast()) };
    result
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

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}

fn is_task_not_found(error: &WindowsError) -> bool {
    let code = error.code().0;
    code == SCHED_E_TASK_NOT_FOUND || code == HRESULT_FILE_NOT_FOUND
}

fn windows_operation(operation: &str, error: WindowsError) -> PortError {
    PortError::Operation(format!(
        "{operation} failed with HRESULT 0x{:08x}",
        error.code().0 as u32
    ))
}

fn win32_operation(operation: &str) -> PortError {
    PortError::Operation(format!(
        "{operation} failed with Win32 error {}",
        unsafe { GetLastError() }
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rendered_task_contract_is_current_user_least_privilege_and_restartable() {
        let xml = render_task_xml(
            "S-1-5-21-1-2-3-1001",
            Path::new(r"C:\Program Files\Hermes\hermes-runtime-manager.exe"),
            WindowsScheduledAction::RuntimeBootstrap,
        );
        assert!(xml.contains("<LogonTrigger>"));
        assert!(xml.contains("<LogonType>InteractiveToken</LogonType>"));
        assert!(xml.contains("<RunLevel>LeastPrivilege</RunLevel>"));
        assert!(xml.contains("<RestartOnFailure>"));
        assert!(xml.contains("<Interval>PT1M</Interval>"));
        assert!(xml.contains("<Count>3</Count>"));
        assert!(xml.contains("serve-read-only"));
        assert!(!xml.to_ascii_lowercase().contains("<password>"));
    }

    #[test]
    fn task_names_and_action_modes_are_bounded() {
        assert!(validate_task_name("HermesRuntimeManager-Bootstrap").is_ok());
        assert!(validate_task_name("OtherTask").is_err());
        assert!(validate_task_name("HermesRuntimeManager-bad/name").is_err());
        assert_eq!(WindowsScheduledAction::VersionProbe.arguments(), "version");
    }
}
