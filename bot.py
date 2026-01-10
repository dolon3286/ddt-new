from __future__ import annotations

import asyncio
import json
import os
import pickle
import shlex
import shutil
import subprocess
import threading
import time
import uuid
import html  # Added for safe HTML formatting
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# --- Configuration ---
BOT_TOKEN = ""  # Enter token here or use env var BOT_TOKEN
SETTING_ACTION = 1


@dataclass
class DownloadResult:
    uploaded_files: List[str]
    skipped_files: List[str]


@dataclass
class TaskStatus:
    stage: str
    processed_bytes: int
    total_bytes: int
    speed_bps: float
    current_name: str
    start_time: float
    lock: threading.Lock

    def update_progress(
        self,
        processed_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None,
        speed_bps: Optional[float] = None,
        current_name: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> None:
        with self.lock:
            if processed_bytes is not None:
                self.processed_bytes = processed_bytes
            if total_bytes is not None:
                self.total_bytes = total_bytes
            if speed_bps is not None:
                self.speed_bps = speed_bps
            if current_name is not None:
                self.current_name = current_name
            if stage is not None:
                self.stage = stage

    def snapshot(self) -> "TaskStatusSnapshot":
        with self.lock:
            return TaskStatusSnapshot(
                stage=self.stage,
                processed_bytes=self.processed_bytes,
                total_bytes=self.total_bytes,
                speed_bps=self.speed_bps,
                current_name=self.current_name,
                start_time=self.start_time,
            )


@dataclass(frozen=True)
class TaskStatusSnapshot:
    stage: str
    processed_bytes: int
    total_bytes: int
    speed_bps: float
    current_name: str
    start_time: float


@dataclass
class TaskControl:
    task_id: str
    cancel_event: threading.Event


# --- Settings & File System Helpers ---

def _settings_path() -> Path:
    return Path(os.getenv("BOT_SETTINGS_PATH", "bot_settings.json"))


def _default_settings() -> Dict[str, Any]:
    return {
        "admin_ids": [],
        "allowed_users": [],
        "drive_folder_id": "",
        "max_tasks": 2,
        "download_command": os.getenv("BOT_DOWNLOAD_CMD", "go run main.go"),
        "download_dirs": [],
        "auto_select_all": True,
        "auto_skip_mv": True,
    }


def load_settings() -> Dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        settings = _default_settings()
        save_settings(settings)
        return settings
    with path.open("r", encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except json.JSONDecodeError:
            data = {}
    defaults = _default_settings()
    defaults.update(data)
    return defaults


def save_settings(settings: Dict[str, Any]) -> None:
    path = _settings_path()
    with path.open("w", encoding="utf-8") as handle:
        json.dump(settings, handle, indent=2, sort_keys=True)


def is_admin(user_id: int, settings: Dict[str, Any]) -> bool:
    return user_id in settings.get("admin_ids", [])


def is_allowed(user_id: int, settings: Dict[str, Any]) -> bool:
    allowed = settings.get("allowed_users", [])
    if not allowed:
        return is_admin(user_id, settings)
    return user_id in allowed or is_admin(user_id, settings)


def load_download_dirs(settings: Dict[str, Any]) -> List[str]:
    if settings.get("download_dirs"):
        return settings["download_dirs"]
    config_path = Path("config.yaml")
    if not config_path.exists():
        return []
    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    keys = ["alac-save-folder", "atmos-save-folder", "aac-save-folder"]
    dirs = [data.get(key) for key in keys if data.get(key)]
    return list({str(Path(d)) for d in dirs})


def load_drive_service(token_path: Path):
    if not token_path.exists():
        raise FileNotFoundError(f"Token file not found at {token_path}")
    
    with token_path.open("rb") as handle:
        creds = pickle.load(handle)
    
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with token_path.open("wb") as handle:
            pickle.dump(creds, handle)
            
    return build("drive", "v3", credentials=creds)


def find_recent_files(dirs: Iterable[str], start_time: float) -> List[Path]:
    found: List[Path] = []
    for root in dirs:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for path in root_path.rglob("*"):
            if path.is_file() and path.stat().st_mtime >= start_time:
                found.append(path)
    return found


def sum_recent_files(dirs: Iterable[str], start_time: float) -> Tuple[int, List[Path]]:
    files = find_recent_files(dirs, start_time)
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total, files


def format_bytes(num: int) -> str:
    step = 1024.0
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < step:
            return f"{num:.2f}{unit}"
        num /= step
    return f"{num:.2f}PB"


def build_progress_bar(percent: float, length: int = 12) -> str:
    """Standard progress bar [■■■■□□□□]"""
    percent = max(0.0, min(100.0, percent))
    filled = int(round((percent / 100.0) * length))
    return "■" * filled + "□" * (length - filled)


def render_status(name: str, snapshot: TaskStatusSnapshot, task_id: str) -> str:
    # 1. DOWNLOAD PHASE
    if snapshot.stage.lower().startswith("download"):
        # Indeterminate Pulse Bar for Download (since total size is unknown)
        bar_len = 12
        tick = int(time.time() * 2) % bar_len
        # Creates a moving block: [□□□■□□□□]
        bar_chars = ["□"] * bar_len
        bar_chars[tick] = "■"
        bar = "".join(bar_chars)
        
        safe_name = html.escape(name) # Escape URL for HTML safety
        
        return (
            f"Downloading: <b>{safe_name}</b>\n"
            f"[{bar}]\n"
            f"Stop: /c{task_id}"
        )

    # 2. UPLOAD PHASE
    elif snapshot.stage.lower().startswith("upload"):
        # Real Percentage Bar for Upload
        percent = 0.0
        if snapshot.total_bytes > 0:
            percent = (snapshot.processed_bytes / snapshot.total_bytes) * 100.0
            
        bar = build_progress_bar(percent, length=12)
        total_size = format_bytes(snapshot.total_bytes)
        current_file = html.escape(snapshot.current_name or name)

        return (
            f"Uploading: <b>{current_file}</b>\n"
            f"[{bar}]\n"
            f"Total Size: {total_size}\n"
            f"Stop: /c{task_id}"
        )

    # 3. OTHER PHASES (Starting, Errors, etc.)
    else:
        safe_name = html.escape(name)
        return (
            f"{snapshot.stage}: {safe_name}\n"
            f"Stop: /c{task_id}"
        )


async def safe_edit_text(message, text: str) -> None:
    try:
        # Using HTML parse mode to allow <b>bold</b> tags
        await message.edit_text(text, parse_mode="HTML")
    except BadRequest as exc:
        if "Message is not modified" in str(exc):
            return
        if "Flood control exceeded" in str(exc):
            return
        # If HTML fails, try plain text as fallback
        try:
            await message.edit_text(text)
        except:
            pass
    except RetryAfter:
        pass
    except Exception:
        pass


# --- Google Drive Logic ---

def resolve_upload_root(files: List[Path], download_dirs: List[str]) -> Tuple[Path, str]:
    if not files:
        return Path("."), "upload"
    
    download_paths = [Path(d).resolve() for d in download_dirs if d]
    top_levels: List[str] = []
    
    for file_path in files:
        try:
            file_resolved = file_path.resolve()
        except FileNotFoundError:
            continue
        
        matched_root = None
        for root in download_paths:
            try:
                file_resolved.relative_to(root)
            except ValueError:
                continue
            matched_root = root
            break
        
        if matched_root is None:
            continue
            
        relative = file_resolved.relative_to(matched_root)
        if relative.parts:
            top_levels.append(relative.parts[0])
            
    if top_levels and len(set(top_levels)) == 1:
        root = next(iter(download_paths), files[0].parent)
        return root / top_levels[0], top_levels[0]
        
    common_root = Path(os.path.commonpath([str(p.resolve()) for p in files]))
    if common_root.is_file():
        common_root = common_root.parent
    return common_root, common_root.name or "upload"


def create_drive_folder(service, name: str, parent_id: str) -> str:
    metadata: Dict[str, Any] = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def ensure_drive_folders(
    service,
    relative_dir: Path,
    cache: Dict[Path, str],
    root_id: str,
) -> str:
    if relative_dir in cache:
        return cache[relative_dir]
    
    current = Path(".")
    current_id = root_id
    
    for part in relative_dir.parts:
        if part == ".": continue
        current = current / part
        if current in cache:
            current_id = cache[current]
            continue
        current_id = create_drive_folder(service, part, current_id)
        cache[current] = current_id
        
    return current_id


def cleanup_empty_dirs(root_folder: Path) -> None:
    if not root_folder.exists():
        return
    for current_root, dirnames, _ in os.walk(root_folder, topdown=False):
        for dirname in dirnames:
            dir_path = Path(current_root) / dirname
            try:
                dir_path.rmdir()
            except OSError:
                continue
    try:
        root_folder.rmdir()
    except OSError:
        pass


def upload_files_to_drive(
    files: List[Path],
    drive_folder_id: str,
    token_path: Path,
    status: TaskStatus,
    cancel_event: threading.Event,
    download_dirs: List[str],
) -> DownloadResult:
    uploaded: List[str] = []
    skipped: List[str] = []
    
    if not files:
        return DownloadResult(uploaded, skipped)
        
    try:
        service = load_drive_service(token_path)
    except Exception as e:
        print(f"Failed to load drive service: {e}")
        return DownloadResult([], [str(e)])

    root_folder, root_name = resolve_upload_root(files, download_dirs)
    
    try:
        drive_root_id = create_drive_folder(service, root_name, drive_folder_id)
    except Exception as e:
         return DownloadResult([], [f"Failed creating root folder: {e}"])

    total_bytes = sum(path.stat().st_size for path in files if path.exists())
    status.update_progress(total_bytes=total_bytes, stage="Upload", current_name=root_name)
    
    processed_before = 0
    folder_cache: Dict[Path, str] = {Path("."): drive_root_id}
    
    for file_path in files:
        if cancel_event.is_set():
            break
            
        try:
            relative = file_path.relative_to(root_folder)
            parent_id = ensure_drive_folders(
                service,
                relative.parent,
                folder_cache,
                drive_root_id,
            )
            
            metadata: Dict[str, Any] = {"name": file_path.name, "parents": [parent_id]}
            media = MediaFileUpload(file_path, resumable=True)
            request = service.files().create(body=metadata, media_body=media, fields="id")
            
            response = None
            while response is None:
                if cancel_event.is_set():
                    break
                status_chunk, response = request.next_chunk()
                if status_chunk is not None:
                    progress = int(status_chunk.resumable_progress)
                    status.update_progress(
                        processed_bytes=processed_before + progress,
                        current_name=file_path.name,
                    )
            
            if cancel_event.is_set():
                break
                
            uploaded.append(f"{file_path.name}")
            processed_before += file_path.stat().st_size
            status.update_progress(processed_bytes=processed_before)
            
            try:
                file_path.unlink()
            except OSError:
                pass
                
        except Exception as e:
            skipped.append(f"{file_path.name} ({str(e)})")
            
    if not cancel_event.is_set():
        cleanup_empty_dirs(root_folder)
        
    return DownloadResult(uploaded, skipped)


# --- Core Logic ---

def build_download_command(command: str, link: str, settings: Dict[str, Any]) -> List[str]:
    args = shlex.split(command)
    if settings.get("auto_select_all", True) and "--all-album" not in args:
        args.append("--all-album")
    if settings.get("auto_skip_mv", True) and "--mv-max" not in args:
        args.extend(["--mv-max", "0"])
    return args + [link]


async def run_download_and_upload(
    link: str,
    settings: Dict[str, Any],
    status_message,
    task_control: TaskControl,
) -> DownloadResult:
    start_time = time.time()
    status = TaskStatus(
        stage="Download",
        processed_bytes=0,
        total_bytes=0,
        speed_bps=0.0,
        current_name=link,
        start_time=start_time,
        lock=threading.Lock(),
    )
    
    command = settings.get("download_command", "go run main.go")
    args = build_download_command(command, link, settings)
    download_dirs = load_download_dirs(settings)
    
    executable = args[0] if args else None
    if not executable or shutil.which(executable) is None:
        status.update_progress(stage=f"Download failed (missing '{executable or 'command'}')")
        await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))
        return DownloadResult([], [])
        
    try:
        process = subprocess.Popen(args)
    except FileNotFoundError:
        status.update_progress(stage=f"Download failed (missing '{executable}')")
        await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))
        return DownloadResult([], [])

    last_check = time.time()
    last_bytes = 0
    
    while process.poll() is None:
        if task_control.cancel_event.is_set():
            process.terminate()
            process.wait(timeout=10)
            status.update_progress(stage="Cancelled")
            await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))
            return DownloadResult([], [])
            
        await asyncio.sleep(15) 
        
        total, recent_files = sum_recent_files(download_dirs, start_time)
        latest_name = ""
        if recent_files:
            latest_file = max(recent_files, key=lambda path: path.stat().st_mtime)
            latest_name = latest_file.name
            
        now = time.time()
        delta_time = max(now - last_check, 1)
        speed = (total - last_bytes) / delta_time
        last_check = now
        last_bytes = total
        
        status.update_progress(
            processed_bytes=total,
            speed_bps=speed,
            stage="Download",
            current_name=latest_name or link,
        )
        await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))

    if process.returncode != 0:
        status.update_progress(stage="Download finished with errors")
        await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))

    total_downloaded, files = sum_recent_files(download_dirs, start_time)
    if not files:
        status.update_progress(processed_bytes=total_downloaded, total_bytes=total_downloaded, stage="No files found")
        await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))
        return DownloadResult([], [])
        
    status.update_progress(processed_bytes=total_downloaded, total_bytes=total_downloaded, stage="Upload")
    await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))

    token_path = Path(os.getenv("GDRIVE_TOKEN", "token.pickle"))
    loop = asyncio.get_running_loop()
    
    upload_future = loop.run_in_executor(
        None,
        upload_files_to_drive,
        files,
        settings.get("drive_folder_id", ""),
        token_path,
        status,
        task_control.cancel_event,
        download_dirs,
    )

    upload_last_check = time.time()
    upload_last_bytes = status.processed_bytes
    
    while not upload_future.done():
        if task_control.cancel_event.is_set():
            break
        
        await asyncio.sleep(15)
        
        snapshot = status.snapshot()
        now = time.time()
        delta_time = max(now - upload_last_check, 1)
        delta_bytes = snapshot.processed_bytes - upload_last_bytes
        upload_speed = delta_bytes / delta_time if delta_bytes > 0 else 0.0
        upload_last_check = now
        upload_last_bytes = snapshot.processed_bytes
        
        status.update_progress(speed_bps=upload_speed)
        await safe_edit_text(status_message, render_status(link, snapshot, task_control.task_id))

    if task_control.cancel_event.is_set():
        status.update_progress(stage="Cancelled")
        await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))
        return DownloadResult([], [])

    try:
        result = await upload_future
    except Exception as e:
        status.update_progress(stage=f"Upload failed: {e}")
        await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))
        return DownloadResult([], [])

    status.update_progress(stage="Complete", processed_bytes=status.total_bytes)
    await safe_edit_text(status_message, render_status(link, status.snapshot(), task_control.task_id))
    return result


