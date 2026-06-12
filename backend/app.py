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
import socket
import ipaddress
import threading
import traceback
from pathlib import Path
from urllib.parse import urlparse

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

        files = [p for p in job_dir.iterdir() if p.is_file()]
        if not files:
            raise RuntimeError("Файл не найден после скачивания")
        result = max(files, key=lambda p: p.stat().st_size)
        with JOBS_LOCK:
            JOBS[job_id].update(
                state="done", percent=100,
                filename=result.name, size=result.stat().st_size)
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
            files = [p for p in d.iterdir() if p.is_file()]
            if not files:
                continue
            result = max(files, key=lambda p: p.stat().st_size)
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


# --- Транскрибация ---------------------------------------------------------
# Сначала пытаемся взять готовые субтитры (быстро, без нагрузки на сервер).
# Если их нет — скачиваем аудио и распознаём локально через faster-whisper
# (модель Whisper на CPU, полностью бесплатно).

# Имя модели Whisper: tiny / base / small / medium. base — компромисс
# скорость/качество на CPU. small точнее для русского, но медленнее.
WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")

_whisper_model = None
_whisper_lock = threading.Lock()

# Задачи транскрибации в памяти: job_id -> dict(state, percent, text, source, error)
TJOBS: dict[str, dict] = {}
TJOBS_LOCK = threading.Lock()

# Ограничиваем число одновременных распознаваний Whisper — оно тяжёлое для CPU.
# Лишние задачи ждут своей очереди на этом семафоре (а не валят сервер).
TRANSCRIBE_SEM = threading.Semaphore(MAX_ACTIVE_TRANSCRIBE)


def _get_whisper():
    """Лениво загружаем модель Whisper один раз (на CPU, int8).
    Первый вызов скачает веса модели с HuggingFace (нужен интернет)."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel(
                WHISPER_MODEL_NAME, device="cpu", compute_type="int8")
        return _whisper_model


def _vtt_to_text(raw: str) -> str:
    """Превращает VTT/SRT-субтитры в сплошной текст: убираем тайм-коды,
    служебные строки и теги, схлопываем подряд идущие повторы (авто-субтитры
    часто дублируют строки от кадра к кадру)."""
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.isdigit():
            continue
        if line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        line = re.sub(r"<[^>]+>", "", line)        # <c>, <00:00:01.000> и пр.
        line = line.replace("&nbsp;", " ").strip()
        if not line:
            continue
        if not out or out[-1] != line:
            out.append(line)
    return "\n".join(out).strip()


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
        return _vtt_to_text(raw) or None
    except Exception:
        traceback.print_exc()
        return None


def _whisper_transcribe(url: str, job_dir: Path, lang: str, job_id: str) -> str:
    """Скачиваем аудио и распознаём его локально через Whisper."""
    audio_opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(job_dir / "audio.%(ext)s"),
        "socket_timeout": 30, "retries": 3,
    }
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

    model = _get_whisper()
    segments, _ = model.transcribe(str(audio), language=lang or None)

    parts = []
    for seg in segments:           # генератор: сам прогон идёт здесь
        parts.append(seg.text.strip())
        if duration:
            pct = min(99, round(seg.end / duration * 100))
            with TJOBS_LOCK:
                j = TJOBS.get(job_id)
                if j:
                    j["percent"] = pct
    return " ".join(p for p in parts if p).strip()


def _transcribe_error(e) -> str:
    if isinstance(e, ModuleNotFoundError) and "faster_whisper" in str(e):
        return ("Для распознавания видео без субтитров нужно установить "
                "faster-whisper на сервере: pip install faster-whisper")
    return friendly_error(e)


def _run_transcribe(job_id: str, url: str, lang: str):
    job_dir = DOWNLOAD_DIR / ("t_" + job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        with TJOBS_LOCK:
            TJOBS[job_id].update(state="subtitles")
        text = _try_subtitles(url, job_dir, lang)
        source = "subtitles"

        if not text:
            # Whisper тяжёлый — ждём своей очереди на семафоре,
            # пока показываем «в очереди».
            with TJOBS_LOCK:
                TJOBS[job_id].update(state="queued")
            with TRANSCRIBE_SEM:
                with TJOBS_LOCK:
                    TJOBS[job_id].update(state="transcribing", percent=0)
                text = _whisper_transcribe(url, job_dir, lang, job_id)
            source = "whisper"

        if not text:
            raise RuntimeError("Не удалось получить текст из этого видео")

        with TJOBS_LOCK:
            TJOBS[job_id].update(
                state="done", percent=100, text=text, source=source)
    except Exception as e:
        traceback.print_exc()
        with TJOBS_LOCK:
            TJOBS[job_id].update(state="error", error=_transcribe_error(e))
    finally:
        # Аудио и субтитры — временные, текст уже в памяти задачи.
        shutil.rmtree(job_dir, ignore_errors=True)


class TranscribeRequest(BaseModel):
    url: str
    lang: str = ""   # пусто = автоопределение языка


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
    job_id = uuid.uuid4().hex[:12]
    with TJOBS_LOCK:
        TJOBS[job_id] = {
            "state": "queued", "percent": None,
            "text": None, "source": None, "error": None,
        }
    threading.Thread(
        target=_run_transcribe, args=(job_id, url, req.lang.strip()),
        daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/transcribe/status/{job_id}", dependencies=[Depends(check_password)])
def transcribe_status(job_id: str):
    with TJOBS_LOCK:
        job = TJOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        return dict(job, job_id=job_id)


# --- Раздача фронтенда -----------------------------------------------------
# Должно идти последним, чтобы не перехватывать /api/*
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
