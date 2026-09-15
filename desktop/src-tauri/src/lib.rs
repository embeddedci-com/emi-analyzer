//! The EMI Analyzer desktop app.
//!
//! This is deliberately thin. Everything the analyzer does lives in `emi-local` (server/cmd/
//! emi-local), a Go binary bundled here as a Tauri sidecar: the API, the database, the board
//! files, the webapp and the Docker worker supervisor. This shell only
//!
//!   1. starts the sidecar with the app's data folder,
//!   2. shows a loading page until the sidecar prints `EMI_LOCAL_READY <url>`,
//!   3. points the window at that URL, and
//!   4. asks the sidecar to stop when the app quits -- it then removes the worker container.
//!
//! The sidecar also exits by itself if this process dies without asking (`-lifeline`: it
//! watches its stdin), so a crash here cannot leave a server or a container behind.
//!
//! The page the window shows is served over http://127.0.0.1 and is given no Tauri APIs.
//! Links that leave 127.0.0.1 open in the system browser.

use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use tauri::webview::NewWindowResponse;
use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

const READY_PREFIX: &str = "EMI_LOCAL_READY ";
const LOG_LINES: usize = 40;

#[derive(Default)]
struct Sidecar {
    child: Mutex<Option<CommandChild>>,
    /// Set once the sidecar has exited; paired with a condvar so quitting can wait for it.
    exited: Arc<(Mutex<bool>, Condvar)>,
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Sidecar::default())
        .setup(|app| {
            let window = WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("EMI Analyzer")
                .inner_size(1400.0, 900.0)
                .min_inner_size(800.0, 600.0)
                .on_navigation(|url| {
                    if is_local(url) {
                        return true;
                    }
                    let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
                    false
                })
                .on_new_window(|url, _features| {
                    // target="_blank" links: never a second webview, always the system browser.
                    let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
                    NewWindowResponse::Deny
                })
                .build()?;

            if let Err(err) = start_sidecar(app.handle(), window.clone()) {
                show_failure(&window, &format!("The local server could not be started: {err}"), "");
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the EMI Analyzer app");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            stop_sidecar(handle);
        }
    });
}

fn is_local(url: &url::Url) -> bool {
    match url.scheme() {
        "tauri" | "asset" | "about" | "data" | "blob" => true,
        "http" | "https" => matches!(
            url.host_str(),
            Some("127.0.0.1") | Some("localhost") | Some("tauri.localhost") | Some("[::1]")
        ),
        _ => false,
    }
}

fn start_sidecar(app: &tauri::AppHandle, window: tauri::WebviewWindow) -> Result<(), Box<dyn std::error::Error>> {
    let data_dir = app.path().app_data_dir()?;
    std::fs::create_dir_all(&data_dir)?;

    let command = app.shell().sidecar("emi-local")?.args([
        "-open=false".to_string(),
        "-lifeline".to_string(),
        "-data-dir".to_string(),
        data_dir.to_string_lossy().into_owned(),
    ]);
    let (mut rx, child) = command.spawn()?;

    let state = app.state::<Sidecar>();
    *state.child.lock().unwrap() = Some(child);
    let exited = state.exited.clone();

    tauri::async_runtime::spawn(async move {
        let mut ready = false;
        let mut recent: VecDeque<String> = VecDeque::with_capacity(LOG_LINES);
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    let line = String::from_utf8_lossy(&line).trim().to_string();
                    if let Some(u) = line.strip_prefix(READY_PREFIX) {
                        match url::Url::parse(u.trim()) {
                            Ok(u) if !ready => {
                                ready = true;
                                // Straight to the analyzer, not via the root redirect, so the
                                // back button has nothing to go back to.
                                let target = u.join("/tools/emi").unwrap_or(u);
                                let _ = window.navigate(target);
                            }
                            _ => {}
                        }
                    }
                    remember(&mut recent, line);
                }
                CommandEvent::Stderr(line) => {
                    let line = String::from_utf8_lossy(&line).trim_end().to_string();
                    eprintln!("emi-local: {line}");
                    remember(&mut recent, line);
                }
                CommandEvent::Terminated(payload) => {
                    let (lock, cvar) = &*exited;
                    *lock.lock().unwrap() = true;
                    cvar.notify_all();
                    let log = recent.iter().cloned().collect::<Vec<_>>().join("\n");
                    let msg = format!(
                        "The local server stopped (exit code {}).",
                        payload.code.map(|c| c.to_string()).unwrap_or_else(|| "unknown".into())
                    );
                    if ready {
                        // The page is the analyzer, which has no failure hook; go back to the
                        // loading page and report there.
                        let _ = window.navigate("tauri://localhost/index.html".parse().unwrap());
                        std::thread::sleep(Duration::from_millis(500));
                    }
                    show_failure(&window, &msg, &log);
                    break;
                }
                _ => {}
            }
        }
    });
    Ok(())
}

fn remember(recent: &mut VecDeque<String>, line: String) {
    if line.is_empty() {
        return;
    }
    if recent.len() == LOG_LINES {
        recent.pop_front();
    }
    recent.push_back(line);
}

fn show_failure(window: &tauri::WebviewWindow, message: &str, log: &str) {
    let js = format!(
        "window.emiFailed && window.emiFailed({}, {})",
        js_string(message),
        js_string(log)
    );
    let _ = window.eval(js);
}

fn js_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => {}
            '<' => out.push_str("\\u003c"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Ask the sidecar to shut down -- it removes the worker container on the way -- and give it
/// a few seconds before killing it.
fn stop_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<Sidecar>();
    let Some(mut child) = state.child.lock().unwrap().take() else {
        return;
    };
    let _ = child.write(b"quit\n");

    let (lock, cvar) = &*state.exited;
    let guard = lock.lock().unwrap();
    let (guard, timeout) = cvar
        .wait_timeout_while(guard, Duration::from_secs(20), |exited| !*exited)
        .unwrap();
    drop(guard);
    if timeout.timed_out() {
        let _ = child.kill();
    }
}
