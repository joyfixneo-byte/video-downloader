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
import mimetypes
import threading
import traceback
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, quote, unquote_plus

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
# сюда плоским списком с человекочитаемым именем. Для торрентов файлы из
# раздачи публикуются туда же автоматически по мере докачки каждого файла
# (см. _publish_newly_done) — кнопка остаётся для обычных yt-dlp-загрузок
# и как ручной повтор. Пусто — раздел библиотеки скрыт.
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

    Папки торрент-задач (помечены файлом .torrent_job) не трогаем вообще —
    их удаляют только вручную кнопкой «Удалить». Маркер на диске, а не в
    JOBS, потому что JOBS — память процесса и теряется при перезапуске.
    """
    while True:
        try:
            now = time.time()
            if DOWNLOAD_DIR.exists():
                for d in DOWNLOAD_DIR.iterdir():
                    if not d.is_dir() or d.name == ".meta":
                        continue
                    # Торренты: если библиотека (SMB) есть — они туда уезжают
                    # и удаляются сами (_drain_torrent_to_library), а всё, что
                    # осталось на сервере, — это НЕ уехавшее в библиотеку, его
                    # автоматически не трогаем. Если библиотеки нет — постоянного
                    # хранилища тоже нет, поэтому чистим их по общему retention,
                    # чтобы диск VM не забивался.
                    if (d / ".torrent_job").exists() and SHARE_PATH is not None:
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
    """Временный/недокачанный файл yt-dlp, либо служебный кэш remux для
    плеера (см. _play_cache_path) — не показываем как готовый файл раздачи
    и не отдаём на скачивание/публикацию в библиотеку (иначе размер
    «прыгает», а в браузере вместо файла открывается мусор)."""
    n = p.name.lower()
    return (n.endswith((".part", ".ytdl", ".tmp", ".temp", ".download", ".aria2"))
            or ".part-frag" in n
            or (n.startswith(".") and n.endswith(".play.mp4")))


def _file_done(p: Path, expected_size: int = None) -> bool:
    """Конкретный файл раздачи полностью докачан.

    С известным ожидаемым размером (из .torrent-метаданных, см.
    _torrent_expected_sizes) сравниваем напрямую с ним — надёжно для любой
    раздачи. Без него — запасная эвристика по соседнему `<файл>.aria2`
    (годится только для одиночного файла).

    ⚠️ Раньше эвристика по .aria2 была единственной проверкой везде. Для
    многофайловой BT-раздачи aria2 ведёт ОДИН служебный `<имя-раздачи>.aria2`
    на весь торрент (лежит рядом с папкой раздачи, не по файлу на каждую
    серию) — так что проверка соседнего файла никогда не находила «свой»
    .aria2 и считала готовым любой файл, едва он появился на диске, даже
    если реально докачано только несколько мегабайт (не хватило пиров на
    конкретную серию — торрент завершается, файл остаётся обрезанным). Из-за
    этого обрезанные файлы уходили в SMB-библиотеку как «готовые»."""
    if not p.is_file() or _is_temp(p):
        return False
    if expected_size is not None:
        return p.stat().st_size == expected_size
    return not p.with_name(p.name + ".aria2").exists()


def _torrent_expected_sizes(job_dir: Path) -> dict:
    """path (как в _torrent_file_list) -> ожидаемый размер файла из
    сохранённых .torrent-метаданных задачи. Пусто, если метаданных нет
    (старая раздача без .meta.torrent, добавленная до этой проверки)."""
    meta = job_dir / ".meta.torrent"
    if not meta.is_file():
        return {}
    try:
        return {f["path"]: f["size"] for f in _torrent_file_list(meta)}
    except Exception:
        return {}


def _real_job_files(job_dir: Path) -> list[Path]:
    """Все настоящие готовые файлы задачи на диске: без временных/ещё
    качающихся (см. _file_done) и служебных (маркер задачи, список выбранных
    файлов, сохранённые .torrent-метаданные) — и независимо от вложенности
    (aria2 кладёт многофайловые торренты в подпапку с именем раздачи)."""
    if not job_dir.exists() or not job_dir.is_dir():
        return []
    sizes = _torrent_expected_sizes(job_dir)
    out = []
    for p in job_dir.rglob("*"):
        if p.suffix.lower() == ".torrent" or p.name in (".selected", ".torrent_job"):
            continue
        rel = str(p.relative_to(job_dir)).replace("\\", "/")
        expected = sizes.get(rel, sizes.get(p.name))
        if _file_done(p, expected):
            out.append(p)
    return out


def _result_file(job_dir: Path):
    """Готовый файл задачи на диске: самый большой среди настоящих (см.
    _real_job_files) — представитель задачи для карточки/скачивания одним
    файлом. Раньше смотрели только верхний уровень папки без фильтра служебных
    файлов — если реальное видео ещё в подпапке (например, сервис
    перезапустили посреди докачки), результатом становился пустой
    файл-маркер. None — если готового файла ещё/уже нет."""
    files = _real_job_files(job_dir)
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_size)


def _resolve_ready_file(job_dir: Path, path: str = "") -> Path:
    """Резолвит готовый файл задачи — либо конкретный файл многофайловой
    раздачи (path, с защитой от выхода за пределы папки задачи), либо
    главный результат задачи. Общая логика для /api/file и /api/play —
    проверка path traversal должна жить в одном месте."""
    if path:
        base = job_dir.resolve()
        target = (job_dir / path).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            raise HTTPException(400, "Некорректный путь")
        rel = str(target.relative_to(base)).replace("\\", "/")
        sizes = _torrent_expected_sizes(job_dir)
        expected = sizes.get(rel, sizes.get(target.name))
        if not _file_done(target, expected):
            raise HTTPException(409, "Файл ещё скачивается — дождитесь завершения")
        return target
    result = _result_file(job_dir)
    if not result:
        raise HTTPException(404, "Файл не найден")
    return result


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

    Вызывается вручную по кнопке «В библиотеку» (см. /api/library/add), а для
    файлов торрент-раздачи — ещё и автоматически по мере докачки каждого
    файла (см. _publish_newly_done). Возвращает имя файла в витрине. Ошибки
    пробрасываем — вызывающий код сам решает, показать их или залогировать.
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


def _publish_newly_done(job_id: str, job_dir: Path):
    """Публикует в SMB-витрину файлы раздачи, которые только что докачались
    (см. _file_done) — не дожидаясь конца всей раздачи. Публикацию каждого
    файла пробуем один раз: успех/неудача запоминаются в JOBS, чтобы при
    сбое шары не долбить копированием большого файла на каждый тик прогресса
    (ручная кнопка «В библиотеку» остаётся как повтор)."""
    if not SHARE_PATH:
        return
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        published = job.setdefault("published", set())
        failed = job.setdefault("publish_failed", set())
    for p in _real_job_files(job_dir):
        rel = str(p.relative_to(job_dir)).replace("\\", "/")
        if rel in published or rel in failed:
            continue
        try:
            _publish_to_share(p)
            published.add(rel)
        except Exception:
            traceback.print_exc()
            failed.add(rel)


def _count_active(jobs: dict, lock, states) -> int:
    """Сколько задач сейчас в работе/очереди (для потолка одновременных задач)."""
    with lock:
        return sum(1 for j in jobs.values() if j.get("state") in states)


DISK_MIN_FREE_MB = int(os.environ.get("DISK_MIN_FREE_MB", "1024"))


def _require_disk_space():
    """Понятная ошибка вместо «непонятного» 500, когда на диске кончилось
    место (ENOSPC при создании папок/файлов задачи). Зовём перед стартом
    любой загрузки.
    ponytail: грубый порог DISK_MIN_FREE_MB — большой торрент всё равно может
    добить диск в процессе, тогда aria2 просто завершится ошибкой; апгрейд —
    сверять со свободным местом реальный размер раздачи перед стартом."""
    try:
        free = shutil.disk_usage(DOWNLOAD_DIR).free
    except OSError:
        return
    if free < DISK_MIN_FREE_MB * 1024 * 1024:
        raise HTTPException(
            507, f"На диске сервера почти нет места ({free // (1024 * 1024)} МБ "
            "свободно). Удалите ненужные файлы в разделах «Файлы на сервере» "
            "или «Библиотека» и повторите.")


def _drain_torrent_to_library(job_id: str, job_dir: Path) -> bool:
    """После докачки торрента: копии файлов уже уехали в SMB-библиотеку
    (см. _publish_newly_done). Удаляем их с сервера, чтобы торрент жил только
    в библиотеке и не забивал диск VM. True — всё уехало, папка задачи удалена.
    False (ничего не удаляем) — библиотека выключена ИЛИ часть файлов не
    опубликовалась (шара отвалилась): тогда файлы остаются на сервере, чтобы
    их не потерять, с ручной кнопкой-повтором «В библиотеку»."""
    if not SHARE_PATH:
        return False
    with JOBS_LOCK:
        job = JOBS.get(job_id) or {}
        failed = set(job.get("publish_failed", ()))
        published = set(job.get("published", ()))
    if failed or not published:
        return False
    shutil.rmtree(job_dir, ignore_errors=True)
    return True


def _magnet_name(magnet: str) -> str:
    """Человеческое имя раздачи из параметра dn= magnet-ссылки (если есть)."""
    m = re.search(r"[?&]dn=([^&]+)", magnet or "")
    return unquote_plus(m.group(1)) if m else ""


def _torrent_root_name(torrent_path) -> str:
    """Имя раздачи (info.name) из .torrent-файла — человеческий заголовок."""
    try:
        info, _ = _bdecode(Path(torrent_path).read_bytes())
        return info[b"info"][b"name"].decode("utf-8", "replace")
    except Exception:
        return ""


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
    _require_disk_space()
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


@app.get("/api/jobs", dependencies=[Depends(check_password)])
def list_jobs():
    """Активные задачи (в очереди/качаются/обрабатываются/на паузе) — фронт
    зовёт при загрузке страницы, чтобы вернуть карточки с прогрессом после
    F5: сама загрузка идёт в фоне на сервере и переживает перезагрузку
    вкладки. Паузу включаем сюда же, иначе карточка пропадала бы с F5."""
    out = []
    with JOBS_LOCK:
        for jid, job in JOBS.items():
            if job.get("state") in ("queued", "downloading", "processing", "paused"):
                out.append({"job_id": jid, "state": job.get("state"),
                            "title": job.get("title"), "type": job.get("type"),
                            "percent": job.get("percent")})
    return {"jobs": out}


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
            # На паузе задача уже показана карточкой из /api/jobs — не дублируем
            # её здесь ещё раз как «прервана».
            if state in ("queued", "downloading", "processing", "paused"):
                continue
            result = _result_file(d)
            is_torrent = (d / ".torrent_job").exists()
            # Прервана крашем/рестартом: раздача, среди выбранных файлов
            # которой есть недокачанный, а сама задача сейчас не активна.
            # Раньше такая задача просто пропадала из списка (result=None,
            # continue) — теперь показываем с прогрессом и кнопкой «Возобновить».
            interrupted = False
            progress = None
            if is_torrent:
                status = _torrent_status_files(d)
                selected_rows = [f for f in status if f["selected"]]
                if selected_rows and not all(f["done"] for f in selected_rows):
                    interrupted = True
                    total = sum(f["size"] for f in selected_rows)
                    done = sum(f["downloaded"] for f in selected_rows)
                    progress = {
                        "percent": int(done * 100 / total) if total else 0,
                        "done_files": sum(1 for f in selected_rows if f["done"]),
                        "total_files": len(selected_rows),
                    }
            if not result and not interrupted:
                continue
            mtime = result.stat().st_mtime if result else d.stat().st_mtime
            # Торрент не чистится авто только если есть библиотека (туда он
            # уезжает). Без библиотеки он живёт на сервере по общему retention.
            kept = is_torrent and SHARE_PATH is not None
            remaining = None if kept else max(
                0, int(RETENTION_SECONDS - (now - mtime)))
            # Человеческое имя раздачи — из сохранённых .torrent-метаданных на
            # диске (переживает рестарт сервиса), иначе из title в памяти.
            title = None
            if is_torrent:
                meta = d / ".meta.torrent"
                if meta.exists():
                    title = _torrent_root_name(meta) or None
                if not title:
                    with JOBS_LOCK:
                        job = JOBS.get(d.name)
                        title = job.get("title") if job else None
            items.append({
                "job_id": d.name,
                "filename": result.name if result else None,
                "title": title,
                "size": result.stat().st_size if result else None,
                "remaining": remaining,
                "torrent": is_torrent,
                "interrupted": interrupted,
                "progress": progress,
                "mtime": mtime,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)  # новые сверху
    return {"files": items}


@app.get("/api/file/{job_id}")
def get_file(job_id: str, path: str = ""):
    # Файл отдаём без пароля в заголовке — браузер скачивает по прямой ссылке.
    # job_id случайный и неугадываемый, этого достаточно для личного сервера.
    # Чистим job_id так же, как при удалении: только буквы/цифры, чтобы нельзя
    # было выйти за пределы папки загрузок.
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    job_dir = DOWNLOAD_DIR / safe

    with JOBS_LOCK:
        job = JOBS.get(safe)
        state = job.get("state") if job else None

    # path — конкретный файл многофайловой торрент-раздачи (раскрывающаяся
    # табличка); в отличие от запроса без path, отдаём его и во время
    # скачивания раздачи — если конкретно этот файл уже докачан (_file_done),
    # остальные файлы раздачи при этом могут ещё качаться.
    if path:
        target = _resolve_ready_file(job_dir, path)
        return FileResponse(
            target, filename=target.name, media_type="application/octet-stream")

    # Без path — весь результат задачи. Пока задача в работе или на паузе,
    # отдавать нечего: ранняя выдача давала битый/недокачанный файл («чёрный
    # экран с кодом» в браузере). Ищем готовый файл на диске, а не в памяти
    # процесса: так файл можно скачать даже после перезапуска сервиса.
    if state in ("queued", "downloading", "processing", "paused"):
        raise HTTPException(409, "Файл ещё скачивается — дождитесь завершения")
    target = _resolve_ready_file(job_dir)
    return FileResponse(
        target, filename=target.name, media_type="application/octet-stream")


# --- Просмотр в браузере (плеер) --------------------------------------------
# mp4/webm/mp3 и т.п. браузер проигрывает нативно — отдаём файл как есть, с
# правильным Content-Type и inline (не download), Range уже умеет FileResponse.
# Остальное (в первую очередь mkv у торрентов) браузер нативно не проигрывает —
# делаем remux в mp4-контейнер без перекодирования (ffmpeg -c copy, секунды
# даже на большой файл) и кэшируем результат рядом с исходником.

FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
NATIVE_PLAYABLE = {".mp4", ".m4v", ".webm", ".ogv", ".mp3", ".m4a", ".wav", ".ogg"}
PLAY_JOBS: dict[str, dict] = {}   # cache_path -> {"error": str|None}, пока идёт remux
PLAY_LOCK = threading.Lock()


def _play_cache_path(target: Path) -> Path:
    """Кэш remux-копии рядом с исходником, скрытый файл (не попадает в
    список файлов раздачи и не публикуется в библиотеку — см. _is_temp)."""
    return target.with_name("." + target.name + ".play.mp4")


def _run_remux(cache_key: str, src: Path, dest: Path):
    """Перепаковка (не перекодирование) видео/аудио-дорожек в mp4-контейнер;
    встроенные субтитры (ass/srt) в mp4 не влезают — намеренно отбрасываются,
    речь только про воспроизведение."""
    tmp = dest.with_suffix(".tmp")
    try:
        proc = subprocess.run(
            [FFMPEG_BIN, "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a",
             "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp)],
            capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            raise RuntimeError((proc.stderr or "ffmpeg завершился с ошибкой")[-300:])
        tmp.replace(dest)
        with PLAY_LOCK:
            PLAY_JOBS.pop(cache_key, None)
    except FileNotFoundError:
        with PLAY_LOCK:
            PLAY_JOBS[cache_key] = {"error": "ffmpeg не установлен на сервере."}
    except Exception as e:  # noqa: BLE001
        with PLAY_LOCK:
            PLAY_JOBS[cache_key] = {"error": str(e)[:300]}
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


@app.get("/api/play/{job_id}")
def play_file(job_id: str, path: str = ""):
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    job_dir = DOWNLOAD_DIR / safe
    target = _resolve_ready_file(job_dir, path)

    if target.suffix.lower() in NATIVE_PLAYABLE:
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=media_type, filename=target.name,
                             content_disposition_type="inline")

    cache = _play_cache_path(target)
    if cache.is_file():
        return FileResponse(cache, media_type="video/mp4",
                             filename=target.stem + ".mp4",
                             content_disposition_type="inline")

    cache_key = str(cache)
    with PLAY_LOCK:
        job = PLAY_JOBS.get(cache_key)
        if job and job.get("error"):
            err = job["error"]
            PLAY_JOBS.pop(cache_key, None)   # следующий клик — новая попытка
            raise HTTPException(500, f"Не удалось подготовить файл для просмотра: {err}")
        if not job:
            PLAY_JOBS[cache_key] = {"error": None}
            threading.Thread(target=_run_remux, args=(cache_key, target, cache),
                              daemon=True).start()
    raise HTTPException(409, "Готовим файл для просмотра — повторите запрос через пару секунд")


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


def _resolve_torrent_source(magnet: str, detail: str) -> str:
    """magnet (ручной ввод) или detail (результат поиска) -> проверенный magnet.
    Общая логика для /api/torrent/files и /api/torrent (путь без token)."""
    magnet = (magnet or "").strip()
    if not magnet and detail:
        magnet = _resolve_magnet(detail.strip())
    if not MAGNET_RE.match(magnet):
        raise HTTPException(400, "Нужна корректная magnet-ссылка")
    return magnet


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


def _capture_magnet_metadata(job_dir: Path, meta_target: Path):
    """Как только aria2 (--bt-save-metadata) получает .torrent-метаданные
    magnet-раздачи, переименовывает их в канонический .meta.torrent — чтобы
    раздачу, добавленную голым magnet, можно было докачать/возобновить после
    краша так же, как обычную .torrent-раздачу (иначе метаданных нигде нет и
    восстановить список файлов после рестарта сервиса нельзя)."""
    if meta_target.exists():
        return
    for p in job_dir.glob("*.torrent"):
        p.replace(meta_target)
        return


def _maybe_continue_unselected(job_id: str, job_dir: Path, meta_target: Path):
    """Если раздача добавлена с флагом «докачать остальное автоматически»
    (.auto_rest) — после того как выбранные файлы полностью докачаны, сама
    добавляет в закачку оставшиеся невыбранные файлы (как обычная ручная
    докачка). Если очередь занята — ничего не делает, файлы остаются
    доступны для ручной докачки, как и раньше."""
    if not (job_dir / ".auto_rest").exists() or not meta_target.is_file():
        return
    selected = _selected_indices(job_dir)
    if selected is None:
        return  # раздача качалась целиком (без выбора) — докачивать нечего
    try:
        all_indices = {f["index"] for f in _torrent_file_list(meta_target)}
    except Exception:
        return
    remaining = all_indices - selected
    if not remaining:
        return
    if _count_active(JOBS, JOBS_LOCK,
                     ("queued", "downloading", "processing")) >= JOB_CEILING:
        return
    select = sorted(selected | remaining)
    (job_dir / ".selected").write_text(",".join(str(i) for i in select))
    with JOBS_LOCK:
        JOBS[job_id].update(state="queued", percent=None, speed=None,
                            eta=None, filename=None, error=None, cancel=False)
    _run_torrent(job_id, str(meta_target), select=select)


def _run_torrent(job_id: str, source: str, select=None, auto_rest: bool = False,
                  check_integrity: bool = False):
    """Качает раздачу через aria2c в папку задачи, обновляя те же поля JOBS,
    что и _run_download (state/percent/speed/eta/total) — чтобы фронтовый
    poll() работал без изменений. Уважает job['cancel'] и job['pause'].

    source — либо magnet-ссылка (старый путь, качаем всё), либо путь к уже
    скачанному .torrent-файлу (после предпросмотра списка файлов, или из
    job_dir/.meta.torrent при повторном запуске через «докачать»/«возобновить»)
    — тогда select задаёт список 1-based индексов файлов для --select-file.
    auto_rest=True помечает задачу для _maybe_continue_unselected (маркер на
    диске переживает рестарт сервиса, поэтому повторные вызовы — резюм,
    докачка, авто-резюм при старте — его не передают, он читается с диска).
    check_integrity=True добавляет aria2 --check-integrity=true (перечитать
    хэш уже скачанных кусков) — используется и вручную (кнопка «Перечитать
    хэш»), и один раз автоматически сразу после обычной докачки (см. ниже),
    поэтому сам себя не рекурсирует дважды."""
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    # Маркер: эта папка — торрент-задача, автоочистка (_cleanup_loop) её не трогает.
    (job_dir / ".torrent_job").touch()
    if auto_rest:
        (job_dir / ".auto_rest").touch()
    is_magnet = source.startswith("magnet:")
    # .torrent-метаданные и список выбранных файлов сохраняем в самой папке
    # задачи (а не во временной .meta/<token>/, которую раньше стирали сразу
    # после старта) — это единственный способ узнать полный список файлов
    # раздачи (включая пропущенные) для живого статуса и для «докачать».
    meta_target = job_dir / ".meta.torrent"
    meta_dir_to_clean = None
    if not is_magnet and Path(source).resolve() != meta_target.resolve():
        meta_dir_to_clean = Path(source).parent
        shutil.copyfile(source, meta_target)
        source = str(meta_target)
    if select:
        (job_dir / ".selected").write_text(
            ",".join(str(i) for i in sorted(set(select))))
    cmd = [
        ARIA2_BIN,
        "--dir", str(job_dir),
        "--seed-time=0",              # не раздаём после докачки — сразу выходим
        "--summary-interval=1",       # строка прогресса раз в секунду
        "--console-log-level=warn",
        "--file-allocation=none",     # не преаллоцируем весь размер на диск
        "--bt-stop-timeout=300",      # 5 мин без пиров — прекращаем
    ]
    if is_magnet:
        cmd.append("--follow-torrent=mem")
        # Сохранить .torrent-метаданные раздачи на диск, как только они
        # получены от пиров/трекера — без этого голый magnet нельзя ни
        # докачать, ни возобновить после краша (см. _capture_magnet_metadata).
        cmd.append("--bt-save-metadata=true")
    if select:
        cmd.append("--select-file=" + ",".join(str(i) for i in select))
    if check_integrity:
        cmd.append("--check-integrity=true")
    cmd.append(source)
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
                if not job or job.get("cancel") or job.get("pause"):
                    break
            m = re.search(r"\((\d+)%\)", line)
            if not m:
                continue
            spd = re.search(r"DL:([\d.]+\s*[KMGTP]?i?B)", line)
            eta = re.search(r"ETA:([0-9hms]+)", line)
            tot = re.search(r"/([\d.]+\s*[KMGTP]?i?B)\(", line)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if job and not job.get("cancel") and not job.get("pause"):
                    job.update(
                        state="downloading",
                        percent=int(m.group(1)),
                        speed=_bytes_from_aria2(spd.group(1)) if spd else None,
                        eta=_seconds_from_aria2(eta.group(1)) if eta else None,
                        total=_bytes_from_aria2(tot.group(1)) if tot else None)
            # Файлы могут докачиваться поодиночке раньше, чем вся раздача —
            # публикуем их в SMB-витрину сразу, не дожидаясь состояния "done".
            _publish_newly_done(job_id, job_dir)
            if is_magnet:
                _capture_magnet_metadata(job_dir, meta_target)

        with JOBS_LOCK:
            cancelled = JOBS.get(job_id, {}).get("cancel")
            paused = JOBS.get(job_id, {}).get("pause")
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
        if paused:
            # В отличие от отмены — файлы на диске не трогаем, aria2
            # корректно допишет свой .aria2-контрольный файл при terminate()
            # (SIGTERM), поэтому «Продолжить» (/resume) докачает с этого места.
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            with JOBS_LOCK:
                JOBS[job_id].update(state="paused", speed=None, eta=None,
                                     pause=False)
            return

        proc.wait()
        _publish_newly_done(job_id, job_dir)  # публикуем всё, что докачалось
        real = [p for p in job_dir.rglob("*")
                if p.is_file() and not _is_temp(p)
                and p.suffix.lower() != ".torrent"
                and p.name not in (".selected", ".torrent_job")]
        if not real:
            raise RuntimeError("aria2 не скачал файл (нет пиров или битый magnet)")
        if not check_integrity:
            # Один доп. проход с перепроверкой хэша уже скачанных кусков
            # перед тем, как считать раздачу окончательно готовой и
            # публиковать в библиотеку — размер уже проверен _file_done, но
            # это не защищает от тихой порчи данных на диске. check_integrity
            # не False у самого себя, поэтому рекурсия ровно на один уровень.
            return _run_torrent(job_id, source, select, check_integrity=True)
        total_size = sum(p.stat().st_size for p in real)
        if _drain_torrent_to_library(job_id, job_dir):
            # Файлы уехали в SMB-библиотеку и удалены с сервера — торрент живёт
            # только в разделе «Библиотека», дубля на сервере нет.
            with JOBS_LOCK:
                JOBS[job_id].update(state="done", percent=100, filename=None,
                                    size=total_size, in_library=True)
        else:
            # Библиотека выключена / часть файлов не уехала — оставляем на
            # сервере (старое поведение). Многофайловая раздача: aria2 кладёт
            # файлы в подпапку с именем торрента, а _result_file/api/files
            # смотрят только верхний уровень — поднимаем самый большой файл
            # (видео) наверх.
            result = _result_file(job_dir)
            if not result:
                biggest = max(real, key=lambda p: p.stat().st_size)
                target = job_dir / biggest.name
                if biggest != target:
                    biggest.replace(target)
                result = target
            with JOBS_LOCK:
                JOBS[job_id].update(
                    state="done", percent=100,
                    filename=result.name, size=result.stat().st_size)
        # Выбранные файлы готовы — если раньше стоял флаг «докачать остальное
        # автоматически», сейчас самое время добавить в закачку остальное.
        _maybe_continue_unselected(job_id, job_dir, meta_target)
    except Exception as e:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass
        with JOBS_LOCK:
            cancelled = JOBS.get(job_id, {}).get("cancel")
            paused = JOBS.get(job_id, {}).get("pause")
        if cancelled:
            shutil.rmtree(job_dir, ignore_errors=True)
            with JOBS_LOCK:
                JOBS[job_id].update(state="cancelled", filename=None)
        elif paused:
            with JOBS_LOCK:
                JOBS[job_id].update(state="paused", speed=None, eta=None,
                                     pause=False)
        else:
            traceback.print_exc()
            with JOBS_LOCK:
                JOBS[job_id].update(state="error", error=str(e)[:200])
    finally:
        # meta_dir_to_clean — временная .meta/<token>/ с исходным .torrent,
        # который мы уже скопировали в job_dir/.meta.torrent; саму временную
        # папку больше не храним. При повторном запуске (см. «докачать»)
        # source уже указывает на job_dir/.meta.torrent — чистить нечего.
        if meta_dir_to_clean:
            shutil.rmtree(meta_dir_to_clean, ignore_errors=True)


class TorrentRequest(BaseModel):
    magnet: str = ""   # прямая magnet-ссылка (ручной ввод)
    detail: str = ""   # ИЛИ путь к раздаче из результатов поиска
    token: str = ""    # ИЛИ токен предпросмотра файлов (/api/torrent/files)
    files: list[int] = []  # какие файлы качать (1-based индексы), с token
    auto_rest: bool = False  # докачать остальные файлы раздачи автоматически
                              # после выбранных (имеет смысл только с token)


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


def _bdecode(data: bytes, i: int = 0):
    """Мини-декодер bencode (.torrent) — нужны только int/bytes/list/dict,
    без внешней зависимости. Возвращает (значение, позиция-после)."""
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _bdecode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _bdecode(data, i)
            v, i = _bdecode(data, i)
            out[k] = v
        return out, i + 1
    colon = data.index(b":", i)
    length = int(data[i:colon])
    start = colon + 1
    return data[start:start + length], start + length


def _torrent_file_list(torrent_path: Path) -> list[dict]:
    """Список файлов раздачи (path, size) из .torrent-файла, в том порядке,
    в котором aria2 их нумерует для --select-file (1-based). path — реальный
    путь на диске относительно job_dir: для многофайловой раздачи aria2
    кладёт файлы в подпапку с именем раздачи, поэтому добавляем её к path."""
    info, _ = _bdecode(torrent_path.read_bytes())
    info = info[b"info"]
    if b"files" in info:
        root = info[b"name"].decode("utf-8", "replace")
        files = []
        for f in info[b"files"]:
            path = "/".join(p.decode("utf-8", "replace") for p in f[b"path"])
            files.append({"path": f"{root}/{path}", "size": f[b"length"]})
    else:
        files = [{"path": info[b"name"].decode("utf-8", "replace"),
                  "size": info[b"length"]}]
    return [dict(f, index=i) for i, f in enumerate(files, start=1)]


def _selected_indices(job_dir: Path):
    """1-based индексы файлов, выбранных при старте/докачке раздачи (см.
    _run_torrent). None — раздача скачивалась без выбора (весь торрент),
    тогда все файлы на диске считаются выбранными."""
    f = job_dir / ".selected"
    if not f.is_file():
        return None
    return {int(x) for x in f.read_text().split(",") if x.strip()}


def _torrent_status_files(job_dir: Path) -> list[dict]:
    """Файлы раздачи с признаком done (см. _file_done — уже докачан, а не
    вся раздача целиком) и selected (участвует в текущей загрузке). Если для
    задачи сохранены .meta.torrent-метаданные, в список попадают и ещё не
    выбранные файлы (size есть, done=False, selected=False) — их можно
    докачать через /api/torrent/{job_id}/add-files."""
    on_disk = {}
    for p in job_dir.rglob("*"):
        if not p.is_file() or _is_temp(p):
            continue
        if p.name in (".selected",) or p.suffix.lower() == ".torrent" or p.name == ".torrent_job":
            continue
        rel = str(p.relative_to(job_dir)).replace("\\", "/")
        on_disk[rel] = p.stat().st_size

    def _row(path, full, downloaded, done, index=None, selected=True):
        # percent — по объёму на диске от полного размера файла (см. --file-
        # allocation=none: aria2 дописывает файл, размер растёт по мере докачки).
        # ponytail: приблизительно (aria2 пишет не строго по порядку), но для
        # индикатора «сколько осталось по файлу» этого достаточно.
        if done:
            pct = 100
        elif full and downloaded:
            pct = min(99, int(downloaded * 100 / full))
        else:
            pct = 0
        return {"path": path, "size": full or downloaded, "downloaded": downloaded,
                "done": done, "percent": pct, "index": index, "selected": selected}

    meta = job_dir / ".meta.torrent"
    if not meta.is_file():
        # Без метаданных не знаем ожидаемый размер файла — запасная эвристика
        # по соседнему .aria2 (годится только для одиночного файла, см. _file_done).
        rows = [_row(rel, size, size, _file_done(job_dir / rel))
                for rel, size in on_disk.items()]
        return sorted(rows, key=lambda x: -x["size"])

    try:
        expected = _torrent_file_list(meta)
    except Exception:
        rows = [_row(rel, size, size, _file_done(job_dir / rel))
                for rel, size in on_disk.items()]
        return sorted(rows, key=lambda x: -x["size"])
    selected = _selected_indices(job_dir)
    items = []
    for f in expected:
        downloaded = on_disk.get(f["path"])
        if downloaded is None:
            # Раздача уже докачана целиком: _run_torrent поднимает самый
            # большой файл в корень job_dir, вложенный путь при этом теряется.
            downloaded = on_disk.get(Path(f["path"]).name)
        is_selected = selected is None or f["index"] in selected
        done = downloaded is not None and downloaded == f["size"]
        items.append(_row(f["path"], f["size"], downloaded or 0, done,
                          index=f["index"], selected=is_selected))
    items.sort(key=lambda x: -x["size"])
    return items


# Предпросмотр файлов раздачи: magnet -> (только метаданные) -> .torrent на
# диске -> список файлов. Токен живёт до TORRENT_META_TTL или до фактического
# скачивания (torrent_add забирает и стирает запись).
TORRENT_META: dict[str, dict] = {}
TORRENT_META_LOCK = threading.Lock()
TORRENT_META_TTL = 1800  # 30 минут — если так и не скачали, чистим за собой
TORRENT_META_DIR = DOWNLOAD_DIR / ".meta"


def _prune_torrent_meta():
    now = time.time()
    with TORRENT_META_LOCK:
        stale = [t for t, v in TORRENT_META.items()
                 if now - v["created"] > TORRENT_META_TTL]
        for t in stale:
            TORRENT_META.pop(t, None)
    for d in [TORRENT_META_DIR / t for t in stale]:
        shutil.rmtree(d, ignore_errors=True)


@app.post("/api/torrent/files", dependencies=[Depends(check_password)])
def torrent_files_preview(req: TorrentRequest):
    magnet = _resolve_torrent_source(req.magnet, req.detail)
    _require_disk_space()
    _prune_torrent_meta()
    token = uuid.uuid4().hex[:12]
    meta_dir = TORRENT_META_DIR / token
    meta_dir.mkdir(parents=True, exist_ok=True)
    cmd = [ARIA2_BIN, "--dir", str(meta_dir),
           "--bt-metadata-only=true", "--bt-save-metadata=true",
           "--console-log-level=warn", magnet]
    try:
        subprocess.run(cmd, timeout=25, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        shutil.rmtree(meta_dir, ignore_errors=True)
        raise HTTPException(
            500, "aria2 не установлен на сервере (нужен apt install aria2).")
    except subprocess.TimeoutExpired:
        shutil.rmtree(meta_dir, ignore_errors=True)
        raise HTTPException(
            504, "Не удалось получить список файлов (нет пиров/метаданных). "
                 "Можно скачать раздачу целиком без выбора файлов.")
    found = list(meta_dir.glob("*.torrent"))
    if not found:
        shutil.rmtree(meta_dir, ignore_errors=True)
        raise HTTPException(
            502, "Не удалось получить метаданные раздачи. "
                 "Можно скачать раздачу целиком без выбора файлов.")
    try:
        files = _torrent_file_list(found[0])
    except Exception:
        shutil.rmtree(meta_dir, ignore_errors=True)
        raise HTTPException(502, "Не удалось разобрать метаданные раздачи.")
    with TORRENT_META_LOCK:
        TORRENT_META[token] = {"torrent_path": str(found[0]), "created": time.time()}
    return {"token": token, "files": files,
            "total_size": sum(f["size"] for f in files)}


@app.post("/api/torrent", dependencies=[Depends(check_password)])
def torrent_add(req: TorrentRequest):
    source = None
    select = None
    if req.token:
        with TORRENT_META_LOCK:
            meta = TORRENT_META.pop(req.token, None)
        if not meta:
            raise HTTPException(
                400, "Список файлов устарел — обновите его и выберите заново.")
        if not req.files:
            shutil.rmtree(Path(meta["torrent_path"]).parent, ignore_errors=True)
            raise HTTPException(400, "Выберите хотя бы один файл")
        source = meta["torrent_path"]
        select = sorted(set(req.files))
        title = _torrent_root_name(source)
    else:
        source = _resolve_torrent_source(req.magnet, req.detail)
        title = _magnet_name(source)
    _require_disk_space()
    if _count_active(JOBS, JOBS_LOCK,
                     ("queued", "downloading", "processing")) >= JOB_CEILING:
        raise HTTPException(429, "Сейчас слишком много загрузок — попробуйте позже.")
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "state": "queued", "percent": None, "speed": None,
            "eta": None, "title": title or "Торрент", "filename": None,
            "error": None, "cancel": False, "total": None, "size": None,
            "type": "torrent",
        }
    threading.Thread(
        target=_run_torrent,
        args=(job_id, source, select),
        kwargs={"auto_rest": req.auto_rest and bool(req.token)},
        daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/torrent/{job_id}/files", dependencies=[Depends(check_password)])
def torrent_job_files(job_id: str):
    """Файлы торрент-задачи с живым статусом (done/selected) — читаем с
    диска, а не из JOBS, чтобы список был виден и после рестарта, и опрашиваем
    почаще во время загрузки, чтобы видеть, какие файлы уже готовы."""
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    job_dir = DOWNLOAD_DIR / safe
    if not job_dir.is_dir():
        raise HTTPException(404, "Задача не найдена")
    return {"files": _torrent_status_files(job_dir)}


class TorrentFileActionRequest(BaseModel):
    path: str


class TorrentAddFilesRequest(BaseModel):
    files: list[int] = []


@app.post("/api/torrent/{job_id}/delete-file", dependencies=[Depends(check_password)])
def torrent_delete_file(job_id: str, req: TorrentFileActionRequest):
    """Удаляет один файл раздачи (не всю задачу) — например, ненужную серию."""
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    job_dir = DOWNLOAD_DIR / safe
    if not job_dir.is_dir():
        raise HTTPException(404, "Задача не найдена")
    base = job_dir.resolve()
    target = (job_dir / req.path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(400, "Некорректный путь")
    if not target.is_file():
        raise HTTPException(404, "Файл не найден")
    rel = str(target.relative_to(base)).replace("\\", "/")
    sizes = _torrent_expected_sizes(job_dir)
    expected = sizes.get(rel, sizes.get(target.name))
    if not _file_done(target, expected):
        raise HTTPException(409, "Файл ещё скачивается — дождитесь завершения")
    target.unlink()
    _play_cache_path(target).unlink(missing_ok=True)   # remux-кэш плеера, если был
    return {"ok": True}


@app.post("/api/torrent/{job_id}/add-files", dependencies=[Depends(check_password)])
def torrent_add_files(job_id: str, req: TorrentAddFilesRequest):
    """Докачивает ранее пропущенные файлы раздачи: перезапускает aria2c в той
    же папке задачи с расширенным списком --select-file. Уже готовые файлы
    aria2 не перекачивает (проверяет по данным на диске), качаются только
    новые. Работает только для задач, у которых сохранены .meta.torrent
    (то есть скачивание шло через выбор файлов, а не «скачать всё как есть»)."""
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    job_dir = DOWNLOAD_DIR / safe
    meta = job_dir / ".meta.torrent"
    if not job_dir.is_dir() or not meta.is_file():
        raise HTTPException(400, "Для этой раздачи докачка недоступна")
    with JOBS_LOCK:
        job = JOBS.get(safe)
        state = job.get("state") if job else None
    if state in ("queued", "downloading", "processing"):
        raise HTTPException(409, "Раздача уже качается")
    add = set(req.files)
    if not add:
        raise HTTPException(400, "Не выбраны файлы для докачки")
    selected = _selected_indices(job_dir) or set()
    select = sorted(selected | add)
    with JOBS_LOCK:
        JOBS[safe] = {
            "state": "queued", "percent": None, "speed": None,
            "eta": None, "title": None, "filename": None, "error": None,
            "cancel": False, "total": None, "size": None, "type": "torrent",
        }
    threading.Thread(
        target=_run_torrent, args=(safe, str(meta), select), daemon=True).start()
    return {"ok": True}


@app.post("/api/torrent/{job_id}/resume", dependencies=[Depends(check_password)])
def torrent_resume(job_id: str, check_integrity: bool = False):
    """Возобновляет прерванную/приостановленную раздачу — перезапускает
    aria2c с тем же .meta.torrent и тем же набором выбранных файлов, что и
    раньше (aria2 сам продолжит с того места, где остановился, по уже
    скачанным на диске кускам). В отличие от «докачать» (add-files) не
    расширяет список файлов, просто продолжает текущий.
    check_integrity=True — это же «▶ Продолжить», но с --check-integrity=true
    (кнопка «🔁 Перечитать хэш»)."""
    safe = re.sub(r"[^a-zA-Z0-9]", "", job_id)
    job_dir = DOWNLOAD_DIR / safe
    meta = job_dir / ".meta.torrent"
    if not job_dir.is_dir() or not meta.is_file():
        raise HTTPException(
            400, "Для этой раздачи возобновление недоступно — нет "
            "сохранённых метаданных (раздача добавлена до появления этой "
            "функции). Удалите и добавьте заново.")
    with JOBS_LOCK:
        job = JOBS.get(safe)
        state = job.get("state") if job else None
    if state in ("queued", "downloading", "processing"):
        raise HTTPException(409, "Раздача уже качается")
    _require_disk_space()
    if _count_active(JOBS, JOBS_LOCK,
                     ("queued", "downloading", "processing")) >= JOB_CEILING:
        raise HTTPException(429, "Сейчас слишком много загрузок — попробуйте позже.")
    title = _torrent_root_name(meta) or "Торрент"
    select = _selected_indices(job_dir)
    with JOBS_LOCK:
        JOBS[safe] = {
            "state": "queued", "percent": None, "speed": None,
            "eta": None, "title": title, "filename": None, "error": None,
            "cancel": False, "pause": False, "total": None, "size": None,
            "type": "torrent",
        }
    threading.Thread(
        target=_run_torrent,
        args=(safe, str(meta), sorted(select) if select else None),
        kwargs={"check_integrity": check_integrity},
        daemon=True).start()
    return {"ok": True}


@app.post("/api/torrent/{job_id}/pause", dependencies=[Depends(check_password)])
def torrent_pause(job_id: str):
    """Останавливает aria2 для этой раздачи, не удаляя скачанное — в отличие
    от /api/cancel. «Продолжить» — тот же /api/torrent/{id}/resume, что и
    восстановление после краша сервиса (это и есть механизм паузы)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "Задача не найдена")
        if job.get("type") != "torrent":
            raise HTTPException(400, "Пауза доступна только для торрентов")
        if job.get("state") not in ("queued", "downloading"):
            raise HTTPException(409, "Раздача сейчас не качается")
        job["pause"] = True
    return {"ok": True}


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
    """Копирует готовые файлы задачи в SMB-витрину (постоянное хранилище).
    Вызывается кнопкой «В библиотеку» на сайте. Для многофайловой раздачи
    (торрент с несколькими сериями/файлами) копирует ВСЕ готовые файлы, а не
    только самый большой — иначе часть серий молча терялась."""
    if not SHARE_PATH:
        raise HTTPException(400, "Библиотека (SMB) не настроена")
    # job_id из тела запроса — оставляем только буквы/цифры, чтобы нельзя
    # было выйти за пределы папки загрузок.
    safe = re.sub(r"[^a-zA-Z0-9]", "", req.job_id)
    files = _real_job_files(DOWNLOAD_DIR / safe)
    if not files:
        raise HTTPException(404, "Файл не найден")
    names = []
    for f in files:
        try:
            names.append(_publish_to_share(f))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            raise HTTPException(500, "Не удалось добавить в библиотеку: " + str(e))
    return {"ok": True, "name": names[0], "names": names}


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


