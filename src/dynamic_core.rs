use super::{CoreRuntime, CoreStatus, Protocol};
use duckflight_extension_abi::{
    DUCKFLIGHT_CORE_ABI_V1, DUCKFLIGHT_CORE_API_SYMBOL_V1, DuckflightBytesV1,
    DuckflightCoreApiEntryV1, DuckflightCoreApiV1, DuckflightCoreCreateOptionsV1,
    DuckflightCoreHandle, DuckflightOutputBufferV1, DuckflightProtocol, DuckflightStatus,
};
use libloading::Library;
use std::{error::Error, ffi::c_void, fmt, path::Path, ptr, slice, str};
use tempfile::NamedTempFile;

const ERROR_CAPACITY: usize = 4096;
const API_HEADER_SIZE: usize =
    std::mem::offset_of!(DuckflightCoreApiV1, abi_version) + std::mem::size_of::<u32>();

#[derive(Debug)]
struct DynamicCoreError(String);

impl fmt::Display for DynamicCoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for DynamicCoreError {}

unsafe fn validate_api_table_header(
    api: *const DuckflightCoreApiV1,
) -> Result<(), DynamicCoreError> {
    if api.is_null() {
        return Err(DynamicCoreError("runtime returned a null API table".into()));
    }

    let struct_size = unsafe { ptr::read_unaligned(ptr::addr_of!((*api).struct_size)) };
    if struct_size < API_HEADER_SIZE as u32 {
        return Err(DynamicCoreError("runtime API header is truncated".into()));
    }
    let abi_version = unsafe { ptr::read_unaligned(ptr::addr_of!((*api).abi_version)) };
    if abi_version != DUCKFLIGHT_CORE_ABI_V1 {
        return Err(DynamicCoreError(format!(
            "runtime ABI {abi_version} does not match extension ABI {DUCKFLIGHT_CORE_ABI_V1}"
        )));
    }
    if struct_size < std::mem::size_of::<DuckflightCoreApiV1>() as u32 {
        return Err(DynamicCoreError("runtime v1 API table is truncated".into()));
    }
    Ok(())
}

pub(super) struct DynamicCore {
    api: *const DuckflightCoreApiV1,
    handle: DuckflightCoreHandle,
    status_detail: &'static str,
    // The API table and handle remain valid only while the defining library is loaded.
    _library: Library,
    // Declared after the library so it is removed only after the OS unloads it.
    _bundle_file: Option<NamedTempFile>,
}

// ABI v1 permits concurrent calls. Runtime providers synchronize their mutable state internally,
// and the library and API table outlive the opaque handle.
unsafe impl Send for DynamicCore {}
unsafe impl Sync for DynamicCore {}

impl DynamicCore {
    #[cfg_attr(duckflight_bundled_core, allow(dead_code))]
    pub(super) unsafe fn load(
        path: &Path,
        extension_info: duckdb::ffi::duckdb_extension_info,
        extension_access: *const duckdb::ffi::duckdb_extension_access,
    ) -> Result<Self, Box<dyn Error>> {
        let library = unsafe { Library::new(path) }
            .map_err(|_| DynamicCoreError("load configured runtime library".into()))?;
        let api = {
            let entry =
                unsafe { library.get::<DuckflightCoreApiEntryV1>(DUCKFLIGHT_CORE_API_SYMBOL_V1) }
                    .map_err(|_| {
                    DynamicCoreError("resolve duckflight_core_api_v1 in configured runtime".into())
                })?;
            unsafe { entry() }
        };
        unsafe {
            Self::initialize(
                api,
                extension_info,
                extension_access,
                library,
                None,
                "external core loaded",
            )
        }
    }