def get_semaphore(context: ContextTypes.DEFAULT_TYPE, settings: Dict[str, Any]) -> asyncio.Semaphore:
    semaphore = context.application.bot_data.get("semaphore")
    max_tasks = max(1, int(settings.get("max_tasks", 1)))
    if not semaphore or semaphore._value != max_tasks:
        semaphore = asyncio.Semaphore(max_tasks)
        context.application.bot_data["semaphore"] = semaphore
    return semaphore


# --- Telegram Handlers ---

def admin_required(update: Update, settings: Dict[str, Any]) -> bool:
    user = update.effective_user
    return user is not None and is_admin(user.id, settings)


def allowed_required(update: Update, settings: Dict[str, Any]) -> bool:
    user = update.effective_user
    return user is not None and is_allowed(user.id, settings)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    user = update.effective_user
    if user and not settings["admin_ids"]:
        settings["admin_ids"] = [user.id]
        save_settings(settings)
    await update.message.reply_text(
        "Ready. Use /dl <link> to download and upload to Google Drive."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/dl <link> - download and upload\n"
        "/uset - open settings menu (admin)\n"
        "/adduser <user_id> - allow a user (admin)\n"
        "/removeuser <user_id> - remove a user (admin)\n"
        "/setdrive <drive_folder_id> - set Google Drive folder (admin)\n"
        "/setmaxtasks <num> - set concurrent tasks (admin)"
    )


