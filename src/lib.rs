use duckdb::{
    Connection, Result as DuckResult,
    core::{DataChunkHandle, Inserter, LogicalTypeHandle, LogicalTypeId},
    vtab::{BindInfo, InitInfo, TableFunctionInfo, VTab},
};
use duckflight_extension_abi::DUCKFLIGHT_CORE_ABI_V1;
use std::{
    error::Error,
    ffi::CString,
    ops::Range,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};
use subtle::ConstantTimeEq;

mod dynamic_core;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Protocol {
    PgWire,
    Adbc,
}

impl Protocol {
    fn as_str(self) -> &'static str {
        match self {
            Self::PgWire => "pgwire",
            Self::Adbc => "adbc",
        }
    }

    fn parse(raw: &str) -> Result<Self, Box<dyn Error>> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "pgwire" | "postgres" | "postgresql" => Ok(Self::PgWire),
            "adbc" | "flight" | "flightsql" | "flight_sql" => Ok(Self::Adbc),
            other => Err(format!("unsupported DuckFlight extension protocol: {other}").into()),
        }
    }
}

struct CoreStatus {
    loaded: bool,
    detail: String,
}

trait CoreRuntime: Send + Sync {
    fn start(
        &self,
        protocol: Protocol,
        address: &str,
        users_file: &str,
    ) -> Result<String, Box<dyn Error>>;

    fn stop(&self, protocol: Protocol, address: &str) -> Result<bool, Box<dyn Error>>;

    fn snapshots(&self) -> Result<Vec<(String, String)>, Box<dyn Error>>;

    fn status(&self) -> CoreStatus;
}

struct UnavailableCore {
    detail: String,
}

impl UnavailableCore {
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }

    fn unavailable(&self) -> Box<dyn Error> {
        format!("DuckFlight core runtime is unavailable: {}", self.detail).into()
    }
}

impl CoreRuntime for UnavailableCore {
    fn start(&self, _: Protocol, _: &str, _: &str) -> Result<String, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn stop(&self, _: Protocol, _: &str) -> Result<bool, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn snapshots(&self) -> Result<Vec<(String, String)>, Box<dyn Error>> {
        Ok(Vec::new())
    }

    fn status(&self) -> CoreStatus {
        CoreStatus {
            loaded: false,
            detail: self.detail.clone(),
        }
    }
}

struct ExtensionState {
    core: Box<dyn CoreRuntime>,
    management: ManagementCapability,
}

impl ExtensionState {
    fn from_box(core: Box<dyn CoreRuntime>, management: ManagementCapability) -> Self {
        Self { core, management }
    }
}

struct ManagementCapability {
    expected: Option<Box<[u8]>>,
}

impl ManagementCapability {
    fn from_environment() -> Self {
        let expected = std::env::var_os("DUCKFLIGHT_MANAGEMENT_TOKEN")
            .and_then(|value| value.into_string().ok())
            .filter(|value| !value.is_empty())
            .map(String::into_bytes)
            .map(Vec::into_boxed_slice);
        Self { expected }
    }

    fn authorize(&self, supplied: &str) -> Result<(), Box<dyn Error>> {
        let authorized = self
            .expected
            .as_deref()
            .is_some_and(|expected| supplied.as_bytes().ct_eq(expected).unwrap_u8() == 1);
        if authorized {
            Ok(())
        } else {
            Err("DuckFlight management authorization failed".into())
        }
    }
}

struct EmitOnce {
    done: AtomicBool,
}

impl EmitOnce {
    fn new() -> Self {
        Self {
            done: AtomicBool::new(false),
        }
    }
}

fn logical_type(id: LogicalTypeId) -> LogicalTypeHandle {
    LogicalTypeHandle::from(id)
}

fn varchar() -> LogicalTypeHandle {
    logical_type(LogicalTypeId::Varchar)
}

