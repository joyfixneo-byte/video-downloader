"""Private Telegram front end for the existing video download API.

This intentionally uses only the standard library for Telegram HTTP, polling,
and persistent state. yt-dlp is already an application dependency.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yt_dlp


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_ID = os.environ.get("TELEGRAM_ALLOWED_ID", "").strip()
API_BASE = os.environ.get("TELEGRAM_API_BASE", "http://127.0.0.1:8000").rstrip("/")
API_PASSWORD = os.environ.get("DOWNLOADER_PASSWORD", "").strip()
STATE_DB = Path(os.environ.get(
    "TELEGRAM_STATE_DB", Path(__file__).resolve().parent / "data" / "telegram_bot.sqlite3"))
POLL_SECONDS = 2
ACTIVE = ("queued", "downloading", "processing")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}


class BotError(Exception):
    pass


def db_connect():
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(STATE_DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS videos (
        video_id TEXT PRIMARY KEY, title TEXT NOT NULL, state TEXT NOT NULL,
        job_id TEXT, filename TEXT, chat_id TEXT NOT NULL, message_id INTEGER,
        error TEXT, updated REAL NOT NULL, notification TEXT,
        notified INTEGER NOT NULL DEFAULT 1)""")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(videos)")}
    if "notification" not in columns:
        db.execute("ALTER TABLE videos ADD COLUMN notification TEXT")
    if "notified" not in columns:
        # Existing terminal rows predate the outbox; do not send old results again.
        db.execute("ALTER TABLE videos ADD COLUMN notified INTEGER NOT NULL DEFAULT 1")
    db.commit()
    return db


def telegram(method: str, payload: dict, timeout: int = 45):
    if not TOKEN:
        raise BotError("TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    body = urllib.parse.urlencode({
        k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        for k, v in payload.items() if v is not None
    }).encode()
    req = urllib.request.Request(url, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise BotError(f"Telegram API: {exc}") from exc
    if not data.get("ok"):
        raise BotError(f"Telegram API: {data.get('description', 'неизвестная ошибка')}")
    return data.get("result")


def app_api(method: str, path: str, payload: dict | None = None):
    headers = {}
    if API_PASSWORD:
        headers["X-Access-Password"] = API_PASSWORD
    data = json.dumps(payload).encode() if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        raise BotError(str(detail)) from exc
    except Exception as exc:
        raise BotError(f"Не удалось связаться с сервером загрузок: {exc}") from exc


def validate_youtube_url(raw: str) -> str:
    url = raw.strip().strip("<>.,;)")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise BotError("Пришлите ссылку на одно видео YouTube (не плейлист).")
    return url


def get_video_info(url: str) -> tuple[str, str, str]:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "socket_timeout": 20, "retries": 1,
            "extractor_retries": 1}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise BotError(f"Не удалось получить данные видео: {exc}") from exc
    if not info or info.get("_type") == "playlist" or not info.get("id"):
        raise BotError("Ссылка должна вести на одно доступное видео YouTube.")
    video_id = str(info["id"])
    canonical = f"https://www.youtube.com/watch?v={urllib.parse.quote(video_id)}"
    return video_id, str(info.get("title") or video_id), canonical


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def library_files() -> list[dict]:
    return app_api("GET", "/api/library").get("files", [])


def exact_legacy_matches(title: str, files: list[dict]) -> list[str]:
    wanted = normalize_title(title)
    if not wanted:
        return []
    matches = []
    for item in files:
        stem = Path(item.get("name", "")).stem
        if normalize_title(stem) == wanted:
            matches.append(item["name"])
    return matches


def stop_markup(job_id: str):
    return {"inline_keyboard": [[{"text": "Остановить", "callback_data": f"stop:{job_id}"}]]}