    #[cfg(duckflight_bundled_core)]
    pub(super) unsafe fn load_bundled(
        extension_info: duckdb::ffi::duckdb_extension_info,
        extension_access: *const duckdb::ffi::duckdb_extension_access,
    ) -> Result<Self, Box<dyn Error>> {
        use std::io::Write;

        static CORE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/duckflight_core.bundle"));
        let mut bundle = NamedTempFile::new()
            .map_err(|_| DynamicCoreError("create temporary bundled core file".into()))?;
        bundle
            .write_all(CORE)
            .and_then(|_| bundle.flush())
            .map_err(|_| DynamicCoreError("write temporary bundled core file".into()))?;
        let library = unsafe { Library::new(bundle.path()) }
            .map_err(|_| DynamicCoreError("load bundled core".into()))?;
        let api = {
            let entry =
                unsafe { library.get::<DuckflightCoreApiEntryV1>(DUCKFLIGHT_CORE_API_SYMBOL_V1) }
                    .map_err(|_| DynamicCoreError("resolve bundled core API".into()))?;
            unsafe { entry() }
        };
        unsafe {
            Self::initialize(
                api,
                extension_info,
                extension_access,
                library,
                Some(bundle),
                "bundled core loaded",
            )
        }
    }

    unsafe fn initialize(
        api: *const DuckflightCoreApiV1,
        extension_info: duckdb::ffi::duckdb_extension_info,
        extension_access: *const duckdb::ffi::duckdb_extension_access,
        library: Library,
        bundle_file: Option<NamedTempFile>,
        status_detail: &'static str,
    ) -> Result<Self, Box<dyn Error>> {
        unsafe { validate_api_table_header(api)? };
        let api_ref = unsafe { &*api };
        let create = api_ref
            .create
            .ok_or_else(|| DynamicCoreError("runtime is missing create".into()))?;
        if api_ref.destroy.is_none()
            || api_ref.start.is_none()
            || api_ref.stop.is_none()
            || api_ref.list.is_none()
        {
            return Err(DynamicCoreError("runtime v1 API table is incomplete".into()).into());
        }

        let options = DuckflightCoreCreateOptionsV1::new(
            extension_info.cast::<c_void>(),
            extension_access.cast::<c_void>(),
            16,
        );
        let mut handle = ptr::null_mut();
        let mut error = ErrorBuffer::new();
        let status = unsafe { create(&options, &mut handle, error.as_ffi()) };
        check_status(status, &error)?;
        if handle.is_null() {
            return Err(DynamicCoreError("runtime created a null handle".into()).into());
        }

        Ok(Self {
            api,
            handle,
            status_detail,
            _library: library,
            _bundle_file: bundle_file,
        })
    }

    fn api(&self) -> &DuckflightCoreApiV1 {
        unsafe { &*self.api }
    }
}

impl Drop for DynamicCore {
    fn drop(&mut self) {
        if let Some(destroy) = self.api().destroy {
            unsafe { destroy(self.handle) };
        }
    }
}

impl CoreRuntime for DynamicCore {
    fn start(
        &self,
        protocol: Protocol,
        address: &str,
        config_file: &str,
    ) -> Result<String, Box<dyn Error>> {
        unsafe extern "C" fn receive(
            context: *mut c_void,
            value: DuckflightBytesV1,
        ) -> DuckflightStatus {
            let output = unsafe { &mut *context.cast::<Result<String, String>>() };
            *output = borrowed_string(value);
            if output.is_ok() {
                DuckflightStatus::OK
            } else {
                DuckflightStatus::INVALID_ARGUMENT
            }
        }

        let mut output = Err("runtime did not return a server address".to_string());
        let mut error = ErrorBuffer::new();
        let status = unsafe {
            self.api().start.unwrap()(
                self.handle,
                abi_protocol(protocol),
                DuckflightBytesV1::from_utf8(address),
                DuckflightBytesV1::from_utf8(config_file),
                Some(receive),
                (&mut output as *mut Result<String, String>).cast(),
                error.as_ffi(),
            )
        };
        check_status(status, &error)?;
        output.map_err(|message| DynamicCoreError(message).into())
    }

    fn stop(&self, protocol: Protocol, address: &str) -> Result<bool, Box<dyn Error>> {
        let mut stopped = false;
        let mut error = ErrorBuffer::new();
        let status = unsafe {
            self.api().stop.unwrap()(
                self.handle,
                abi_protocol(protocol),
                DuckflightBytesV1::from_utf8(address),
                &mut stopped,
                error.as_ffi(),
            )
        };
        check_status(status, &error)?;
        Ok(stopped)
    }

