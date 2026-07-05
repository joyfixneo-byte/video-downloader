"""
Мини-сайт для скачивания видео по ссылке.
Бэкенд: FastAPI + yt-dlp. Отдаёт фронтенд и REST API.

Запуск (локально):  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import re
import html
import json
import time
import uuid
import shutil
import socket
import ipaddress
import threading
import traceback
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, quote

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

# --- Настройки -------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Папка-витрина для готовых файлов: SMB-шара на хосте (SRV-HOST), которую по
# локальной сети видит телевизор/Apple TV в VLC. Если переменная задана —
# на сайте появляется кнопка «В библиотеку», по которой готовый файл копируется
# сюда плоским списком с человекочитаемым именем (автоматически при скачивании
# НЕ копируем — только вручную). Пусто — раздел библиотеки скрыт.
# ⚠️ Задавать ТОЛЬКО после того, как шара примонтирована и проверена на запись,
# иначе копия ляжет на локальный диск VM и забьёт его.
SHARE_DIR = os.environ.get("SHARE_DIR", "").strip()
SHARE_PATH = Path(SHARE_DIR) if SHARE_DIR else None

# Необязательный пароль. Если переменная окружения DOWNLOADER_PASSWORD задана,
# то фронтенд должен присылать её в заголовке X-Access-Password.
ACCESS_PASSWORD = os.environ.get("DOWNLOADER_PASSWORD", "").strip()

# Через сколько часов после скачивания удалять файлы (по умолчанию 3 часа).
RETENTION_SECONDS = int(float(os.environ.get("RETENTION_HOURS", "3")) * 3600)
CLEANUP_INTERVAL = 600  # как часто проверять папку, секунд (10 минут)

# --- Лимиты безопасности (чтобы чужой не уронил сервер) --------------------
# Потолок одновременных/ожидающих задач: больше — отклоняем с 429, чтобы поток
# запросов не плодил бесконечно потоки и не съел память.
JOB_CEILING = int(os.environ.get("JOB_CEILING", "25"))        # скачивания
TJOB_CEILING = int(os.environ.get("TJOB_CEILING", "15"))      # транскрибации
# Сколько распознаваний Whisper крутить одновременно. Whisper грузит CPU,
# поэтому по умолчанию строго одно — остальные ждут в очереди.
MAX_ACTIVE_TRANSCRIBE = int(os.environ.get("MAX_ACTIVE_TRANSCRIBE", "1"))
# Не распознаём слишком длинные ролики (Whisper на CPU считал бы их вечно).
WHISPER_MAX_MINUTES = float(os.environ.get("WHISPER_MAX_MINUTES", "90"))
# Лимит размера одного файла, ГБ (0 = без лимита). Имеет смысл задать, если
# сайт открыт без пароля, чтобы не забили диск.
DOWNLOAD_MAX_GB = float(os.environ.get("DOWNLOAD_MAX_GB", "0"))

app = FastAPI(title="Video Downloader")

# Хранилище задач скачивания в памяти процесса.
# job_id -> dict(state, percent, speed, eta, title, filename, error)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


class _Cancelled(Exception):
    """Бросается из progress-хука, когда пользователь нажал «Остановить»."""


# --- Автоудаление старых файлов -------------------------------------------

def _cleanup_loop():
    """Раз в CLEANUP_INTERVAL удаляет папки задач старше RETENTION_SECONDS.

    Возраст считаем по самому свежему файлу в папке — то есть отсчёт идёт
    от момента, когда скачивание завершилось. Файл, который ещё качается
    (mtime обновляется), под удаление не попадёт.
    """
    while True:
        try:
            now = time.time()
            if DOWNLOAD_DIR.exists():
                for d in DOWNLOAD_DIR.iterdir():
                    if not d.is_dir():
                        continue
                    files = [p for p in d.iterdir() if p.is_file()]
                    newest = (max(p.stat().st_mtime for p in files)
                              if files else d.stat().st_mtime)
                    if now - newest > RETENTION_SECONDS:
                        shutil.rmtree(d, ignore_errors=True)
                        with JOBS_LOCK:
                            job = JOBS.get(d.name)
                            if job:
                                job.update(state="expired", filename=None)
        except Exception:
            traceback.print_exc()
        time.sleep(CLEANUP_INTERVAL)


threading.Thread(target=_cleanup_loop, daemon=True).start()


# --- Защита паролем --------------------------------------------------------

async def check_password(request: Request):
    if not ACCESS_PASSWORD:
        return  # пароль не настроен — пускаем всех
    sent = request.headers.get("x-access-password", "")
    if sent != ACCESS_PASSWORD:
        raise HTTPException(status_code=401, detail="Неверный пароль")


# --- Вспомогательное -------------------------------------------------------

def safe_name(name: str) -> str:
    """Убираем из имени файла символы, опасные для файловой системы."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name or "video")
    return name.strip()[:150] or "video"


def _is_temp(p: Path) -> bool:
    """Временный/недокачанный файл yt-dlp: его нельзя показывать как готовый
    и нельзя отдавать на скачивание (иначе размер «прыгает», а в браузере
    вместо файла открывается мусор)."""
    n = p.name.lower()
    return (n.endswith((".part", ".ytdl", ".tmp", ".temp", ".download", ".aria2"))
            or ".part-frag" in n)


def _result_file(job_dir: Path):
    """Готовый файл задачи на диске: самый большой, не считая временных.
    None — если готового файла ещё/уже нет."""
    if not job_dir.exists() or not job_dir.is_dir():
        return None
    files = [p for p in job_dir.iterdir() if p.is_file() and not _is_temp(p)]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)