fn set_string(
    output: &mut DataChunkHandle,
    column: usize,
    row: usize,
    value: &str,
) -> Result<(), Box<dyn Error>> {
    let vector = output.flat_vector(column);
    if row >= vector.capacity() {
        return Err(format!(
            "DuckFlight attempted to write row {row} beyond output vector capacity {}",
            vector.capacity()
        )
        .into());
    }
    vector.insert(row, CString::new(value)?);
    Ok(())
}

fn chunk_bounds(total: usize, start: usize, capacity: usize) -> Option<Range<usize>> {
    if start >= total || capacity == 0 {
        return None;
    }
    Some(start..start.saturating_add(capacity).min(total))
}

#[derive(Clone)]
struct ServeBindData {
    address: String,
    users_file: String,
    management_token: String,
}

struct PgWireServe;

impl VTab for PgWireServe {
    type InitData = EmitOnce;
    type BindData = ServeBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        bind.add_result_column("protocol", varchar());
        bind.add_result_column("address", varchar());
        Ok(ServeBindData {
            address: bind.get_parameter(0).to_string(),
            users_file: bind.get_parameter(1).to_string(),
            management_token: bind.get_parameter(2).to_string(),
        })
    }

    fn init(_: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        Ok(EmitOnce::new())
    }

    fn func(
        info: &TableFunctionInfo<Self>,
        output: &mut DataChunkHandle,
    ) -> Result<(), Box<dyn Error>> {
        if info.get_init_data().done.swap(true, Ordering::Relaxed) {
            output.set_len(0);
            return Ok(());
        }
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        state
            .management
            .authorize(&info.get_bind_data().management_token)?;
        let address = state.core.start(
            Protocol::PgWire,
            &info.get_bind_data().address,
            &info.get_bind_data().users_file,
        )?;
        set_string(output, 0, 0, Protocol::PgWire.as_str())?;
        set_string(output, 1, 0, &address)?;
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar(), varchar(), varchar()])
    }
}

struct AdbcServe;

impl VTab for AdbcServe {
    type InitData = EmitOnce;
    type BindData = ServeBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        bind.add_result_column("protocol", varchar());
        bind.add_result_column("address", varchar());
        Ok(ServeBindData {
            address: bind.get_parameter(0).to_string(),
            users_file: bind.get_parameter(1).to_string(),
            management_token: bind.get_parameter(2).to_string(),
        })
    }

    fn init(_: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        Ok(EmitOnce::new())
    }

    fn func(
        info: &TableFunctionInfo<Self>,
        output: &mut DataChunkHandle,
    ) -> Result<(), Box<dyn Error>> {
        if info.get_init_data().done.swap(true, Ordering::Relaxed) {
            output.set_len(0);
            return Ok(());
        }
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        state
            .management
            .authorize(&info.get_bind_data().management_token)?;
        let address = state.core.start(
            Protocol::Adbc,
            &info.get_bind_data().address,
            &info.get_bind_data().users_file,
        )?;
        set_string(output, 0, 0, Protocol::Adbc.as_str())?;
        set_string(output, 1, 0, &address)?;
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar(), varchar(), varchar()])
    }
}

#[derive(Clone)]
struct StopBindData {
    protocol: String,
    address: String,
    management_token: String,
}

struct StopServer;

impl VTab for StopServer {
    type InitData = EmitOnce;
    type BindData = StopBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        bind.add_result_column("status", varchar());
        Ok(StopBindData {
            protocol: bind.get_parameter(0).to_string(),
            address: bind.get_parameter(1).to_string(),
            management_token: bind.get_parameter(2).to_string(),
        })
    }

    fn init(_: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        Ok(EmitOnce::new())
    }

    fn func(
        info: &TableFunctionInfo<Self>,
        output: &mut DataChunkHandle,
    ) -> Result<(), Box<dyn Error>> {
        if info.get_init_data().done.swap(true, Ordering::Relaxed) {
            output.set_len(0);
            return Ok(());
        }
        let bind = info.get_bind_data();
        let protocol = Protocol::parse(&bind.protocol)?;
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        state.management.authorize(&bind.management_token)?;
        let status = if state.core.stop(protocol, &bind.address)? {
            format!("stopped {} server on {}", protocol.as_str(), bind.address)
        } else {
            format!("no {} server on {}", protocol.as_str(), bind.address)
        };
        set_string(output, 0, 0, &status)?;
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar(), varchar(), varchar()])
    }
}