def handle_video(chat_id: str, message_id: int, text: str):
    url = validate_youtube_url(text)
    video_id, title, canonical = get_video_info(url)
    db = db_connect()
    try:
        existing = db.execute("SELECT * FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if existing and existing["state"] in ACTIVE:
            telegram("sendMessage", {"chat_id": chat_id,
                "text": f"«{existing['title']}» уже загружается.",
                "reply_markup": stop_markup(existing["job_id"]) if existing["job_id"] else None})
            return
        files = library_files()
        if (existing and existing["state"] == "legacy_match"
                and existing["filename"] in {f["name"] for f in files}):
            telegram("sendMessage", {"chat_id": chat_id,
                "text": f"Возможный дубль уже найден в SMB: {existing['filename']}. Повторно не скачиваю."})
            return
        if existing and existing["state"] == "done" and existing["filename"] in {f["name"] for f in files}:
            telegram("sendMessage", {"chat_id": chat_id,
                "text": f"Это видео уже есть в SMB-библиотеке: {existing['filename']}"})
            return
        if not existing:
            matches = exact_legacy_matches(title, files)
            if matches:
                note = ("Возможный дубль в SMB, не скачиваю: " + ", ".join(matches)
                        if len(matches) == 1 else "Возможные дубли в SMB, не скачиваю: " + ", ".join(matches))
                telegram("sendMessage", {"chat_id": chat_id, "text": note})
                db.execute("INSERT INTO videos VALUES (?, ?, ?, NULL, ?, ?, NULL, NULL, ?)",
                           (video_id, title, "legacy_match", matches[0], str(chat_id), time.time()))
                db.commit()
                return
        result = app_api("POST", "/api/download", {"url": canonical, "quality": "best"})
        job_id = result["job_id"]
        db.execute("""INSERT INTO videos(video_id,title,state,job_id,chat_id,updated)
            VALUES(?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
            title=excluded.title,state=excluded.state,job_id=excluded.job_id,
            filename=NULL,chat_id=excluded.chat_id,message_id=NULL,error=NULL,updated=excluded.updated""",
            (video_id, title, "queued", job_id, str(chat_id), time.time()))
        db.commit()
    finally:
        db.close()
    sent = telegram("sendMessage", {"chat_id": chat_id,
        "text": f"Поставил в очередь: {title}", "reply_markup": stop_markup(job_id)})
    db = db_connect()
    db.execute("UPDATE videos SET message_id=?, updated=? WHERE video_id=?",
               (sent["message_id"], time.time(), video_id))
    db.commit()
    db.close()


def handle_callback(callback: dict):
    sender_id = str(callback.get("from", {}).get("id", ""))
    query_id = callback.get("id")
    data = callback.get("data", "")
    if sender_id != ALLOWED_ID:
        telegram("answerCallbackQuery", {"callback_query_id": query_id, "text": "Нет доступа"})
        return
    if not data.startswith("stop:"):
        telegram("answerCallbackQuery", {"callback_query_id": query_id})
        return
    job_id = data[5:]
    try:
        result = app_api("POST", f"/api/cancel/{urllib.parse.quote(job_id)}", {})
        state = result.get("state", "остановка запрошена")
        telegram("answerCallbackQuery", {"callback_query_id": query_id, "text": f"{state}"})
    except BotError as exc:
        telegram("answerCallbackQuery", {"callback_query_id": query_id, "text": str(exc)[:180]})


def handle_update(update: dict):
    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    sender_id = str(message.get("from", {}).get("id", ""))
    if sender_id != ALLOWED_ID:
        return
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()
    if text == "/start":
        telegram("sendMessage", {"chat_id": chat_id,
            "text": "Пришлите ссылку на видео YouTube. Загружу лучшее качество в SMB-библиотеку."})
        return
    try:
        handle_video(chat_id, message.get("message_id", 0), text)
    except BotError as exc:
        telegram("sendMessage", {"chat_id": chat_id, "text": f"Не получилось: {exc}"})
    except Exception as exc:
        telegram("sendMessage", {"chat_id": chat_id, "text": f"Ошибка обработки ссылки: {exc}"})


def poll_jobs():
    db = db_connect()
    rows = db.execute("SELECT * FROM videos WHERE state IN ('queued','downloading','processing') OR notified=0").fetchall()
    for row in rows:
        if row["state"] in ACTIVE:
            try:
                job = app_api("GET", f"/api/status/{urllib.parse.quote(row['job_id'])}")
            except BotError as exc:
                # A missing in-memory job means the application restarted during
                # this download. Queue a durable failure notice; never resubmit it.
                if "не найдена" not in str(exc).lower() and "not found" not in str(exc).lower():
                    continue
                error = "Сервер перезапустился во время загрузки; проверьте SMB и отправьте ссылку повторно."
                db.execute("UPDATE videos SET state='error',error=?,notification=?,notified=0,updated=? WHERE video_id=?",
                           (error, f"Не завершено: {row['title']}. {error}", time.time(), row["video_id"]))
                db.commit()
            else:
                state = job.get("state", "")
                if state in ACTIVE:
                    db.execute("UPDATE videos SET state=?,updated=? WHERE video_id=?",
                               (state, time.time(), row["video_id"]))
                    db.commit()
                    continue
                if state == "done":
                    filename = job.get("library_name") or job.get("filename") or row["title"]
                    message = f"Готово, добавил в SMB-библиотеку: {filename}"
                    db.execute("UPDATE videos SET state='done',filename=?,error=NULL,notification=?,notified=0,updated=? WHERE video_id=?",
                               (filename, message, time.time(), row["video_id"]))
                elif state == "cancelled":
                    message = f"Загрузка остановлена: {row['title']}"
                    db.execute("UPDATE videos SET state='cancelled',notification=?,notified=0,updated=? WHERE video_id=?",
                               (message, time.time(), row["video_id"]))
                else:
                    error = job.get("error") or f"состояние задачи: {state or 'неизвестно'}"
                    message = f"Не удалось скачать «{row['title']}»: {error}"
                    db.execute("UPDATE videos SET state='error',error=?,notification=?,notified=0,updated=? WHERE video_id=?",
                               (error, message, time.time(), row["video_id"]))
                db.commit()

        pending = db.execute("SELECT * FROM videos WHERE video_id=? AND notified=0",
                             (row["video_id"],)).fetchone()
        if not pending:
            continue
        try:
            telegram("sendMessage", {"chat_id": pending["chat_id"],
                                      "text": pending["notification"]})
        except BotError:
            print(f"Telegram notification pending for {pending['video_id']}", flush=True)
            continue
        db.execute("UPDATE videos SET notified=1 WHERE video_id=?", (pending["video_id"],))
        db.commit()
        if pending["message_id"]:
            try:
                telegram("editMessageReplyMarkup", {"chat_id": pending["chat_id"],
                    "message_id": pending["message_id"], "reply_markup": {"inline_keyboard": []}})
            except BotError:
                pass
    db.close()


def main():
    if not TOKEN or not ALLOWED_ID.isdigit():
        raise SystemExit("Нужно задать TELEGRAM_BOT_TOKEN и числовой TELEGRAM_ALLOWED_ID")
    # Remove a stale webhook; long polling and webhooks cannot run together.
    telegram("deleteWebhook", {"drop_pending_updates": False})
    db = db_connect()
    row = db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()
    offset = int(row["value"]) if row else None
    db.close()
    print("Telegram downloader polling started", flush=True)
    last_status = 0.0
    while True:
        now = time.monotonic()
        if now - last_status >= POLL_SECONDS:
            try:
                poll_jobs()
            except Exception as exc:
                print(f"Status poll failed: {exc}", flush=True)
            last_status = now
        try:
            updates = telegram("getUpdates", {"offset": offset, "timeout": 25,
                "allowed_updates": ["message", "callback_query"]}, timeout=35) or []
            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update)
                db = db_connect()
                db.execute("INSERT INTO settings(key,value) VALUES('offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (str(offset),))
                db.commit()
                db.close()
        except Exception as exc:
            print(f"Telegram polling failed: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
