//! C ABI shared by the public DuckDB extension shim and the private DuckFlight core.
//!
//! The contract deliberately contains no Rust-owned strings, trait objects, futures, or
//! allocator-owned values. Every pointer is borrowed for the duration of one call unless the
//! field explicitly describes an opaque handle returned by the core.

use std::ffi::c_void;

pub const DUCKFLIGHT_CORE_ABI_V1: u32 = 1;
pub const DUCKFLIGHT_CORE_API_SYMBOL_V1: &[u8] = b"duckflight_core_api_v1\0";

pub type DuckflightCoreHandle = *mut c_void;

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DuckflightStatus(pub i32);

impl DuckflightStatus {
    pub const OK: Self = Self(0);
    pub const INVALID_ARGUMENT: Self = Self(1);
    pub const ABI_MISMATCH: Self = Self(2);
    pub const BUFFER_TOO_SMALL: Self = Self(3);
    pub const NOT_FOUND: Self = Self(4);
    pub const INTERNAL: Self = Self(5);

    pub const fn is_ok(self) -> bool {
        self.0 == Self::OK.0
    }
}

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct DuckflightProtocol(pub u32);

impl DuckflightProtocol {
    pub const PGWIRE: Self = Self(1);
    pub const FLIGHT_SQL: Self = Self(2);
    pub const HTTP: Self = Self(3);
    pub const QUACK: Self = Self(4);
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct DuckflightBytesV1 {
    pub data: *const u8,
    pub len: usize,
}

impl DuckflightBytesV1 {
    pub const EMPTY: Self = Self {
        data: std::ptr::null(),
        len: 0,
    };

    pub fn from_utf8(value: &str) -> Self {
        Self {
            data: value.as_ptr(),
            len: value.len(),
        }
    }
}

/// Caller-owned output storage.
///
/// The core always writes the required byte count to `required` when it is non-null. Output is
/// UTF-8 without a trailing NUL. A null `data` pointer with zero capacity is a size probe.
#[repr(C)]
#[derive(Debug)]
pub struct DuckflightOutputBufferV1 {
    pub data: *mut u8,
    pub capacity: usize,
    pub required: *mut usize,
}

/// Inputs required to bind a private runtime to the DuckDB instance loading the public shim.
///
/// `extension_info` and `extension_access` are the opaque values supplied to DuckDB's C extension
/// entrypoint. The private core uses them to initialize its own C API function table and obtain the
/// exact host `duckdb_database` handle. They are borrowed only during `create`.
#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct DuckflightCoreCreateOptionsV1 {
    pub struct_size: u32,
    pub abi_version: u32,
    pub extension_info: *mut c_void,
    pub extension_access: *const c_void,
    pub pool_size: u32,
    pub reserved: u32,
}

/// Optional size-tagged suffix for a shim with linked ACL enforcement. Old
/// cores consume only the unchanged v1 prefix. The callback remains valid for
/// the lifetime of the host extension and takes a live duckdb_database.
#[repr(C)]
pub struct DuckflightCoreAclOptionsV1 {
    pub base: DuckflightCoreCreateOptionsV1,
    pub install_acl: unsafe extern "C" fn(*mut c_void) -> bool,
}

impl DuckflightCoreCreateOptionsV1 {
    pub fn new(
        extension_info: *mut c_void,
        extension_access: *const c_void,
        pool_size: u32,
    ) -> Self {
        Self {
            struct_size: std::mem::size_of::<Self>() as u32,
            abi_version: DUCKFLIGHT_CORE_ABI_V1,
            extension_info,
            extension_access,
            pool_size,
            reserved: 0,
        }
    }
}

pub type DuckflightServerVisitorV1 = unsafe extern "C" fn(
    context: *mut c_void,
    protocol: DuckflightProtocol,
    address: DuckflightBytesV1,
) -> DuckflightStatus;

pub type DuckflightStringReceiverV1 =
    unsafe extern "C" fn(context: *mut c_void, value: DuckflightBytesV1) -> DuckflightStatus;

