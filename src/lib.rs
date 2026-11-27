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
        Arc, OnceLock,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
};
mod dynamic_core;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum Protocol {
    PgWire,
    FlightSql,
    Http,
    Quack,
}

impl Protocol {
    fn as_str(self) -> &'static str {
        match self {
            Self::PgWire => "pgwire",
            Self::FlightSql => "flight",
            Self::Http => "http",
            Self::Quack => "quack",
        }
    }

    fn parse(raw: &str) -> Result<Self, Box<dyn Error>> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "pgwire" | "postgres" | "postgresql" => Ok(Self::PgWire),
            "flight" => Ok(Self::FlightSql),
            "http" => Ok(Self::Http),
            "quack" => Ok(Self::Quack),
            other => Err(format!("unsupported DuckFlight extension protocol: {other}").into()),
        }
    }
}

struct CoreStatus {
    loaded: bool,
    detail: String,
}

/// What a config file defines, without any of its secrets.
struct ConfigSummary {
    users: u64,
    tokens: u64,
    oidc_providers: u64,
    tls: bool,
    acl: bool,
    /// None from a core that predates the account store.
    store: Option<StoreSummary>,
}

/// The account store a config file resolves to.
struct StoreSummary {
    path: String,
    exists: bool,
    initialized: bool,
    users: u64,
    /// `[users]` entries of the config file that the initialized store makes ineffective.
    ignored_users: u64,
    /// `[tokens]` entries of the config file that the initialized store makes ineffective.
    ignored_tokens: u64,
}

/// A user added to the account store. Deliberately not `Debug`: it holds the generated password.
struct CreatedUser {
    user: String,
    /// None when the password came from a password file.
    password: Option<String>,
}

/// A token added to the account store. Deliberately not `Debug`: it holds the token.
struct CreatedToken {
    user: String,
    token_name: String,
    token: String,
}

/// One account store user.
struct Account {
    user: String,
    role: String,
    has_password: bool,
    tokens: u64,
    tenant: Option<String>,
}

/// One user or token an import brought into the account store, or found already there.
struct ImportedAccount {
    kind: String,
    name: String,
    account: String,
    role: String,
    note: String,
}

/// The error for a setup function the loaded core does not provide.
fn unsupported(function: &str) -> Box<dyn Error> {
    format!(
        "{function} is not supported by the installed DuckFlight core, which predates the \
         account store; install a DuckFlight build whose core provides it"
    )
    .into()
}

trait CoreRuntime: Send + Sync {
    fn start(
        &self,
        protocol: Protocol,
        address: &str,
        config_file: &str,
    ) -> Result<String, Box<dyn Error>>;

    fn stop(&self, protocol: Protocol, address: &str) -> Result<bool, Box<dyn Error>>;

    fn snapshots(&self) -> Result<Vec<(String, String)>, Box<dyn Error>>;

    /// Validate a config file as a listener would when it starts.
    fn check_config(&self, path: &str) -> Result<ConfigSummary, Box<dyn Error>>;

    /// Add a bearer token and return it. `scopes` is comma-separated; empty means the defaults.
    fn create_token(&self, path: &str, name: &str, scopes: &str) -> Result<String, Box<dyn Error>>;

    /// Add a user. An empty `password_file` generates a password and returns it.
    fn create_user(
        &self,
        path: &str,
        name: &str,
        password_file: &str,
    ) -> Result<Option<String>, Box<dyn Error>>;

    /// Create the account store's first user, an administrator. `config_file` (empty for none)
    /// only selects the store; an empty `password_file` generates a password and returns it.
    fn init(
        &self,
        config_file: &str,
        admin: &str,
        password_file: &str,
    ) -> Result<CreatedUser, Box<dyn Error>>;

    /// Add a user to an initialized account store. An empty `role` selects reader.
    fn add_user(
        &self,
        config_file: &str,
        name: &str,
        role: &str,
        password_file: &str,
    ) -> Result<CreatedUser, Box<dyn Error>>;

    /// Add a generated bearer token to an existing account store user.
    fn add_token(
        &self,
        config_file: &str,
        user: &str,
        token_name: &str,
    ) -> Result<CreatedToken, Box<dyn Error>>;

