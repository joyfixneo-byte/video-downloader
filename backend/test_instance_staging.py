"""Two independent app processes sharing SMB; no network, aria2 or background jobs."""
import importlib.util
import os
import socket
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import HTTPException


def load_instance(monkeypatch, tmp_path, name, instance_id):
    monkeypatch.setenv("SHARE_DIR", str(tmp_path / "share"))
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / name))
    if instance_id is None:
        monkeypatch.delenv("INSTANCE_ID", raising=False)
    else:
        monkeypatch.setenv("INSTANCE_ID", instance_id)
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("app.py"))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    with monkeypatch.context() as context:
        context.setattr(threading.Thread, "start", lambda self: None)
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def instances(tmp_path, monkeypatch):
    a = load_instance(monkeypatch, tmp_path, "instance_apps", "apps")
    b = load_instance(monkeypatch, tmp_path, "instance_vpn", "apps-vpn")
    for instance in (a, b):
        instance.STAGING_DIR.mkdir(parents=True)
        monkeypatch.setattr(instance, "_require_disk_space", lambda *args: None)
        monkeypatch.setattr(instance, "_require_library", lambda: None)
    return a, b


def make_job(root, job_id, torrent=False):
    directory = root / job_id
    directory.mkdir(parents=True)
    (directory / "video.mkv").write_bytes(b"video")
    if torrent:
        (directory / ".torrent_job").touch()
        # Single-file torrent: expected length exceeds the downloaded file.
        (directory / ".meta.torrent").write_bytes(
            b"d4:infod6:lengthi100e4:name9:video.mkvee")
        (directory / ".selected").write_text("1")
    return directory


def test_namespaces_and_common_library(instances):
    a, b = instances
    assert a.STAGING_DIR == a.SHARE_PATH / ".staging" / "apps"
    assert b.STAGING_DIR == a.SHARE_PATH / ".staging" / "apps-vpn"
    assert a.TORRENT_META_DIR != b.TORRENT_META_DIR
    make_job(a.STAGING_DIR, "aaa")
    make_job(b.STAGING_DIR, "bbb")
    make_job(a.SHARE_PATH / ".staging", "oldshared")
    make_job(a.DOWNLOAD_DIR, "legacy")
    make_job(b.DOWNLOAD_DIR, "otherlegacy")
    (a.SHARE_PATH / "shared.mkv").write_bytes(b"finished")
    assert {item["job_id"] for item in a.list_files()["files"]} == {"aaa", "legacy"}
    assert {item["job_id"] for item in b.list_files()["files"]} == {"bbb", "otherlegacy"}
    assert a.library_list() == b.library_list()
    assert [item["name"] for item in a.library_list()["files"]] == ["shared.mkv"]


def test_auto_resume_only_own_and_local_legacy(instances, monkeypatch):
    a, b = instances
    own = make_job(a.STAGING_DIR, "aaa", torrent=True)
    legacy = make_job(a.DOWNLOAD_DIR, "legacy", torrent=True)
    foreign = make_job(b.STAGING_DIR, "bbb", torrent=True)
    old = make_job(a.SHARE_PATH / ".staging", "oldshared", torrent=True)
    workers = Mock()
    monkeypatch.setattr(a.threading, "Thread", workers)
    a._resume_interrupted_torrents()
    assert set(a.JOBS) == {"aaa", "legacy"}
    assert {call.kwargs["args"][1] for call in workers.call_args_list} == {
        str(own / ".meta.torrent"), str(legacy / ".meta.torrent")}
    assert b.JOBS == {}
    workers.reset_mock()
    b._resume_interrupted_torrents()
    assert set(b.JOBS) == {"bbb"}
    assert workers.call_args.kwargs["args"][1] == str(foreign / ".meta.torrent")
    assert old.exists()


def test_all_disk_job_apis_cannot_access_foreign_or_old_shared(instances, monkeypatch):
    a, b = instances
    workers = Mock()
    monkeypatch.setattr(a.threading, "Thread", workers)
    foreign = make_job(b.STAGING_DIR, "bbb", torrent=True)
    old = make_job(a.SHARE_PATH / ".staging", "oldshared", torrent=True)
    for directory in (foreign, old):
        job_id = directory.name
        for action in (
            lambda: a.get_file(job_id),
            lambda: a.play_file(job_id),
            lambda: a.torrent_job_files(job_id),
            lambda: a.torrent_resume(job_id),
            lambda: a.torrent_add_files(job_id, a.TorrentAddFilesRequest(files=[1])),
            lambda: a.torrent_delete_file(job_id, a.TorrentFileActionRequest(path="video.mkv")),
            lambda: a.library_add(a.LibraryAddRequest(job_id=job_id)),
            lambda: a.status(job_id),
            lambda: a.cancel(job_id),
            lambda: a.torrent_pause(job_id),
        ):
            with pytest.raises(HTTPException):
                action()
        a.delete_file(job_id)
        assert (directory / "video.mkv").read_bytes() == b"video"
    workers.assert_not_called()
    assert a.JOBS == {}


def test_identical_ids_delete_only_own_and_legacy_still_works(instances):
    a, b = instances
    own = make_job(a.STAGING_DIR, "same")
    foreign = make_job(b.STAGING_DIR, "same")
    legacy = make_job(a.DOWNLOAD_DIR, "legacy")
    assert Path(a.get_file("legacy").path) == legacy / "video.mkv"
    a.delete_file("same")
    assert not own.exists()
    assert foreign.exists()
    a.delete_file("legacy")
    assert not legacy.exists()