pub type DuckflightCoreCreateV1 = unsafe extern "C" fn(
    options: *const DuckflightCoreCreateOptionsV1,
    out_handle: *mut DuckflightCoreHandle,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

pub type DuckflightCoreDestroyV1 = unsafe extern "C" fn(handle: DuckflightCoreHandle);

pub type DuckflightCoreStartV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    protocol: DuckflightProtocol,
    address: DuckflightBytesV1,
    config_file: DuckflightBytesV1,
    address_receiver: Option<DuckflightStringReceiverV1>,
    address_context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

pub type DuckflightCoreStopV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    protocol: DuckflightProtocol,
    address: DuckflightBytesV1,
    out_stopped: *mut bool,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

pub type DuckflightCoreListV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    visitor: Option<DuckflightServerVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Whether the core runs statements on the DuckDB connection with this id: one of its pooled or
/// listener-session connections. The shim keeps its server functions away from those. A handle the
/// core does not recognize counts as owned.
pub type DuckflightCoreOwnsConnectionV1 =
    unsafe extern "C" fn(handle: DuckflightCoreHandle, connection_id: u64) -> bool;

/// What a config file defines, without any of its secrets. `struct_size` is set by the caller;
/// see `DuckflightConfigStoreSummaryV1` for the optional suffix describing the account store.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct DuckflightConfigSummaryV1 {
    pub struct_size: u32,
    pub tls: bool,
    pub acl: bool,
    pub users: u64,
    pub tokens: u64,
    pub oidc_providers: u64,
}

/// Validates the config file at `path` as a listener would when it starts. An empty `path` checks
/// the default configuration and describes the default account store, without creating it.
pub type DuckflightCoreConfigCheckV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    path: DuckflightBytesV1,
    out_summary: *mut DuckflightConfigSummaryV1,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Adds a bearer token to the config file at `path` and passes the token to `token_receiver`.
/// `scopes` is comma-separated; empty selects the default scopes. Refused once the account store
/// the file resolves to is initialized, since the token would be ignored; use `token_add`.
pub type DuckflightCoreTokenCreateV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    path: DuckflightBytesV1,
    name: DuckflightBytesV1,
    scopes: DuckflightBytesV1,
    token_receiver: Option<DuckflightStringReceiverV1>,
    token_context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Adds a user to the config file at `path`. With an empty `password_file` a password is
/// generated and passed to `password_receiver`; otherwise the receiver is not called. Refused
/// once the account store the file resolves to is initialized, since the user would be ignored;
/// use `user_add`.
pub type DuckflightCoreUserCreateV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    path: DuckflightBytesV1,
    name: DuckflightBytesV1,
    password_file: DuckflightBytesV1,
    password_receiver: Option<DuckflightStringReceiverV1>,
    password_context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Optional size-tagged suffix of a config summary: the account store the config file resolves
/// to. A caller that wants it passes a pointer to this struct, cast to
/// `*mut DuckflightConfigSummaryV1`, with `base.struct_size` set to `size_of::<Self>()`, and owns
/// `store_path`. A caller passing only the v1 size gets only the v1 fields; a core that predates
/// this suffix writes only those.
///
/// On return `base.struct_size` holds the size the core filled. The core writes the store path
/// as UTF-8 without a trailing NUL into `store_path` and its byte length to `*store_path.required`
/// (when non-null). If `store_path.capacity` is too small, every other field is still filled and
/// `config_check` returns `BUFFER_TOO_SMALL`; call again with at least `required` bytes.
#[repr(C)]
#[derive(Debug)]
pub struct DuckflightConfigStoreSummaryV1 {
    pub base: DuckflightConfigSummaryV1,
    /// Caller-owned output for the resolved store path.
    pub store_path: DuckflightOutputBufferV1,
    /// Whether the store file exists.
    pub store_exists: bool,
    /// Whether the store has at least one user. Once it does, it is the only source of password
    /// users and tokens, and the config file's `[users]` and `[tokens]` are ignored.
    pub store_initialized: bool,
    /// Users in the store.
    pub store_users: u64,
    /// `[users]` entries of the config file that are ignored because the store is initialized.
    pub ignored_users: u64,
    /// `[tokens]` entries of the config file that are ignored because the store is initialized.
    pub ignored_tokens: u64,
}

impl DuckflightConfigStoreSummaryV1 {
    /// A summary request whose store path is written to `store_path`.
    pub fn new(store_path: DuckflightOutputBufferV1) -> Self {
        Self {
            base: DuckflightConfigSummaryV1 {
                struct_size: std::mem::size_of::<Self>() as u32,
                ..Default::default()
            },
            store_path,
            store_exists: false,
            store_initialized: false,
            store_users: 0,
            ignored_users: 0,
            ignored_tokens: 0,
        }
    }
}

/// Receives the user created by `init` or `user_add`. `user` is UTF-8. `password` is the
/// generated password as UTF-8 when `has_password` is true; when the password came from a
/// password file, `has_password` is false and `password` is empty (SQL NULL). Both are borrowed
/// for the duration of the call only. Called once, after the user was committed. A non-OK status
/// is returned by the operation unchanged, and the user is dropped again unless the store changed
/// in the meantime; the error output says which.
pub type DuckflightCreatedUserVisitorV1 = unsafe extern "C" fn(
    context: *mut c_void,
    user: DuckflightBytesV1,
    password: DuckflightBytesV1,
    has_password: bool,
) -> DuckflightStatus;