async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /adduser <user_id>")
        return
    try:
        user_id = int(context.args[0])
        if user_id not in settings["allowed_users"]:
            settings["allowed_users"].append(user_id)
            save_settings(settings)
        await update.message.reply_text(f"Allowed user: {user_id}")
    except ValueError:
        await update.message.reply_text("Invalid user ID.")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return
    try:
        user_id = int(context.args[0])
        settings["allowed_users"] = [uid for uid in settings["allowed_users"] if uid != user_id]
        save_settings(settings)
        await update.message.reply_text(f"Removed user: {user_id}")
    except ValueError:
        await update.message.reply_text("Invalid user ID.")


async def set_drive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setdrive <drive_folder_id>")
        return
    settings["drive_folder_id"] = context.args[0].strip()
    save_settings(settings)
    await update.message.reply_text("Drive folder updated.")


async def set_max_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setmaxtasks <num>")
        return
    try:
        settings["max_tasks"] = max(1, int(context.args[0]))
        save_settings(settings)
        await update.message.reply_text("Max tasks updated.")
    except ValueError:
        await update.message.reply_text("Invalid number.")


async def dl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    if not allowed_required(update, settings):
        await update.message.reply_text("Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /dl <link>")
        return
    link = context.args[0]
    semaphore = get_semaphore(context, settings)
    
    await update.message.reply_text("Queued download.")
    status_message = await update.message.reply_text("Starting...")
    
    task_id = f"{int(time.time())}_{uuid.uuid4().hex[:6]}"
    tasks = context.application.bot_data.setdefault("tasks", {})
    task_control = TaskControl(task_id=task_id, cancel_event=threading.Event())
    tasks[task_id] = task_control

    try:
        async with semaphore:
            result = await run_download_and_upload(link, settings, status_message, task_control)
            
            if not result.uploaded_files and not result.skipped_files:
                await safe_edit_text(status_message, f"Task {task_id} finished. No files uploaded.")
                return

            message = ["Task Complete."]
            if result.uploaded_files:
                message.append("Uploaded:\n" + "\n".join(result.uploaded_files[:20]))
            if result.skipped_files:
                message.append("Skipped:\n" + "\n".join(result.skipped_files[:20]))
                
            await update.message.reply_text("\n\n".join(message))
    finally:
        tasks.pop(task_id, None)


