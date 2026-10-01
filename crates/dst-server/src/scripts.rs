//! Build and verify the SDK's native DST script bundle without executing Lua.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use full_moon::{
    LuaVersion,
    ast::{Call, Expression, FunctionArgs, Prefix, Stmt, Suffix},
    tokenizer::{StringLiteralQuoteType, TokenType},
};
use include_dir::{Dir, include_dir};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use zip::{CompressionMethod, ZipArchive, ZipWriter, read::Config, write::SimpleFileOptions};

pub const ENTRYPOINT: &str = "scripts/globalvariableoverrides.lua";
pub const MANIFEST: &str = "scripts/dst_server_bundle.json";
pub const ORIGINAL_ENTRYPOINT: &[u8] = b"-- Intentionally blank\n";
pub const BOOTSTRAP: &[u8] = b"require(\"dst_server.bootstrap\").start()\n";
const MAIN: &str = "scripts/main.lua";
const MAX_ARCHIVE_BYTES: u64 = 1024 * 1024 * 1024;
const MAX_ENTRY_BYTES: u64 = 64 * 1024 * 1024;
const MAX_DIRECTORY_BYTES: u64 = 16 * 1024 * 1024;
const MAX_ENTRIES: usize = 20_000;
const SDK_VERSION: &str = env!("CARGO_PKG_VERSION");
static LUA: Dir<'_> = include_dir!("$CARGO_MANIFEST_DIR/../../resources/lua");

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ScriptBundle {
    pub path: PathBuf,
    pub sdk_version: String,
    pub source_digest: String,
    pub native_files: usize,
    pub sdk_files: usize,
    pub entrypoint: String,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    format: u8,
    sdk_version: String,
    source_digest: String,
    files: BTreeMap<String, String>,
    sdk_files: Vec<String>,
}