/// Receives the token created by `token_add`: its user, its name and the token, each UTF-8 and
/// borrowed for the duration of the call only. Called once, after the token was committed; this
/// is the only time the token is available. A non-OK status is returned by the operation
/// unchanged, and the token is dropped again unless the store changed in the meantime; the error
/// output says which.
pub type DuckflightCreatedTokenVisitorV1 = unsafe extern "C" fn(
    context: *mut c_void,
    user: DuckflightBytesV1,
    token_name: DuckflightBytesV1,
    token: DuckflightBytesV1,
) -> DuckflightStatus;

/// Receives one store user per call, ordered by name, as `SHOW USERS` describes it. `user` and
/// `role` (reader, writer or admin) are UTF-8; `tenant` is UTF-8 when `has_tenant` is true and
/// empty (SQL NULL) otherwise. All are borrowed for the duration of the call only. A non-OK status
/// stops the listing and is returned.
pub type DuckflightAccountVisitorV1 = unsafe extern "C" fn(
    context: *mut c_void,
    user: DuckflightBytesV1,
    role: DuckflightBytesV1,
    has_password: bool,
    tokens: u64,
    tenant: DuckflightBytesV1,
    has_tenant: bool,
) -> DuckflightStatus;

/// Receives one row per imported item: `kind` is `user` or `token`, `name` the user or token name,
/// `account` the store user it belongs to, `role` that user's role, and `note` what the import did
/// with it. All are UTF-8 and borrowed for the duration of the call only. The rows are delivered
/// after the import was committed; a non-OK status stops them and is returned, but the import
/// remains.
pub type DuckflightImportVisitorV1 = unsafe extern "C" fn(
    context: *mut c_void,
    kind: DuckflightBytesV1,
    name: DuckflightBytesV1,
    account: DuckflightBytesV1,
    role: DuckflightBytesV1,
    note: DuckflightBytesV1,
) -> DuckflightStatus;

/// Creates the account store if needed and its first user `admin_name`, with the admin role, as
/// the host process. Fails if the store already has a user. `config_file` (UTF-8 path, empty for
/// none) only selects the store. `password_file` (UTF-8 path, empty for none) holds the password,
/// without its final newline; without one a password is generated and passed to `visitor`.
/// `visitor` is required. All inputs are borrowed for the duration of the call.
pub type DuckflightCoreInitV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    admin_name: DuckflightBytesV1,
    password_file: DuckflightBytesV1,
    visitor: Option<DuckflightCreatedUserVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Adds a password user `name` to an initialized store as the host process. `role` (UTF-8) is
/// reader, writer or admin, in any letter case; empty selects reader. `config_file` and
/// `password_file` are as for `init`. `visitor` is required. All inputs are borrowed for the
/// duration of the call.
pub type DuckflightCoreUserAddV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    name: DuckflightBytesV1,
    role: DuckflightBytesV1,
    password_file: DuckflightBytesV1,
    visitor: Option<DuckflightCreatedUserVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Adds a generated bearer token `token_name` to the existing store user `user` as the host
/// process and passes it to `visitor`, which is required. `config_file` is as for `init`. All
/// inputs are UTF-8 and borrowed for the duration of the call.
pub type DuckflightCoreTokenAddV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    user: DuckflightBytesV1,
    token_name: DuckflightBytesV1,
    visitor: Option<DuckflightCreatedTokenVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Lists the store's users to `visitor`, which is required. An absent store lists none and is
/// not created. `config_file` is as for `init` and borrowed for the duration of the call.
pub type DuckflightCoreAccountsV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    visitor: Option<DuckflightAccountVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// Imports the `[users]` and `[tokens]` of the config file at `config_file` (UTF-8 path,
/// required) into the store it resolves to, in one all-or-nothing batch, and passes one row per
/// item to `visitor`, which is required. The file is not changed. Borrowed for the duration of
/// the call.
pub type DuckflightCoreImportV1 = unsafe extern "C" fn(
    handle: DuckflightCoreHandle,
    config_file: DuckflightBytesV1,
    visitor: Option<DuckflightImportVisitorV1>,
    context: *mut c_void,
    error: *mut DuckflightOutputBufferV1,
) -> DuckflightStatus;

