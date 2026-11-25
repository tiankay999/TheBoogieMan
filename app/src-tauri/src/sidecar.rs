//! Spawns and supervises the Python brain process.

use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};

/// Where the brain lives. Override with BOOGEYMAN_BRAIN_DIR; defaults to <repo>/brain in dev.
fn brain_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("BOOGEYMAN_BRAIN_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../brain")
}

/// Prefer the brain's own virtualenv, fall back to the system interpreter.
/// Virtualenvs differ by platform: POSIX puts it in bin/python, Windows in Scripts/python.exe.
fn python_bin(dir: &Path) -> PathBuf {
    if let Ok(py) = std::env::var("BOOGEYMAN_PYTHON") {
        return PathBuf::from(py);
    }
    let venv = if cfg!(windows) {
        dir.join(".venv/Scripts/python.exe")
    } else {
        dir.join(".venv/bin/python")
    };
    if venv.exists() {
        venv
    } else if cfg!(windows) {
        PathBuf::from("python")
    } else {
        PathBuf::from("python3")
    }
}

/// Ask the OS for a free localhost port.
pub fn free_port() -> std::io::Result<u16> {
    Ok(TcpListener::bind("127.0.0.1:0")?.local_addr()?.port())
}

pub fn spawn(port: u16) -> std::io::Result<Child> {
    let dir = brain_dir();
    let python = python_bin(&dir);
    log::info!("starting brain: {} main.py (cwd {}, port {port})", python.display(), dir.display());

    // tokio::process needs a runtime context; setup() runs on the main thread, outside it.
    let _rt = tauri::async_runtime::handle().inner().enter();
    let mut cmd = Command::new(&python);
    // If the app dies for any reason (crash, kill -9), the kernel sends the brain SIGTERM,
    // so we never leave an orphaned Python process behind.
    #[cfg(target_os = "linux")]
    unsafe {
        cmd.pre_exec(|| {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = cmd
        .arg("main.py")
        .current_dir(&dir)
        .env("BOOGEYMAN_BRAIN_PORT", port.to_string())
        .env("PYTHONUNBUFFERED", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;

    // Forward the brain's output into our log so `tauri dev` shows everything in one place.
    if let Some(out) = child.stdout.take() {
        tauri::async_runtime::spawn(async move {
            let mut lines = BufReader::new(out).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                log::info!("[brain] {line}");
            }
        });
    }
    if let Some(err) = child.stderr.take() {
        tauri::async_runtime::spawn(async move {
            let mut lines = BufReader::new(err).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                log::info!("[brain] {line}");
            }
        });
    }
    Ok(child)
}
