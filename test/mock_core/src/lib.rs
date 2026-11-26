use duckflight_extension_abi::{
    DUCKFLIGHT_CORE_ABI_V1, DuckflightAccountVisitorV1, DuckflightBytesV1,
    DuckflightConfigStoreSummaryV1, DuckflightConfigSummaryV1, DuckflightCoreApiV1,
    DuckflightCoreCreateOptionsV1, DuckflightCoreHandle, DuckflightCreatedTokenVisitorV1,
    DuckflightCreatedUserVisitorV1, DuckflightImportVisitorV1, DuckflightOutputBufferV1,
    DuckflightProtocol, DuckflightServerVisitorV1, DuckflightStatus, DuckflightStringReceiverV1,
};
use std::{
    collections::BTreeMap,
    ffi::c_void,
    panic::{AssertUnwindSafe, catch_unwind},
    ptr, slice, str,
    sync::{Mutex, OnceLock},
};

/// Selects which core the mock acts as, so the shim can be tested against cores it must stay
/// compatible with. Unset means the current core.
const MODE_VARIABLE: &str = "DUCKFLIGHT_MOCK_CORE";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Mode {
    Current,
    /// A core built before the account store: its table ends before `init`, and its config check
    /// fills only the v1 summary.
    BeforeAccountStore,
    /// A table of the current size whose account store entries are empty.
    NullAccountEntries,
    /// Every connection is the core's, as for a statement that arrived through a listener.
    OwnsConnections,
}

fn mode() -> Mode {
    static MODE: OnceLock<Mode> = OnceLock::new();
    *MODE.get_or_init(|| match std::env::var(MODE_VARIABLE).as_deref() {
        Ok("before-account-store") => Mode::BeforeAccountStore,
        Ok("null-account-entries") => Mode::NullAccountEntries,
        Ok("owns-connections") => Mode::OwnsConnections,
        _ => Mode::Current,
    })
}

/// A store user. The mock keeps its account store in memory, one per handle.
struct MockAccount {
    role: &'static str,
    has_password: bool,
    tokens: u64,
    tenant: Option<String>,
}

struct MockCore {
    servers: Mutex<Vec<(DuckflightProtocol, String)>>,
    accounts: Mutex<BTreeMap<String, MockAccount>>,
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
        DuckflightProtocol::PGWIRE
        | DuckflightProtocol::FLIGHT_SQL
        | DuckflightProtocol::HTTP
        | DuckflightProtocol::QUACK => Ok(()),
        other => Err(format!("unsupported protocol value {}", other.0)),
    }
}