struct Inspected {
    names: Vec<String>,
    native: BTreeMap<String, String>,
    manifest: Option<Manifest>,
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn source_digest(files: &BTreeMap<String, String>) -> Result<String> {
    Ok(digest(&serde_json::to_vec(files)?))
}

fn owned(name: &str) -> bool {
    name.starts_with("scripts/dst_server/")
        || matches!(
            name,
            "scripts/dst_server.lua" | "scripts/components/dst_server_runtime.lua"
        )
}

fn payload() -> Result<BTreeMap<String, Vec<u8>>> {
    let mut files = BTreeMap::new();
    let mut directories = vec![&LUA];
    while let Some(directory) = directories.pop() {
        directories.extend(directory.dirs());
        for file in directory
            .files()
            .filter(|file| file.path().extension().is_some_and(|ext| ext == "lua"))
        {
            let name = format!(
                "scripts/{}",
                file.path().to_str().context("invalid embedded Lua path")?
            );
            files.insert(name, file.contents().to_vec());
        }
    }
    validate_payload(&files)?;
    Ok(files)
}

fn validate_payload(files: &BTreeMap<String, Vec<u8>>) -> Result<()> {
    ensure!(
        files.keys().all(|name| owned(name)),
        "SDK Lua resources are outside the bundle namespace"
    );
    for required in [
        "scripts/dst_server.lua",
        "scripts/dst_server/bootstrap.lua",
        "scripts/components/dst_server_runtime.lua",
    ] {
        ensure!(
            files.contains_key(required),
            "SDK Lua resources are incomplete"
        );
    }
    Ok(())
}

fn valid_name(name: &str) -> bool {
    name.starts_with("scripts/")
        && !name.contains(['\\', '\0'])
        && name
            .strip_suffix('/')
            .unwrap_or(name)
            .split('/')
            .all(|part| !matches!(part, "" | "." | ".."))
}

fn le16(bytes: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes(bytes[offset..offset + 2].try_into().unwrap())
}

fn le32(bytes: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(bytes[offset..offset + 4].try_into().unwrap())
}

fn le64(bytes: &[u8], offset: usize) -> u64 {
    u64::from_le_bytes(bytes[offset..offset + 8].try_into().unwrap())
}

/// zip's name index overwrites duplicate entries. Check the bounded central
/// directory first, before the library allocates its index or hides duplicates.
fn preflight_directory(file: &mut File) -> Result<usize> {
    let size = file.metadata()?.len();
    ensure!(
        (22..=MAX_ARCHIVE_BYTES).contains(&size),
        "script archive exceeds the size limit or is truncated"
    );
    let tail_size = size.min(65_535 + 22);
    file.seek(SeekFrom::End(-(tail_size as i64)))?;
    let mut tail = vec![0; tail_size as usize];
    file.read_exact(&mut tail)?;
    let end = (0..=tail.len() - 22)
        .rev()
        .find(|offset| {
            &tail[*offset..*offset + 4] == b"PK\x05\x06"
                && *offset + 22 + usize::from(le16(&tail, *offset + 20)) == tail.len()
        })
        .context("script archive has no complete end record")?;
    let end_record = &tail[end..end + 22];
    ensure!(
        le16(end_record, 4) == 0 && le16(end_record, 6) == 0,
        "split script archives are unsupported"
    );
    ensure!(
        le16(end_record, 8) == le16(end_record, 10),
        "inconsistent script archive entry count"
    );
    let mut count = u64::from(le16(end_record, 10));
    let mut directory_size = u64::from(le32(end_record, 12));
    let mut directory_start = u64::from(le32(end_record, 16));
    let mut trailer_start = size - tail_size + end as u64;
    if trailer_start >= 20 {
        file.seek(SeekFrom::Start(trailer_start - 20))?;
        let mut locator = [0; 20];
        file.read_exact(&mut locator)?;
        if &locator[..4] == b"PK\x06\x07" {
            ensure!(
                le32(&locator, 4) == 0 && le32(&locator, 16) == 1,
                "split ZIP64 archives are unsupported"
            );
            let offset = le64(&locator, 8);
            ensure!(
                offset
                    .checked_add(56)
                    .is_some_and(|end| end <= trailer_start - 20),
                "invalid ZIP64 end record"
            );
            file.seek(SeekFrom::Start(offset))?;
            let mut record = [0; 56];
            file.read_exact(&mut record)?;
            ensure!(
                &record[..4] == b"PK\x06\x06" && le64(&record, 4) >= 44,
                "invalid ZIP64 end record"
            );
            ensure!(
                offset
                    .checked_add(12)
                    .and_then(|start| start.checked_add(le64(&record, 4)))
                    == Some(trailer_start - 20),
                "invalid ZIP64 end record size"
            );
            ensure!(
                le32(&record, 16) == 0
                    && le32(&record, 20) == 0
                    && le64(&record, 24) == le64(&record, 32),
                "split or inconsistent ZIP64 archive"
            );
            count = le64(&record, 32);
            directory_size = le64(&record, 40);
            directory_start = le64(&record, 48);
            trailer_start = offset;
        }
    }
    ensure!(
        count <= MAX_ENTRIES as u64,
        "script archive contains too many entries"
    );
    ensure!(
        directory_size <= MAX_DIRECTORY_BYTES
            && directory_start.checked_add(directory_size) == Some(trailer_start),
        "invalid or oversized script central directory"
    );
    file.seek(SeekFrom::Start(directory_start))?;
    let mut directory = vec![0; directory_size as usize];
    file.read_exact(&mut directory)?;
    let mut position = 0;
    let mut names = BTreeSet::new();
    while position < directory.len() {
        ensure!(
            directory.len() - position >= 46,
            "truncated script directory entry"
        );
        let entry = &directory[position..];
        ensure!(
            &entry[..4] == b"PK\x01\x02",
            "invalid script directory entry"
        );
        let name_length = usize::from(le16(entry, 28));
        let record_length =
            46 + name_length + usize::from(le16(entry, 30)) + usize::from(le16(entry, 32));
        ensure!(
            record_length <= entry.len() && le16(entry, 34) == 0,
            "truncated or split script directory entry"
        );
        let name = std::str::from_utf8(&entry[46..46 + name_length])
            .context("script archive names must be UTF-8")?;
        ensure!(
            valid_name(name) && names.insert(name.to_owned()),
            "unsupported or duplicate script archive entry"
        );
        ensure!(
            names.len() <= MAX_ENTRIES,
            "script archive contains too many entries"
        );
        position += record_length;
    }
    ensure!(
        names.len() as u64 == count,
        "inconsistent script archive entry count"
    );
    file.rewind()?;
    Ok(names.len())
}

fn open_archive(path: &Path) -> Result<ZipArchive<File>> {
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .with_context(|| format!("open script archive {}", path.display()))?;
    ensure!(
        file.metadata()?.is_file(),
        "script archive must be a regular file"
    );
    let count = preflight_directory(&mut file)?;
    let archive = ZipArchive::with_config(
        Config {
            archive_offset: zip::read::ArchiveOffset::Known(0),
        },
        file,
    )?;
    ensure!(archive.len() == count, "duplicate script archive entries");
    Ok(archive)
}

fn read_entry(archive: &mut ZipArchive<File>, name: &str) -> Result<Vec<u8>> {
    let mut file = archive.by_name(name)?;
    let size = file.size();
    ensure!(
        size <= MAX_ENTRY_BYTES,
        "script archive entry exceeds the size limit"
    );
    let mut content = Vec::new();
    (&mut file)
        .take(MAX_ENTRY_BYTES + 1)
        .read_to_end(&mut content)?;
    ensure!(
        content.len() as u64 == size,
        "script archive entry length is inconsistent"
    );
    Ok(content)
}

fn check_startup(source: &[u8]) -> Result<()> {
    ensure!(
        source.len() <= crate::lua::MAX_SOURCE_BYTES,
        "native main.lua exceeds the parser byte limit"
    );
    let source = std::str::from_utf8(source).context("native main.lua must be UTF-8")?;
    let source = crate::lua::normalize_line_endings(source);
    let ast = full_moon::parse_fallible(&source, LuaVersion::lua51())
        .into_result()
        .map_err(|_| anyhow::anyhow!("native main.lua has invalid Lua 5.1 syntax"))?;
    ensure!(
        ast.to_string() == source,
        "native main.lua was not completely parsed"
    );
    let found = ast.nodes().stmts().any(|statement| {
        let Stmt::FunctionCall(call) = statement else { return false; };
        let Prefix::Name(name) = call.prefix() else { return false; };
        if !matches!(name.token_type(), TokenType::Identifier { identifier } if identifier.as_str() == "require") { return false; }
        let mut suffixes = call.suffixes();
        let Some(Suffix::Call(Call::AnonymousCall(arguments))) = suffixes.next() else { return false; };
        if suffixes.next().is_some() { return false; }
        let token = match arguments {
            FunctionArgs::Parentheses { arguments, .. } if arguments.len() == 1 => {
                let mut expression = arguments.iter().next().unwrap();
                while let Expression::Parentheses { expression: inner, .. } = expression {
                    expression = inner;
                }
                let Expression::String(token) = expression else { return false; };
                token
            },
            FunctionArgs::String(token) => token,
            _ => return false,
        };
        if let TokenType::StringLiteral { literal, quote_type: StringLiteralQuoteType::Brackets, .. } = token.token_type() {
            return literal.as_str().strip_prefix('\n').unwrap_or(literal.as_str()) == "globalvariableoverrides";
        }
        crate::lua::parse_literal(&token.token().to_string()).is_ok_and(|value| value == "globalvariableoverrides")
    });
    ensure!(
        found,
        "native main.lua no longer loads globalvariableoverrides at top level"
    );
    Ok(())
}

fn inspect(
    archive: &mut ZipArchive<File>,
    expected: Option<(&BTreeMap<String, Vec<u8>>, &str)>,
) -> Result<Inspected> {
    let mut names = Vec::new();
    let mut seen = BTreeSet::new();
    let mut total = 0_u64;
    for index in 0..archive.len() {
        let file = archive.by_index(index)?;
        let name = file.name().to_owned();
        ensure!(
            valid_name(&name)
                && seen.insert(name.clone())
                && !file.is_symlink()
                && !file.encrypted()
                && matches!(
                    file.compression(),
                    CompressionMethod::Stored | CompressionMethod::Deflated
                ),
            "unsupported or duplicate script archive entry"
        );
        total = total
            .checked_add(file.size())
            .context("script archive size overflow")?;
        ensure!(
            file.size() <= MAX_ENTRY_BYTES && total <= MAX_ARCHIVE_BYTES,
            "script archive exceeds the uncompressed size limit"
        );
        names.push(name);
    }
    ensure!(
        seen.contains(ENTRYPOINT) && seen.contains(MAIN),
        "archive does not contain native DST startup scripts"
    );
    check_startup(&read_entry(archive, MAIN)?)?;
    let manifest = if seen.contains(MANIFEST) {
        let manifest: Manifest = serde_json::from_slice(&read_entry(archive, MANIFEST)?)
            .context("invalid script bundle manifest")?;
        ensure!(
            manifest.format == 1,
            "unsupported script bundle manifest version"
        );
        Some(manifest)
    } else {
        None
    };
    let mut files = BTreeMap::new();
    for name in names.iter().filter(|name| name.as_str() != MANIFEST) {
        files.insert(name.clone(), digest(&read_entry(archive, name)?));
    }
    let Some(manifest) = manifest else {
        ensure!(
            read_entry(archive, ENTRYPOINT)? == ORIGINAL_ENTRYPOINT,
            "native globalvariableoverrides.lua entrypoint has changed"
        );
        ensure!(
            !files.keys().any(|name| owned(name)),
            "unmanaged archive contains files in the SDK namespace"
        );
        return Ok(Inspected {
            names,
            native: files,
            manifest: None,
        });
    };
    let declared: BTreeSet<_> = manifest.sdk_files.iter().cloned().collect();
    let actual: BTreeSet<_> = files.keys().filter(|name| owned(name)).cloned().collect();
    ensure!(
        manifest.files == files
            && declared.len() == manifest.sdk_files.len()
            && declared == actual
            && files.get(ENTRYPOINT) == Some(&digest(BOOTSTRAP)),
        "managed script bundle contents do not match its manifest"
    );
    let mut native: BTreeMap<_, _> = files
        .iter()
        .filter(|(name, _)| !owned(name))
        .map(|(name, hash)| (name.clone(), hash.clone()))
        .collect();
    native.insert(ENTRYPOINT.to_owned(), digest(ORIGINAL_ENTRYPOINT));
    ensure!(
        source_digest(&native)? == manifest.source_digest,
        "managed script bundle native contents do not match its source digest"
    );
    if let Some((payload, version)) = expected {
        let expected: BTreeMap<_, _> = payload
            .iter()
            .map(|(name, bytes)| (name.clone(), digest(bytes)))
            .collect();
        let actual: BTreeMap<_, _> = files.into_iter().filter(|(name, _)| owned(name)).collect();
        ensure!(
            actual == expected && manifest.sdk_version == version,
            "managed script bundle does not match the installed SDK"
        );
    }
    Ok(Inspected {
        names,
        native,
        manifest: Some(manifest),
    })
}

/// Verify all content and the embedded SDK. Supply a trusted native source to
/// verify provenance; an archive's own manifest only detects corruption.
pub fn verify_bundle(path: impl AsRef<Path>, source: Option<&Path>) -> Result<ScriptBundle> {
    verify_with_payload(path.as_ref(), source, &payload()?, SDK_VERSION)
}

fn verify_with_payload(
    path: &Path,
    source: Option<&Path>,
    payload: &BTreeMap<String, Vec<u8>>,
    version: &str,
) -> Result<ScriptBundle> {
    let inspected = inspect(&mut open_archive(path)?, Some((payload, version)))?;
    let manifest = inspected
        .manifest
        .context("script archive has not been built with the SDK")?;
    if let Some(source) = source {
        let original = inspect(&mut open_archive(source)?, None)?;
        ensure!(
            inspected.native == original.native,
            "script bundle does not preserve the supplied native source"
        );
    }
    Ok(ScriptBundle {
        path: std::path::absolute(path)?,
        sdk_version: manifest.sdk_version,
        source_digest: manifest.source_digest,
        native_files: inspected.native.len(),
        sdk_files: manifest.sdk_files.len(),
        entrypoint: ENTRYPOINT.to_owned(),
    })
}

/// Atomically build, rebuild or upgrade a bundle. Run while the game is stopped.
/// Source and output may name the same file.
pub fn build_bundle(source: impl AsRef<Path>, output: impl AsRef<Path>) -> Result<ScriptBundle> {
    build_with_payload(source.as_ref(), output.as_ref(), &payload()?, SDK_VERSION)
}

fn build_with_payload(
    source: &Path,
    output: &Path,
    payload: &BTreeMap<String, Vec<u8>>,
    version: &str,
) -> Result<ScriptBundle> {
    validate_payload(payload)?;
    let mut original = open_archive(source)?;
    let inspected = inspect(&mut original, None)?;
    let mut files = inspected.native.clone();
    files.insert(ENTRYPOINT.to_owned(), digest(BOOTSTRAP));
    files.extend(
        payload
            .iter()
            .map(|(name, bytes)| (name.clone(), digest(bytes))),
    );
    let manifest = Manifest {
        format: 1,
        sdk_version: version.to_owned(),
        source_digest: source_digest(&inspected.native)?,
        files,
        sdk_files: payload.keys().cloned().collect(),
    };
    let parent = output
        .parent()
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let filename = output
        .file_name()
        .context("bundle output must have a filename")?
        .to_string_lossy();
    let mut temporary = tempfile::Builder::new()
        .prefix(&format!(".{filename}."))
        .suffix(".tmp")
        .tempfile_in(parent)?;
    {
        let mut writer = ZipWriter::new(temporary.as_file_mut());
        writer.set_raw_comment(original.comment().into())?;
        for name in inspected
            .names
            .iter()
            .filter(|name| name.as_str() != MANIFEST && !owned(name))
        {
            let file = original.by_name(name)?;
            if name == ENTRYPOINT {
                let options = file
                    .options()
                    .into_full_options()
                    .with_file_comment(file.comment());
                writer.start_file(name, options)?;
                writer.write_all(BOOTSTRAP)?;
            } else {
                writer.raw_copy_file(file)?;
            }
        }
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .unix_permissions(0o644);
        for (name, bytes) in payload {
            writer.start_file(name, options)?;
            writer.write_all(bytes)?;
        }
        writer.start_file(MANIFEST, options)?;
        serde_json::to_writer(&mut writer, &manifest)?;
        writer.write_all(b"\n")?;
        writer.finish()?;
    }
    temporary
        .as_file()
        .set_permissions(original.into_inner().metadata()?.permissions())?;
    temporary.as_file().sync_all()?;
    let mut result = verify_with_payload(temporary.path(), None, payload, version)?;
    temporary.persist(output).map_err(|error| error.error)?;
    File::open(parent)?.sync_all()?;
    result.path = std::path::absolute(output)?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};
    use zip::DateTime;

