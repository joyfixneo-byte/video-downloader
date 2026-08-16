"""
Самопроверка живого статуса файлов торрент-раздачи (docachka, автопуш в SMB).
Без сети и без aria2 — синтетический .torrent (bencode) + FastAPI TestClient.

Запуск:  python backend/test_torrent_files.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))


def bencode(v):
    if isinstance(v, bool):
        raise TypeError(v)
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, str):
        b = v.encode()
        return b"%d:%s" % (len(b), b)
    if isinstance(v, list):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        out = b"d"
        for k in sorted(v.keys()):
            out += bencode(k) + bencode(v[k])
        return out + b"e"
    raise TypeError(v)


def make_torrent(name: str, files: list) -> bytes:
    info = {b"name": name.encode(), b"files": [
        {b"path": [p.encode()], b"length": size} for p, size in files
    ]}
    return bencode({b"info": info})


def main():
    tmp = Path(tempfile.mkdtemp(prefix="torrent_files_test_"))
    os.environ["DOWNLOAD_DIR"] = str(tmp)
    import app as appmod  # noqa: E402  (после DOWNLOAD_DIR, чтобы модуль его подхватил)
    from fastapi.testclient import TestClient

    # Проверки ниже, где написано «упадёт без aria2 — это ок», раньше молча
    # опирались на то, что aria2 на машине разработчика не установлен. На
    # сервере он есть — тест начинал запускать НАСТОЯЩИЕ закачки синтетических
    # торрентов и падал (state не "error"). Подменяем путь к бинарнику, как в
    # п.21 с FFMPEG_BIN: результат одинаков и на ноуте, и на VM.
    appmod.ARIA2_BIN = "definitely-not-a-real-aria2-binary"

    client = TestClient(appmod.app)

    try:
        # --- 1. _torrent_file_list: путь на диске включает папку раздачи ---
        data = make_torrent("MyShow", [
            ("Episode1.mkv", 100), ("Episode2.mkv", 200), ("Episode3.mkv", 300)])
        job_dir = tmp / "job1"
        job_dir.mkdir()
        meta = job_dir / ".meta.torrent"
        meta.write_bytes(data)

        parsed = appmod._torrent_file_list(meta)
        assert parsed[0]["path"] == "MyShow/Episode1.mkv", parsed
        assert parsed[0]["index"] == 1 and parsed[2]["index"] == 3

        # --- 2. _torrent_status_files: done / downloading / not-selected ---
        sub = job_dir / "MyShow"
        sub.mkdir()
        (sub / "Episode1.mkv").write_bytes(b"x" * 100)          # готов
        (sub / "Episode2.mkv").write_bytes(b"x" * 50)           # качается
        (sub / "Episode2.mkv.aria2").write_bytes(b"ctrl")
        (job_dir / ".selected").write_text("1,2")                # 3 не выбран

        status = {f["path"]: f for f in appmod._torrent_status_files(job_dir)}
        assert status["MyShow/Episode1.mkv"]["done"] is True
        assert status["MyShow/Episode1.mkv"]["selected"] is True
        assert status["MyShow/Episode1.mkv"]["percent"] == 100          # готов
        assert status["MyShow/Episode2.mkv"]["done"] is False
        assert status["MyShow/Episode2.mkv"]["selected"] is True
        assert status["MyShow/Episode2.mkv"]["size"] == 200             # полный размер
        assert status["MyShow/Episode2.mkv"]["percent"] == 25           # 50 из 200
        assert status["MyShow/Episode3.mkv"]["done"] is False
        assert status["MyShow/Episode3.mkv"]["selected"] is False
        assert status["MyShow/Episode3.mkv"]["percent"] == 0            # нет на диске

        # --- 2b. _result_file: не путает служебные файлы (.meta.torrent,
        # .selected) на верхнем уровне с готовым видео, которое ещё лежит в
        # подпапке раздачи (так кладёт aria2 многофайловые торренты) ---
        result = appmod._result_file(job_dir)
        assert result is not None and result.name == "Episode1.mkv", result
        assert result.parent.name == "MyShow", result

        # --- 3. Без .meta.torrent — обычный плоский список с диска ---
        raw_dir = tmp / "rawjob"
        raw_dir.mkdir()
        (raw_dir / "movie.mkv").write_bytes(b"x" * 500)
        raw_status = appmod._torrent_status_files(raw_dir)
        assert raw_status == [{"path": "movie.mkv", "size": 500, "downloaded": 500,
                               "done": True, "percent": 100, "index": None,
                               "selected": True}], raw_status

        # --- 4. /api/file отдаёт готовый файл раздачи, даже если она "качается" ---
        job_id = "job1"   # совпадает с job_dir выше (tmp / "job1")
        appmod.JOBS[job_id] = {
            "state": "downloading", "percent": 40, "speed": None, "eta": None,
            "title": None, "filename": None, "error": None, "cancel": False,
            "total": None, "size": None, "type": "torrent"}
        r = client.get(f"/api/file/{job_id}", params={"path": "MyShow/Episode1.mkv"})
        assert r.status_code == 200, r.text
        r = client.get(f"/api/file/{job_id}", params={"path": "MyShow/Episode2.mkv"})
        assert r.status_code == 409, r.text   # ещё качается — не отдаём

        # без path и с активным состоянием — по-прежнему 409 (весь job не готов)
        r = client.get(f"/api/file/{job_id}")
        assert r.status_code == 409, r.text

        # --- 5. path traversal защищён ---
        r = client.get(f"/api/file/{job_id}", params={"path": "../../etc/passwd"})
        assert r.status_code == 400, r.text
        r = client.post(f"/api/torrent/{job_id}/delete-file",
                         json={"path": "../../etc/passwd"})
        assert r.status_code == 400, r.text

        # --- 6. delete-file: нельзя удалить файл, который ещё качается ---
        r = client.post(f"/api/torrent/{job_id}/delete-file",
                         json={"path": "MyShow/Episode2.mkv"})
        assert r.status_code == 409, r.text
        # готовый — можно
        r = client.post(f"/api/torrent/{job_id}/delete-file",
                         json={"path": "MyShow/Episode1.mkv"})
        assert r.status_code == 200, r.text
        assert not (sub / "Episode1.mkv").exists()

        # --- 7. add-files ("докачать"): объединяет .selected, не трогает диск ---
        job_dir2 = tmp / "job2"
        job_dir2.mkdir()
        (job_dir2 / ".meta.torrent").write_bytes(
            make_torrent("Show2", [("A.mkv", 10), ("B.mkv", 20)]))
        (job_dir2 / ".selected").write_text("1")
        (job_dir2 / "Show2").mkdir()
        (job_dir2 / "Show2" / "A.mkv").write_bytes(b"x" * 10)
        job_id2 = "job2"
        appmod.JOBS[job_id2] = {
            "state": "done", "percent": 100, "speed": None, "eta": None,
            "title": None, "filename": "A.mkv", "error": None, "cancel": False,
            "total": None, "size": 10, "type": "torrent"}

        r = client.post(f"/api/torrent/{job_id2}/add-files", json={"files": [2]})
        assert r.status_code == 200, r.text
        time.sleep(0.5)   # даём фоновому потоку стартовать (упадёт без aria2 — это ок)
        assert (job_dir2 / ".selected").read_text() == "1,2"
        assert job_dir2.is_dir(), "докачка не должна удалять папку задачи"
        assert (job_dir2 / "Show2" / "A.mkv").is_file(), "уже скачанный файл цел"

        # нельзя докачивать раздачу без сохранённых метаданных
        no_meta_dir = tmp / "job3"
        no_meta_dir.mkdir()
        appmod.JOBS["job3"] = dict(appmod.JOBS[job_id2])
        r = client.post("/api/torrent/job3/add-files", json={"files": [1]})
        assert r.status_code == 400, r.text

        # --- 8. Человеческое имя раздачи и перенос в библиотеку ---
        assert appmod._magnet_name(
            "magnet:?xt=urn:btih:ABC&dn=Dune%3A+Part+Two&tr=x") == "Dune: Part Two"
        assert appmod._magnet_name("magnet:?xt=urn:btih:ABC") == ""
        assert appmod._torrent_root_name(meta) == "MyShow"

        # _drain_torrent_to_library: без библиотеки (SHARE_PATH=None) не трогаем
        assert appmod.SHARE_PATH is None
        drain_dir = tmp / "drain"
        drain_dir.mkdir()
        appmod.JOBS["drain"] = {"published": {"a"}, "publish_failed": set()}
        assert appmod._drain_torrent_to_library("drain", drain_dir) is False
        assert drain_dir.is_dir()   # ничего не удалили — библиотеки нет

        # с «библиотекой»: всё опубликовано и без ошибок → папку удаляем
        share = tmp / "share"
        share.mkdir()
        appmod.SHARE_PATH = share
        try:
            appmod.JOBS["drain"] = {"published": {"a"}, "publish_failed": {"b"}}
            assert appmod._drain_torrent_to_library("drain", drain_dir) is False  # есть провал
            assert drain_dir.is_dir()
            appmod.JOBS["drain"] = {"published": {"a"}, "publish_failed": set()}
            assert appmod._drain_torrent_to_library("drain", drain_dir) is True
            assert not drain_dir.exists()   # уехало в библиотеку — удалено с сервера
        finally:
            appmod.SHARE_PATH = None

        # --- 9. /api/library/add копирует ВСЕ готовые файлы задачи, а не
        # только самый большой (раньше вторая серия молча терялась) ---
        share2 = tmp / "share2"
        share2.mkdir()
        appmod.SHARE_PATH = share2
        try:
            multi_dir = tmp / "jobmulti"
            sub2 = multi_dir / "Season"
            sub2.mkdir(parents=True)
            (sub2 / "E01.mkv").write_bytes(b"x" * 300)
            (sub2 / "E02.mkv").write_bytes(b"y" * 100)
            r = client.post("/api/library/add", json={"job_id": "jobmulti"})
            assert r.status_code == 200, r.text
            assert sorted(r.json()["names"]) == ["E01.mkv", "E02.mkv"], r.json()
            assert (share2 / "E01.mkv").stat().st_size == 300
            assert (share2 / "E02.mkv").stat().st_size == 100
        finally:
            appmod.SHARE_PATH = None

        # --- 10. _capture_magnet_metadata: переименовывает .torrent, который
        # aria2 (--bt-save-metadata) сохранил для magnet-раздачи, в
        # канонический .meta.torrent — без этого голый magnet нельзя ни
        # докачать, ни возобновить после краша ---
        magnet_dir = tmp / "jobmagnet"
        magnet_dir.mkdir()
        raw_meta = magnet_dir / "deadbeef.torrent"
        raw_meta.write_bytes(make_torrent("MagnetShow", [("only.mkv", 42)]))
        appmod._capture_magnet_metadata(magnet_dir, magnet_dir / ".meta.torrent")
        assert not raw_meta.exists()
        assert appmod._torrent_root_name(magnet_dir / ".meta.torrent") == "MagnetShow"

        # --- 11. _maybe_continue_unselected: с флагом .auto_rest, как только
        # выбранный файл готов, сама добавляет оставшиеся в .selected и
        # пробует продолжить закачку (упадёт без aria2 — это ок, как в п.7) ---
        auto_dir = tmp / "jobauto"
        auto_dir.mkdir()
        auto_meta = auto_dir / ".meta.torrent"
        auto_meta.write_bytes(make_torrent("AutoShow", [("A.mkv", 10), ("B.mkv", 20)]))
        (auto_dir / ".selected").write_text("1")
        (auto_dir / ".auto_rest").touch()
        (auto_dir / "AutoShow").mkdir()
        (auto_dir / "AutoShow" / "A.mkv").write_bytes(b"x" * 10)   # выбранный готов
        appmod.JOBS["jobauto"] = {
            "state": "done", "percent": 100, "speed": None, "eta": None,
            "title": None, "filename": "A.mkv", "error": None, "cancel": False,
            "total": None, "size": 10, "type": "torrent",
        }
        appmod._maybe_continue_unselected("jobauto", auto_dir, auto_meta)
        assert (auto_dir / ".selected").read_text() == "1,2"
        assert appmod.JOBS["jobauto"]["state"] == "error"  # aria2 нет — это ок

        # --- 12. /api/torrent/{id}/resume: только для задач с сохранёнными
        # метаданными, не для уже активных, уважает лимит очереди ---
        resume_dir = tmp / "jobresume"
        resume_dir.mkdir()
        r = client.post("/api/torrent/jobresume/resume")
        assert r.status_code == 400, r.text   # нет .meta.torrent

        (resume_dir / ".meta.torrent").write_bytes(
            make_torrent("ResumeShow", [("R.mkv", 5)]))
        (resume_dir / ".torrent_job").touch()
        r = client.post("/api/torrent/jobresume/resume")
        assert r.status_code == 200, r.text
        time.sleep(0.5)
        assert appmod.JOBS["jobresume"]["state"] == "error"  # aria2 нет — это ок

        appmod.JOBS["jobresume"]["state"] = "downloading"
        r = client.post("/api/torrent/jobresume/resume")
        assert r.status_code == 409, r.text   # уже качается

        appmod.JOBS["jobresume"]["state"] = "error"
        old_ceiling = appmod.JOB_CEILING
        appmod.JOB_CEILING = 0
        try:
            r = client.post("/api/torrent/jobresume/resume")
            assert r.status_code == 429, r.text
        finally:
            appmod.JOB_CEILING = old_ceiling

        # --- 13. /api/files показывает прерванную раздачу с прогрессом —
        # раньше такая карточка просто пропадала (ни одного готового файла) ---
        interrupted_dir = tmp / "jobinterrupted"
        interrupted_dir.mkdir()
        int_meta = interrupted_dir / ".meta.torrent"
        int_meta.write_bytes(
            make_torrent("Interrupted", [("I1.mkv", 100), ("I2.mkv", 100)]))
        (interrupted_dir / ".torrent_job").touch()
        (interrupted_dir / ".selected").write_text("1,2")
        sub3 = interrupted_dir / "Interrupted"
        sub3.mkdir()
        (sub3 / "I1.mkv").write_bytes(b"x" * 100)   # готов
        # I2.mkv вообще не начат; задачи в JOBS нет — как после рестарта сервиса
        appmod.JOBS.pop("jobinterrupted", None)
        r = client.get("/api/files")
        assert r.status_code == 200, r.text
        items = {i["job_id"]: i for i in r.json()["files"]}
        assert "jobinterrupted" in items, items.keys()
        item = items["jobinterrupted"]
        assert item["interrupted"] is True, item
        assert item["progress"]["done_files"] == 1, item
        assert item["progress"]["total_files"] == 2, item
        assert item["progress"]["percent"] == 50, item

        # --- 14. _resume_interrupted_torrents: при старте сервиса сама
        # подхватывает прерванную раздачу из п.13 (в JOBS её нет — ровно как
        # после рестарта сервиса) ---
        assert "jobinterrupted" not in appmod.JOBS
        appmod._resume_interrupted_torrents()
        time.sleep(0.5)
        assert "jobinterrupted" in appmod.JOBS
        assert appmod.JOBS["jobinterrupted"]["state"] == "error"  # aria2 нет — это ок

        # --- 15. Реальный баг: для многофайлового BT-торрента aria2 ведёт
        # ОДИН .aria2-файл на весь торрент (лежит рядом с папкой раздачи, не
        # по файлу на каждую серию) — обрезанный файл без соседнего .aria2
        # раньше ложно считался готовым (не хватило пиров на конкретную
        # серию → торрент завершился, файл остался обрезанным, но его всё
        # равно публиковали в SMB-библиотеку как «готовый»). Теперь
        # готовность сверяется с реальным ожидаемым размером из метаданных ---
        trunc_dir = tmp / "jobtruncated"
        trunc_meta = trunc_dir / ".meta.torrent"
        trunc_sub = trunc_dir / "TruncShow"
        trunc_sub.mkdir(parents=True)
        trunc_meta.write_bytes(
            make_torrent("TruncShow", [("A.mkv", 1_000_000), ("B.mkv", 2_000_000)]))
        (trunc_dir / ".selected").write_text("1,2")
        (trunc_sub / "A.mkv").write_bytes(b"x" * 1_000_000)   # реально готов
        (trunc_sub / "B.mkv").write_bytes(b"y" * 2_000)       # обрезан, но БЕЗ .aria2
        # единственный служебный файл aria2 для всего торрента — снаружи подпапки,
        # с именем торрента, не серии (так реально ведёт себя aria2 для BT)
        (trunc_dir / "TruncShow.aria2").write_bytes(b"ctrl")

        status = {f["path"]: f for f in appmod._torrent_status_files(trunc_dir)}
        assert status["TruncShow/A.mkv"]["done"] is True, status
        assert status["TruncShow/B.mkv"]["done"] is False, status   # раньше было бы True

        real = {p.name for p in appmod._real_job_files(trunc_dir)}
        assert real == {"A.mkv"}, real   # B.mkv обрезан — не должен считаться готовым

        result = appmod._result_file(trunc_dir)
        assert result is not None and result.name == "A.mkv", result

        appmod.JOBS["jobtruncated"] = {
            "state": "done", "percent": 100, "speed": None, "eta": None,
            "title": None, "filename": "A.mkv", "error": None, "cancel": False,
            "total": None, "size": 1_000_000, "type": "torrent",
        }
        r = client.get("/api/file/jobtruncated", params={"path": "TruncShow/B.mkv"})
        assert r.status_code == 409, r.text   # обрезанный файл не отдаём

        # --- 16. /api/torrent/{id}/pause: гварды и флаг (без реального aria2 —
        # эндпоинт только помечает job["pause"], сам процесс not involved) ---
        r = client.post("/api/torrent/nosuchjob/pause")
        assert r.status_code == 404, r.text

        pause_dir = tmp / "jobpause"
        pause_dir.mkdir()
        appmod.JOBS["jobpause"] = {
            "state": "downloading", "percent": 30, "speed": None, "eta": None,
            "title": None, "filename": None, "error": None, "cancel": False,
            "total": None, "size": None, "type": "yt-dlp"}
        r = client.post("/api/torrent/jobpause/pause")
        assert r.status_code == 400, r.text   # пауза только для торрентов

        appmod.JOBS["jobpause"]["type"] = "torrent"
        appmod.JOBS["jobpause"]["state"] = "done"
        r = client.post("/api/torrent/jobpause/pause")
        assert r.status_code == 409, r.text   # не качается — паузить нечего

        appmod.JOBS["jobpause"]["state"] = "downloading"
        r = client.post("/api/torrent/jobpause/pause")
        assert r.status_code == 200, r.text
        assert appmod.JOBS["jobpause"]["pause"] is True

        # --- 17. Пауза внутри _run_torrent (в отличие от cancel) не удаляет
        # папку задачи — с фейковым aria2-процессом (реального в тестах нет,
        # см. остальные пункты), чтобы дойти до самой ветки job["pause"]
        # внутри цикла чтения stdout, а не только до "aria2 не установлен" ---
        class _FakeAria2Proc:
            def __init__(self):
                self._n = 0
                self.stdout = self
                self.terminated = False
            def __iter__(self):
                return self
            def __next__(self):
                self._n += 1
                if self._n > 2000:
                    raise StopIteration
                time.sleep(0.01)   # имитация реального времени между строками
                                    # прогресса — иначе pause не успеет прийти
                return "[#1 SIZE:1MiB/2MiB(50%) DL:1MiB ETA:1s]\n"
            def terminate(self):
                self.terminated = True
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass

        run_dir = tmp / "jobrun"
        run_dir.mkdir()
        (run_dir / "keep.txt").write_bytes(b"x")   # имитация уже скачанного
        appmod.JOBS["jobrun"] = {
            "state": "downloading", "percent": 10, "speed": None, "eta": None,
            "title": None, "filename": None, "error": None, "cancel": False,
            "total": None, "size": None, "type": "torrent"}
        old_popen = appmod.subprocess.Popen
        appmod.subprocess.Popen = lambda *a, **k: _FakeAria2Proc()
        try:
            th = threading.Thread(
                target=appmod._run_torrent,
                args=("jobrun", str(run_dir / ".meta.torrent")), daemon=True)
            th.start()
            time.sleep(0.2)
            with appmod.JOBS_LOCK:
                appmod.JOBS["jobrun"]["pause"] = True
            th.join(timeout=5)
            assert not th.is_alive(), "пауза должна была завершить поток"
        finally:
            appmod.subprocess.Popen = old_popen
        assert appmod.JOBS["jobrun"]["state"] == "paused", appmod.JOBS["jobrun"]
        assert appmod.JOBS["jobrun"]["pause"] is False   # флаг сброшен
        assert run_dir.is_dir(), "пауза не должна удалять папку задачи"
        assert (run_dir / "keep.txt").is_file()

        # --- 18. /api/torrent/{id}/resume разрешает возобновление из
        # state="paused" (не только error/interrupted — тот же guard) ---
        appmod.JOBS["jobresume"]["state"] = "paused"
        r = client.post("/api/torrent/jobresume/resume")
        assert r.status_code == 200, r.text
        time.sleep(0.3)

        # --- 19. /api/jobs включает paused (карточка не пропадает с F5),
        # /api/files не дублирует её как "прервана" ---
        jobs_dir = tmp / "jobpaused2"
        jobs_dir.mkdir()
        (jobs_dir / ".torrent_job").touch()
        appmod.JOBS["jobpaused2"] = {
            "state": "paused", "percent": 55, "speed": None, "eta": None,
            "title": "Paused Show", "filename": None, "error": None,
            "cancel": False, "total": None, "size": None, "type": "torrent"}
        r = client.get("/api/jobs")
        ids = {j["job_id"] for j in r.json()["jobs"]}
        assert "jobpaused2" in ids, ids
        r = client.get("/api/files")
        ids2 = {i["job_id"] for i in r.json()["files"]}
        assert "jobpaused2" not in ids2, ids2   # не дублируем — уже в /api/jobs

        # --- 20. _is_temp исключает remux-кэш плеера (.<имя>.play.mp4) из
        # списка реальных файлов раздачи и из живой таблички ---
        cache_dir = tmp / "jobcache"
        cache_dir.mkdir()
        (cache_dir / "Episode.mkv").write_bytes(b"x" * 100)
        (cache_dir / ".Episode.mkv.play.mp4").write_bytes(b"y" * 100)
        real_names = {p.name for p in appmod._real_job_files(cache_dir)}
        assert real_names == {"Episode.mkv"}, real_names
        rows = {f["path"] for f in appmod._torrent_status_files(cache_dir)}
        assert rows == {"Episode.mkv"}, rows

        # --- 21. /api/play: нативный формат отдаётся сразу inline с верным
        # Content-Type; не нативный (mkv) перепаковывается на лету, БЕЗ
        # кэш-файла на диске (раньше один просмотр 8-гигового фильма съедал
        # ещё 8 ГБ на диске VM и заставлял ждать конца перепаковки). probe=1
        # отдаёт плееру справку (нативный ли формат + длительность для своего
        # ползунка), а без ffmpeg приходит понятная 500 ---
        play_native_dir = tmp / "jobplaynative"
        play_native_dir.mkdir()
        (play_native_dir / "movie.mp4").write_bytes(b"x" * 10)
        r = client.get("/api/play/jobplaynative")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("video/mp4"), r.headers
        assert "inline" in r.headers.get("content-disposition", ""), r.headers

        play_mkv_dir = tmp / "jobplaymkv"
        play_mkv_dir.mkdir()
        (play_mkv_dir / "movie.mkv").write_bytes(b"x" * 10)
        old_ffmpeg, old_ffprobe = appmod.FFMPEG_BIN, appmod.FFPROBE_BIN
        appmod.FFMPEG_BIN = "definitely-not-a-real-ffmpeg-binary"
        appmod.FFPROBE_BIN = "definitely-not-a-real-ffprobe-binary"
        try:
            r = client.get("/api/play/jobplaynative", params={"probe": 1})
            assert r.status_code == 200 and r.json()["native"] is True, r.text

            r = client.get("/api/play/jobplaymkv", params={"probe": 1})
            assert r.status_code == 200, r.text
            assert r.json()["native"] is False, r.text
            assert r.json()["duration"] is None, r.text   # ffprobe нет — это ок

            r = client.get("/api/play/jobplaymkv")
            assert r.status_code == 500, r.text
            assert "ffmpeg" in r.json()["detail"].lower(), r.text
            # никакого кэша рядом с исходником больше не появляется
            assert not appmod._play_cache_path(play_mkv_dir / "movie.mkv").exists()
        finally:
            appmod.FFMPEG_BIN, appmod.FFPROBE_BIN = old_ffmpeg, old_ffprobe

        # --- 21b. Реальный баг: в плеере был чёрный квадрат. Раздачи с
        # трекера — это часто HEVC + AC3/DTS, а `-c copy` тащил их в mp4 как
        # есть: DTS туда вообще не мукается (ffmpeg падал на старте, поток
        # приходил пустым), HEVC/AC3 не декодирует браузер. Копируем дорожку
        # только если её поймёт браузер, иначе перекодируем ---
        args = appmod._remux_codec_args({"video": "h264", "audio": "aac"})
        assert args == ["-c:v", "copy", "-c:a", "copy"], args
        args = appmod._remux_codec_args({"video": "hevc", "audio": "dts"})
        assert "libx264" in args and "aac" in args and "copy" not in args, args
        args = appmod._remux_codec_args({"video": "h264", "audio": "eac3"})
        assert args[:2] == ["-c:v", "copy"] and "aac" in args, args
        # ffprobe не сработал (пустой dict) — перекодируем на всякий случай,
        # это хуже по нагрузке, но играет всегда
        assert "copy" not in appmod._remux_codec_args({})

        # --- 21в. Реальный баг: /api/play с настоящим ffmpeg падал с
        # NameError (`_safe_name` вместо `safe_name`) — браузер получал 500
        # вместо видео и показывал чёрный квадрат. Проверки выше это
        # пропускали: они подменяли ffmpeg заведомо несуществующим бинарником
        # и до строчки с ошибкой не доходили. Поэтому здесь — настоящий
        # ffmpeg и настоящий поток. Без ffmpeg на машине проверка пропускается
        # (на VM он есть, см. память test-env-has-real-binaries) ---
        if shutil.which(appmod.FFMPEG_BIN):
            real_dir = tmp / "jobrealplay"
            real_dir.mkdir()
            gen = subprocess.run(
                [appmod.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10",
                 "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 str(real_dir / "movie.mkv")], capture_output=True)
            assert gen.returncode == 0, gen.stderr[-400:]
            r = client.get("/api/play/jobrealplay")
            assert r.status_code == 200, r.text[:400]
            assert r.content[4:8] == b"ftyp", r.content[:32]   # это mp4, а не 500
            # diag=1: та же команда с замером — чем чинить «не воспроизводится»
            d = client.get("/api/play/jobrealplay", params={"diag": 1}).json()
            assert d["code"] == 0 and d["bytes"] > 0, d

        # --- 22. Реальный баг: aria2 (--file-allocation=none) пишет куски
        # вразнобой, и как только записан ПОСЛЕДНИЙ кусок, st_size файла
        # становится полным, а середина — дыры (sparse), читаются нулями.
        # Проверка "st_size == ожидаемый размер" такой файл считала готовым:
        # он уезжал в SMB-библиотеку (8 ГБ нулей, ffprobe не находит даже
        # EBML-заголовка), исходник удалялся, а в табличке файл показывал
        # "качается... 99%" через секунду после добавления раздачи ---
        sparse_dir = tmp / "jobsparse"
        sparse_sub = sparse_dir / "SparseShow"
        sparse_sub.mkdir(parents=True)
        (sparse_dir / ".meta.torrent").write_bytes(
            make_torrent("SparseShow", [("S.mkv", 10_000_000)]))
        sparse_file = sparse_sub / "S.mkv"
        with open(sparse_file, "wb") as fh:
            fh.truncate(10_000_000)      # дыра на весь файл, блоки не выделены
            fh.seek(9_999_999)
            fh.write(b"x")               # записан только последний кусок
        assert sparse_file.stat().st_size == 10_000_000   # st_size уже "полный"

        if hasattr(sparse_file.stat(), "st_blocks"):      # Unix; на Windows нет sparse
            assert appmod._written_bytes(sparse_file) < 1_000_000
            assert appmod._file_done(sparse_file, 10_000_000) is False
            assert appmod._real_job_files(sparse_dir) == []
            row = appmod._torrent_status_files(sparse_dir)[0]
            assert row["done"] is False, row
            assert row["percent"] == 0, row
            # а докачанный целиком (реально записанный) — по-прежнему готов
            sparse_file.write_bytes(b"x" * 10_000_000)
            assert appmod._file_done(sparse_file, 10_000_000) is True
            assert appmod._torrent_status_files(sparse_dir)[0]["percent"] == 100
        else:
            print("  (п.22: sparse-проверка пропущена — не Unix)")

        # --- 23. Место на диске: /api/files отдаёт свободно/всего (сайт
        # показывает это в шапке «Файлы на сервере»), а _require_disk_space
        # умеет проверять не только запас, но и конкретный нужный объём —
        # чтобы не начинать раздачу, которая заведомо не влезет ---
        disk = client.get("/api/files").json()["disk"]
        assert disk["total"] > 0 and disk["free"] > 0, disk
        appmod._require_disk_space()             # запас есть — не бросает
        appmod._require_disk_space(1024)         # килобайт тоже влезет
        try:
            appmod._require_disk_space(10 ** 18)   # эксабайт — точно нет
            raise AssertionError("должно было отказать по месту")
        except Exception as e:
            assert getattr(e, "status_code", None) == 507, e
            assert "не хватит места" in str(e.detail).lower(), e.detail

        # сумма выбранных файлов раздачи (meta из п.1: MyShow 100/200/300)
        assert appmod._selected_size(str(meta), [1, 3]) == 400
        assert appmod._selected_size(str(meta), None) == 600     # вся раздача
        assert appmod._selected_size("magnet:?xt=urn:btih:ABC") == 0  # размер не известен

        # --- 24. Порядок файлов раздачи — как в самом торренте (index), а не
        # по размеру: иначе серии в табличке шли вперемешку. Без метаданных
        # (плоский список с диска) — по имени, с числами как числами ---
        order_dir = tmp / "joborder"
        order_sub = order_dir / "OrderShow"
        order_sub.mkdir(parents=True)
        order_dir.joinpath(".meta.torrent").write_bytes(make_torrent(
            "OrderShow", [("E01.mkv", 300), ("E02.mkv", 100), ("E03.mkv", 200)]))
        paths = [f["path"] for f in appmod._torrent_status_files(order_dir)]
        assert paths == ["OrderShow/E01.mkv", "OrderShow/E02.mkv",
                         "OrderShow/E03.mkv"], paths

        flat_dir = tmp / "jobflat"
        flat_dir.mkdir()
        for n, size in (("E10.mkv", 300), ("E2.mkv", 100), ("E1.mkv", 200)):
            (flat_dir / n).write_bytes(b"x" * size)
        flat = [f["path"] for f in appmod._torrent_status_files(flat_dir)]
        assert flat == ["E1.mkv", "E2.mkv", "E10.mkv"], flat   # E2 раньше E10

        # --- 25. /api/library/play: просмотр файла SMB-витрины тем же плеером
        # (общая логика с /api/play — см. _play_response), с той же защитой от
        # выхода за пределы шары, что и у /api/library/file ---
        share3 = tmp / "share3"
        share3.mkdir()
        appmod.SHARE_PATH = share3
        # ffprobe подменяем, как в п.21: иначе probe=1 зовёт настоящий ffprobe
        # на 10-байтовом «файле», и тест зависит от того, что стоит на машине.
        appmod.FFPROBE_BIN = "definitely-not-a-real-ffprobe-binary"
        try:
            (share3 / "movie.mp4").write_bytes(b"x" * 10)
            r = client.get("/api/library/play", params={"name": "movie.mp4"})
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith("video/mp4"), r.headers
            assert "inline" in r.headers.get("content-disposition", ""), r.headers

            r = client.get("/api/library/play",
                            params={"name": "movie.mp4", "probe": 1})
            assert r.json()["native"] is True, r.text
            assert r.json()["name"] == "movie.mp4", r.text

            r = client.get("/api/library/play", params={"name": "../../etc/passwd"})
            assert r.status_code in (400, 404), r.text   # за пределы шары нельзя
            r = client.get("/api/library/play", params={"name": "нет-такого.mp4"})
            assert r.status_code == 404, r.text
        finally:
            appmod.SHARE_PATH = None
            appmod.FFPROBE_BIN = old_ffprobe

        print("OK: все проверки живого статуса файлов торрента прошли")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