#[derive(Clone)]
struct EmptyBindData;

struct ServerList;

struct ServerListInit {
    snapshots: Vec<(String, String)>,
    offset: AtomicUsize,
}

#[derive(Clone)]
struct ManagementBindData {
    management_token: String,
}

impl VTab for ServerList {
    type InitData = ServerListInit;
    type BindData = ManagementBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        bind.add_result_column("protocol", varchar());
        bind.add_result_column("address", varchar());
        Ok(ManagementBindData {
            management_token: bind.get_parameter(0).to_string(),
        })
    }

    fn init(info: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let bind = unsafe { &*info.get_bind_data::<ManagementBindData>() };
        state.management.authorize(&bind.management_token)?;
        Ok(ServerListInit {
            snapshots: state.core.snapshots()?,
            offset: AtomicUsize::new(0),
        })
    }

    fn func(
        info: &TableFunctionInfo<Self>,
        output: &mut DataChunkHandle,
    ) -> Result<(), Box<dyn Error>> {
        let init = info.get_init_data();
        let capacity = output.flat_vector(0).capacity();
        let start = init.offset.fetch_add(capacity, Ordering::Relaxed);
        let Some(bounds) = chunk_bounds(init.snapshots.len(), start, capacity) else {
            output.set_len(0);
            return Ok(());
        };
        for (row, (protocol, address)) in init.snapshots[bounds.clone()].iter().enumerate() {
            set_string(output, 0, row, protocol)?;
            set_string(output, 1, row, address)?;
        }
        output.set_len(bounds.len());
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar()])
    }
}

struct CoreStatusTable;

impl VTab for CoreStatusTable {
    type InitData = EmitOnce;
    type BindData = EmptyBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        bind.add_result_column("loaded", logical_type(LogicalTypeId::Boolean));
        bind.add_result_column("abi_version", logical_type(LogicalTypeId::UBigint));
        bind.add_result_column("detail", varchar());
        Ok(EmptyBindData)
    }

    fn init(_: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        Ok(EmitOnce::new())
    }

    fn func(
        info: &TableFunctionInfo<Self>,
        output: &mut DataChunkHandle,
    ) -> Result<(), Box<dyn Error>> {
        if info.get_init_data().done.swap(true, Ordering::Relaxed) {
            output.set_len(0);
            return Ok(());
        }
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let status = state.core.status();
        unsafe {
            output.flat_vector(0).as_mut_slice::<bool>()[0] = status.loaded;
            output.flat_vector(1).as_mut_slice::<u64>()[0] = u64::from(DUCKFLIGHT_CORE_ABI_V1);
        }
        set_string(output, 2, 0, &status.detail)?;
        output.set_len(1);
        Ok(())
    }
}

fn register_extension(
    connection: &Connection,
    state: Arc<ExtensionState>,
) -> DuckResult<(), Box<dyn Error>> {
    connection
        .register_table_function_with_extra_info::<PgWireServe, _>("duckflight_pg_serve", &state)?;
    connection
        .register_table_function_with_extra_info::<AdbcServe, _>("duckflight_adbc_serve", &state)?;
    connection
        .register_table_function_with_extra_info::<StopServer, _>("duckflight_stop", &state)?;
    connection
        .register_table_function_with_extra_info::<ServerList, _>("duckflight_servers", &state)?;
    connection.register_table_function_with_extra_info::<CoreStatusTable, _>(
        "duckflight_core_status",
        &state,
    )?;
    Ok(())
}

