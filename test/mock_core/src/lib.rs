use duckflight_extension_abi::{
    DUCKFLIGHT_CORE_ABI_V1, DuckflightBytesV1, DuckflightCoreApiV1, DuckflightCoreCreateOptionsV1,
    DuckflightCoreHandle, DuckflightOutputBufferV1, DuckflightProtocol, DuckflightServerVisitorV1,
    DuckflightStatus, DuckflightStringReceiverV1,
};
use std::{
    ffi::c_void,
    panic::{AssertUnwindSafe, catch_unwind},
    ptr, slice, str,
    sync::Mutex,
};

struct MockCore {
    servers: Mutex<Vec<(DuckflightProtocol, String)>>,
}

unsafe fn borrowed_str<'a>(value: DuckflightBytesV1, name: &str) -> Result<&'a str, String> {
    if value.len == 0 {
        return Ok("");
    }
    if value.data.is_null() {
        return Err(format!("{name} pointer is null with non-zero length"));
    }
    str::from_utf8(unsafe { slice::from_raw_parts(value.data, value.len) })
        .map_err(|error| format!("{name} is not valid UTF-8: {error}"))
}

unsafe fn write_output(output: *mut DuckflightOutputBufferV1, value: &str) -> DuckflightStatus {
    if output.is_null() {
        return DuckflightStatus::BUFFER_TOO_SMALL;
    }
    let output = unsafe { &mut *output };
    if !output.required.is_null() {
        unsafe { *output.required = value.len() };
    }
    if output.capacity < value.len() || (!value.is_empty() && output.data.is_null()) {
        return DuckflightStatus::BUFFER_TOO_SMALL;
    }
    if !value.is_empty() {
        unsafe { ptr::copy_nonoverlapping(value.as_ptr(), output.data, value.len()) };
    }
    DuckflightStatus::OK
}

unsafe fn report_error(
    output: *mut DuckflightOutputBufferV1,
    status: DuckflightStatus,
    message: impl AsRef<str>,
) -> DuckflightStatus {
    let _ = unsafe { write_output(output, message.as_ref()) };
    status
}

unsafe fn with_error_boundary(
    error: *mut DuckflightOutputBufferV1,
    operation: impl FnOnce() -> Result<DuckflightStatus, String>,
) -> DuckflightStatus {
    match catch_unwind(AssertUnwindSafe(operation)) {
        Ok(Ok(status)) => status,
        Ok(Err(message)) => unsafe { report_error(error, DuckflightStatus::INTERNAL, message) },
        Err(_) => unsafe {
            report_error(
                error,
                DuckflightStatus::INTERNAL,
                "mock core panicked across an FFI operation",
            )
        },
    }
}

unsafe fn handle_ref<'a>(handle: DuckflightCoreHandle) -> Result<&'a MockCore, String> {
    if handle.is_null() {
        return Err("mock core handle is null".into());
    }
    Ok(unsafe { &*handle.cast::<MockCore>() })
}

fn validate_protocol(protocol: DuckflightProtocol) -> Result<(), String> {
    match protocol {
        DuckflightProtocol::PGWIRE | DuckflightProtocol::FLIGHT_SQL => Ok(()),
        other => Err(format!("unsupported protocol value {}", other.0)),
    }
}

fn bound_address(protocol: DuckflightProtocol, address: &str) -> String {
    if address.ends_with(":0") {
        match protocol {
            DuckflightProtocol::PGWIRE => "127.0.0.1:15432".into(),
            DuckflightProtocol::FLIGHT_SQL => "127.0.0.1:15051".into(),
            _ => unreachable!("protocol validated before binding"),
        }
    } else {
        address.into()
    }
}

unsafe extern "C" fn create(
    options: *const DuckflightCoreCreateOptionsV1,
    out_handle: *mut DuckflightCoreHandle,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            if options.is_null() || out_handle.is_null() {
                return Ok(report_error(
                    error,
                    DuckflightStatus::INVALID_ARGUMENT,
                    "create options and output handle are required",
                ));
            }
            *out_handle = ptr::null_mut();
            let options = &*options;
            if options.struct_size < size_of::<DuckflightCoreCreateOptionsV1>() as u32 {
                return Ok(report_error(
                    error,
                    DuckflightStatus::ABI_MISMATCH,
                    "create options are smaller than DuckflightCoreCreateOptionsV1",
                ));
            }
            if options.abi_version != DUCKFLIGHT_CORE_ABI_V1 {
                return Ok(report_error(
                    error,
                    DuckflightStatus::ABI_MISMATCH,
                    format!(
                        "unsupported DuckFlight core ABI {}; expected {}",
                        options.abi_version, DUCKFLIGHT_CORE_ABI_V1
                    ),
                ));
            }
            *out_handle = Box::into_raw(Box::new(MockCore {
                servers: Mutex::new(Vec::new()),
            }))
            .cast::<c_void>();
            Ok(DuckflightStatus::OK)
        })
    }
}

