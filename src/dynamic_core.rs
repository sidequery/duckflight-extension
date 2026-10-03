use super::{CoreRuntime, CoreStatus, Protocol};
use duckflight_extension_abi::{
    DUCKFLIGHT_CORE_ABI_V1, DUCKFLIGHT_CORE_API_SYMBOL_V1, DuckflightBytesV1,
    DuckflightCoreApiEntryV1, DuckflightCoreApiV1, DuckflightCoreCreateOptionsV1,
    DuckflightCoreHandle, DuckflightOutputBufferV1, DuckflightProtocol, DuckflightStatus,
};
use libloading::Library;
use std::{error::Error, ffi::c_void, fmt, path::Path, ptr, slice, str};
#[cfg(target_os = "linux")]
type BundleFile = std::fs::File;
#[cfg(not(target_os = "linux"))]
type BundleFile = tempfile::NamedTempFile;

#[cfg(any(duckflight_bundled_core, test))]
fn prepare_bundle(bytes: &[u8]) -> Result<(BundleFile, std::path::PathBuf), DynamicCoreError> {
    use std::io::Write;

    #[cfg(target_os = "linux")]
    let (mut bundle, path) = {
        use std::os::fd::{AsRawFd, FromRawFd};

        // An executable memfd is independent of TMPDIR's mount flags. Request execution
        // explicitly on newer kernels; older kernels do not recognize MFD_EXEC.
        let flags = libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING;
        let mut fd =
            unsafe { libc::memfd_create(c"duckflight-core".as_ptr(), flags | libc::MFD_EXEC) };
        if fd < 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::EINVAL) {
            fd = unsafe { libc::memfd_create(c"duckflight-core".as_ptr(), flags) };
        }
        if fd < 0 {
            return Err(DynamicCoreError(format!(
                "create executable bundled core memfd: {}",
                std::io::Error::last_os_error()
            )));
        }
        let bundle = unsafe { std::fs::File::from_raw_fd(fd) };
        let path = std::path::PathBuf::from(format!("/proc/self/fd/{}", bundle.as_raw_fd()));
        (bundle, path)
    };
    #[cfg(not(target_os = "linux"))]
    let (mut bundle, path) = {
        let bundle = tempfile::NamedTempFile::new().map_err(|error| {
            DynamicCoreError(format!("create temporary bundled core file: {error}"))
        })?;
        let path = bundle.path().to_owned();
        (bundle, path)
    };
    bundle
        .write_all(bytes)
        .and_then(|_| bundle.flush())
        .map_err(|error| DynamicCoreError(format!("write bundled core: {error}")))?;

    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;

        // Seal the complete payload before executing it. Keep the descriptor alive
        // until after dlclose so its /proc path cannot be reused by another library.
        let seals =
            libc::F_SEAL_WRITE | libc::F_SEAL_GROW | libc::F_SEAL_SHRINK | libc::F_SEAL_SEAL;
        if unsafe { libc::fcntl(bundle.as_raw_fd(), libc::F_ADD_SEALS, seals) } < 0 {
            return Err(DynamicCoreError(format!(
                "seal bundled core memfd: {}",
                std::io::Error::last_os_error()
            )));
        }
    }
    Ok((bundle, path))
}