    fn native(directory: &Path) -> PathBuf {
        let path = directory.join("native.zip");
        let mut writer = ZipWriter::new(File::create(&path).unwrap());
        writer.set_comment("native archive comment").unwrap();
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .unix_permissions(0o640)
            .last_modified_time(DateTime::from_date_and_time(2026, 9, 16, 1, 2, 4).unwrap())
            .into_full_options()
            .with_file_comment("native member comment");
        for (name, content) in [
            (MAIN, b"require(\"globalvariableoverrides\")\r\n".as_slice()),
            (ENTRYPOINT, ORIGINAL_ENTRYPOINT),
            (
                "scripts/components/native.lua",
                b"-- native\r\nreturn '\xff'\0\n",
            ),
        ] {
            writer.start_file(name, options.clone()).unwrap();
            writer.write_all(content).unwrap();
        }
        writer.finish().unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
        path
    }

    fn rewrite(path: &Path, changes: &[(&str, &[u8])]) {
        let mut original = ZipArchive::new(File::open(path).unwrap()).unwrap();
        let mut temporary = tempfile::NamedTempFile::new_in(path.parent().unwrap()).unwrap();
        {
            let mut writer = ZipWriter::new(temporary.as_file_mut());
            writer.set_raw_comment(original.comment().into()).unwrap();
            let mut remaining: BTreeMap<_, _> = changes.iter().copied().collect();
            for index in 0..original.len() {
                let file = original.by_index(index).unwrap();
                let name = file.name().to_owned();
                if let Some(bytes) = remaining.remove(name.as_str()) {
                    let options = file
                        .options()
                        .into_full_options()
                        .with_file_comment(file.comment());
                    writer.start_file(name, options).unwrap();
                    writer.write_all(bytes).unwrap();
                } else {
                    writer.raw_copy_file(file).unwrap();
                }
            }
            for (name, bytes) in remaining {
                writer
                    .start_file(name, SimpleFileOptions::default())
                    .unwrap();
                writer.write_all(bytes).unwrap();
            }
            writer.finish().unwrap();
        }
        temporary.persist(path).unwrap();
    }