unsafe extern "C" fn destroy(handle: DuckflightCoreHandle) {
    if !handle.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| unsafe {
            drop(Box::from_raw(handle.cast::<MockCore>()));
        }));
    }
}

unsafe extern "C" fn start(
    handle: DuckflightCoreHandle,
    protocol: DuckflightProtocol,
    address: DuckflightBytesV1,
    config_file: DuckflightBytesV1,
    receiver: Option<DuckflightStringReceiverV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let core = handle_ref(handle)?;
            validate_protocol(protocol)?;
            let address = borrowed_str(address, "address")?;
            let _ = borrowed_str(config_file, "config_file")?;
            let receiver = receiver.ok_or_else(|| "address receiver is required".to_string())?;
            let actual = bound_address(protocol, address);
            let mut servers = core.servers.lock().map_err(|error| error.to_string())?;
            servers.retain(|item| item != &(protocol, actual.clone()));
            servers.push((protocol, actual.clone()));
            Ok(receiver(context, DuckflightBytesV1::from_utf8(&actual)))
        })
    }
}

unsafe extern "C" fn stop(
    handle: DuckflightCoreHandle,
    protocol: DuckflightProtocol,
    address: DuckflightBytesV1,
    out_stopped: *mut bool,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            if out_stopped.is_null() {
                return Ok(report_error(
                    error,
                    DuckflightStatus::INVALID_ARGUMENT,
                    "stop output pointer is required",
                ));
            }
            let core = handle_ref(handle)?;
            validate_protocol(protocol)?;
            let address = borrowed_str(address, "address")?;
            let mut servers = core.servers.lock().map_err(|error| error.to_string())?;
            let before = servers.len();
            servers.retain(|item| item.0 != protocol || item.1 != address);
            *out_stopped = servers.len() != before;
            Ok(DuckflightStatus::OK)
        })
    }
}

unsafe extern "C" fn list(
    handle: DuckflightCoreHandle,
    visitor: Option<DuckflightServerVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let core = handle_ref(handle)?;
            let visitor = visitor.ok_or_else(|| "server visitor is required".to_string())?;
            let servers = core.servers.lock().map_err(|error| error.to_string())?;
            for (protocol, address) in servers.iter() {
                let status = visitor(context, *protocol, DuckflightBytesV1::from_utf8(address));
                if !status.is_ok() {
                    return Ok(status);
                }
            }
            Ok(DuckflightStatus::OK)
        })
    }
}

static API_V1: DuckflightCoreApiV1 = DuckflightCoreApiV1 {
    struct_size: size_of::<DuckflightCoreApiV1>() as u32,
    abi_version: DUCKFLIGHT_CORE_ABI_V1,
    create: Some(create),
    destroy: Some(destroy),
    start: Some(start),
    stop: Some(stop),
    list: Some(list),
};

#[unsafe(no_mangle)]
pub extern "C" fn duckflight_core_api_v1() -> *const DuckflightCoreApiV1 {
    &API_V1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exposes_a_complete_v1_function_table() {
        let api = unsafe { &*duckflight_core_api_v1() };
        assert_eq!(api.abi_version, DUCKFLIGHT_CORE_ABI_V1);
        assert_eq!(api.struct_size as usize, size_of_val(api));
        assert!(api.create.is_some());
        assert!(api.destroy.is_some());
        assert!(api.start.is_some());
        assert!(api.stop.is_some());
        assert!(api.list.is_some());
    }

    #[test]
    fn binds_ephemeral_addresses_deterministically() {
        assert_eq!(
            bound_address(DuckflightProtocol::PGWIRE, "0.0.0.0:0"),
            "127.0.0.1:15432"
        );
        assert_eq!(
            bound_address(DuckflightProtocol::FLIGHT_SQL, "0.0.0.0:0"),
            "127.0.0.1:15051"
        );
    }
}