    /// The account store's users, ordered by name.
    fn accounts(&self, config_file: &str) -> Result<Vec<Account>, Box<dyn Error>>;

    /// Bring the users and tokens of a config file into the account store it resolves to.
    fn import(&self, config_file: &str) -> Result<Vec<ImportedAccount>, Box<dyn Error>>;

    /// Whether the runtime runs statements on this DuckDB connection, as it does for its
    /// listeners' sessions. When in doubt the answer is yes.
    fn owns_connection(&self, connection_id: u64) -> bool;

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

    fn check_config(&self, _: &str) -> Result<ConfigSummary, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn create_token(&self, _: &str, _: &str, _: &str) -> Result<String, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn create_user(&self, _: &str, _: &str, _: &str) -> Result<Option<String>, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn init(&self, _: &str, _: &str, _: &str) -> Result<CreatedUser, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn add_user(&self, _: &str, _: &str, _: &str, _: &str) -> Result<CreatedUser, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn add_token(&self, _: &str, _: &str, _: &str) -> Result<CreatedToken, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn accounts(&self, _: &str) -> Result<Vec<Account>, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn import(&self, _: &str) -> Result<Vec<ImportedAccount>, Box<dyn Error>> {
        Err(self.unavailable())
    }

    fn owns_connection(&self, _: u64) -> bool {
        // Without a runtime there are no listeners, so every connection is the host's.
        false
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
}

impl ExtensionState {
    fn from_box(core: Box<dyn CoreRuntime>) -> Self {
        Self { core }
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

/// `set_string` for a generated password or token, whose errors never carry the value.
fn set_secret(
    output: &mut DataChunkHandle,
    column: usize,
    row: usize,
    secret: &str,
) -> Result<(), Box<dyn Error>> {
    set_string(output, column, row, secret)
        .map_err(|_| "DuckFlight could not return the generated secret".into())
}

fn set_optional_string(
    output: &mut DataChunkHandle,
    column: usize,
    row: usize,
    value: Option<&str>,
) -> Result<(), Box<dyn Error>> {
    match value {
        Some(value) => set_string(output, column, row, value),
        None => {
            output.flat_vector(column).set_null(row);
            Ok(())
        }
    }
}

/// Write a fixed-width value. The caller's row is within the chunk it was given.
fn set_value<T: Copy>(output: &mut DataChunkHandle, column: usize, row: usize, value: T) {
    unsafe { output.flat_vector(column).as_mut_slice::<T>()[row] = value };
}

fn chunk_bounds(total: usize, start: usize, capacity: usize) -> Option<Range<usize>> {
    if start >= total || capacity == 0 {
        return None;
    }
    Some(start..start.saturating_add(capacity).min(total))
}

// duckdb-rs keeps the raw bind handle private. `BindInfo` holds nothing else, which this checks,
// so the handle is the value at its address.
const _: () = assert!(
    std::mem::size_of::<BindInfo>() == std::mem::size_of::<duckdb::ffi::duckdb_bind_info>()
);

/// The id of the DuckDB connection that is binding a table function.
fn binding_connection_id(bind: &BindInfo) -> Result<u64, Box<dyn Error>> {
    unsafe {
        let raw = std::ptr::from_ref(bind)
            .cast::<duckdb::ffi::duckdb_bind_info>()
            .read();
        let mut context: duckdb::ffi::duckdb_client_context = std::ptr::null_mut();
        duckdb::ffi::duckdb_table_function_get_client_context(raw, &mut context);
        if context.is_null() {
            return Err("DuckDB returned no client context for this call".into());
        }
        let connection_id = duckdb::ffi::duckdb_client_context_get_connection_id(context);
        duckdb::ffi::duckdb_destroy_client_context(&mut context);
        Ok(connection_id)
    }
}

/// Starting, stopping and listing servers, and managing accounts and credentials, belongs to the
/// process that owns the database. A
/// session that arrived through a listener runs on one of the runtime's connections and is
/// refused, whatever its credentials allow.
fn require_host_connection(bind: &BindInfo) -> Result<(), Box<dyn Error>> {
    let state = unsafe { &*bind.get_extra_info::<Arc<ExtensionState>>() };
    if state.core.owns_connection(binding_connection_id(bind)?) {
        return Err(HOST_ONLY.into());
    }
    Ok(())
}

const HOST_ONLY: &str = "DuckFlight server and setup functions can only be called from the process that \
     owns the database, not through a DuckFlight listener";

#[derive(Clone)]
struct ServeBindData {
    protocol: Protocol,
    address: String,
    config_file: String,
}

/// Starts one listener. Without `config_file` the listener serves the host's account store.
struct Serve;

impl VTab for Serve {
    type InitData = EmitOnce;
    type BindData = ServeBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("protocol", varchar());
        bind.add_result_column("address", varchar());
        Ok(ServeBindData {
            protocol: Protocol::parse(&bind.get_parameter(0).to_string())?,
            address: bind.get_parameter(1).to_string(),
            config_file: optional_argument(bind, "config_file"),
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
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let address = state
            .core
            .start(bind.protocol, &bind.address, &bind.config_file)?;
        set_string(output, 0, 0, bind.protocol.as_str())?;
        set_string(output, 1, 0, &address)?;
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar(), varchar()])
    }

    fn named_parameters() -> Option<Vec<(String, LogicalTypeHandle)>> {
        named_varchars(&["config_file"])
    }
}

#[derive(Clone)]
struct StopBindData {
    protocol: String,
    address: String,
}

struct StopServer;

impl VTab for StopServer {
    type InitData = EmitOnce;
    type BindData = StopBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("status", varchar());
        Ok(StopBindData {
            protocol: bind.get_parameter(0).to_string(),
            address: bind.get_parameter(1).to_string(),
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
        Some(vec![varchar(), varchar()])
    }
}

#[derive(Clone)]
struct EmptyBindData;

struct ServerList;

struct ServerListInit {
    snapshots: Vec<(String, String)>,
    offset: AtomicUsize,
}

impl VTab for ServerList {
    type InitData = ServerListInit;
    type BindData = EmptyBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("protocol", varchar());
        bind.add_result_column("address", varchar());
        Ok(EmptyBindData)
    }

