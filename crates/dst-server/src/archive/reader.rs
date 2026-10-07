//! Read common archive formats without allowing extraction or external filters.

use std::ffi::{CStr, c_char, c_int, c_void};
use std::fs::File;
use std::io::{self, Read};
use std::marker::PhantomData;
use std::os::fd::AsRawFd;
use std::ptr::{self, NonNull};

use anyhow::{Context, Result, bail, ensure};

use super::importing::Entry;

// Only the stable read API is needed; pkg-config links the system libarchive.
unsafe extern "C" {
    fn archive_read_new() -> *mut c_void;
    fn archive_read_free(archive: *mut c_void) -> c_int;
    fn archive_read_support_filter_by_code(archive: *mut c_void, code: c_int) -> c_int;
    fn archive_read_support_format_tar(archive: *mut c_void) -> c_int;
    fn archive_read_support_format_zip_seekable(archive: *mut c_void) -> c_int;
    fn archive_read_support_format_7zip(archive: *mut c_void) -> c_int;
    fn archive_read_support_format_rar(archive: *mut c_void) -> c_int;
    fn archive_read_support_format_rar5(archive: *mut c_void) -> c_int;
    fn archive_read_set_format_option(
        archive: *mut c_void,
        module: *const c_char,
        option: *const c_char,
        value: *const c_char,
    ) -> c_int;
    fn archive_read_open_fd(archive: *mut c_void, fd: c_int, block: usize) -> c_int;
    fn archive_filter_count(archive: *mut c_void) -> c_int;
    fn archive_read_next_header(archive: *mut c_void, entry: *mut *mut c_void) -> c_int;
    fn archive_read_data(archive: *mut c_void, buffer: *mut c_void, length: usize) -> isize;
    fn archive_error_string(archive: *mut c_void) -> *const c_char;
    fn archive_entry_pathname(entry: *mut c_void) -> *const c_char;
    fn archive_entry_size(entry: *mut c_void) -> i64;
    fn archive_entry_filetype(entry: *mut c_void) -> libc::mode_t;
    fn archive_entry_hardlink(entry: *mut c_void) -> *const c_char;
    fn archive_entry_symlink(entry: *mut c_void) -> *const c_char;
    fn archive_entry_is_encrypted(entry: *mut c_void) -> c_int;
}

pub(super) struct Reader<'a> {
    handle: NonNull<c_void>,
    file: PhantomData<&'a mut File>,
}

impl<'a> Reader<'a> {
    pub(super) fn open(file: &'a mut File, signature: &[u8; 6]) -> Result<Self> {
        let _locale = Utf8Locale::enter()?;
        // SAFETY: this allocates an independent handle, owned by Reader until Drop.
        let handle =
            NonNull::new(unsafe { archive_read_new() }).context("allocate archive reader")?;
        let reader = Self {
            handle,
            file: PhantomData,
        };
        let handle = reader.handle.as_ptr();
        // SAFETY: all calls use the live handle and the file outlives the reader.
        unsafe {
            reader.check(archive_read_support_filter_by_code(handle, 0))?;
            if signature == b"7z\xbc\xaf\x27\x1c" {
                reader.check(archive_read_support_format_7zip(handle))?;
            } else if signature == b"Rar!\x1a\x07" {
                reader.check(archive_read_support_format_rar(handle))?;
                reader.check(archive_read_support_format_rar5(handle))?;
            } else if signature.starts_with(b"PK") {
                reader.check(archive_read_support_format_zip_seekable(handle))?;
                reader.check(archive_read_set_format_option(
                    handle,
                    c"zip".as_ptr(),
                    c"hdrcharset".as_ptr(),
                    c"CP437".as_ptr(),
                ))?;
            } else {
                // gzip, bzip2, xz, zstd; reject external-program fallbacks.
                for code in [1, 2, 6, 14] {
                    reader.check(archive_read_support_filter_by_code(handle, code))?;
                }
                reader.check(archive_read_support_format_tar(handle))?;
                // Read through TAR padding so compression trailers are validated.
                reader.check(archive_read_set_format_option(
                    handle,
                    c"tar".as_ptr(),
                    c"read_concatenated_archives".as_ptr(),
                    c"1".as_ptr(),
                ))?;
            }
            reader.check(archive_read_open_fd(handle, file.as_raw_fd(), 64 * 1024))?;
            // The gzip precheck validates one outer stream, not nested compression.
            ensure!(
                archive_filter_count(handle) <= 2,
                "nested archive compression is unsupported"
            );
        }
        Ok(reader)
    }