    fn snapshots(&self) -> Result<Vec<(String, String)>, Box<dyn Error>> {
        unsafe extern "C" fn visit(
            context: *mut c_void,
            protocol: DuckflightProtocol,
            address: DuckflightBytesV1,
        ) -> DuckflightStatus {
            let output = unsafe { &mut *context.cast::<Result<Vec<(String, String)>, String>>() };
            let item = (protocol_name(protocol), borrowed_string(address));
            match item {
                (Ok(protocol), Ok(address)) => {
                    if let Ok(servers) = output {
                        servers.push((protocol, address));
                        DuckflightStatus::OK
                    } else {
                        DuckflightStatus::INTERNAL
                    }
                }
                (protocol, address) => {
                    *output = Err(protocol.err().or_else(|| address.err()).unwrap());
                    DuckflightStatus::INVALID_ARGUMENT
                }
            }
        }

        let mut output = Ok(Vec::new());
        let mut error = ErrorBuffer::new();
        let status = unsafe {
            self.api().list.unwrap()(
                self.handle,
                Some(visit),
                (&mut output as *mut Result<Vec<(String, String)>, String>).cast(),
                error.as_ffi(),
            )
        };
        check_status(status, &error)?;
        output.map_err(|message| DynamicCoreError(message).into())
    }

    fn status(&self) -> CoreStatus {
        CoreStatus {
            loaded: true,
            detail: self.status_detail.to_string(),
        }
    }
}

fn abi_protocol(protocol: Protocol) -> DuckflightProtocol {
    match protocol {
        Protocol::PgWire => DuckflightProtocol::PGWIRE,
        Protocol::FlightSql => DuckflightProtocol::FLIGHT_SQL,
    }
}

fn protocol_name(protocol: DuckflightProtocol) -> Result<String, String> {
    match protocol {
        DuckflightProtocol::PGWIRE => Ok("pgwire".into()),
        DuckflightProtocol::FLIGHT_SQL => Ok("flight".into()),
        other => Err(format!("runtime returned unsupported protocol {}", other.0)),
    }
}

fn borrowed_string(value: DuckflightBytesV1) -> Result<String, String> {
    if value.len == 0 {
        return Ok(String::new());
    }
    if value.data.is_null() {
        return Err("runtime returned a null string pointer".into());
    }
    let bytes = unsafe { slice::from_raw_parts(value.data, value.len) };
    str::from_utf8(bytes)
        .map(str::to_owned)
        .map_err(|error| format!("runtime returned invalid UTF-8: {error}"))
}

struct ErrorBuffer {
    bytes: [u8; ERROR_CAPACITY],
    required: usize,
    ffi: DuckflightOutputBufferV1,
}

impl ErrorBuffer {
    fn new() -> Self {
        Self {
            bytes: [0; ERROR_CAPACITY],
            required: 0,
            ffi: DuckflightOutputBufferV1 {
                data: ptr::null_mut(),
                capacity: ERROR_CAPACITY,
                required: ptr::null_mut(),
            },
        }
    }

    fn as_ffi(&mut self) -> *mut DuckflightOutputBufferV1 {
        self.ffi.data = self.bytes.as_mut_ptr();
        self.ffi.required = &mut self.required;
        &mut self.ffi
    }

    fn message(&self) -> String {
        let used = self.required.min(self.bytes.len());
        String::from_utf8_lossy(&self.bytes[..used]).into_owned()
    }
}

fn check_status(status: DuckflightStatus, error: &ErrorBuffer) -> Result<(), Box<dyn Error>> {
    if status.is_ok() {
        return Ok(());
    }
    let message = error.message();
    let detail = if message.is_empty() {
        format!("DuckFlight runtime failed with status {}", status.0)
    } else {
        message
    };
    Err(DynamicCoreError(detail).into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[repr(C)]
    struct ApiHeader {
        struct_size: u32,
        abi_version: u32,
    }

    #[test]
    fn rejects_truncated_api_before_forming_full_reference() {
        let header = ApiHeader {
            struct_size: std::mem::size_of::<ApiHeader>() as u32,
            abi_version: DUCKFLIGHT_CORE_ABI_V1,
        };
        let api = (&raw const header).cast::<DuckflightCoreApiV1>();
        let error = unsafe { validate_api_table_header(api) }.unwrap_err();
        assert_eq!(error.to_string(), "runtime v1 API table is truncated");
    }
}