    fn init(info: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
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
        None
    }
}

/// A named varchar argument, or the empty string when it was not given.
fn optional_argument(bind: &BindInfo, name: &str) -> String {
    bind.get_named_parameter(name)
        .filter(|value| !value.is_null())
        .map(|value| value.to_string())
        .unwrap_or_default()
}

#[derive(Clone)]
struct ConfigPathBindData {
    path: String,
}

struct ConfigCheck;

impl VTab for ConfigCheck {
    type InitData = EmitOnce;
    type BindData = ConfigPathBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        for name in ["users", "tokens", "oidc_providers"] {
            bind.add_result_column(name, logical_type(LogicalTypeId::UBigint));
        }
        for name in ["tls", "acl"] {
            bind.add_result_column(name, logical_type(LogicalTypeId::Boolean));
        }
        // The account store the file resolves to: null from a core that predates the store.
        bind.add_result_column("store_path", varchar());
        for name in ["store_exists", "store_initialized"] {
            bind.add_result_column(name, logical_type(LogicalTypeId::Boolean));
        }
        for name in ["store_users", "ignored_users", "ignored_tokens"] {
            bind.add_result_column(name, logical_type(LogicalTypeId::UBigint));
        }
        Ok(ConfigPathBindData {
            path: bind.get_parameter(0).to_string(),
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
        let summary = state.core.check_config(&info.get_bind_data().path)?;
        let counts = [summary.users, summary.tokens, summary.oidc_providers];
        for (column, count) in counts.into_iter().enumerate() {
            unsafe { output.flat_vector(column).as_mut_slice::<u64>()[0] = count };
        }
        for (column, flag) in [summary.tls, summary.acl].into_iter().enumerate() {
            unsafe {
                output
                    .flat_vector(counts.len() + column)
                    .as_mut_slice::<bool>()[0] = flag
            };
        }
        const STORE_COLUMNS: Range<usize> = 5..11;
        match summary.store {
            Some(store) => {
                set_string(output, STORE_COLUMNS.start, 0, &store.path)?;
                for (column, flag) in [store.exists, store.initialized].into_iter().enumerate() {
                    set_value(output, STORE_COLUMNS.start + 1 + column, 0, flag);
                }
                let counts = [store.users, store.ignored_users, store.ignored_tokens];
                for (column, count) in counts.into_iter().enumerate() {
                    set_value(output, STORE_COLUMNS.start + 3 + column, 0, count);
                }
            }
            None => {
                for column in STORE_COLUMNS {
                    output.flat_vector(column).set_null(0);
                }
            }
        }
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar()])
    }
}

/// A credential to add to a config file. The secret is not an argument: the runtime generates
/// it and the function returns it once, so it never appears in a statement's text.
#[derive(Clone)]
struct CredentialBindData {
    path: String,
    name: String,
    option: String,
}

struct TokenCreate;

impl VTab for TokenCreate {
    type InitData = EmitOnce;
    type BindData = CredentialBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("name", varchar());
        bind.add_result_column("token", varchar());
        Ok(CredentialBindData {
            path: bind.get_parameter(0).to_string(),
            name: bind.get_parameter(1).to_string(),
            option: optional_argument(bind, "scopes"),
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
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let token = state
            .core
            .create_token(&bind.path, &bind.name, &bind.option)?;
        set_string(output, 0, 0, &bind.name)?;
        set_string(output, 1, 0, &token)?;
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar(), varchar()])
    }