def _fetch_meta(url: str) -> dict:
    """Лёгкий запрос метаданных без скачивания: ссылка + описание с площадки.
    Best-effort — при любой осечке возвращаем пустое описание, чтобы не
    ронять саму транскрибацию."""
    try:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                "skip_download": True, "socket_timeout": 20, "retries": 1}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return {"link": info.get("webpage_url") or url,
                "description": (info.get("description") or "").strip()}
    except Exception:
        return {"link": url, "description": ""}


def _run_transcribe(job_id: str, url: str, lang: str,
                    force_whisper: bool = False, quality: str = "fast",
                    include_meta: bool = False):
    job_dir = DOWNLOAD_DIR / ("t_" + job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        segments = None
        source = "whisper"
        meta = _fetch_meta(url) if include_meta else None

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
                segments=segments, source=source,
                link=(meta or {}).get("link"),
                description=(meta or {}).get("description"))
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
    include_meta: bool = False   # прикрепить ссылку + описание с площадки


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
            "link": None, "description": None,
            "error": None, "cancel": False,
        }
    threading.Thread(
        target=_run_transcribe,
        args=(job_id, url, req.lang.strip(), bool(req.force_whisper), quality,
              bool(req.include_meta)),
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


def _resume_interrupted_torrents():
    """Один раз при старте сервиса докачивает раздачи, прерванные крашем/
    рестартом (см. /api/torrent/{job_id}/resume — та же механика). Кандидат:
    папка с .torrent_job и .meta.torrent, среди выбранных файлов есть
    недокачанный, задачи ещё нет в JOBS (свежий старт процесса). Уважает
    JOB_CEILING — то, что не влезло, остаётся с ручной кнопкой «Возобновить»
    на карточке (см. list_files)."""
    if not DOWNLOAD_DIR.exists():
        return
    for d in DOWNLOAD_DIR.iterdir():
        if not d.is_dir() or d.name == ".meta":
            continue
        if not (d / ".torrent_job").exists():
            continue
        meta = d / ".meta.torrent"
        if not meta.is_file():
            continue
        with JOBS_LOCK:
            if d.name in JOBS:
                continue
        try:
            status = _torrent_status_files(d)
        except Exception:
            continue
        selected_rows = [f for f in status if f["selected"]]
        if not selected_rows or all(f["done"] for f in selected_rows):
            continue  # уже всё готово или нечего качать — не наш случай
        if _count_active(JOBS, JOBS_LOCK,
                         ("queued", "downloading", "processing")) >= JOB_CEILING:
            break  # остальное — по ручной кнопке «Возобновить»
        title = _torrent_root_name(meta) or "Торрент"
        select = _selected_indices(d)
        with JOBS_LOCK:
            JOBS[d.name] = {
                "state": "queued", "percent": None, "speed": None,
                "eta": None, "title": title, "filename": None, "error": None,
                "cancel": False, "total": None, "size": None, "type": "torrent",
            }
        threading.Thread(
            target=_run_torrent,
            args=(d.name, str(meta), sorted(select) if select else None),
            daemon=True).start()


threading.Thread(target=_resume_interrupted_torrents, daemon=True).start()


# --- Раздача фронтенда -----------------------------------------------------
# Должно идти последним, чтобы не перехватывать /api/*
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
