use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// CREATE_NO_WINDOW：GUI 进程拉起控制台程序时不弹黑框。
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn hidden_command(program: &str) -> Command {
    let mut command = Command::new(program);
    command.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    command.creation_flags(CREATE_NO_WINDOW);
    command
}

fn user_data_dir(root: &Path) -> PathBuf {
    for key in ["PRISM_DATA_DIR", "SENTINEL_DATA_DIR"] {
        if let Ok(value) = std::env::var(key) {
            if !value.trim().is_empty() {
                return PathBuf::from(value);
            }
        }
    }
    if let Ok(value) = std::env::var("APPDATA") {
        if !value.trim().is_empty() {
            return PathBuf::from(value).join("Prism");
        }
    }
    if let Ok(value) = std::env::var("USERPROFILE") {
        if !value.trim().is_empty() {
            return PathBuf::from(value).join(".prism");
        }
    }
    root.join(".claude")
}

/// 改名前的数据目录，仅用于首次迁移复制（不删除旧目录）。
fn legacy_user_data_dir(root: &Path) -> PathBuf {
    if let Ok(value) = std::env::var("APPDATA") {
        if !value.trim().is_empty() {
            return PathBuf::from(value).join("AgentSentinel");
        }
    }
    if let Ok(value) = std::env::var("USERPROFILE") {
        if !value.trim().is_empty() {
            return PathBuf::from(value).join(".agentsentinel");
        }
    }
    root.join(".claude")
}

fn ensure_workspace_db(root: &Path, data_dir: &Path) -> PathBuf {
    let db = data_dir.join("workspaces.sqlite3");
    if !db.exists() {
        let legacy = legacy_user_data_dir(root).join("workspaces.sqlite3");
        if legacy != db && legacy.exists() {
            if let Some(parent) = db.parent() {
                let _ = std::fs::create_dir_all(parent);
            }
            let _ = std::fs::copy(&legacy, &db);
        }
    }
    db
}

fn start_daemon_blocking(
    root: String,
    home: Option<String>,
    project: Option<String>,
    workspace_id: Option<i64>,
) -> Result<(), String> {
    let root_path = PathBuf::from(root);
    let control_home = root_path.join(".claude").join("orchestrator");
    let home_path = home.map(PathBuf::from).unwrap_or_else(|| control_home.clone());
    let data_dir = user_data_dir(&root_path);
    let workspace_db = ensure_workspace_db(&root_path, &data_dir);
    let mut command = hidden_command("uv");
    command
        .args([
            "run",
            "python",
            "-m",
            "tools.orchestrator",
            "daemon",
            "start",
            "--port",
            "0",
        ])
        .current_dir(&root_path)
        .env("PRISM_ORCH_HOME", &home_path)
        .env("PRISM_CONTROL_HOME", &control_home)
        .env("PRISM_WORKSPACES_DB", &workspace_db)
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8");
    if let Some(project) = project {
        command.env("PRISM_PROJECT_ROOT", project);
    }
    if let Some(workspace_id) = workspace_id {
        command.env("PRISM_WORKSPACE_ID", workspace_id.to_string());
    }
    let status = command
        .status()
        .map_err(|error| format!("无法启动 uv：{error}"))?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("daemon start 退出码 {status}"))
    }
}

/// 静默拉起 daemon；可为指定 Workspace 使用独立 home/project。
#[tauri::command]
async fn start_daemon(
    root: String,
    home: Option<String>,
    project: Option<String>,
    workspace_id: Option<i64>,
) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        start_daemon_blocking(root, home, project, workspace_id)
    })
    .await
    .map_err(|error| error.to_string())?
}

/// daemon 未在超时内退出时兜底强杀整棵进程树（含 PTY / claude）。
#[tauri::command]
async fn force_kill_pid(pid: u32) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || {
        if pid == 0 {
            return Ok(());
        }
        let status = hidden_command("taskkill")
            .args(["/PID", &pid.to_string(), "/T", "/F"])
            .status()
            .map_err(|error| format!("taskkill 启动失败：{error}"))?;
        if status.success() {
            Ok(())
        } else {
            Err(format!("taskkill 退出码 {status}"))
        }
    })
    .await
    .map_err(|error| error.to_string())?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_clipboard_manager::init())
        .invoke_handler(tauri::generate_handler![start_daemon, force_kill_pid])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