fn bound_address(protocol: DuckflightProtocol, address: &str) -> String {
    if address.ends_with(":0") {
        match protocol {
            DuckflightProtocol::PGWIRE => "127.0.0.1:15432".into(),
            DuckflightProtocol::FLIGHT_SQL => "127.0.0.1:15051".into(),
            DuckflightProtocol::HTTP => "127.0.0.1:18080".into(),
            DuckflightProtocol::QUACK => "127.0.0.1:18081".into(),
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
                accounts: Mutex::new(BTreeMap::new()),
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

// The mock keeps no files: it answers the setup calls with fixed values that echo its inputs.
// Its account store lives in memory, and a config file path containing `missing` stands for one
// that cannot be read.
fn read_config(config_file: &str) -> Result<(), String> {
    if config_file.contains("missing") {
        return Err(format!("Failed to read DuckFlight config {config_file}"));
    }
    Ok(())
}

/// Where the mock says a config file's account store is. A path containing `long-store` gets a
/// store path longer than a caller is likely to provide room for at first.
fn store_path(config_file: &str) -> String {
    if config_file.contains("long-store") {
        format!("/{}store.duckdb", "long-store-directory/".repeat(40))
    } else {
        ".duckflight/store.duckdb".into()
    }
}

unsafe extern "C" fn config_check(
    handle: DuckflightCoreHandle,
    path: DuckflightBytesV1,
    out_summary: *mut DuckflightConfigSummaryV1,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let core = handle_ref(handle)?;
            let path = borrowed_str(path, "path")?;
            if path.is_empty() {
                return Err("a DuckFlight config file path is required".into());
            }
            read_config(path)?;
            if out_summary.is_null()
                || ((*out_summary).struct_size as usize) < size_of::<DuckflightConfigSummaryV1>()
            {
                return Err("a config summary output of the v1 size is required".into());
            }
            // Like a real core, fill no more than the caller declared or this core knows.
            let extended = mode() != Mode::BeforeAccountStore
                && (*out_summary).struct_size as usize
                    >= size_of::<DuckflightConfigStoreSummaryV1>();
            let filled = if extended {
                size_of::<DuckflightConfigStoreSummaryV1>()
            } else {
                size_of::<DuckflightConfigSummaryV1>()
            };
            *out_summary = DuckflightConfigSummaryV1 {
                struct_size: filled as u32,
                tls: path.contains("tls"),
                acl: false,
                users: 2,
                tokens: 1,
                oidc_providers: 0,
            };
            if !extended {
                return Ok(DuckflightStatus::OK);
            }
            let users = core
                .accounts
                .lock()
                .map_err(|error| error.to_string())?
                .len() as u64;
            let initialized = users > 0;
            let out = out_summary.cast::<DuckflightConfigStoreSummaryV1>();
            (*out).store_exists = initialized;
            (*out).store_initialized = initialized;
            (*out).store_users = users;
            (*out).ignored_users = if initialized { 2 } else { 0 };
            (*out).ignored_tokens = if initialized { 1 } else { 0 };
            Ok(write_output(
                ptr::addr_of_mut!((*out).store_path),
                &store_path(path),
            ))
        })
    }
}

unsafe extern "C" fn token_create(
    handle: DuckflightCoreHandle,
    path: DuckflightBytesV1,
    name: DuckflightBytesV1,
    scopes: DuckflightBytesV1,
    token_receiver: Option<DuckflightStringReceiverV1>,
    token_context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            handle_ref(handle)?;
            borrowed_str(path, "path")?;
            let receiver = token_receiver.ok_or("token receiver is required")?;
            let token = format!(
                "mock-token:{}:{}",
                borrowed_str(name, "name")?,
                borrowed_str(scopes, "scopes")?
            );
            Ok(receiver(
                token_context,
                DuckflightBytesV1::from_utf8(&token),
            ))
        })
    }
}

unsafe extern "C" fn user_create(
    handle: DuckflightCoreHandle,
    path: DuckflightBytesV1,
    name: DuckflightBytesV1,
    password_file: DuckflightBytesV1,
    password_receiver: Option<DuckflightStringReceiverV1>,
    password_context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            handle_ref(handle)?;
            borrowed_str(path, "path")?;
            if !borrowed_str(password_file, "password_file")?.is_empty() {
                return Ok(DuckflightStatus::OK);
            }
            let receiver = password_receiver.ok_or("password receiver is required")?;
            let password = format!("mock-password:{}", borrowed_str(name, "name")?);
            Ok(receiver(
                password_context,
                DuckflightBytesV1::from_utf8(&password),
            ))
        })
    }
}

unsafe extern "C" fn owns_connection(_: DuckflightCoreHandle, _: u64) -> bool {
    // The mock opens no connections of its own, unless it is standing in for a listener session.
    mode() == Mode::OwnsConnections
}

fn not_initialized() -> String {
    "the mock account store is not initialized; run duckflight_init('<admin name>') first".into()
}

/// The password a created user gets: generated unless it came from a password file.
fn mock_password(name: &str, password_file: &str) -> Option<String> {
    password_file
        .is_empty()
        .then(|| format!("mock-password:{name}"))
}

unsafe fn deliver_user(
    visitor: DuckflightCreatedUserVisitorV1,
    context: *mut c_void,
    name: &str,
    password: Option<&str>,
) -> DuckflightStatus {
    unsafe {
        visitor(
            context,
            DuckflightBytesV1::from_utf8(name),
            password.map_or(DuckflightBytesV1::EMPTY, DuckflightBytesV1::from_utf8),
            password.is_some(),
        )
    }
}

