from unittest.mock import Mock
import threading

import pytest
from fastapi import HTTPException

from test_instance_staging import instances, make_job


def test_file_pause_resume_and_restart(instances, monkeypatch):
    app, _ = instances
    job_dir = make_job(app.STAGING_DIR, "aaa", torrent=True)
    app.JOBS["aaa"] = {"type": "torrent", "state": "downloading"}
    app.torrent_pause_files("aaa", app.TorrentAddFilesRequest(files=[1]))
    assert app._selected_indices(job_dir) == set()
    assert app._torrent_status_files(job_dir)[0]["paused"]
    app._finish_pause("aaa", str(job_dir / ".meta.torrent"))
    assert app.JOBS["aaa"]["state"] == "paused"
    with pytest.raises(HTTPException):
        app.torrent_resume("aaa")
    app.JOBS.clear()
    workers = Mock()
    monkeypatch.setattr(app.threading, "Thread", workers)
    app._resume_interrupted_torrents()
    workers.assert_not_called()
    app.torrent_add_files("aaa", app.TorrentAddFilesRequest(files=[1]))
    assert app._selected_indices(job_dir) == {1}
    assert app._paused_indices(job_dir) == set()
    assert workers.call_count == 1


def test_delete_preserves_or_removes_owned_files(instances):
    app, _ = instances
    keep = make_job(app.STAGING_DIR, "aaa", torrent=True)
    result = app.torrent_delete("aaa", app.TorrentDeleteRequest(delete_files=False))
    assert result["ok"] and (keep / "video.mkv").read_bytes() == b"video"
    assert not (keep / ".torrent_job").exists()
    job = make_job(app.STAGING_DIR, "bbb", torrent=True)
    (job / ".published").write_text('{"video.mkv":"saved.mkv"}', encoding="utf-8")
    (app.SHARE_PATH / "saved.mkv").write_bytes(b"saved")
    app.torrent_delete("bbb", app.TorrentDeleteRequest(delete_files=True))
    assert not job.exists() and not (app.SHARE_PATH / "saved.mkv").exists()


def test_delete_does_not_remove_unknown_library_file(instances):
    app, _ = instances
    job = make_job(app.STAGING_DIR, "aaa", torrent=True)
    (job / ".published").write_text('not json', encoding="utf-8")
    unrelated = app.SHARE_PATH / "unrelated.mkv"
    unrelated.write_bytes(b"safe")
    result = app.torrent_delete("aaa", app.TorrentDeleteRequest(delete_files=True))
    assert unrelated.exists() and result["warnings"]


def test_torrent_pause_survives_restart(instances, monkeypatch):
    app, _ = instances
    job = make_job(app.STAGING_DIR, "aaa", torrent=True)
    app.JOBS["aaa"] = {"type": "torrent", "state": "downloading"}
    app.torrent_pause("aaa")
    assert (job / ".paused").exists()
    app.JOBS.clear()
    workers = Mock()
    monkeypatch.setattr(app.threading, "Thread", workers)
    app._resume_interrupted_torrents()
    workers.assert_not_called()
    app.torrent_resume("aaa")
    assert not (job / ".paused").exists()
    assert workers.call_count == 1


def test_delete_blocks_restart_even_without_in_memory_job(instances, monkeypatch):
    app, _ = instances
    job = make_job(app.STAGING_DIR, "aaa", torrent=True)
    entered, release = threading.Event(), threading.Event()
    original = app.shutil.rmtree

    def blocked_delete(path, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(app.shutil, "rmtree", blocked_delete)
    worker = threading.Thread(target=app.torrent_delete, args=("aaa", app.TorrentDeleteRequest(delete_files=True)))
    worker.start()
    try:
        assert entered.wait(5)
        for action in (
            lambda: app.torrent_resume("aaa"),
            lambda: app.torrent_add_files("aaa", app.TorrentAddFilesRequest(files=[1])),
            lambda: app.torrent_pause_files("aaa", app.TorrentAddFilesRequest(files=[1])),
            lambda: app.torrent_delete("aaa", app.TorrentDeleteRequest(delete_files=True)),
        ):
            with pytest.raises(HTTPException) as exc:
                action()
            assert exc.value.status_code == 409
        assert job.exists()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not job.exists() and "aaa" not in app.JOBS


def test_delete_waits_for_processing_to_finish(instances):
    app, _ = instances
    job = make_job(app.STAGING_DIR, "aaa", torrent=True)
    app.JOBS["aaa"] = {"type": "torrent", "state": "processing"}
    worker = threading.Thread(target=app.torrent_delete, args=("aaa", app.TorrentDeleteRequest(delete_files=True)))
    worker.start()
    try:
        for _ in range(100):
            with app.JOBS_LOCK:
                if app.JOBS["aaa"].get("deleting"):
                    break
            threading.Event().wait(0.01)
        assert job.exists()
        with app.JOBS_LOCK:
            assert app.JOBS["aaa"]["pause"]
            app.JOBS["aaa"]["state"] = "paused"
    finally:
        worker.join(5)
    assert not worker.is_alive() and not job.exists()