    fn named_parameters() -> Option<Vec<(String, LogicalTypeHandle)>> {
        Some(vec![("scopes".to_string(), varchar())])
    }
}

struct UserCreate;

impl VTab for UserCreate {
    type InitData = EmitOnce;
    type BindData = CredentialBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("name", varchar());
        bind.add_result_column("password", varchar());
        Ok(CredentialBindData {
            path: bind.get_parameter(0).to_string(),
            name: bind.get_parameter(1).to_string(),
            option: optional_argument(bind, "password_file"),
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
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let password = state
            .core
            .create_user(&bind.path, &bind.name, &bind.option)?;
        set_string(output, 0, 0, &bind.name)?;
        match password {
            Some(password) => set_string(output, 1, 0, &password)?,
            // The password came from a file the caller already has.
            None => output.flat_vector(1).set_null(0),
        }
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar(), varchar()])
    }

    fn named_parameters() -> Option<Vec<(String, LogicalTypeHandle)>> {
        Some(vec![("password_file".to_string(), varchar())])
    }
}

/// A positional varchar argument, which must not be NULL.
fn required_argument(bind: &BindInfo, index: u64, name: &str) -> Result<String, Box<dyn Error>> {
    let value = bind.get_parameter(index);
    if value.is_null() {
        return Err(format!("{name} must not be NULL").into());
    }
    Ok(value.to_string())
}

fn named_varchars(names: &[&str]) -> Option<Vec<(String, LogicalTypeHandle)>> {
    Some(
        names
            .iter()
            .map(|name| (name.to_string(), varchar()))
            .collect(),
    )
}

/// The account store functions take an optional `config_file`. It is needed only when a config
/// file names a custom store path; otherwise the host's default store is used.
#[derive(Clone)]
struct AccountUserBindData {
    config_file: String,
    name: String,
    role: String,
    password_file: String,
}

fn emit_created_user(
    output: &mut DataChunkHandle,
    created: CreatedUser,
) -> Result<(), Box<dyn Error>> {
    set_string(output, 0, 0, &created.user)?;
    match created.password {
        Some(password) => set_secret(output, 1, 0, &password)?,
        // The password came from a file the caller already has.
        None => output.flat_vector(1).set_null(0),
    }
    output.set_len(1);
    Ok(())
}

struct AccountInit;

impl VTab for AccountInit {
    type InitData = EmitOnce;
    type BindData = AccountUserBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("user", varchar());
        bind.add_result_column("password", varchar());
        Ok(AccountUserBindData {
            config_file: optional_argument(bind, "config_file"),
            name: required_argument(bind, 0, "admin")?,
            role: String::new(),
            password_file: optional_argument(bind, "password_file"),
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
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let created = state
            .core
            .init(&bind.config_file, &bind.name, &bind.password_file)?;
        emit_created_user(output, created)
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar()])
    }

    fn named_parameters() -> Option<Vec<(String, LogicalTypeHandle)>> {
        named_varchars(&["config_file", "password_file"])
    }
}

struct AccountUserAdd;