@pytest.mark.parametrize("job_id", ["", ".", "..", "///", "../apps-vpn", "a/b", "a!b", " a ", "a\\b"])
def test_invalid_ids_never_alias_an_existing_job_or_root(instances, job_id):
    a, _ = instances
    victim = make_job(a.STAGING_DIR, "ab")
    with pytest.raises(HTTPException) as exc:
        a.delete_file(job_id)
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        a._job_dir(job_id)
    assert victim.exists() and a.STAGING_DIR.exists() and a.DOWNLOAD_DIR.exists()


def test_cleanup_preserves_other_instance_and_cleans_legacy_transcripts(instances, monkeypatch):
    a, b = instances
    own = make_job(a.STAGING_DIR, "aaa")
    foreign = make_job(b.STAGING_DIR, "bbb")
    old = make_job(a.SHARE_PATH / ".staging", "oldshared")
    legacy = make_job(a.DOWNLOAD_DIR, "legacy")
    transcript = make_job(a.DOWNLOAD_DIR, "t_123abc")
    torrent = make_job(a.STAGING_DIR, "torrent", torrent=True)
    for directory in (own, foreign, old, legacy, transcript, torrent):
        for path in directory.iterdir():
            os.utime(path, (1, 1))

    class StopLoop(BaseException):
        pass

    def stop(_):
        raise StopLoop()

    monkeypatch.setattr(a.time, "sleep", stop)
    with pytest.raises(StopLoop):
        a._cleanup_loop()
    assert not any(d.exists() for d in (own, legacy, transcript))
    assert all(d.exists() for d in (foreign, old, torrent))


def test_preview_and_prune_are_isolated(instances, monkeypatch):
    a, b = instances
    def metadata(cmd, **kwargs):
        directory = Path(cmd[cmd.index("--dir") + 1])
        (directory / "preview.torrent").write_bytes(
            b"d4:infod6:lengthi100e4:name9:video.mkvee")
    monkeypatch.setattr(a.subprocess, "run", metadata)
    for instance in (a, b):
        monkeypatch.setattr(instance, "_resolve_torrent_source", lambda *args: "magnet:test")
    first = a.torrent_files_preview(a.TorrentRequest(magnet="magnet:test"))
    second = b.torrent_files_preview(b.TorrentRequest(magnet="magnet:test"))
    assert first["files"] == second["files"]
    with pytest.raises(HTTPException, match="устарел"):
        a.torrent_add(a.TorrentRequest(token=second["token"], files=[1]))
    a.TORRENT_META[first["token"]]["created"] = 0
    a._prune_torrent_meta()
    assert not (a.TORRENT_META_DIR / first["token"]).exists()
    assert (b.TORRENT_META_DIR / second["token"]).is_dir()


@pytest.mark.parametrize("instance_id", ["", "..", "../apps", "apps/vpn", "apps\\vpn", " x", "x" * 64])
def test_invalid_instance_id_fails_startup(tmp_path, monkeypatch, instance_id):
    with pytest.raises(ValueError, match="INSTANCE_ID"):
        load_instance(monkeypatch, tmp_path, "bad_instance", instance_id)


def test_hostname_default_is_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "APPS-VPN")
    a = load_instance(monkeypatch, tmp_path, "default_instance", None)
    b = load_instance(monkeypatch, tmp_path, "restarted_instance", None)
    assert a.INSTANCE_ID == "apps-vpn"
    assert a.STAGING_DIR == b.STAGING_DIR


@pytest.mark.parametrize("location", ["job", "legacy", "instance", "meta"])
def test_directory_links_are_not_processed(instances, monkeypatch, location):
    a, b = instances
    foreign = make_job(b.STAGING_DIR, "bbb", torrent=True)
    if location == "job":
        link = a.STAGING_DIR / "linked"
    elif location == "legacy":
        link = a.DOWNLOAD_DIR / "linked"
    elif location == "instance":
        link = a.STAGING_DIR
        link.rmdir()
    else:
        link = a.TORRENT_META_DIR
    try:
        link.symlink_to(foreign, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Directory symlinks unavailable: {exc}")
    if location == "meta":
        with pytest.raises(HTTPException):
            a._prune_torrent_meta()
    else:
        with pytest.raises(HTTPException):
            a.delete_file("linked")
        assert list(a._iter_job_dirs()) == []
        workers = Mock()
        monkeypatch.setattr(a.threading, "Thread", workers)
        a._resume_interrupted_torrents()
        workers.assert_not_called()
    assert (foreign / "video.mkv").read_bytes() == b"video"


def test_without_share_legacy_lookup_still_works(instances, monkeypatch):
    a, _ = instances
    monkeypatch.setattr(a, "SHARE_PATH", None)
    monkeypatch.setattr(a, "STAGING_DIR", None)
    legacy = make_job(a.DOWNLOAD_DIR, "legacy")
    assert a._job_dir("legacy") == legacy
    assert list(a._iter_job_dirs()) == [legacy]


def test_linked_payload_is_not_listed_downloaded_or_published(instances):
    a, b = instances
    own = a.STAGING_DIR / "aaa"
    own.mkdir()
    foreign = make_job(b.STAGING_DIR, "bbb") / "video.mkv"
    try:
        (own / "link.mkv").symlink_to(foreign)
    except OSError as exc:
        pytest.skip(f"File symlinks unavailable: {exc}")
    assert a.list_files()["files"] == []
    assert a.torrent_job_files("aaa")["files"] == []
    for action in (
        lambda: a.get_file("aaa"),
        lambda: a.get_file("aaa", path="link.mkv"),
        lambda: a.library_add(a.LibraryAddRequest(job_id="aaa")),
    ):
        with pytest.raises(HTTPException):
            action()
    assert foreign.read_bytes() == b"video"