def check_url_safe(url: str):
    """Защита от SSRF: разрешаем только http/https на публичные адреса.
    Блокируем localhost, приватные/служебные сети и облачные метаданные
    (169.254.169.254 и т.п.), чтобы через ссылку нельзя было ходить во
    внутреннюю сеть сервера."""
    url = (url or "").strip()
    if len(url) > 2000:
        raise HTTPException(400, "Слишком длинная ссылка")
    try:
        p = urlparse(url)
    except Exception:
        raise HTTPException(400, "Некорректная ссылка")
    if p.scheme not in ("http", "https"):
        raise HTTPException(400, "Поддерживаются только http/https ссылки")
    host = p.hostname
    if not host:
        raise HTTPException(400, "В ссылке не указан адрес сайта")
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        raise HTTPException(400, "Не удалось определить адрес сайта")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(400, "Этот адрес недоступен для скачивания")


def _publish_to_share(result: Path) -> str:
    """Копирует готовый файл в папку-витрину (SMB-шару) для просмотра с ТВ.

    Вызывается вручную по кнопке «В библиотеку» (см. /api/library/add) —
    автоматически при скачивании больше не копируем. Возвращает имя файла
    в витрине. Ошибки пробрасываем: кнопке нужно показать результат.
    Копируем во временное имя `*.part` и переименовываем — чтобы плеер на ТВ
    не подхватил полускопированный файл."""
    SHARE_PATH.mkdir(parents=True, exist_ok=True)
    target = SHARE_PATH / result.name
    # Такой же файл уже опубликован (то же имя и размер) — не копируем второй раз.
    if target.exists() and target.stat().st_size == result.stat().st_size:
        return target.name
    # Имя занято другим роликом — добавляем короткий суффикс, чтобы не затереть.
    if target.exists():
        target = SHARE_PATH / f"{result.stem} ({uuid.uuid4().hex[:6]}){result.suffix}"
    tmp = SHARE_PATH / (target.name + ".part")
    shutil.copyfile(result, tmp)
    tmp.replace(target)
    return target.name


def _count_active(jobs: dict, lock, states) -> int:
    """Сколько задач сейчас в работе/очереди (для потолка одновременных задач)."""
    with lock:
        return sum(1 for j in jobs.values() if j.get("state") in states)


def build_format(quality: str) -> dict:
    """Возвращает кусок ydl_opts под выбранное качество."""
    if quality == "audio":
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": "192"}
            ],
        }
    if quality == "best":
        fmt = "bestvideo+bestaudio/best"
    else:
        # quality = "1080" / "720" / "480" ...
        h = int(quality)
        fmt = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    return {"format": fmt, "merge_output_format": "mp4"}


def friendly_error(e) -> str:
    """Переводит технические ошибки yt-dlp в понятное сообщение по-русски."""
    msg = str(e)
    low = msg.lower()
    if "unsupported url" in low:
        return "Этот сайт или ссылка пока не поддерживаются."
    if "timed out" in low or "timeout" in low or "read operation" in low:
        return ("Сайт не ответил вовремя. Возможно, он недоступен с этого "
                "сервера или перегружен — проверьте ссылку или попробуйте позже.")
    if "video unavailable" in low:
        return "Видео недоступно — удалено или закрыто владельцем."
    if "private" in low:
        return "Видео приватное, доступ к нему закрыт."
    if "age" in low and ("confirm" in low or "restrict" in low or "sign" in low):
        return "Сайт требует подтверждение возраста или вход — скачать не получится."
    if "sign in" in low or "log in" in low or "login required" in low:
        return "Сайт требует вход в аккаунт — скачать без авторизации нельзя."
    if "drm" in low:
        return "Видео защищено DRM — такое скачать невозможно."
    if "no video" in low or "no media" in low:
        return "На этой странице не найдено видео."
    if ("name or service not known" in low or "failed to resolve" in low
            or "connection" in low or "network is unreachable" in low):
        return ("Не удалось соединиться с сайтом. Проверьте ссылку "
                "или попробуйте позже.")
    # Запасной вариант — коротко показываем суть.
    return "Не удалось обработать ссылку: " + msg[:200]


def _extract_with_timeout(url: str, opts: dict, timeout: int):
    """Запускает yt-dlp в отдельном потоке с жёстким таймаутом,
    чтобы запрос не висел бесконечно."""
    box: dict = {}

    def run():
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                box["data"] = ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("Сайт слишком долго не отвечает")
    if "error" in box:
        raise box["error"]
    return box.get("data")


# --- API-модели ------------------------------------------------------------

class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    quality: str = "best"


# --- /api/info: разбор ссылки ---------------------------------------------

@app.post("/api/info", dependencies=[Depends(check_password)])
def info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Пустая ссылка")
    check_url_safe(url)

    # extract_flat — быстро узнаём, плейлист это или одно видео,
    # не скачивая ничего и не разбирая каждый элемент целиком.
    # socket_timeout + ограничение повторов, чтобы запрос не висел вечно.
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "noprogress": True,
    }
    try:
        data = _extract_with_timeout(url, opts, timeout=60)
    except Exception as e:
        raise HTTPException(400, friendly_error(e))
    if not data:
        raise HTTPException(400, "На этой странице не найдено видео.")

    if data.get("_type") == "playlist" and data.get("entries"):
        entries = []
        for i, e in enumerate(data["entries"]):
            if not e:
                continue
            entries.append({
                "index": i,
                "title": e.get("title") or f"Серия {i + 1}",
                "url": e.get("url") or e.get("webpage_url") or e.get("id"),
            })
        return {
            "type": "playlist",
            "title": data.get("title") or "Плейлист",
            "count": len(entries),
            "entries": entries,
        }

    return {
        "type": "video",
        "title": data.get("title") or "Видео",
        "url": data.get("webpage_url") or url,
        "thumbnail": data.get("thumbnail"),
        "duration": data.get("duration"),
    }