impl VTab for AccountUserAdd {
    type InitData = EmitOnce;
    type BindData = AccountUserBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("user", varchar());
        bind.add_result_column("password", varchar());
        Ok(AccountUserBindData {
            config_file: optional_argument(bind, "config_file"),
            name: required_argument(bind, 0, "name")?,
            // Empty lets the core apply its default, reader.
            role: optional_argument(bind, "role"),
            password_file: optional_argument(bind, "password_file"),
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
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let created = state.core.add_user(
            &bind.config_file,
            &bind.name,
            &bind.role,
            &bind.password_file,
        )?;
        emit_created_user(output, created)
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar()])
    }

    fn named_parameters() -> Option<Vec<(String, LogicalTypeHandle)>> {
        named_varchars(&["role", "config_file", "password_file"])
    }
}

#[derive(Clone)]
struct AccountTokenBindData {
    config_file: String,
    user: String,
    token_name: String,
}

struct AccountTokenAdd;

impl VTab for AccountTokenAdd {
    type InitData = EmitOnce;
    type BindData = AccountTokenBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        for name in ["user", "token_name", "token"] {
            bind.add_result_column(name, varchar());
        }
        Ok(AccountTokenBindData {
            config_file: optional_argument(bind, "config_file"),
            user: required_argument(bind, 0, "user")?,
            token_name: required_argument(bind, 1, "token_name")?,
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
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let created = state
            .core
            .add_token(&bind.config_file, &bind.user, &bind.token_name)?;
        set_string(output, 0, 0, &created.user)?;
        set_string(output, 1, 0, &created.token_name)?;
        set_secret(output, 2, 0, &created.token)?;
        output.set_len(1);
        Ok(())
    }

    fn parameters() -> Option<Vec<LogicalTypeHandle>> {
        Some(vec![varchar(), varchar()])
    }

    fn named_parameters() -> Option<Vec<(String, LogicalTypeHandle)>> {
        named_varchars(&["config_file"])
    }
}

/// Rows a function fetches from the core when it first runs, never while it is planned, and
/// returns a chunk at a time.
struct FetchedRows<T> {
    rows: OnceLock<Vec<T>>,
    offset: AtomicUsize,
}

impl<T> FetchedRows<T> {
    fn new() -> Self {
        Self {
            rows: OnceLock::new(),
            offset: AtomicUsize::new(0),
        }
    }

    /// The rows for the next chunk, fetching them on the first call.
    fn next_chunk(
        &self,
        capacity: usize,
        fetch: impl FnOnce() -> Result<Vec<T>, Box<dyn Error>>,
    ) -> Result<&[T], Box<dyn Error>> {
        let rows = match self.rows.get() {
            Some(rows) => rows,
            None => {
                let fetched = fetch()?;
                self.rows.get_or_init(|| fetched)
            }
        };
        let start = self.offset.fetch_add(capacity, Ordering::Relaxed);
        Ok(chunk_bounds(rows.len(), start, capacity).map_or(&[], |bounds| &rows[bounds]))
    }
}

#[derive(Clone)]
struct ConfigFileBindData {
    config_file: String,
}

struct AccountList;

impl VTab for AccountList {
    type InitData = FetchedRows<Account>;
    type BindData = ConfigFileBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        bind.add_result_column("user", varchar());
        bind.add_result_column("role", varchar());
        bind.add_result_column("has_password", logical_type(LogicalTypeId::Boolean));
        bind.add_result_column("tokens", logical_type(LogicalTypeId::UBigint));
        bind.add_result_column("tenant", varchar());
        Ok(ConfigFileBindData {
            config_file: optional_argument(bind, "config_file"),
        })
    }

    fn init(_: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        Ok(FetchedRows::new())
    }

    fn func(
        info: &TableFunctionInfo<Self>,
        output: &mut DataChunkHandle,
    ) -> Result<(), Box<dyn Error>> {
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let capacity = output.flat_vector(0).capacity();
        let rows = info.get_init_data().next_chunk(capacity, || {
            state.core.accounts(&info.get_bind_data().config_file)
        })?;
        for (row, account) in rows.iter().enumerate() {
            set_string(output, 0, row, &account.user)?;
            set_string(output, 1, row, &account.role)?;
            set_value(output, 2, row, account.has_password);
            set_value(output, 3, row, account.tokens);
            set_optional_string(output, 4, row, account.tenant.as_deref())?;
        }
        output.set_len(rows.len());
        Ok(())
    }