    fn error(&self) -> String {
        // SAFETY: libarchive owns this NUL-terminated string until its next call.
        unsafe {
            let message = archive_error_string(self.handle.as_ptr());
            if message.is_null() {
                "archive decoding failed".into()
            } else {
                CStr::from_ptr(message).to_string_lossy().into_owned()
            }
        }
    }

    fn check(&self, status: c_int) -> Result<()> {
        ensure!(status == 0, "archive: {}", self.error());
        Ok(())
    }

    pub(super) fn next(&mut self) -> Result<Option<Entry>> {
        let _locale = Utf8Locale::enter()?;
        let mut entry = ptr::null_mut();
        // SAFETY: the output entry and its metadata remain owned by the live reader.
        unsafe {
            let status = archive_read_next_header(self.handle.as_ptr(), &mut entry);
            if status == 1 {
                return Ok(None);
            }
            self.check(status)?;
            ensure!(!entry.is_null(), "archive returned no entry");
            ensure!(
                archive_entry_hardlink(entry).is_null() && archive_entry_symlink(entry).is_null(),
                "archive links are unsupported"
            );
            ensure!(
                archive_entry_is_encrypted(entry) == 0,
                "encrypted archives are unsupported"
            );
            let name = archive_entry_pathname(entry);
            ensure!(!name.is_null(), "archive entry has no path");
            let name = CStr::from_ptr(name)
                .to_str()
                .context("archive path is not UTF-8")?;
            let mode = archive_entry_filetype(entry);
            if !matches!(mode, libc::S_IFREG | libc::S_IFDIR) {
                bail!("archive entries must be regular files or directories");
            }
            Ok(Some(Entry::new(
                name,
                mode == libc::S_IFDIR,
                archive_entry_size(entry)
                    .try_into()
                    .context("negative archive entry size")?,
                Some(mode),
            )?))
        }
    }
}

impl Read for Reader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        // SAFETY: libarchive writes at most buffer.len() bytes into this live slice.
        let size = unsafe {
            archive_read_data(
                self.handle.as_ptr(),
                buffer.as_mut_ptr().cast(),
                buffer.len(),
            )
        };
        if size < 0 {
            Err(io::Error::other(self.error()))
        } else {
            Ok(size as usize)
        }
    }
}

impl Drop for Reader<'_> {
    fn drop(&mut self) {
        // SAFETY: this is the sole owner and libarchive does not close the borrowed fd.
        unsafe { archive_read_free(self.handle.as_ptr()) };
    }
}

struct Utf8Locale {
    current: libc::locale_t,
    previous: libc::locale_t,
}

impl Utf8Locale {
    fn enter() -> Result<Self> {
        // SAFETY: this changes only the current thread, for one synchronous library call.
        unsafe {
            let current =
                libc::newlocale(libc::LC_CTYPE_MASK, c"C.UTF-8".as_ptr(), ptr::null_mut());
            ensure!(
                !current.is_null(),
                "create UTF-8 archive locale: {}",
                io::Error::last_os_error()
            );
            let previous = libc::uselocale(current);
            if previous.is_null() {
                let error = io::Error::last_os_error();
                libc::freelocale(current);
                return Err(error).context("select UTF-8 archive locale");
            }
            Ok(Self { current, previous })
        }
    }
}

impl Drop for Utf8Locale {
    fn drop(&mut self) {
        // SAFETY: the guard never crosses an await or thread boundary; restore before freeing.
        unsafe {
            libc::uselocale(self.previous);
            libc::freelocale(self.current);
        }
    }
}
