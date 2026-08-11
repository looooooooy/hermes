use crate::model::PlatformKind;
use crate::ports::PortError;
use std::path::{Path, PathBuf};

pub(crate) fn safe_release_id(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 160
        && value != "."
        && value != ".."
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'+')
        })
}

pub(crate) fn validated_release_directory(
    releases_root: &Path,
    release_id: &str,
) -> Result<PathBuf, PortError> {
    if !safe_release_id(release_id) {
        return Err(PortError::Operation("release identity is invalid".to_owned()));
    }
    let path = releases_root.join(release_id);
    if path.is_symlink() || !path.is_dir() {
        return Err(PortError::Operation(
            "immutable release directory is missing or symlinked".to_owned(),
        ));
    }
    let resolved = path.canonicalize()?;
    let root = releases_root.canonicalize()?;
    if resolved.parent() != Some(root.as_path()) || resolved.file_name() != Some(release_id.as_ref()) {
        return Err(PortError::Operation(
            "immutable release escaped the release root".to_owned(),
        ));
    }
    Ok(resolved)
}

pub(crate) fn validated_console_script(
    releases_root: &Path,
    release_id: &str,
    platform: PlatformKind,
    runtime: &str,
    name: &str,
) -> Result<PathBuf, PortError> {
    let release_dir = validated_release_directory(releases_root, release_id)?;
    let path = match platform {
        PlatformKind::Windows => release_dir
            .join(runtime)
            .join("venv")
            .join("Scripts")
            .join(format!("{name}.exe")),
        PlatformKind::Macos | PlatformKind::Linux => {
            release_dir.join(runtime).join("venv").join("bin").join(name)
        }
    };
    if path.is_symlink() || !path.is_file() {
        return Err(PortError::Operation(
            "release console entrypoint is missing or symlinked".to_owned(),
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if path.metadata()?.permissions().mode() & 0o111 == 0 {
            return Err(PortError::Operation(
                "release console entrypoint is not executable".to_owned(),
            ));
        }
    }
    Ok(path)
}
