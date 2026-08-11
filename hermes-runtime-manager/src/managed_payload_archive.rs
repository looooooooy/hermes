use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use tar::{Archive, Builder, EntryType, Header};
use thiserror::Error;

const MAX_FILES: usize = 100_000;
const MAX_EXPANDED_BYTES: u64 = 16 * 1024 * 1024 * 1024;
const COPY_BUFFER_BYTES: usize = 1024 * 1024;
const REQUIRED_MANIFEST: &str = "MANAGED-RELEASE-PAYLOAD.json";

#[derive(Debug, Error)]
pub enum ManagedPayloadArchiveError {
    #[error("managed payload archive input is invalid: {0}")]
    InvalidInput(String),
    #[error("managed payload archive entry is unsafe: {0}")]
    UnsafeEntry(String),
    #[error("managed payload archive exceeds bounded expansion limits")]
    ExpansionLimit,
    #[error("managed payload archive is missing its portable manifest")]
    MissingManifest,
    #[error("managed payload archive I/O failed: {0}")]
    Io(#[from] std::io::Error),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedPayloadArchiveReceiptV1 {
    pub files: usize,
    pub expanded_bytes: u64,
    pub output_root: PathBuf,
}

pub fn pack_managed_payload(
    payload_root: &Path,
    output: &Path,
) -> Result<ManagedPayloadArchiveReceiptV1, ManagedPayloadArchiveError> {
    let root = canonical_payload_root(payload_root)?;
    let files = enumerate_regular_files(&root)?;
    if files.is_empty() || files.len() > MAX_FILES {
        return Err(ManagedPayloadArchiveError::ExpansionLimit);
    }
    if !files
        .iter()
        .any(|path| path == Path::new(REQUIRED_MANIFEST))
    {
        return Err(ManagedPayloadArchiveError::MissingManifest);
    }
    let expanded_bytes = files.iter().try_fold(0u64, |total, relative| {
        let size = fs::metadata(root.join(relative))?.len();
        total
            .checked_add(size)
            .filter(|value| *value <= MAX_EXPANDED_BYTES)
            .ok_or(ManagedPayloadArchiveError::ExpansionLimit)
    })?;

    if !output.is_absolute() || output.exists() || output.is_symlink() {
        return Err(ManagedPayloadArchiveError::InvalidInput(
            "output must be a fresh absolute path".to_owned(),
        ));
    }
    let parent = output.parent().ok_or_else(|| {
        ManagedPayloadArchiveError::InvalidInput("output has no parent directory".to_owned())
    })?;
    prepare_private_dir(parent)?;
    let file = create_private_file(output)?;
    let mut encoder = zstd::Encoder::new(file, 19)?;
    encoder.include_checksum(true)?;
    let mut builder = Builder::new(encoder);

    for relative in &files {
        validate_relative(relative)?;
        let source = root.join(relative);
        let mut input = File::open(&source)?;
        let metadata = input.metadata()?;
        if !metadata.is_file() || source.is_symlink() {
            return Err(ManagedPayloadArchiveError::InvalidInput(format!(
                "payload source changed while packing: {}",
                relative.display()
            )));
        }
        let mut header = Header::new_gnu();
        header.set_entry_type(EntryType::Regular);
        header.set_size(metadata.len());
        header.set_mode(0o444);
        header.set_uid(0);
        header.set_gid(0);
        header.set_mtime(0);
        builder.append_data(&mut header, relative, &mut input)?;
    }
    let encoder = builder.into_inner()?;
    let output_file = encoder.finish()?;
    output_file.sync_all()?;

    Ok(ManagedPayloadArchiveReceiptV1 {
        files: files.len(),
        expanded_bytes,
        output_root: root,
    })
}

pub fn unpack_managed_payload(
    archive_path: &Path,
    destination: &Path,
) -> Result<ManagedPayloadArchiveReceiptV1, ManagedPayloadArchiveError> {
    if !archive_path.is_absolute() || archive_path.is_symlink() || !archive_path.is_file() {
        return Err(ManagedPayloadArchiveError::InvalidInput(
            "archive must be an absolute regular non-symlink file".to_owned(),
        ));
    }
    if !destination.is_absolute() || destination.exists() || destination.is_symlink() {
        return Err(ManagedPayloadArchiveError::InvalidInput(
            "destination must be a fresh absolute directory".to_owned(),
        ));
    }
    prepare_private_dir(destination)?;

    let file = File::open(archive_path)?;
    let decoder = zstd::Decoder::new(file)?;
    let mut archive = Archive::new(decoder);
    let mut files = 0usize;
    let mut expanded_bytes = 0u64;
    let mut saw_manifest = false;
    let mut buffer = vec![0u8; COPY_BUFFER_BYTES];

    for item in archive.entries()? {
        let mut entry = item?;
        let entry_type = entry.header().entry_type();
        if !entry_type.is_file() {
            return cleanup_error(
                destination,
                ManagedPayloadArchiveError::UnsafeEntry(format!(
                    "non-regular entry type {:?}",
                    entry_type
                )),
            );
        }
        let relative = entry.path()?.into_owned();
        if let Err(error) = validate_relative(&relative) {
            return cleanup_error(destination, error);
        }
        files += 1;
        if files > MAX_FILES {
            return cleanup_error(destination, ManagedPayloadArchiveError::ExpansionLimit);
        }
        let declared_size = entry.size();
        expanded_bytes = match expanded_bytes.checked_add(declared_size) {
            Some(value) if value <= MAX_EXPANDED_BYTES => value,
            _ => return cleanup_error(destination, ManagedPayloadArchiveError::ExpansionLimit),
        };
        if relative == Path::new(REQUIRED_MANIFEST) {
            saw_manifest = true;
        }
        let output = destination.join(&relative);
        if let Some(parent) = output.parent() {
            if let Err(error) = prepare_private_dir(parent) {
                return cleanup_error(destination, error);
            }
        }
        if output.exists() || output.is_symlink() {
            return cleanup_error(
                destination,
                ManagedPayloadArchiveError::UnsafeEntry(format!(
                    "duplicate output path: {}",
                    relative.display()
                )),
            );
        }
        let mut target = match create_private_file(&output) {
            Ok(file) => file,
            Err(error) => return cleanup_error(destination, error),
        };
        let mut remaining = declared_size;
        while remaining > 0 {
            let maximum = usize::try_from(remaining.min(COPY_BUFFER_BYTES as u64))
                .map_err(|_| ManagedPayloadArchiveError::ExpansionLimit)?;
            let read = entry.read(&mut buffer[..maximum])?;
            if read == 0 {
                return cleanup_error(
                    destination,
                    ManagedPayloadArchiveError::UnsafeEntry(format!(
                        "truncated entry: {}",
                        relative.display()
                    )),
                );
            }
            target.write_all(&buffer[..read])?;
            remaining -= read as u64;
        }
        let mut extra = [0u8; 1];
        if entry.read(&mut extra)? != 0 {
            return cleanup_error(
                destination,
                ManagedPayloadArchiveError::UnsafeEntry(format!(
                    "entry exceeded declared size: {}",
                    relative.display()
                )),
            );
        }
        target.sync_all()?;
    }
    if !saw_manifest {
        return cleanup_error(destination, ManagedPayloadArchiveError::MissingManifest);
    }
    Ok(ManagedPayloadArchiveReceiptV1 {
        files,
        expanded_bytes,
        output_root: destination.to_path_buf(),
    })
}

fn canonical_payload_root(path: &Path) -> Result<PathBuf, ManagedPayloadArchiveError> {
    if !path.is_absolute() || path.is_symlink() || !path.is_dir() {
        return Err(ManagedPayloadArchiveError::InvalidInput(
            "payload root must be an absolute regular directory".to_owned(),
        ));
    }
    Ok(path.canonicalize()?)
}

fn enumerate_regular_files(root: &Path) -> Result<Vec<PathBuf>, ManagedPayloadArchiveError> {
    let mut output = Vec::new();
    recurse(root, root, &mut output)?;
    output.sort();
    Ok(output)
}

fn recurse(
    root: &Path,
    current: &Path,
    output: &mut Vec<PathBuf>,
) -> Result<(), ManagedPayloadArchiveError> {
    let mut entries = fs::read_dir(current)?.collect::<Result<Vec<_>, _>>()?;
    entries.sort_by_key(|entry| entry.file_name());
    for entry in entries {
        let path = entry.path();
        let file_type = entry.file_type()?;
        if file_type.is_symlink() {
            return Err(ManagedPayloadArchiveError::UnsafeEntry(format!(
                "symlink in payload: {}",
                path.display()
            )));
        }
        if file_type.is_dir() {
            recurse(root, &path, output)?;
        } else if file_type.is_file() {
            let relative = path.strip_prefix(root).map_err(|_| {
                ManagedPayloadArchiveError::UnsafeEntry(path.display().to_string())
            })?;
            validate_relative(relative)?;
            output.push(relative.to_path_buf());
            if output.len() > MAX_FILES {
                return Err(ManagedPayloadArchiveError::ExpansionLimit);
            }
        } else {
            return Err(ManagedPayloadArchiveError::UnsafeEntry(format!(
                "special file in payload: {}",
                path.display()
            )));
        }
    }
    Ok(())
}

fn validate_relative(path: &Path) -> Result<(), ManagedPayloadArchiveError> {
    if path.as_os_str().is_empty() || path.is_absolute() {
        return Err(ManagedPayloadArchiveError::UnsafeEntry(path.display().to_string()));
    }
    for component in path.components() {
        if !matches!(component, Component::Normal(_)) {
            return Err(ManagedPayloadArchiveError::UnsafeEntry(path.display().to_string()));
        }
    }
    Ok(())
}

fn prepare_private_dir(path: &Path) -> Result<(), ManagedPayloadArchiveError> {
    if path.is_symlink() {
        return Err(ManagedPayloadArchiveError::UnsafeEntry(path.display().to_string()));
    }
    if path.exists() {
        if !path.is_dir() {
            return Err(ManagedPayloadArchiveError::InvalidInput(format!(
                "directory path is not a directory: {}",
                path.display()
            )));
        }
    } else {
        fs::create_dir_all(path)?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn create_private_file(path: &Path) -> Result<File, ManagedPayloadArchiveError> {
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o400);
    }
    Ok(options.open(path)?)
}

fn cleanup_error<T>(
    destination: &Path,
    error: ManagedPayloadArchiveError,
) -> Result<T, ManagedPayloadArchiveError> {
    let _ = fs::remove_dir_all(destination);
    Err(error)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(1);

    #[test]
    fn deterministic_pack_round_trips_regular_payload() {
        let root = temp_root();
        let payload = root.join("payload");
        fs::create_dir_all(payload.join("nested")).unwrap();
        fs::write(payload.join(REQUIRED_MANIFEST), b"{}\n").unwrap();
        fs::write(payload.join("nested/data.bin"), b"hello").unwrap();
        let first = root.join("first.tar.zst");
        let second = root.join("second.tar.zst");
        pack_managed_payload(&payload, &first).unwrap();
        pack_managed_payload(&payload, &second).unwrap();
        assert_eq!(fs::read(&first).unwrap(), fs::read(&second).unwrap());

        let unpacked = root.join("unpacked");
        let receipt = unpack_managed_payload(&first, &unpacked).unwrap();
        assert_eq!(receipt.files, 2);
        assert_eq!(fs::read(unpacked.join("nested/data.bin")).unwrap(), b"hello");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn pack_round_trips_long_linux_wheel_filename() {
        let root = temp_root();
        let payload = root.join("payload");
        let wheelhouse = payload.join("wheelhouse");
        fs::create_dir_all(&wheelhouse).unwrap();
        fs::write(payload.join(REQUIRED_MANIFEST), b"{}\n").unwrap();
        let wheel = "charset_normalizer-3.4.4-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl";
        fs::write(wheelhouse.join(wheel), b"wheel").unwrap();

        let archive = root.join("payload.tar.zst");
        pack_managed_payload(&payload, &archive).unwrap();
        let unpacked = root.join("unpacked");
        unpack_managed_payload(&archive, &unpacked).unwrap();

        assert_eq!(
            fs::read(unpacked.join("wheelhouse").join(wheel)).unwrap(),
            b"wheel"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn unpack_rejects_symlink_entry_and_cleans_destination() {
        let root = temp_root();
        let archive_path = root.join("malicious.tar.zst");
        let file = create_private_file(&archive_path).unwrap();
        let encoder = zstd::Encoder::new(file, 3).unwrap();
        let mut builder = Builder::new(encoder);
        let mut manifest = Header::new_gnu();
        manifest.set_entry_type(EntryType::Regular);
        manifest.set_size(3);
        manifest.set_mode(0o444);
        manifest.set_path(REQUIRED_MANIFEST).unwrap();
        manifest.set_cksum();
        builder.append(&manifest, Cursor::new(b"{}\n")).unwrap();
        let mut link = Header::new_gnu();
        link.set_entry_type(EntryType::Symlink);
        link.set_size(0);
        link.set_mode(0o777);
        link.set_path("escape").unwrap();
        link.set_link_name("../outside").unwrap();
        link.set_cksum();
        builder.append(&link, Cursor::new(Vec::<u8>::new())).unwrap();
        let encoder = builder.into_inner().unwrap();
        encoder.finish().unwrap().sync_all().unwrap();
        let destination = root.join("unpacked");
        assert!(unpack_managed_payload(&archive_path, &destination).is_err());
        assert!(!destination.exists());
        let _ = fs::remove_dir_all(root);
    }

    fn temp_root() -> PathBuf {
        let id = COUNTER.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "hermes-managed-payload-archive-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        root
    }
}