    #[test]
    fn startup_validation_accepts_native_string_forms() {
        for source in [
            "require 'globalvariableoverrides'",
            "require[[globalvariableoverrides]]",
            "require[=[\nglobalvariableoverrides]=]",
            "require(('globalvariableoverrides'))",
            "local value = 'a\\\r\nb'; require('globalvariableoverrides')",
        ] {
            check_startup(source.as_bytes()).unwrap();
        }
    }

    #[test]
    fn native_bytes_metadata_provenance_and_repeatability_survive() {
        let directory = tempfile::tempdir().unwrap();
        let source = native(directory.path());
        let original = fs::read(&source).unwrap();
        let output = directory.path().join("managed.zip");
        let result = build_bundle(&source, &output).unwrap();
        assert_eq!(fs::read(&source).unwrap(), original);
        assert_eq!(
            fs::metadata(&output).unwrap().permissions().mode() & 0o777,
            0o640
        );
        assert_eq!(verify_bundle(&output, Some(&source)).unwrap(), result);
        let mut native = open_archive(&source).unwrap();
        let mut bundled = open_archive(&output).unwrap();
        assert_eq!(native.comment(), bundled.comment());
        assert_eq!(result.native_files, native.len());
        for index in 0..native.len() {
            let old = native.by_index(index).unwrap();
            let name = old.name().to_owned();
            let new = bundled.by_name(&name).unwrap();
            assert_eq!(
                (old.last_modified(), old.comment(), old.unix_mode()),
                (new.last_modified(), new.comment(), new.unix_mode())
            );
            drop(old);
            drop(new);
            let expected = if name == ENTRYPOINT {
                BOOTSTRAP.to_vec()
            } else {
                read_entry(&mut native, &name).unwrap()
            };
            assert_eq!(read_entry(&mut bundled, &name).unwrap(), expected);
        }
        let before = fs::read(&output).unwrap();
        assert_eq!(build_bundle(&output, &output).unwrap(), result);
        assert_eq!(fs::read(&output).unwrap(), before);
    }

