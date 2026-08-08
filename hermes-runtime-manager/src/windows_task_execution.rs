#![cfg(windows)]

use crate::ports::PortError;
use std::thread;
use std::time::Duration;
use windows::core::{BSTR, Error as WindowsError};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CoUninitialize, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
};
use windows::Win32::System::TaskScheduler::{
    ITaskFolder, ITaskService, TaskScheduler, TASK_STATE, TASK_STATE_QUEUED, TASK_STATE_RUNNING,
};
use windows::Win32::System::Variant::VARIANT;

const TASK_NAME_PREFIX: &str = "HermesRuntimeManager-";
const MAX_TASK_NAME_BYTES: usize = 180;
const WAIT_ATTEMPTS: usize = 200;
const WAIT_INTERVAL: Duration = Duration::from_millis(100);

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct WindowsTaskRunEvidence {
    pub last_run_time_before: f64,
    pub last_run_time_after: f64,
    pub last_task_result: i32,
    pub registered_state: TASK_STATE,
    pub running_instance_state: TASK_STATE,
}

/// Run one already-registered Hermes task and prove that this exact invocation
/// completed successfully.
///
/// `IRunningTask` represents a running instance. The durable task-level evidence is
/// taken from `IRegisteredTask`: the task's LastRunTime must advance after `Run`, its
/// operational state must leave queued/running, and LastTaskResult must be zero.
/// The running-instance state is retained only as diagnostic evidence.
pub fn run_registered_task_and_wait_for_fresh_completion(
    task_name: &str,
) -> Result<WindowsTaskRunEvidence, PortError> {
    validate_task_name(task_name)?;
    let apartment = TaskSchedulerProbeApartment::connect()?;
    let root = apartment.root_folder()?;
    let task = unsafe { root.GetTask(&BSTR::from(task_name)) }
        .map_err(|error| windows_operation("ITaskFolder::GetTask", error))?;

    let before = unsafe { task.LastRunTime() }
        .map_err(|error| windows_operation("IRegisteredTask::LastRunTime(before)", error))?;
    let running = unsafe { task.Run(&VARIANT::default()) }
        .map_err(|error| windows_operation("IRegisteredTask::Run", error))?;

    let mut last_registered_state = unsafe { task.State() }
        .map_err(|error| windows_operation("IRegisteredTask::State", error))?;
    let mut last_running_state = unsafe { running.State() }
        .map_err(|error| windows_operation("IRunningTask::State", error))?;
    let mut last_run_time = before;
    let mut last_result = unsafe { task.LastTaskResult() }
        .map_err(|error| windows_operation("IRegisteredTask::LastTaskResult", error))?;

    for _ in 0..WAIT_ATTEMPTS {
        last_registered_state = unsafe { task.State() }
            .map_err(|error| windows_operation("IRegisteredTask::State", error))?;
        last_running_state = unsafe { running.State() }
            .map_err(|error| windows_operation("IRunningTask::State", error))?;
        last_run_time = unsafe { task.LastRunTime() }
            .map_err(|error| windows_operation("IRegisteredTask::LastRunTime(after)", error))?;
        last_result = unsafe { task.LastTaskResult() }
            .map_err(|error| windows_operation("IRegisteredTask::LastTaskResult", error))?;

        let fresh_run_observed = last_run_time > before;
        let registered_task_terminal =
            last_registered_state != TASK_STATE_RUNNING && last_registered_state != TASK_STATE_QUEUED;
        if fresh_run_observed && registered_task_terminal {
            if last_result != 0 {
                return Err(PortError::Operation(format!(
                    "scheduled Runtime Manager probe completed with task result 0x{:08x}; registered_state={:?}; running_instance_state={:?}; last_run_time_before={before}; last_run_time_after={last_run_time}",
                    last_result as u32, last_registered_state, last_running_state
                )));
            }
            return Ok(WindowsTaskRunEvidence {
                last_run_time_before: before,
                last_run_time_after: last_run_time,
                last_task_result: last_result,
                registered_state: last_registered_state,
                running_instance_state: last_running_state,
            });
        }
        thread::sleep(WAIT_INTERVAL);
    }

    Err(PortError::Operation(format!(
        "scheduled Runtime Manager fresh-run proof timed out; registered_state={:?}; running_instance_state={:?}; last_task_result=0x{:08x}; last_run_time_before={before}; last_run_time_after={last_run_time}",
        last_registered_state, last_running_state, last_result as u32
    )))
}

struct TaskSchedulerProbeApartment {
    service: Option<ITaskService>,
}

impl TaskSchedulerProbeApartment {
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
            PortError::Operation("Task Scheduler probe COM apartment is already closed".to_owned())
        })?;
        unsafe { service.GetFolder(&BSTR::from("\\")) }
            .map_err(|error| windows_operation("ITaskService::GetFolder", error))
    }
}

impl Drop for TaskSchedulerProbeApartment {
    fn drop(&mut self) {
        drop(self.service.take());
        unsafe { CoUninitialize() };
    }
}

fn validate_task_name(value: &str) -> Result<(), PortError> {
    if !value.starts_with(TASK_NAME_PREFIX)
        || value.len() > MAX_TASK_NAME_BYTES
        || !value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')
        })
    {
        return Err(PortError::Operation(
            "invalid Hermes Task Scheduler probe task name".to_owned(),
        ));
    }
    Ok(())
}

fn windows_operation(operation: &str, error: WindowsError) -> PortError {
    PortError::Operation(format!(
        "{operation} failed with HRESULT 0x{:08x}",
        error.code().0 as u32
    ))
}