unsafe fn duckflight_init_c_api_internal(
    info: duckdb::ffi::duckdb_extension_info,
    access: *const duckdb::ffi::duckdb_extension_access,
) -> Result<bool, Box<dyn Error>> {
    unsafe {
        if !duckdb::ffi::duckdb_rs_extension_api_init(info, access, "v1.5.5")? {
            return Ok(false);
        }

        let get_database = (*access)
            .get_database
            .ok_or("get_database function pointer is null in duckdb_extension_access")?;
        let database_ptr = get_database(info);
        if database_ptr.is_null() {
            return Ok(false);
        }
        let database: duckdb::ffi::duckdb_database = *database_ptr;
        let connection = Connection::open_from_raw(database.cast())?;

        #[cfg(duckflight_bundled_core)]
        let core: Box<dyn CoreRuntime> = match dynamic_core::DynamicCore::load_bundled(info, access)
        {
            Ok(core) => Box::new(core),
            Err(_) => Box::new(UnavailableCore::new("bundled core failed to initialize")),
        };

        #[cfg(not(duckflight_bundled_core))]
        let core: Box<dyn CoreRuntime> = match std::env::var_os("DUCKFLIGHT_CORE_PATH") {
            None => Box::new(UnavailableCore::new("DUCKFLIGHT_CORE_PATH is not set")),
            Some(path) => match dynamic_core::DynamicCore::load(path.as_ref(), info, access) {
                Ok(core) => Box::new(core),
                Err(_) => Box::new(UnavailableCore::new(
                    "configured runtime failed to load or initialize",
                )),
            },
        };
        register_extension(
            &connection,
            Arc::new(ExtensionState::from_box(
                core,
                ManagementCapability::from_environment(),
            )),
        )?;
        Ok(true)
    }
}

/// DuckDB C API entrypoint for the public extension shim.
///
/// # Safety
///
/// DuckDB supplies both pointers and guarantees they remain valid for initialization.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn duckflight_init_c_api(
    info: duckdb::ffi::duckdb_extension_info,
    access: *const duckdb::ffi::duckdb_extension_access,
) -> bool {
    unsafe {
        match duckflight_init_c_api_internal(info, access) {
            Ok(initialized) => initialized,
            Err(error) => {
                if let Some(set_error) = (*access).set_error {
                    match CString::new(error.to_string()) {
                        Ok(message) => set_error(info, message.as_ptr()),
                        Err(_) => set_error(
                            info,
                            c"DuckFlight initialization failed and its error was not valid UTF-8"
                                .as_ptr(),
                        ),
                    }
                }
                false
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn protocol_aliases_are_supported() {
        assert_eq!(Protocol::parse("postgresql").unwrap(), Protocol::PgWire);
        assert_eq!(Protocol::parse("flight_sql").unwrap(), Protocol::Adbc);
        assert!(Protocol::parse("http").is_err());
    }

    #[test]
    fn unavailable_core_is_safe_to_introspect() {
        let core = UnavailableCore::new("not configured");
        assert!(!core.status().loaded);
        assert!(core.snapshots().unwrap().is_empty());
        assert!(core.start(Protocol::PgWire, "127.0.0.1:0", "").is_err());
    }

    #[test]
    fn management_capability_fails_closed() {
        let disabled = ManagementCapability { expected: None };
        assert!(disabled.authorize("anything").is_err());

        let enabled = ManagementCapability {
            expected: Some(b"correct-token".to_vec().into_boxed_slice()),
        };
        assert!(enabled.authorize("correct-token").is_ok());
        assert!(enabled.authorize("wrong-token").is_err());
    }

    #[test]
    fn server_snapshots_are_paginated_to_vector_capacity() {
        let vector_capacity = 2_048;
        let total = vector_capacity * 2 + 17;
        let first = chunk_bounds(total, 0, vector_capacity).unwrap();
        let second = chunk_bounds(total, vector_capacity, vector_capacity).unwrap();
        let third = chunk_bounds(total, vector_capacity * 2, vector_capacity).unwrap();

        assert_eq!(first, 0..vector_capacity);
        assert_eq!(second, vector_capacity..vector_capacity * 2);
        assert_eq!(third, vector_capacity * 2..total);
        assert!(first.len() <= vector_capacity);
        assert!(second.len() <= vector_capacity);
        assert!(third.len() <= vector_capacity);
        assert!(chunk_bounds(total, total, vector_capacity).is_none());
    }
}
