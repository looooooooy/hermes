use std::fs;
use std::io;
use std::path::Path;

fn main() {
    #[cfg(windows)]
    generate_png_compressed_ico().expect("failed to generate Windows Hermes icon.ico");
    tauri_build::build()
}

#[cfg(windows)]
fn generate_png_compressed_ico() -> io::Result<()> {
    const PNG_SIGNATURE: &[u8; 8] = b"\x89PNG\r\n\x1a\n";
    const IHDR_OFFSET: usize = 8 + 4 + 4;

    let png_path = Path::new("icons/icon.png");
    let ico_path = Path::new("icons/icon.ico");
    let png = fs::read(png_path)?;
    if png.len() < IHDR_OFFSET + 13 || png.get(0..8) != Some(PNG_SIGNATURE) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "icons/icon.png is not a valid PNG",
        ));
    }

    let width = u32::from_be_bytes(png[IHDR_OFFSET..IHDR_OFFSET + 4].try_into().unwrap());
    let height = u32::from_be_bytes(
        png[IHDR_OFFSET + 4..IHDR_OFFSET + 8]
            .try_into()
            .unwrap(),
    );
    if width == 0 || height == 0 || width > 256 || height > 256 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("unsupported Windows icon PNG dimensions: {width}x{height}"),
        ));
    }

    // ICO header + one directory entry. Windows Vista+ resource tools accept a PNG
    // payload directly inside an ICO; this avoids the legacy BMP/DIB encoding that
    // triggered RC2176 on Windows Server 2025 / VS 2026 runners.
    let mut ico = Vec::with_capacity(6 + 16 + png.len());
    ico.extend_from_slice(&0u16.to_le_bytes()); // reserved
    ico.extend_from_slice(&1u16.to_le_bytes()); // icon type
    ico.extend_from_slice(&1u16.to_le_bytes()); // one image
    ico.push(if width == 256 { 0 } else { width as u8 });
    ico.push(if height == 256 { 0 } else { height as u8 });
    ico.push(0); // palette colors
    ico.push(0); // reserved
    ico.extend_from_slice(&1u16.to_le_bytes()); // planes
    ico.extend_from_slice(&32u16.to_le_bytes()); // bit depth
    ico.extend_from_slice(&(png.len() as u32).to_le_bytes());
    ico.extend_from_slice(&22u32.to_le_bytes()); // payload offset
    ico.extend_from_slice(&png);

    fs::write(ico_path, ico)?;
    println!("cargo:rerun-if-changed={}", png_path.display());
    Ok(())
}