/// The core's function table. Entries are only ever appended: a caller must check that
/// `struct_size` covers an entry before reading it, since an older core's table ends earlier.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct DuckflightCoreApiV1 {
    pub struct_size: u32,
    pub abi_version: u32,
    pub create: Option<DuckflightCoreCreateV1>,
    pub destroy: Option<DuckflightCoreDestroyV1>,
    pub start: Option<DuckflightCoreStartV1>,
    pub stop: Option<DuckflightCoreStopV1>,
    pub list: Option<DuckflightCoreListV1>,
    pub owns_connection: Option<DuckflightCoreOwnsConnectionV1>,
    pub config_check: Option<DuckflightCoreConfigCheckV1>,
    pub token_create: Option<DuckflightCoreTokenCreateV1>,
    pub user_create: Option<DuckflightCoreUserCreateV1>,
    // Account store host setup. Absent from cores that predate the account store.
    pub init: Option<DuckflightCoreInitV1>,
    pub user_add: Option<DuckflightCoreUserAddV1>,
    pub token_add: Option<DuckflightCoreTokenAddV1>,
    pub accounts: Option<DuckflightCoreAccountsV1>,
    pub import: Option<DuckflightCoreImportV1>,
}

pub type DuckflightCoreApiEntryV1 = unsafe extern "C" fn() -> *const DuckflightCoreApiV1;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn create_options_are_self_describing() {
        let options =
            DuckflightCoreCreateOptionsV1::new(std::ptr::null_mut(), std::ptr::null(), 16);
        assert_eq!(
            options.struct_size as usize,
            std::mem::size_of_val(&options)
        );
        assert_eq!(options.abi_version, DUCKFLIGHT_CORE_ABI_V1);
        assert_eq!(options.pool_size, 16);
        assert_eq!(options.reserved, 0);
    }

    /// The function table as shims built before the account store entries know it.
    #[repr(C)]
    struct ApiBeforeAccountStore {
        struct_size: u32,
        abi_version: u32,
        create: Option<DuckflightCoreCreateV1>,
        destroy: Option<DuckflightCoreDestroyV1>,
        start: Option<DuckflightCoreStartV1>,
        stop: Option<DuckflightCoreStopV1>,
        list: Option<DuckflightCoreListV1>,
        owns_connection: Option<DuckflightCoreOwnsConnectionV1>,
        config_check: Option<DuckflightCoreConfigCheckV1>,
        token_create: Option<DuckflightCoreTokenCreateV1>,
        user_create: Option<DuckflightCoreUserCreateV1>,
    }

    #[test]
    fn appended_entries_keep_the_layout_older_shims_read() {
        use std::mem::{offset_of, size_of};
        macro_rules! same_offset {
            ($($field:ident),*) => {$(
                assert_eq!(
                    offset_of!(DuckflightCoreApiV1, $field),
                    offset_of!(ApiBeforeAccountStore, $field),
                    stringify!($field)
                );
            )*};
        }
        same_offset!(
            struct_size,
            abi_version,
            create,
            destroy,
            start,
            stop,
            list,
            owns_connection,
            config_check,
            token_create,
            user_create
        );
        // The new entries follow the old table, which an older shim's size check accepts.
        assert_eq!(
            offset_of!(DuckflightCoreApiV1, init),
            size_of::<ApiBeforeAccountStore>()
        );
        assert!(size_of::<DuckflightCoreApiV1>() > size_of::<ApiBeforeAccountStore>());

        // The store summary extends the unchanged v1 summary.
        assert_eq!(offset_of!(DuckflightConfigStoreSummaryV1, base), 0);
        assert_eq!(size_of::<DuckflightConfigSummaryV1>(), 32);
        assert!(
            offset_of!(DuckflightConfigStoreSummaryV1, store_path)
                >= size_of::<DuckflightConfigSummaryV1>()
        );
        let request = DuckflightConfigStoreSummaryV1::new(DuckflightOutputBufferV1 {
            data: std::ptr::null_mut(),
            capacity: 0,
            required: std::ptr::null_mut(),
        });
        assert_eq!(
            request.base.struct_size as usize,
            size_of::<DuckflightConfigStoreSummaryV1>()
        );
    }

    #[test]
    fn status_and_protocol_values_are_stable() {
        assert!(DuckflightStatus::OK.is_ok());
        assert!(!DuckflightStatus::INTERNAL.is_ok());
        assert_eq!(DuckflightProtocol::PGWIRE.0, 1);
        assert_eq!(DuckflightProtocol::FLIGHT_SQL.0, 2);
        assert_eq!(DuckflightProtocol::HTTP.0, 3);
        assert_eq!(DuckflightProtocol::QUACK.0, 4);
    }
}