unsafe extern "C" fn init(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    admin_name: DuckflightBytesV1,
    password_file: DuckflightBytesV1,
    visitor: Option<DuckflightCreatedUserVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let visitor = visitor.ok_or("created-user visitor is required")?;
            let core = handle_ref(handle)?;
            read_config(borrowed_str(config_file, "config_file")?)?;
            let admin = borrowed_str(admin_name, "admin_name")?;
            if admin.is_empty() {
                return Err("user name must be nonempty".into());
            }
            let mut accounts = core.accounts.lock().map_err(|error| error.to_string())?;
            if !accounts.is_empty() {
                return Err(format!(
                    "the mock account store is already initialized with {} user(s)",
                    accounts.len()
                ));
            }
            accounts.insert(
                admin.to_string(),
                MockAccount {
                    role: "admin",
                    has_password: true,
                    tokens: 0,
                    tenant: None,
                },
            );
            drop(accounts);
            let password = mock_password(admin, borrowed_str(password_file, "password_file")?);
            Ok(deliver_user(visitor, context, admin, password.as_deref()))
        })
    }
}

#[allow(clippy::too_many_arguments)]
unsafe extern "C" fn user_add(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    name: DuckflightBytesV1,
    role: DuckflightBytesV1,
    password_file: DuckflightBytesV1,
    visitor: Option<DuckflightCreatedUserVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let visitor = visitor.ok_or("created-user visitor is required")?;
            let core = handle_ref(handle)?;
            read_config(borrowed_str(config_file, "config_file")?)?;
            let name = borrowed_str(name, "name")?;
            let role = borrowed_str(role, "role")?;
            let role = match role.to_ascii_lowercase().as_str() {
                "" | "reader" => "reader",
                "writer" => "writer",
                "admin" => "admin",
                _ => {
                    return Err(format!(
                        "role must be reader, writer or admin, not {role:?}"
                    ));
                }
            };
            let mut accounts = core.accounts.lock().map_err(|error| error.to_string())?;
            if accounts.is_empty() {
                return Err(not_initialized());
            }
            if accounts.contains_key(name) {
                return Err(format!("user already exists: {name}"));
            }
            accounts.insert(
                name.to_string(),
                MockAccount {
                    role,
                    has_password: true,
                    tokens: 0,
                    tenant: None,
                },
            );
            drop(accounts);
            let password = mock_password(name, borrowed_str(password_file, "password_file")?);
            Ok(deliver_user(visitor, context, name, password.as_deref()))
        })
    }
}

unsafe extern "C" fn token_add(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    user: DuckflightBytesV1,
    token_name: DuckflightBytesV1,
    visitor: Option<DuckflightCreatedTokenVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let visitor = visitor.ok_or("created-token visitor is required")?;
            let core = handle_ref(handle)?;
            read_config(borrowed_str(config_file, "config_file")?)?;
            let user = borrowed_str(user, "user")?;
            let token_name = borrowed_str(token_name, "token_name")?;
            let mut accounts = core.accounts.lock().map_err(|error| error.to_string())?;
            if accounts.is_empty() {
                return Err(not_initialized());
            }
            let account = accounts
                .get_mut(user)
                .ok_or_else(|| format!("user does not exist: {user}"))?;
            account.tokens += 1;
            drop(accounts);
            let token = format!("mock-token:{user}:{token_name}");
            Ok(visitor(
                context,
                DuckflightBytesV1::from_utf8(user),
                DuckflightBytesV1::from_utf8(token_name),
                DuckflightBytesV1::from_utf8(&token),
            ))
        })
    }
}

unsafe extern "C" fn accounts(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    visitor: Option<DuckflightAccountVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let visitor = visitor.ok_or("account visitor is required")?;
            let core = handle_ref(handle)?;
            read_config(borrowed_str(config_file, "config_file")?)?;
            let accounts = core.accounts.lock().map_err(|error| error.to_string())?;
            for (user, account) in accounts.iter() {
                let tenant = account.tenant.as_deref();
                let status = visitor(
                    context,
                    DuckflightBytesV1::from_utf8(user),
                    DuckflightBytesV1::from_utf8(account.role),
                    account.has_password,
                    account.tokens,
                    tenant.map_or(DuckflightBytesV1::EMPTY, DuckflightBytesV1::from_utf8),
                    tenant.is_some(),
                );
                if !status.is_ok() {
                    return Ok(status);
                }
            }
            Ok(DuckflightStatus::OK)
        })
    }
}

