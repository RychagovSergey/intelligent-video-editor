//! Десктопная оболочка (этап 8, Р-4): окно с интерфейсом плюс backend как дочерний процесс.
//!
//! Backend не упаковывается — запускается из `.venv` репозитория (решение заказчика:
//! PyInstaller с librosa/numba не стоит своей хрупкости, пока пользователь один).
//! Оболочка добавляет то, чего нет у браузерного режима: один запуск вместо двух
//! терминалов, вшитые статические ffmpeg/ffprobe/ffplay (Р-5), нативный выбор папок
//! и остановку backend вместе с окном.

use std::net::{SocketAddr, TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

/// Сколько ждать, пока uvicorn начнёт слушать порт: импорт librosa на холодном
/// старте занимает несколько секунд, первый запуск после установки — дольше.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(90);

struct Backend(Mutex<Option<Child>>);

/// Корень репозитория: там лежат `.venv`, `.env` и `backend/`.
///
/// В release-сборке путь вшивается на этапе компиляции — приложение собирается на той
/// же машине, где живёт репозиторий. `IVE_PROJECT_ROOT` позволяет переопределить.
fn project_root() -> PathBuf {
    if let Some(root) = std::env::var_os("IVE_PROJECT_ROOT") {
        return PathBuf::from(root);
    }
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
}

/// Вшитый сайдкар: Tauri кладёт externalBin рядом с исполняемым файлом под коротким именем.
fn bundled_binary(name: &str) -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let path = exe.parent()?.join(name);
    path.is_file().then_some(path)
}

fn backend_port() -> u16 {
    // В dev-режиме Vite проксирует /api на BACKEND_PORT (по умолчанию 8001) — держим тот же
    // порт, чтобы браузерный режим и окно Tauri ходили в один backend.
    if let Some(port) = std::env::var("BACKEND_PORT").ok().and_then(|p| p.parse().ok()) {
        return port;
    }
    if cfg!(debug_assertions) {
        return 8001;
    }
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8001)
}

fn port_open(port: u16) -> bool {
    let addr: SocketAddr = ([127, 0, 0, 1], port).into();
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

fn spawn_backend(root: &Path, port: u16) -> Result<Child, String> {
    let python = root.join(".venv/bin/python");
    if !python.is_file() {
        return Err(format!(
            "Не найден интерпретатор {}.\nСоздайте окружение: python3.12 -m venv .venv && \
             .venv/bin/pip install -r backend/requirements.txt",
            python.display()
        ));
    }
    let mut cmd = Command::new(python);
    cmd.args(["-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port"])
        .arg(port.to_string())
        .current_dir(root);
    // Вшитые бинарники сильнее .env: переменная окружения процесса имеет приоритет над
    // файлом в pydantic-settings, и панель настроек это честно показывает.
    for name in ["ffmpeg", "ffprobe", "ffplay"] {
        if let Some(path) = bundled_binary(name) {
            cmd.env(format!("{}_PATH", name.to_uppercase()), path);
        }
    }
    cmd.spawn().map_err(|e| format!("Не удалось запустить backend: {e}"))
}

fn wait_for_backend(child: &mut Child, port: u16) -> Result<(), String> {
    let started = Instant::now();
    while started.elapsed() < STARTUP_TIMEOUT {
        if port_open(port) {
            return Ok(());
        }
        if let Ok(Some(status)) = child.try_wait() {
            return Err(format!(
                "Backend завершился при старте ({status}). Запустите его вручную из терминала, \
                 чтобы увидеть причину:\n.venv/bin/uvicorn backend.app.main:app --port {port}"
            ));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    Err(format!("Backend не ответил на порту {port} за {} с", STARTUP_TIMEOUT.as_secs()))
}

fn open_main_window(app: &tauri::AppHandle, port: u16) -> tauri::Result<()> {
    // Интерфейс узнаёт адрес backend до загрузки своих скриптов — в собранном приложении
    // страница живёт на tauri://localhost, и относительный /api ей недоступен.
    let script = format!("window.__IVE_BACKEND__ = 'http://127.0.0.1:{port}';");
    WebviewWindowBuilder::new(app, "main", WebviewUrl::default())
        .title("Intelligent Video Editor")
        .inner_size(1440.0, 900.0)
        .min_inner_size(1024.0, 640.0)
        .initialization_script(&script)
        .build()?;
    Ok(())
}

fn fatal(app: &tauri::AppHandle, message: String) {
    app.dialog()
        .message(message)
        .kind(MessageDialogKind::Error)
        .title("Intelligent Video Editor")
        .blocking_show();
    app.exit(1);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let root = project_root();
            let port = backend_port();

            // Ожидание backend — в отдельном потоке, чтобы не блокировать цикл событий.
            std::thread::spawn(move || {
                // Backend, оставшийся от прошлого сеанса или запущенный руками, переиспользуем.
                let result = if port_open(port) {
                    Ok(None)
                } else {
                    spawn_backend(&root, port).and_then(|mut child| {
                        wait_for_backend(&mut child, port).map(|_| Some(child))
                    })
                };
                match result {
                    Ok(child) => {
                        *handle.state::<Backend>().0.lock().unwrap() = child;
                        let h = handle.clone();
                        let _ = handle.run_on_main_thread(move || {
                            if let Err(e) = open_main_window(&h, port) {
                                fatal(&h, format!("Не удалось открыть окно: {e}"));
                            }
                        });
                    }
                    Err(message) => {
                        let h = handle.clone();
                        let _ = handle.run_on_main_thread(move || fatal(&h, message));
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            // Окно закрыли — backend не должен остаться висеть на порту.
            if let Some(mut child) = handle.state::<Backend>().0.lock().unwrap().take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    });
}
