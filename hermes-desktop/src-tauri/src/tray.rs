use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, Runtime,
};

pub const HERMES_TRAY_ID: &str = "hermes-runtime";
pub const MENU_OPEN: &str = "open-hermes";
pub const MENU_HIDE: &str = "hide-hermes";
pub const MENU_QUIT: &str = "quit-hermes";

pub fn create_tray<R: Runtime>(app: &AppHandle<R>) -> tauri::Result<()> {
    let heading = MenuItem::with_id(
        app,
        "hermes-status",
        "Hermes · Managed Runtime",
        false,
        None::<&str>,
    )?;
    let open = MenuItem::with_id(app, MENU_OPEN, "Open Hermes", true, None::<&str>)?;
    let hide = MenuItem::with_id(app, MENU_HIDE, "Hide window", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, MENU_QUIT, "Quit Hermes", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&heading, &open, &hide, &quit])?;

    let _tray = TrayIconBuilder::with_id(HERMES_TRAY_ID)
        .tooltip("Hermes Managed Runtime")
        .icon(
            app.default_window_icon()
                .expect("Hermes Desktop must ship a default application icon")
                .clone(),
        )
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| match event.id.as_ref() {
            MENU_OPEN => show_main_window(app),
            MENU_HIDE => hide_main_window(app),
            MENU_QUIT => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    Ok(())
}

pub fn show_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

pub fn hide_main_window<R: Runtime>(app: &AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.hide();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tray_contract_ids_are_stable_and_distinct() {
        let ids = [HERMES_TRAY_ID, MENU_OPEN, MENU_HIDE, MENU_QUIT];
        for id in ids {
            assert!(id.is_ascii());
            assert!(!id.contains(' '));
        }
        assert_ne!(MENU_OPEN, MENU_HIDE);
        assert_ne!(MENU_OPEN, MENU_QUIT);
        assert_ne!(MENU_HIDE, MENU_QUIT);
    }
}