    #[test]
    fn managed_upgrade_removes_obsolete_resources() {
        let directory = tempfile::tempdir().unwrap();
        let source = native(directory.path());
        let output = directory.path().join("managed.zip");
        let mut old = payload().unwrap();
        old.insert(
            "scripts/dst_server/obsolete.lua".to_owned(),
            b"return {}\n".to_vec(),
        );
        let before = build_with_payload(&source, &output, &old, "old").unwrap();
        assert!(verify_bundle(&output, None).is_err());
        let after = build_bundle(&output, &output).unwrap();
        assert_eq!(after.source_digest, before.source_digest);
        assert_eq!(after.sdk_version, SDK_VERSION);
        assert_eq!(verify_bundle(&output, Some(&source)).unwrap(), after);
        assert!(
            open_archive(&output)
                .unwrap()
                .by_name("scripts/dst_server/obsolete.lua")
                .is_err()
        );
    }

    #[test]
    fn unsafe_or_changed_sources_leave_output_untouched() {
        for (name, content) in [
            (ENTRYPOINT, b"-- upstream changed\n".as_slice()),
            (MAIN, b"return {}\n"),
            (MAIN, b"-- require('globalvariableoverrides')\n"),
            (
                MAIN,
                b"if false then require('globalvariableoverrides') end\n",
            ),
            (
                MAIN,
                b"function helper() require('globalvariableoverrides') end\n",
            ),
            ("scripts/dst_server/custom.lua", b"return {}\n"),
            ("scripts/../outside.lua", b"return {}\n"),
        ] {
            let directory = tempfile::tempdir().unwrap();
            let source = native(directory.path());
            rewrite(&source, &[(name, content)]);
            let output = directory.path().join("managed.zip");
            fs::write(&output, b"existing output").unwrap();
            assert!(build_bundle(&source, &output).is_err(), "accepted {name}");
            assert_eq!(fs::read(output).unwrap(), b"existing output");
            assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 2);
        }
    }

    #[test]
    fn corruption_and_wrong_native_source_are_rejected() {
        for name in [
            "scripts/components/native.lua",
            "scripts/dst_server/bootstrap.lua",
            ENTRYPOINT,
        ] {
            let directory = tempfile::tempdir().unwrap();
            let source = native(directory.path());
            let output = directory.path().join("managed.zip");
            build_bundle(&source, &output).unwrap();
            rewrite(&output, &[(name, b"tampered\n")]);
            let before = fs::read(&output).unwrap();
            assert!(verify_bundle(&output, None).is_err());
            assert!(build_bundle(&output, &output).is_err());
            assert_eq!(fs::read(&output).unwrap(), before);
        }
        let directory = tempfile::tempdir().unwrap();
        let source = native(directory.path());
        let output = directory.path().join("managed.zip");
        build_bundle(&source, &output).unwrap();
        rewrite(
            &source,
            &[("scripts/components/native.lua", b"different native version")],
        );
        assert!(verify_bundle(output, Some(&source)).is_err());
    }

    #[test]
    fn directory_limits_and_duplicate_records_are_checked_before_zip_indexing() {
        let directory = tempfile::tempdir().unwrap();
        let source = native(directory.path());
        let bytes = fs::read(&source).unwrap();
        let end = bytes
            .windows(4)
            .rposition(|part| part == b"PK\x05\x06")
            .unwrap();
        let start = le32(&bytes, end + 16) as usize;
        let length = 46
            + usize::from(le16(&bytes, start + 28))
            + usize::from(le16(&bytes, start + 30))
            + usize::from(le16(&bytes, start + 32));
        let mut duplicate = bytes[..end].to_vec();
        duplicate.extend_from_slice(&bytes[start..start + length]);
        let mut end_record = bytes[end..].to_vec();
        let count = le16(&end_record, 10) + 1;
        end_record[8..10].copy_from_slice(&count.to_le_bytes());
        end_record[10..12].copy_from_slice(&count.to_le_bytes());
        let size = le32(&end_record, 12) + length as u32;
        end_record[12..16].copy_from_slice(&size.to_le_bytes());
        duplicate.extend_from_slice(&end_record);
        fs::write(&source, duplicate).unwrap();
        assert!(
            open_archive(&source)
                .unwrap_err()
                .to_string()
                .contains("duplicate")
        );
        let mut excessive = bytes.clone();
        let count = (MAX_ENTRIES as u16 + 1).to_le_bytes();
        excessive[end + 8..end + 10].copy_from_slice(&count);
        excessive[end + 10..end + 12].copy_from_slice(&count);
        fs::write(&source, excessive).unwrap();
        assert!(
            open_archive(&source)
                .unwrap_err()
                .to_string()
                .contains("too many")
        );
    }
}