/// Every config file the mock imports holds the same two accounts: a writer with a password and
/// a tenant, and a token whose user has neither.
unsafe extern "C" fn import(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    visitor: Option<DuckflightImportVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus {
    unsafe {
        with_error_boundary(error, || {
            let visitor = visitor.ok_or("import visitor is required")?;
            let core = handle_ref(handle)?;
            let config_file = borrowed_str(config_file, "config_file")?;
            if config_file.is_empty() {
                return Err("a DuckFlight config file path is required".into());
            }
            read_config(config_file)?;
            let mut accounts = core.accounts.lock().map_err(|error| error.to_string())?;
            let mut rows = Vec::new();
            for (kind, name, account, role, has_password, tenant) in [
                ("user", "carol", "carol", "writer", true, Some("acme")),
                ("token", "nightly", "svc", "reader", false, None),
            ] {
                let note = if accounts.contains_key(account) {
                    "already in the store; skipped"
                } else {
                    accounts.insert(
                        account.to_string(),
                        MockAccount {
                            role,
                            has_password,
                            tokens: u64::from(kind == "token"),
                            tenant: tenant.map(str::to_string),
                        },
                    );
                    "imported"
                };
                rows.push([kind, name, account, role, note]);
            }
            drop(accounts);
            for [kind, name, account, role, note] in rows {
                let status = visitor(
                    context,
                    DuckflightBytesV1::from_utf8(kind),
                    DuckflightBytesV1::from_utf8(name),
                    DuckflightBytesV1::from_utf8(account),
                    DuckflightBytesV1::from_utf8(role),
                    DuckflightBytesV1::from_utf8(note),
                );
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
    owns_connection: Some(owns_connection),
    config_check: Some(config_check),
    token_create: Some(token_create),
    user_create: Some(user_create),
    init: Some(init),
    user_add: Some(user_add),
    token_add: Some(token_add),
    accounts: Some(accounts),
    import: Some(import),
};

/// A core built before the account store reports the size of its shorter table. The entries
/// after it stay filled in, so a caller that reads past the reported size would find them.
static API_V1_BEFORE_ACCOUNT_STORE: DuckflightCoreApiV1 = DuckflightCoreApiV1 {
    struct_size: std::mem::offset_of!(DuckflightCoreApiV1, init) as u32,
    ..API_V1
};

static API_V1_NULL_ACCOUNT_ENTRIES: DuckflightCoreApiV1 = DuckflightCoreApiV1 {
    init: None,
    user_add: None,
    token_add: None,
    accounts: None,
    import: None,
    ..API_V1
};

#[unsafe(no_mangle)]
pub extern "C" fn duckflight_core_api_v1() -> *const DuckflightCoreApiV1 {
    match mode() {
        Mode::BeforeAccountStore => &API_V1_BEFORE_ACCOUNT_STORE,
        Mode::NullAccountEntries => &API_V1_NULL_ACCOUNT_ENTRIES,
        Mode::Current | Mode::OwnsConnections => &API_V1,
    }
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
        assert!(api.init.is_some());
        assert!(api.import.is_some());
    }

    #[test]
    fn older_core_tables_hide_the_account_store_entries() {
        assert_eq!(
            API_V1_BEFORE_ACCOUNT_STORE.struct_size as usize,
            std::mem::offset_of!(DuckflightCoreApiV1, init)
        );
        assert!(API_V1_BEFORE_ACCOUNT_STORE.init.is_some());
        assert_eq!(API_V1_NULL_ACCOUNT_ENTRIES.struct_size, API_V1.struct_size);
        assert!(API_V1_NULL_ACCOUNT_ENTRIES.init.is_none());
        assert!(API_V1_NULL_ACCOUNT_ENTRIES.import.is_none());
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
