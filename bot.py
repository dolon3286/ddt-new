import asyncio
import json
import os
import pickle
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

import yaml
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

BOT_TOKEN = ""

SETTING_ACTION = 1


@dataclass
class DownloadResult:
    uploaded_files: List[str]
    skipped_files: List[str]


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
    }


def load_settings() -> Dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        settings = _default_settings()
        save_settings(settings)
        return settings
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
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


def upload_files_to_drive(
    files: List[Path],
    drive_folder_id: str,
    token_path: Path,
) -> DownloadResult:
    uploaded: List[str] = []
    skipped: List[str] = []
    if not files:
        return DownloadResult(uploaded, skipped)
    service = load_drive_service(token_path)
    for file_path in files:
        metadata: Dict[str, Any] = {"name": file_path.name}
        if drive_folder_id:
            metadata["parents"] = [drive_folder_id]
        media = MediaFileUpload(file_path, resumable=True)
        try:
            file = (
                service.files()
                .create(body=metadata, media_body=media, fields="id")
                .execute()
            )
            uploaded.append(f"{file_path} -> {file.get('id')}")
        except Exception:
            skipped.append(str(file_path))
    return DownloadResult(uploaded, skipped)


def build_download_command(command: str, link: str) -> List[str]:
    args = shlex.split(command)
    return args + [link]


def run_download_and_upload(link: str, settings: Dict[str, Any]) -> DownloadResult:
    start_time = time.time()
    command = settings.get("download_command", "go run main.go")
    args = build_download_command(command, link)
    process = subprocess.run(args, check=False)
    if process.returncode != 0:
        return DownloadResult([], [])
    download_dirs = load_download_dirs(settings)
    files = find_recent_files(download_dirs, start_time)
    token_path = Path(os.getenv("GDRIVE_TOKEN", "token.pickle"))
    try:
        return upload_files_to_drive(files, settings.get("drive_folder_id", ""), token_path)
    except Exception:
        return DownloadResult([], [])


def get_semaphore(context: ContextTypes.DEFAULT_TYPE, settings: Dict[str, Any]) -> asyncio.Semaphore:
    semaphore = context.application.bot_data.get("semaphore")
    max_tasks = max(1, int(settings.get("max_tasks", 1)))
    if not semaphore or semaphore._value != max_tasks:
        semaphore = asyncio.Semaphore(max_tasks)
        context.application.bot_data["semaphore"] = semaphore
    return semaphore


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
    user_id = int(context.args[0])
    if user_id not in settings["allowed_users"]:
        settings["allowed_users"].append(user_id)
        save_settings(settings)
    await update.message.reply_text(f"Allowed user: {user_id}")


async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return
    user_id = int(context.args[0])
    settings["allowed_users"] = [uid for uid in settings["allowed_users"] if uid != user_id]
    save_settings(settings)
    await update.message.reply_text(f"Removed user: {user_id}")


async def set_drive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = load_settings()
    if not admin_required(update, settings):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setdrive <drive_folder_id>")
        return
    settings["drive_folder_id"] = context.args[0]
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
    settings["max_tasks"] = max(1, int(context.args[0]))
    save_settings(settings)
    await update.message.reply_text("Max tasks updated.")


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

    async with semaphore:
        await update.message.reply_text("Starting download...")
        result = await asyncio.to_thread(run_download_and_upload, link, settings)
        if not result.uploaded_files and not result.skipped_files:
            await update.message.reply_text("Download failed or no files found.")
            return
        message = ["Upload complete."]
        if result.uploaded_files:
            message.append("Uploaded:\n" + "\n".join(result.uploaded_files[:20]))
        if result.skipped_files:
            message.append("Skipped:\n" + "\n".join(result.skipped_files[:20]))
        await update.message.reply_text("\n\n".join(message))


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
    return ConversationHandler.END


def main() -> None:
    token = os.getenv("BOT_TOKEN", BOT_TOKEN)
    if not token:
        raise SystemExit("BOT_TOKEN is not set.")
    application = Application.builder().token(token).build()

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

    application.run_polling()


if __name__ == "__main__":
    main()