async def cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if not text.startswith("/c"):
        return
    
    try:
        task_id = text[2:]
    except IndexError:
        return
        
    tasks = context.application.bot_data.get("tasks", {})
    task_control = tasks.get(task_id)
    
    if not task_control:
        await update.message.reply_text("Unknown task id or task already finished.")
        return
        
    task_control.cancel_event.set()
    await update.message.reply_text(f"Stopping task {task_id}...")


async def open_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("Set Drive ID", callback_data="set_drive")],
        [InlineKeyboardButton("Set Max Tasks", callback_data="set_max")],
        [InlineKeyboardButton("Add User", callback_data="add_user")],
        [InlineKeyboardButton("Remove User", callback_data="remove_user")],
    ]
    await update.message.reply_text(
        "Select a setting:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SETTING_ACTION


async def handle_setting_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["setting_action"] = query.data
    prompt = {
        "set_drive": "Send Drive folder ID:",
        "set_max": "Send max concurrent tasks:",
        "add_user": "Send user ID to allow:",
        "remove_user": "Send user ID to remove:",
    }.get(query.data, "Send value:")
    await query.edit_message_text(prompt)
    return SETTING_ACTION


async def handle_setting_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return ConversationHandler.END
        
    action = context.user_data.get("setting_action")
    value = update.message.text.strip()
    
    try:
        if action == "set_drive":
            settings["drive_folder_id"] = value
        elif action == "set_max":
            settings["max_tasks"] = max(1, int(value))
        elif action == "add_user":
            user_id = int(value)
            if user_id not in settings["allowed_users"]:
                settings["allowed_users"].append(user_id)
        elif action == "remove_user":
            user_id = int(value)
            settings["allowed_users"] = [uid for uid in settings["allowed_users"] if uid != user_id]
        else:
            await update.message.reply_text("Unknown action.")
            return ConversationHandler.END
            
        save_settings(settings)
        await update.message.reply_text("Setting updated.")
        
    except ValueError:
        await update.message.reply_text("Invalid input value.")
        
    return ConversationHandler.END


def main() -> None:
    token = os.getenv("BOT_TOKEN", BOT_TOKEN)
    if not token:
        raise SystemExit("BOT_TOKEN is not set.")
        
    request = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=10.0,
    )
    application = Application.builder().token(token).request(request).build()

    settings_handler = ConversationHandler(
        entry_points=[CommandHandler("uset", open_settings_menu)],
        states={
            SETTING_ACTION: [
                CallbackQueryHandler(handle_setting_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_setting_value),
            ]
        },
        fallbacks=[],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("dl", dl))
    application.add_handler(CommandHandler("adduser", add_user))
    application.add_handler(CommandHandler("removeuser", remove_user))
    application.add_handler(CommandHandler("setdrive", set_drive))
    application.add_handler(CommandHandler("setmaxtasks", set_max_tasks))
    application.add_handler(settings_handler)
    
    # Handle /c command via regex text filter or command handler
    application.add_handler(MessageHandler(filters.Regex(r"^/c"), cancel_task))

    application.run_polling()


if __name__ == "__main__":
    main()