# --- Фоновое скачивание ----------------------------------------------------

def _run_download(job_id: str, url: str, quality: str):
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
            if not job:
                return
            cancel = job.get("cancel")
            if not cancel:
                if d["status"] == "downloading":
                    total = (d.get("total_bytes")
                             or d.get("total_bytes_estimate") or 0)
                    done = d.get("downloaded_bytes") or 0
                    job["state"] = "downloading"
                    job["percent"] = round(done / total * 100, 1) if total else None
                    job["speed"] = d.get("speed")
                    job["eta"] = d.get("eta")
                    job["total"] = total or None
                elif d["status"] == "finished":
                    # видео скачано, дальше может идти склейка/конвертация
                    job["state"] = "processing"
                    job["percent"] = 100
        # Прерываем загрузку вне блокировки, чтобы yt-dlp поймал исключение.
        if cancel:
            raise _Cancelled()

    opts = {
        "outtmpl": str(job_dir / "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,  # качаем именно это видео, а не весь плейлист
        "progress_hooks": [hook],
        "restrictfilenames": False,
        "socket_timeout": 30,
        "retries": 3,
    }
    opts.update(build_format(quality))
    if DOWNLOAD_MAX_GB > 0:
        opts["max_filesize"] = int(DOWNLOAD_MAX_GB * 1024 ** 3)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        # Отмену могли нажать на этапе склейки, когда хук уже не вызывается.
        with JOBS_LOCK:
            cancelled = JOBS.get(job_id, {}).get("cancel")
        if cancelled:
            shutil.rmtree(job_dir, ignore_errors=True)
            with JOBS_LOCK:
                JOBS[job_id].update(state="cancelled", filename=None)
            return

        result = _result_file(job_dir)
        if not result:
            raise RuntimeError("Файл не найден после скачивания")
        with JOBS_LOCK:
            JOBS[job_id].update(
                state="done", percent=100,
                filename=result.name, size=result.stat().st_size)
        # В SMB-витрину файл больше не копируется автоматически — только
        # вручную, кнопкой «В библиотеку» (эндпоинт /api/library/add).
    except Exception as e:
        # Если это была отмена пользователем — чистим частичные файлы.
        with JOBS_LOCK:
            cancelled = JOBS.get(job_id, {}).get("cancel")
        if cancelled or isinstance(e, _Cancelled):
            shutil.rmtree(job_dir, ignore_errors=True)
            with JOBS_LOCK:
                JOBS[job_id].update(state="cancelled", filename=None)
        else:
            traceback.print_exc()
            with JOBS_LOCK:
                JOBS[job_id].update(state="error", error=friendly_error(e))


@app.post("/api/download", dependencies=[Depends(check_password)])
def download(req: DownloadRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Пустая ссылка")
    check_url_safe(url)
    if _count_active(JOBS, JOBS_LOCK,
                     ("queued", "downloading", "processing")) >= JOB_CEILING:
        raise HTTPException(429, "Сейчас слишком много загрузок — попробуйте позже.")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "queued", "percent": None, "speed": None,
            "eta": None, "title": None, "filename": None, "error": None,
            "cancel": False, "total": None, "size": None,
        }
    t = threading.Thread(
        target=_run_download, args=(job_id, url, req.quality), daemon=True)
    t.start()
    return {"job_id": job_id}


@app.get("/api/status/{job_id}", dependencies=[Depends(check_password)])
def status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        return dict(job, job_id=job_id)


@app.post("/api/cancel/{job_id}", dependencies=[Depends(check_password)])
def cancel(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        if job.get("state") in ("done", "error", "cancelled", "deleted", "expired"):
            return {"ok": True, "state": job["state"]}
        job["cancel"] = True
    return {"ok": True}


@app.post("/api/delete/{job_id}", dependencies=[Depends(check_password)])
def delete_file(job_id: str):
    # job_id приходит из адреса — оставляем только буквы/цифры,
    # чтобы нельзя было выйти за пределы папки загрузок.
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    target = DOWNLOAD_DIR / safe
    if target.exists() and target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    with JOBS_LOCK:
        job = JOBS.get(safe)
        if job:
            job.update(state="deleted", filename=None, size=None)
    return {"ok": True}


@app.get("/api/files", dependencies=[Depends(check_password)])
def list_files():
    """Список файлов, реально лежащих на сервере (читаем с диска,
    чтобы видеть их даже после перезапуска сервиса)."""
    items = []
    if DOWNLOAD_DIR.exists():
        now = time.time()
        for d in DOWNLOAD_DIR.iterdir():
            if not d.is_dir():
                continue
            # Задачу, которая ещё качается/склеивается, не показываем как
            # готовый файл — иначе её размер «прыгает» при обновлении и она
            # скачивается битой.
            with JOBS_LOCK:
                job = JOBS.get(d.name)
                state = job.get("state") if job else None
            if state in ("queued", "downloading", "processing"):
                continue
            result = _result_file(d)
            if not result:
                continue
            mtime = result.stat().st_mtime
            remaining = int(RETENTION_SECONDS - (now - mtime))
            items.append({
                "job_id": d.name,
                "filename": result.name,
                "size": result.stat().st_size,
                "remaining": max(0, remaining),
                "mtime": mtime,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)  # новые сверху
    return {"files": items}


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    # Файл отдаём без пароля в заголовке — браузер скачивает по прямой ссылке.
    # job_id случайный и неугадываемый, этого достаточно для личного сервера.
    # Чистим job_id так же, как при удалении: только буквы/цифры, чтобы нельзя
    # было выйти за пределы папки загрузок.
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    job_dir = DOWNLOAD_DIR / safe

    # Пока задача в работе — отдавать нечего: ранняя выдача давала битый/
    # недокачанный файл («чёрный экран с кодом» в браузере).
    with JOBS_LOCK:
        job = JOBS.get(safe)
        state = job.get("state") if job else None
    if state in ("queued", "downloading", "processing"):
        raise HTTPException(409, "Файл ещё скачивается — дождитесь завершения")

    # Ищем готовый файл на диске, а не в памяти процесса: так файл можно
    # скачать даже после перезапуска сервиса (когда список задач в памяти пуст).
    result = _result_file(job_dir)
    if not result:
        raise HTTPException(404, "Файл не найден")
    return FileResponse(
        result, filename=result.name, media_type="application/octet-stream")


# --- Торренты (aria2 + поиск по публичному трекеру) ------------------------
# Фаза 1: нашёл раздачу (или вставил magnet) → aria2c качает её на сервер в ту
# же папку DOWNLOAD_DIR/<job_id>/. Готовый файл попадает в общий список
# (/api/files) и отдаётся теми же ручками, что и yt-dlp-загрузки — торрент-
# задача живёт в тех же JOBS, поэтому /api/status, /api/cancel, /api/delete,
# /api/file и автоочистка работают для неё без изменений. Просмотр — после
# докачки (стриминг во время загрузки — отдельный этап).
# ⚠️ Только личный просмотр легального контента (дистрибутивы и т.п.).

ARIA2_BIN = os.environ.get("ARIA2_BIN", "aria2c")
# Хост публичного трекера для поиска. Через env — на случай смены зеркала.
TRACKER_BASE = os.environ.get("TRACKER_BASE", "https://1337x.to").rstrip("/")
TORRENT_HTTP_TIMEOUT = 20  # секунд на один запрос к трекеру
MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:[a-zA-Z0-9]+", re.I)


def _tracker_get(url: str) -> str:
    """GET страницы трекера с браузерным User-Agent (без него часто 403)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=TORRENT_HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _search_torrents(query: str) -> list:
    """Скрейпит страницу поиска 1337x. Возвращает список раздач с путём к
    странице раздачи (detail) — magnet берём лениво при добавлении, чтобы
    поиск не делал по запросу на каждую строку. ⚠️ Хрупкая часть: трекер
    может сменить вёрстку/уйти за Cloudflare — тогда падаем в 502, а
    пользователь вставляет magnet вручную."""
    page = _tracker_get(f"{TRACKER_BASE}/search/{quote(query)}/1/")
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        m = re.search(r'href="(/torrent/\d+/[^"]+/)"[^>]*>([^<]+)</a>', row)
        if not m:
            continue
        seeds = re.search(r'coll-2 seeds[^>]*>(\d+)', row)
        leech = re.search(r'coll-3 leeches[^>]*>(\d+)', row)
        size = re.search(r'coll-4 size[^>]*>([\d.,]+\s*[KMGTP]?i?B)', row)
        out.append({
            "title": html.unescape(m.group(2)).strip(),
            "detail": m.group(1),
            "size": size.group(1).strip() if size else "?",
            "seeders": int(seeds.group(1)) if seeds else 0,
            "leechers": int(leech.group(1)) if leech else 0,
        })
        if len(out) >= 30:
            break
    out.sort(key=lambda x: x["seeders"], reverse=True)  # больше сидов — выше
    return out


def _resolve_magnet(detail: str) -> str:
    """Достаёт magnet-ссылку со страницы конкретной раздачи 1337x."""
    if not detail.startswith("/torrent/"):
        raise HTTPException(400, "Некорректная ссылка на раздачу")
    page = _tracker_get(f"{TRACKER_BASE}{detail}")
    m = re.search(r'href="(magnet:\?xt=urn:btih:[^"]+)"', page)
    if not m:
        raise HTTPException(502, "Не удалось получить magnet-ссылку раздачи")
    return html.unescape(m.group(1))


def _bytes_from_aria2(s: str):
    """'5.2MiB' → байты (для speed/размера из вывода aria2c). None если не разобрать."""
    m = re.match(r"([\d.]+)\s*([KMGTP]?)i?B", s, re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
            "T": 1024 ** 4, "P": 1024 ** 5}
    return int(float(m.group(1)) * mult.get(m.group(2).upper(), 1))


def _seconds_from_aria2(s: str):
    """'3m30s' / '1h2m' / '45s' → секунды. None если пусто."""
    total = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u]
                for n, u in re.findall(r"(\d+)([hms])", s))
    return total or None


def _run_torrent(job_id: str, magnet: str):
    """Качает magnet через aria2c в папку задачи, обновляя те же поля JOBS,
    что и _run_download (state/percent/speed/eta/total) — чтобы фронтовый
    poll() работал без изменений. Уважает job['cancel']."""
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        ARIA2_BIN,
        "--dir", str(job_dir),
        "--seed-time=0",              # не раздаём после докачки — сразу выходим
        "--summary-interval=1",       # строка прогресса раз в секунду
        "--console-log-level=warn",
        "--file-allocation=none",     # не преаллоцируем весь размер на диск
        "--bt-stop-timeout=300",      # 5 мин без пиров — прекращаем
        "--follow-torrent=mem",
        magnet,
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
    except FileNotFoundError:
        with JOBS_LOCK:
            JOBS[job_id].update(
                state="error",
                error="aria2 не установлен на сервере (нужен apt install aria2).")
        return

    with JOBS_LOCK:
        JOBS[job_id]["state"] = "downloading"

    try:
        for line in proc.stdout:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job or job.get("cancel"):
                    break
            m = re.search(r"\((\d+)%\)", line)
            if not m:
                continue
            spd = re.search(r"DL:([\d.]+\s*[KMGTP]?i?B)", line)
            eta = re.search(r"ETA:([0-9hms]+)", line)
            tot = re.search(r"/([\d.]+\s*[KMGTP]?i?B)\(", line)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job and not job.get("cancel"):
                    job.update(
                        state="downloading",
                        percent=int(m.group(1)),
                        speed=_bytes_from_aria2(spd.group(1)) if spd else None,
                        eta=_seconds_from_aria2(eta.group(1)) if eta else None,
                        total=_bytes_from_aria2(tot.group(1)) if tot else None)

        with JOBS_LOCK:
            cancelled = JOBS.get(job_id, {}).get("cancel")
        if cancelled:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            shutil.rmtree(job_dir, ignore_errors=True)
            with JOBS_LOCK:
                JOBS[job_id].update(state="cancelled", filename=None)
            return

        proc.wait()
        result = _result_file(job_dir)
        if not result:
            # Многофайловая раздача: aria2 кладёт файлы в подпапку с именем
            # торрента, а _result_file/api/files смотрят только верхний уровень.
            # Поднимаем самый большой не-временный файл (видео) наверх.
            nested = [p for p in job_dir.rglob("*")
                      if p.is_file() and not _is_temp(p)]
            if nested:
                biggest = max(nested, key=lambda p: p.stat().st_size)
                target = job_dir / biggest.name
                if biggest != target:
                    biggest.replace(target)
                result = target
        if not result:
            raise RuntimeError("aria2 не скачал файл (нет пиров или битый magnet)")
        with JOBS_LOCK:
            JOBS[job_id].update(
                state="done", percent=100,
                filename=result.name, size=result.stat().st_size)
    except Exception as e:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass
        with JOBS_LOCK:
            cancelled = JOBS.get(job_id, {}).get("cancel")
        if cancelled:
            shutil.rmtree(job_dir, ignore_errors=True)
            with JOBS_LOCK:
                JOBS[job_id].update(state="cancelled", filename=None)
        else:
            traceback.print_exc()
            with JOBS_LOCK:
                JOBS[job_id].update(state="error", error=str(e)[:200])


class TorrentRequest(BaseModel):
    magnet: str = ""   # прямая magnet-ссылка (ручной ввод)
    detail: str = ""   # ИЛИ путь к раздаче из результатов поиска


@app.get("/api/torrent/search", dependencies=[Depends(check_password)])
def torrent_search(q: str):
    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(400, "Слишком короткий запрос")
    if len(q) > 200:
        raise HTTPException(400, "Слишком длинный запрос")
    try:
        return {"results": _search_torrents(q)}
    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise HTTPException(
            502, "Поиск сейчас недоступен (трекер не ответил или сменил "
                 "вёрстку). Можно вставить magnet-ссылку вручную.")


@app.post("/api/torrent", dependencies=[Depends(check_password)])
def torrent_add(req: TorrentRequest):
    magnet = (req.magnet or "").strip()
    if not magnet and req.detail:
        magnet = _resolve_magnet(req.detail.strip())
    if not MAGNET_RE.match(magnet):
        raise HTTPException(400, "Нужна корректная magnet-ссылка")
    if _count_active(JOBS, JOBS_LOCK,
                     ("queued", "downloading", "processing")) >= JOB_CEILING:
        raise HTTPException(429, "Сейчас слишком много загрузок — попробуйте позже.")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "queued", "percent": None, "speed": None,
            "eta": None, "title": None, "filename": None, "error": None,
            "cancel": False, "total": None, "size": None,
        }
    threading.Thread(
        target=_run_torrent, args=(job_id, magnet), daemon=True).start()
    return {"job_id": job_id}


# --- Библиотека: файлы в SMB-витрине (постоянное хранилище для ТВ) ---------
# В отличие от downloads/ (рабочая папка, чистится через RETENTION_HOURS),
# витрина не удаляется автоматически — это библиотека. Даём ей управление
# с сайта: список + скачать + удалить.

def _safe_share_file(name: str) -> Path:
    """Путь к файлу витрины по имени с защитой от выхода за пределы папки
    (path traversal). Берём только имя файла и проверяем, что итог внутри
    SHARE_PATH."""
    if not SHARE_PATH:
        raise HTTPException(404, "Витрина (SMB) не настроена")
    fname = os.path.basename((name or "").strip())
    if not fname or fname in (".", ".."):
        raise HTTPException(400, "Некорректное имя файла")
    base = SHARE_PATH.resolve()
    target = (base / fname).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(400, "Некорректное имя файла")
    return target


class LibraryAddRequest(BaseModel):
    job_id: str


@app.post("/api/library/add", dependencies=[Depends(check_password)])
def library_add(req: LibraryAddRequest):
    """Копирует готовый файл задачи в SMB-витрину (постоянное хранилище).
    Вызывается кнопкой «В библиотеку» на сайте."""
    if not SHARE_PATH:
        raise HTTPException(400, "Библиотека (SMB) не настроена")
    # job_id из тела запроса — оставляем только буквы/цифры, чтобы нельзя
    # было выйти за пределы папки загрузок.
    safe = re.sub(r"[^a-zA-Z0-9]", "", req.job_id)
    result = _result_file(DOWNLOAD_DIR / safe)
    if not result:
        raise HTTPException(404, "Файл не найден")
    try:
        name = _publish_to_share(result)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(500, "Не удалось добавить в библиотеку: " + str(e))
    return {"ok": True, "name": name}


@app.get("/api/library", dependencies=[Depends(check_password)])
def library_list():
    """Список файлов в SMB-витрине. enabled=False — витрина не настроена."""
    if not SHARE_PATH:
        return {"enabled": False, "files": []}
    items = []
    if SHARE_PATH.exists():
        for p in SHARE_PATH.iterdir():
            if not p.is_file() or _is_temp(p):
                continue
            st = p.stat()
            items.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)  # новые сверху
    return {"enabled": True, "files": items}


@app.get("/api/library/file")
def library_file(name: str):
    # Прямая ссылка для браузера — без пароля в заголовке (как /api/file).
    # Имена файлов видны только из защищённого паролем списка /api/library.
    target = _safe_share_file(name)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(
        target, filename=target.name, media_type="application/octet-stream")


class LibraryDeleteRequest(BaseModel):
    name: str


@app.post("/api/library/delete", dependencies=[Depends(check_password)])
def library_delete(req: LibraryDeleteRequest):
    target = _safe_share_file(req.name)
    if target.exists() and target.is_file():
        try:
            target.unlink()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, "Не удалось удалить файл: " + str(e))
    return {"ok": True}


@app.get("/api/config")
def config():
    # Фронтенду нужно знать, спрашивать ли пароль.
    return {"password_required": bool(ACCESS_PASSWORD)}


# --- Рекорд тетриса (один глобальный рекорд за всё время) ------------------
# Минимально, без БД: один int в JSON-файле. Общий для всех, кто заходит.
BEST_FILE = Path(os.environ.get(
    "TETRIS_BEST_FILE", BASE_DIR / "data" / "tetris_best.json"))
BEST_FILE.parent.mkdir(parents=True, exist_ok=True)
BEST_LOCK = threading.Lock()
TETRIS_SCORE_CAP = 1_000_000  # выше — считаем мусором/читом и игнорируем


def _read_best() -> int:
    try:
        return int(json.loads(BEST_FILE.read_text()).get("best", 0))
    except Exception:
        return 0


class TetrisScore(BaseModel):
    score: int


@app.get("/api/tetris/best", dependencies=[Depends(check_password)])
def tetris_best():
    return {"best": _read_best()}


@app.post("/api/tetris/best", dependencies=[Depends(check_password)])
def tetris_best_submit(payload: TetrisScore):
    s = payload.score
    if not isinstance(s, int) or s < 0 or s > TETRIS_SCORE_CAP:
        return {"best": _read_best()}          # мусор — просто отдаём текущий
    with BEST_LOCK:
        cur = _read_best()
        if s > cur:
            BEST_FILE.write_text(json.dumps({"best": s}))
            cur = s
    return {"best": cur}


# --- Транскрибация ---------------------------------------------------------
# Сначала пытаемся взять готовые субтитры (быстро, без нагрузки на сервер).
# Если их нет — скачиваем аудио и распознаём локально через faster-whisper
# (модель Whisper на CPU, полностью бесплатно).

# Имя модели Whisper: tiny / base / small / medium. base — компромисс
# скорость/качество на CPU. small точнее для русского, но медленнее.
# Быстрый режим — WHISPER_MODEL (по умолчанию base), точный («свои субтитры»)
# — WHISPER_MODEL_ACCURATE (по умолчанию small): заметно лучше на русском и
# на плохом звуке, ценой скорости.
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
WHISPER_MODEL_ACCURATE = os.environ.get("WHISPER_MODEL_ACCURATE", "small")

# Кэш загруженных моделей по имени (быстрая и точная живут одновременно).
_whisper_models: dict[str, object] = {}
_whisper_lock = threading.Lock()

# Задачи транскрибации в памяти: job_id -> dict(state, percent, text, source, error)
TJOBS: dict[str, dict] = {}
TJOBS_LOCK = threading.Lock()

# Ограничиваем число одновременных распознаваний Whisper — оно тяжёлое для CPU.
# Лишние задачи ждут своей очереди на этом семафоре (а не валят сервер).
TRANSCRIBE_SEM = threading.Semaphore(MAX_ACTIVE_TRANSCRIBE)


def _tset(job_id: str, **kw):
    """Короткое обновление полей задачи транскрибации под локом."""
    with TJOBS_LOCK:
        j = TJOBS.get(job_id)
        if j:
            j.update(**kw)


def _tcancelled(job_id: str) -> bool:
    with TJOBS_LOCK:
        j = TJOBS.get(job_id)
        return bool(j and j.get("cancel"))


def _get_whisper(model_name: str):
    """Лениво загружаем модель Whisper один раз на имя (на CPU, int8).
    Первый вызов скачает веса модели с HuggingFace (нужен интернет)."""
    with _whisper_lock:
        model = _whisper_models.get(model_name)
        if model is None:
            from faster_whisper import WhisperModel
            model = WhisperModel(
                model_name, device="cpu", compute_type="int8")
            _whisper_models[model_name] = model
        return model


def _parse_vtt_time(t: str) -> float:
    """'00:01:02.345' / '01:02.345' / '00:01:02,345' → секунды (float)."""
    t = t.strip().replace(",", ".")
    try:
        parts = [float(p) for p in t.split(":")]
    except ValueError:
        return 0.0
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + s


_VTT_TS = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s*-->\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})")


def _clean_cue(line: str) -> str:
    """Чистим строку субтитра: убираем теги <c>/<00:..>, декодируем сущности."""
    line = re.sub(r"<[^>]+>", "", line)
    line = html.unescape(line.replace("&nbsp;", " "))
    return line.strip()


def _vtt_to_segments(raw: str) -> list[dict]:
    """VTT/SRT → список сегментов [{start, end, text}] с таймкодами.
    Схлопываем подряд идущие дубли (авто-субтитры «прокручиваются» и повторяют
    одну и ту же строку от кадра к кадру): такой повтор просто продлевает
    предыдущий сегмент, а не плодит новые строки."""
    segs: list[dict] = []
    for block in re.split(r"\n[ \t]*\n", raw):
        lines = block.strip("\n").splitlines()
        ts_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_idx is None:
            continue
        m = _VTT_TS.search(lines[ts_idx])
        if not m:
            continue
        start, end = _parse_vtt_time(m.group(1)), _parse_vtt_time(m.group(2))
        text = " ".join(
            c for c in (_clean_cue(ln) for ln in lines[ts_idx + 1:]) if c
        ).strip()
        if not text:
            continue
        if segs and segs[-1]["text"] == text:
            segs[-1]["end"] = end        # тот же текст — просто продлеваем
            continue
        segs.append({"start": start, "end": end, "text": text})
    return segs


def _segments_to_text(segs: list[dict]) -> str:
    """Сплошной текст из сегментов (для копирования/поиска)."""
    return "\n".join(s["text"] for s in segs if s.get("text")).strip()


def _pick_sub(subs: dict, langs):
    """Из словаря субтитров yt-dlp выбираем (lang, url) для VTT по приоритету
    языков. Если точных совпадений нет — берём любой доступный язык."""
    if not subs:
        return None
    order = [l for l in langs if l in subs]
    order += [l for l in subs if l not in order]
    for lang in order:
        for fmt in subs.get(lang) or []:
            if fmt.get("ext") == "vtt" and fmt.get("url"):
                return lang, fmt["url"]
    return None


def _try_subtitles(url: str, job_dir: Path, lang: str):
    """Берём готовые/авто-субтитры одним запросом и качаем ровно один файл —
    так не спамим запросами (иначе YouTube отвечает 429) и не падаем на
    отсутствующем языке. Любая осечка → None, тогда отработает Whisper."""
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "socket_timeout": 30, "retries": 2,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            # Приоритет языков: явно заданный → оригинальный язык видео →
            # ru → en → любой доступный (фолбэк внутри _pick_sub).
            # info["language"] — оригинальный язык ролика, чтобы не хватать
            # авто-перевод вместо родной дорожки.
            langs = []
            for l in ([lang] if lang else []) + [info.get("language"), "ru", "en"]:
                if l and l not in langs:
                    langs.append(l)
            # Сначала «ручные» субтитры, потом автоматические.
            pick = (_pick_sub(info.get("subtitles"), langs)
                    or _pick_sub(info.get("automatic_captions"), langs))
            if not pick:
                return None
            raw = ydl.urlopen(pick[1]).read().decode("utf-8", "ignore")
        return _vtt_to_segments(raw) or None
    except Exception:
        traceback.print_exc()
        return None


def _whisper_transcribe(url: str, job_dir: Path, lang: str, job_id: str,
                        quality: str = "fast") -> list[dict]:
    """Скачиваем аудио и распознаём его локально через Whisper.
    Возвращаем сегменты [{start, end, text}] с таймкодами."""
    # Для распознавания важен только звук, качество видео не нужно. Поэтому
    # берём отдельную аудио-дорожку, а если её нет — САМЫЙ ЛЁГКИЙ поток со
    # звуком (worst), а не best: на сайтах без audio-only это превращает
    # закачку из гигабайтов видео в десятки МБ.
    audio_opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "format": "bestaudio/worstaudio/worst",
        "outtmpl": str(job_dir / "audio.%(ext)s"),
        "socket_timeout": 30, "retries": 3,
    }
    _tset(job_id, stage="Скачиваю аудио…")
    with yt_dlp.YoutubeDL(audio_opts) as ydl:
        meta = ydl.extract_info(url, download=True)

    audios = [p for p in job_dir.iterdir()
              if p.is_file() and p.name.startswith("audio.")]
    if not audios:
        raise RuntimeError("Не удалось скачать аудио для распознавания")
    audio = max(audios, key=lambda p: p.stat().st_size)
    duration = meta.get("duration") or 0

    if WHISPER_MAX_MINUTES and duration > WHISPER_MAX_MINUTES * 60:
        raise RuntimeError(
            f"Видео длиннее {int(WHISPER_MAX_MINUTES)} мин — распознавание речью "
            "для такой длины отключено. Попробуйте видео с готовыми субтитрами.")

    accurate = quality == "accurate"
    model_name = WHISPER_MODEL_ACCURATE if accurate else WHISPER_MODEL_NAME
    _tset(job_id, stage="Загружаю модель…")
    model = _get_whisper(model_name)

    # Два режима:
    #  • fast — жадный поиск (beam_size=1) на модели base: быстро, чуть грубее.
    #  • accurate («свои субтитры») — лучевой поиск (beam_size=5) на модели
    #    small + учёт предыдущего текста и мягкий VAD: точнее на русском и на
    #    плохом звуке, но ощутимо медленнее. vad_filter в обоих режимах
    #    пропускает тишину/музыку — на роликах с паузами это большой выигрыш.
    if accurate:
        tr_kwargs = dict(
            beam_size=5, best_of=5, temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            condition_on_previous_text=True, vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500))
    else:
        tr_kwargs = dict(beam_size=1, vad_filter=True)

    _tset(job_id, stage="Распознаю речь")
    segments, _ = model.transcribe(
        str(audio), language=lang or None, **tr_kwargs)

    out = []
    for seg in segments:           # генератор: сам прогон идёт здесь
        if _tcancelled(job_id):    # пользователь нажал «Остановить»
            raise _Cancelled()
        text = (seg.text or "").strip()
        if text:
            out.append({"start": seg.start, "end": seg.end, "text": text})
        if duration:
            pct = min(99, round(seg.end / duration * 100))
            _tset(job_id, percent=pct)
    return out


def _transcribe_error(e) -> str:
    if isinstance(e, ModuleNotFoundError) and "faster_whisper" in str(e):
        return ("Для распознавания видео без субтитров нужно установить "
                "faster-whisper на сервере: pip install faster-whisper")
    return friendly_error(e)


def _run_transcribe(job_id: str, url: str, lang: str,
                    force_whisper: bool = False, quality: str = "fast"):
    job_dir = DOWNLOAD_DIR / ("t_" + job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        segments = None
        source = "whisper"

        # Готовые субтитры пропускаем, если пользователь явно попросил «свои
        # субтитры» (force_whisper): встроенные авто-субтитры часто плохие.
        if not force_whisper:
            with TJOBS_LOCK:
                TJOBS[job_id].update(state="subtitles")
            segments = _try_subtitles(url, job_dir, lang)
            if segments:
                source = "subtitles"

        if not segments:
            # Whisper тяжёлый — ждём своей очереди на семафоре,
            # пока показываем «в очереди».
            with TJOBS_LOCK:
                TJOBS[job_id].update(state="queued")
            with TRANSCRIBE_SEM:
                with TJOBS_LOCK:
                    if TJOBS[job_id].get("cancel"):
                        raise _Cancelled()
                    TJOBS[job_id].update(
                        state="transcribing", percent=0, stage="Готовлю…")
                segments = _whisper_transcribe(
                    url, job_dir, lang, job_id, quality)
            source = "whisper"

        if not segments:
            raise RuntimeError("Не удалось получить текст из этого видео")

        with TJOBS_LOCK:
            TJOBS[job_id].update(
                state="done", percent=100, text=_segments_to_text(segments),
                segments=segments, source=source)
    except _Cancelled:
        with TJOBS_LOCK:
            TJOBS[job_id].update(state="cancelled", text=None)
    except Exception as e:
        traceback.print_exc()
        with TJOBS_LOCK:
            TJOBS[job_id].update(state="error", error=_transcribe_error(e))
    finally:
        # Аудио и субтитры — временные, текст уже в памяти задачи.
        shutil.rmtree(job_dir, ignore_errors=True)


class TranscribeRequest(BaseModel):
    url: str
    lang: str = ""              # пусто = автоопределение языка
    force_whisper: bool = False  # «свои субтитры»: игнорировать встроенные
    quality: str = "fast"        # fast | accurate


@app.post("/api/transcribe", dependencies=[Depends(check_password)])
def transcribe(req: TranscribeRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Пустая ссылка")
    check_url_safe(url)
    if _count_active(TJOBS, TJOBS_LOCK,
                     ("queued", "subtitles", "transcribing")) >= TJOB_CEILING:
        raise HTTPException(429, "Сейчас слишком много задач распознавания — "
                                 "попробуйте позже.")
    quality = "accurate" if req.quality == "accurate" else "fast"
    job_id = uuid.uuid4().hex[:12]
    with TJOBS_LOCK:
        TJOBS[job_id] = {
            "state": "queued", "percent": None, "stage": None,
            "text": None, "segments": None, "source": None,
            "error": None, "cancel": False,
        }
    threading.Thread(
        target=_run_transcribe,
        args=(job_id, url, req.lang.strip(), bool(req.force_whisper), quality),
        daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/transcribe/status/{job_id}", dependencies=[Depends(check_password)])
def transcribe_status(job_id: str):
    with TJOBS_LOCK:
        job = TJOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        return dict(job, job_id=job_id)


@app.post("/api/transcribe/cancel/{job_id}", dependencies=[Depends(check_password)])
def transcribe_cancel(job_id: str):
    with TJOBS_LOCK:
        job = TJOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        if job.get("state") in ("done", "error", "cancelled"):
            return {"ok": True, "state": job["state"]}
        job["cancel"] = True
    return {"ok": True}


# --- Раздача фронтенда -----------------------------------------------------
# Должно идти последним, чтобы не перехватывать /api/*
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