#[cfg(any(duckflight_bundled_core, test))]
unsafe fn load_bundle(bytes: &[u8]) -> Result<(Library, BundleFile), DynamicCoreError> {
    let (bundle, path) = prepare_bundle(bytes)?;
    let library = unsafe { Library::new(&path) }.map_err(|error| {
        DynamicCoreError(format!(
            "load bundled core from {}: {error}",
            path.display()
        ))
    })?;
    Ok((library, bundle))
}

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
    _bundle_file: Option<BundleFile>,
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
        let library = unsafe { Library::new(path) }.map_err(|error| {
            DynamicCoreError(format!(
                "load configured runtime library {}: {error}",
                path.display()
            ))
        })?;
        let api = {
            let entry =
                unsafe { library.get::<DuckflightCoreApiEntryV1>(DUCKFLIGHT_CORE_API_SYMBOL_V1) }
                    .map_err(|error| {
                    DynamicCoreError(format!(
                        "resolve duckflight_core_api_v1 in configured runtime {}: {error}",
                        path.display()
                    ))
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
        static CORE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/duckflight_core.bundle"));
        let (library, bundle) = unsafe { load_bundle(CORE) }?;
        let api = {
            let entry = unsafe {
                library.get::<DuckflightCoreApiEntryV1>(DUCKFLIGHT_CORE_API_SYMBOL_V1)
            }
            .map_err(|error| DynamicCoreError(format!("resolve bundled core API: {error}")))?;
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
        bundle_file: Option<BundleFile>,
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

    fn message(&self) -> Option<String> {
        // `required` is the full diagnostic size, not a count of bytes copied.
        // Providers may leave the buffer untouched when it cannot hold the message.
        if self.required > self.bytes.len() {
            return None;
        }
        Some(String::from_utf8_lossy(&self.bytes[..self.required]).into_owned())
    }
}

fn check_status(status: DuckflightStatus, error: &ErrorBuffer) -> Result<(), Box<dyn Error>> {
    if status.is_ok() {
        return Ok(());
    }
    let detail = match error.message() {
        None => format!(
            "DuckFlight runtime failed with status {}; diagnostic requires {} bytes (buffer capacity {})",
            status.0,
            error.required,
            error.bytes.len()
        ),
        Some(message) if message.is_empty() => {
            format!("DuckFlight runtime failed with status {}", status.0)
        }
        Some(message) => message,
    };
    Err(DynamicCoreError(detail).into())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Model the ABI provider's all-or-nothing output behavior used by mock_core.
    unsafe extern "C" fn report_error(
        message: DuckflightBytesV1,
        status: DuckflightStatus,
        output: *mut DuckflightOutputBufferV1,
    ) -> DuckflightStatus {
        let output = unsafe { &mut *output };
        unsafe { *output.required = message.len };
        if message.len <= output.capacity && message.len != 0 {
            unsafe { ptr::copy_nonoverlapping(message.data, output.data, message.len) };
        }
        status
    }

    fn provider_error(message: &str, status: DuckflightStatus) -> String {
        let mut error = ErrorBuffer::new();
        let status = unsafe {
            report_error(
                DuckflightBytesV1::from_utf8(message),
                status,
                error.as_ffi(),
            )
        };
        check_status(status, &error).unwrap_err().to_string()
    }

    #[test]
    fn oversized_provider_diagnostic_preserves_status_without_reading_output() {
        let message = format!("{}é", "a".repeat(ERROR_CAPACITY - 1));
        // Neither zero-filled storage nor plausible text in unwritten bytes is a diagnostic.
        for unwritten in [0, b'x'] {
            let mut error = ErrorBuffer::new();
            error.bytes.fill(unwritten);
            let status = unsafe {
                report_error(
                    DuckflightBytesV1::from_utf8(&message),
                    DuckflightStatus::INVALID_ARGUMENT,
                    error.as_ffi(),
                )
            };
            assert_eq!(status, DuckflightStatus::INVALID_ARGUMENT);
            assert_eq!(error.bytes, [unwritten; ERROR_CAPACITY]);
            let detail = check_status(status, &error).unwrap_err().to_string();
            assert_eq!(
                detail,
                "DuckFlight runtime failed with status 1; diagnostic requires 4097 bytes (buffer capacity 4096)"
            );
            assert!(std::ffi::CString::new(detail).is_ok());
        }
    }

    #[test]
    fn short_provider_diagnostic_preserves_utf8() {
        assert_eq!(
            provider_error(
                "cannot bind café: 地址 unavailable",
                DuckflightStatus::INTERNAL
            ),
            "cannot bind café: 地址 unavailable"
        );
    }

    #[test]
    fn provider_diagnostic_fits_exactly_at_utf8_boundary() {
        let message = format!("{}é", "a".repeat(ERROR_CAPACITY - 2));
        assert_eq!(
            provider_error(&message, DuckflightStatus::INTERNAL),
            message
        );
    }

    #[test]
    fn empty_provider_diagnostic_uses_original_status() {
        assert_eq!(
            provider_error("", DuckflightStatus::NOT_FOUND),
            "DuckFlight runtime failed with status 4"
        );
    }

    #[test]
    fn external_loader_error_preserves_path_and_os_reason() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("missing-duckflight-core");
        let loader_error = unsafe { Library::new(&path) }.err().unwrap();
        let error = unsafe { DynamicCore::load(&path, ptr::null_mut(), ptr::null()) }
            .err()
            .unwrap();
        assert_eq!(
            error.to_string(),
            format!(
                "load configured runtime library {}: {loader_error}",
                path.display()
            )
        );
    }

    #[test]
    fn bundled_loader_error_preserves_os_reason() {
        let error = unsafe { load_bundle(b"invalid native library") }
            .err()
            .unwrap()
            .to_string();
        let (_, reason) = error.split_once(": ").expect("loader reason is included");
        assert!(error.starts_with("load bundled core from "));
        assert!(!reason.is_empty());
        // The loader identifies the failed image, rather than only our operation.
        assert!(reason.contains("/"));
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn bundled_memfd_is_sealed_and_loads_native_code() {
        use std::os::fd::AsRawFd;

        // Locate the running process's native libc without assuming a distro path.
        let mut info: libc::Dl_info = unsafe { std::mem::zeroed() };
        assert_ne!(
            unsafe { libc::dladdr(libc::getpid as *const () as *const c_void, &mut info) },
            0
        );
        let path = unsafe { std::ffi::CStr::from_ptr(info.dli_fname) }
            .to_str()
            .unwrap();
        let bytes = std::fs::read(path).unwrap();
        let (library, bundle) = unsafe { load_bundle(&bytes) }.unwrap();
        let getpid =
            unsafe { library.get::<unsafe extern "C" fn() -> libc::pid_t>(b"getpid\0") }.unwrap();
        assert_eq!(unsafe { getpid() }, unsafe { libc::getpid() });
        let seals = unsafe { libc::fcntl(bundle.as_raw_fd(), libc::F_GET_SEALS) };
        assert_eq!(
            seals,
            libc::F_SEAL_WRITE | libc::F_SEAL_GROW | libc::F_SEAL_SHRINK | libc::F_SEAL_SEAL
        );
        assert_eq!(unsafe { libc::ftruncate(bundle.as_raw_fd(), 0) }, -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::EPERM)
        );
        drop(library);
        drop(bundle);
    }

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
