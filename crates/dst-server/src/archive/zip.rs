//! Bounded ZIP metadata checks shared by native saves and script bundles.

use anyhow::{Context, Result, ensure};
use std::{
    collections::BTreeSet,
    fs::File,
    io::{Read, Seek, SeekFrom},
};
use zip::{
    ZipArchive,
    read::{ArchiveOffset, Config},
};

const MAX_DIRECTORY_BYTES: u64 = 16 * 1024 * 1024;

pub(crate) fn open(mut file: File, max_bytes: u64, max_entries: usize) -> Result<ZipArchive<File>> {
    let count = preflight_directory(&mut file, max_bytes, max_entries)?;
    let archive = ZipArchive::with_config(
        Config {
            archive_offset: ArchiveOffset::Known(0),
        },
        file,
    )?;
    ensure!(archive.len() == count, "duplicate ZIP archive entries");
    Ok(archive)
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
fn preflight_directory(file: &mut File, max_bytes: u64, max_entries: usize) -> Result<usize> {
    let size = file.metadata()?.len();
    ensure!(
        (22..=max_bytes).contains(&size),
        "ZIP archive exceeds the size limit or is truncated"
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
        .context("ZIP archive has no complete end record")?;
    let end_record = &tail[end..end + 22];
    ensure!(
        le16(end_record, 4) == 0 && le16(end_record, 6) == 0,
        "split ZIP archives are unsupported"
    );
    ensure!(
        le16(end_record, 8) == le16(end_record, 10),
        "inconsistent ZIP archive entry count"
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
        count <= max_entries as u64,
        "ZIP archive contains too many entries"
    );
    ensure!(
        directory_size <= MAX_DIRECTORY_BYTES
            && directory_start.checked_add(directory_size) == Some(trailer_start),
        "invalid or oversized ZIP central directory"
    );
    file.seek(SeekFrom::Start(directory_start))?;
    let mut directory = vec![0; directory_size as usize];
    file.read_exact(&mut directory)?;
    let mut position = 0;
    let mut names = BTreeSet::new();
    while position < directory.len() {
        ensure!(
            directory.len() - position >= 46,
            "truncated ZIP directory entry"
        );
        let entry = &directory[position..];
        ensure!(&entry[..4] == b"PK\x01\x02", "invalid ZIP directory entry");
        let name_length = usize::from(le16(entry, 28));
        let record_length =
            46 + name_length + usize::from(le16(entry, 30)) + usize::from(le16(entry, 32));
        ensure!(
            record_length <= entry.len() && le16(entry, 34) == 0,
            "truncated or split ZIP directory entry"
        );
        let name = std::str::from_utf8(&entry[46..46 + name_length])
            .context("ZIP archive names must be UTF-8")?;
        ensure!(
            names.insert(name.to_owned()),
            "unsupported or duplicate ZIP archive entry"
        );
        ensure!(
            names.len() <= max_entries,
            "ZIP archive contains too many entries"
        );
        position += record_length;
    }
    ensure!(
        names.len() as u64 == count,
        "inconsistent ZIP archive entry count"
    );
    file.rewind()?;
    Ok(names.len())
}
