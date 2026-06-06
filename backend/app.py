"""
Мини-сайт для скачивания видео по ссылке.
Бэкенд: FastAPI + yt-dlp. Отдаёт фронтенд и REST API.

Запуск (локально):  uvicorn app:app --host 0.0.0.0 --port 8000
"""
import os
import re
import time
import uuid
import shutil
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

# --- Настройки -------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Необязательный пароль. Если переменная окружения DOWNLOADER_PASSWORD задана,
# то фронтенд должен присылать её в заголовке X-Access-Password.
ACCESS_PASSWORD = os.environ.get("DOWNLOADER_PASSWORD", "").strip()

# Через сколько часов после скачивания удалять файлы (по умолчанию 3 часа).
RETENTION_SECONDS = int(float(os.environ.get("RETENTION_HOURS", "3")) * 3600)
CLEANUP_INTERVAL = 600  # как часто проверять папку, секунд (10 минут)

app = FastAPI(title="Video Downloader")

# Хранилище задач скачивания в памяти процесса.
# job_id -> dict(state, percent, speed, eta, title, filename, error)
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


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
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                job["state"] = "downloading"
                job["percent"] = round(done / total * 100, 1) if total else None
                job["speed"] = d.get("speed")
                job["eta"] = d.get("eta")
            elif d["status"] == "finished":
                # видео скачано, дальше может идти склейка/конвертация
                job["state"] = "processing"
                job["percent"] = 100

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

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        files = [p for p in job_dir.iterdir() if p.is_file()]
        if not files:
            raise RuntimeError("Файл не найден после скачивания")
        result = max(files, key=lambda p: p.stat().st_size)
        with JOBS_LOCK:
            JOBS[job_id].update(
                state="done", percent=100, filename=result.name)
    except Exception as e:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id].update(state="error", error=friendly_error(e))


@app.post("/api/download", dependencies=[Depends(check_password)])
def download(req: DownloadRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "Пустая ссылка")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "queued", "percent": None, "speed": None,
            "eta": None, "title": None, "filename": None, "error": None,
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


@app.get("/api/file/{job_id}")
def get_file(job_id: str):
    # Файл отдаём без пароля в заголовке — браузер скачивает по прямой ссылке.
    # job_id случайный и неугадываемый, этого достаточно для личного сервера.
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("state") != "done" or not job.get("filename"):
        raise HTTPException(404, "Файл не готов")
    path = DOWNLOAD_DIR / job_id / job["filename"]
    if not path.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(
        path, filename=job["filename"], media_type="application/octet-stream")


@app.get("/api/config")
def config():
    # Фронтенду нужно знать, спрашивать ли пароль.
    return {"password_required": bool(ACCESS_PASSWORD)}


# --- Раздача фронтенда -----------------------------------------------------
# Должно идти последним, чтобы не перехватывать /api/*
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