    fn named_parameters() -> Option<Vec<(String, LogicalTypeHandle)>> {
        named_varchars(&["config_file"])
    }
}

struct AccountImport;

impl VTab for AccountImport {
    type InitData = FetchedRows<ImportedAccount>;
    type BindData = ConfigFileBindData;

    fn bind(bind: &BindInfo) -> Result<Self::BindData, Box<dyn Error>> {
        require_host_connection(bind)?;
        for name in ["kind", "name", "account", "role", "note"] {
            bind.add_result_column(name, varchar());
        }
        Ok(ConfigFileBindData {
            config_file: required_argument(bind, 0, "config_file")?,
        })
    }

    fn init(_: &InitInfo) -> Result<Self::InitData, Box<dyn Error>> {
        Ok(FetchedRows::new())
    }

    fn func(
        info: &TableFunctionInfo<Self>,
        output: &mut DataChunkHandle,
    ) -> Result<(), Box<dyn Error>> {
        let state = unsafe { &*info.get_extra_info::<Arc<ExtensionState>>() };
        let capacity = output.flat_vector(0).capacity();
        let rows = info.get_init_data().next_chunk(capacity, || {
            state.core.import(&info.get_bind_data().config_file)
        })?;
        for (row, item) in rows.iter().enumerate() {
            let values = [
                &item.kind,
                &item.name,
                &item.account,
                &item.role,
                &item.note,
            ];
            for (column, value) in values.into_iter().enumerate() {
                set_string(output, column, row, value)?;
            }
        }
        output.set_len(rows.len());
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
    connection.register_table_function_with_extra_info::<Serve, _>("duckflight_serve", &state)?;
    connection
        .register_table_function_with_extra_info::<StopServer, _>("duckflight_stop", &state)?;
    connection
        .register_table_function_with_extra_info::<ServerList, _>("duckflight_servers", &state)?;
    connection.register_table_function_with_extra_info::<ConfigCheck, _>(
        "duckflight_config_check",
        &state,
    )?;
    connection.register_table_function_with_extra_info::<TokenCreate, _>(
        "duckflight_token_create",
        &state,
    )?;
    connection.register_table_function_with_extra_info::<UserCreate, _>(
        "duckflight_user_create",
        &state,
    )?;
    connection
        .register_table_function_with_extra_info::<AccountInit, _>("duckflight_init", &state)?;
    connection.register_table_function_with_extra_info::<AccountUserAdd, _>(
        "duckflight_user_add",
        &state,
    )?;
    connection.register_table_function_with_extra_info::<AccountTokenAdd, _>(
        "duckflight_token_add",
        &state,
    )?;
    connection
        .register_table_function_with_extra_info::<AccountList, _>("duckflight_accounts", &state)?;
    connection
        .register_table_function_with_extra_info::<AccountImport, _>("duckflight_import", &state)?;
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
        if !duckdb::ffi::duckdb_rs_extension_api_init(info, access, "v1.5.6")? {
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
            Err(error) => Box::new(UnavailableCore::new(format!(
                "bundled core failed to initialize: {error}"
            ))),
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
        register_extension(&connection, Arc::new(ExtensionState::from_box(core)))?;
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
    fn protocol_names_are_supported() {
        assert_eq!(Protocol::parse("postgresql").unwrap(), Protocol::PgWire);
        assert_eq!(Protocol::parse("flight").unwrap(), Protocol::FlightSql);
        assert!(Protocol::parse("flight_sql").is_err());
        assert!(Protocol::parse("adbc").is_err());
        assert_eq!(Protocol::parse(" HTTP ").unwrap(), Protocol::Http);
        assert_eq!(Protocol::parse(" QuAcK ").unwrap(), Protocol::Quack);
    }

    #[test]
    fn unavailable_core_is_safe_to_introspect() {
        let core = UnavailableCore::new("not configured");
        assert!(!core.status().loaded);
        assert!(core.snapshots().unwrap().is_empty());
        assert!(core.start(Protocol::PgWire, "127.0.0.1:0", "").is_err());
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
