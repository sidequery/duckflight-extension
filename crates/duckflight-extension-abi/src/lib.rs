//! Stable C ABI shared by the public DuckDB extension and a DuckFlight runtime provider.
//!
//! No Rust-owned strings, trait objects, futures, or allocator-owned values cross this boundary.
//! Every pointer is borrowed for one call unless it is an opaque core handle returned by `create`.

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

/// Caller-owned UTF-8 output storage without a trailing NUL.
#[repr(C)]
#[derive(Debug)]
pub struct DuckflightOutputBufferV1 {
    pub data: *mut u8,
    pub capacity: usize,
    pub required: *mut usize,
}

/// Inputs used to bind a runtime provider to the exact DuckDB host database.
///
/// The DuckDB pointers are borrowed only during `create`.
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
    users_file: DuckflightBytesV1,
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

    #[test]
    fn status_and_protocol_values_are_stable() {
        assert!(DuckflightStatus::OK.is_ok());
        assert!(!DuckflightStatus::INTERNAL.is_ok());
        assert_eq!(DuckflightProtocol::PGWIRE.0, 1);
        assert_eq!(DuckflightProtocol::FLIGHT_SQL.0, 2);
    }
}
