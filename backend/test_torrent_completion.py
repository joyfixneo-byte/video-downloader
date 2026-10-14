"""Offline completion, publication failure and resume regressions."""
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
with patch.object(threading.Thread, "start"):
    import app
from test_torrent_files import make_torrent


@pytest.fixture
def torrent(tmp_path, monkeypatch):
    share = tmp_path / "share"
    staging = share / ".staging" / "tests"
    job = staging / "job"
    (job / "Show").mkdir(parents=True)
    (job / ".meta.torrent").write_bytes(make_torrent(
        "Show", [("first.mkv", 100), ("second.mkv", 200), ("third.mkv", 300)]))
    (job / ".torrent_job").touch()
    monkeypatch.setattr(app, "SHARE_PATH", share)
    monkeypatch.setattr(app, "STAGING_DIR", staging)
    monkeypatch.setattr(app, "DOWNLOAD_DIR", tmp_path / "legacy")
    monkeypatch.setattr(app, "_require_library", lambda: None)
    monkeypatch.setattr(app, "JOBS", {"job": {
        "state": "queued", "type": "torrent", "cancel": False, "pause": False}})
    processes = []

    def spawn(cmd, **kwargs):
        proc = Mock(stdout=iter([]))
        proc.wait.return_value = 0
        processes.append((cmd, proc))
        return proc

    monkeypatch.setattr(app.subprocess, "Popen", Mock(side_effect=spawn))
    return job, share, processes


def run(job, select=(1,), **kwargs):
    app._run_torrent("job", str(job / ".meta.torrent"),
                     list(select) if select is not None else None, **kwargs)


def published(share):
    return sorted(p.name for p in share.iterdir() if p.is_file())


@pytest.mark.parametrize("size", [None, 20, 100])
@pytest.mark.parametrize("code", [0, 1])
def test_completion_requires_exit_success_and_every_selected_file(torrent, monkeypatch, size, code):
    job, share, _ = torrent
    first = job / "Show/first.mkv"
    if size is not None:
        first.write_bytes(b"a" * size)
    proc = Mock(stdout=iter(["[#abc 100B/100B(100%) CN:1]"]))
    proc.wait.return_value = code
    monkeypatch.setattr(app.subprocess, "Popen", Mock(return_value=proc))
    run(job, check_integrity=True)
    state = app.JOBS["job"]
    if size == 100 and code == 0:
        assert state["state"] == "done" and state["in_library"]
        assert published(share) == ["first.mkv"]
    else:
        assert state["state"] == "error" and not state["in_library"]
        assert state["percent"] != 100
        assert state["error"]
        assert published(share) == []
        assert first.exists() == (size is not None)
        assert not (job / "first.mkv").exists()
        assert app._selected_indices(job) == {1}


def test_entire_selection_is_validated_before_any_publication(torrent):
    job, share, _ = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    (job / "Show/second.mkv").write_bytes(b"b" * 20)
    run(job, (1, 2), check_integrity=True)
    assert app.JOBS["job"]["state"] == "error"
    assert published(share) == []
    assert (job / "Show/first.mkv").exists()
    assert app._drain_torrent_to_library("job", job) is False


@pytest.mark.parametrize("unselected_size", [20, 200])
def test_success_preserves_unselected_files_and_skeleton(torrent, unselected_size):
    job, share, processes = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    remaining = job / "Show/second.mkv"
    remaining.write_bytes(b"b" * unselected_size)
    run(job)
    assert app.JOBS["job"]["state"] == "done"
    assert len(processes) == 2
    assert "--check-integrity=true" not in processes[0][0]
    assert "--check-integrity=true" in processes[1][0]
    assert published(share) == ["first.mkv"]
    assert remaining.read_bytes() == b"b" * unselected_size
    assert (job / ".torrent_job").exists() and (job / ".meta.torrent").exists()
    assert app._selected_indices(job) == set()
    assert app._published_map(job) == {"Show/first.mkv": "first.mkv"}
    run(job, None)
    assert app.JOBS["job"]["state"] == "done"
    assert len(processes) == 2, "empty selection must not restart the entire torrent"


def test_recheck_failure_never_publishes(torrent, monkeypatch):
    job, share, _ = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    procs = [Mock(stdout=iter([])), Mock(stdout=iter([]))]
    procs[0].wait.return_value = 0
    procs[1].wait.return_value = 1
    monkeypatch.setattr(app.subprocess, "Popen", Mock(side_effect=procs))
    run(job)
    assert app.JOBS["job"]["state"] == "error"
    assert published(share) == []


@pytest.mark.parametrize("manual_retry", [False, True])
def test_partial_publication_and_retry(torrent, monkeypatch, manual_retry):
    job, share, processes = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    (job / "Show/second.mkv").write_bytes(b"b" * 200)
    (job / "Show/third.mkv").write_bytes(b"c" * 20)
    publish = app._publish_to_share

    def fail_second(path):
        if path.name == "second.mkv":
            raise OSError("SMB publication failed")
        return publish(path)

    monkeypatch.setattr(app, "_publish_to_share", fail_second)
    run(job, (1, 2), check_integrity=True)
    assert app.JOBS["job"]["state"] == "error"
    assert published(share) == ["first.mkv"]
    assert app._published_map(job) == {"Show/first.mkv": "first.mkv"}
    assert app._selected_indices(job) == {2}
    assert (job / "Show/second.mkv").exists()
    monkeypatch.setattr(app, "_publish_to_share", publish)
    if manual_retry:
        result = app.library_add(app.LibraryAddRequest(job_id="job"))
        assert result["names"] == ["second.mkv"]
    run(job, None, check_integrity=True)
    assert app.JOBS["job"]["state"] == "done"
    assert published(share) == ["first.mkv", "second.mkv"]
    assert app._selected_indices(job) == set()
    assert len(app._published_map(job)) == 2
    assert (job / "Show/third.mkv").read_bytes() == b"c" * 20
    if not manual_retry:
        assert "--select-file=2" in processes[-1][0]


@pytest.mark.parametrize("marker", [".published", ".selected"])
def test_metadata_commit_failure_is_retryable(torrent, monkeypatch, marker):
    job, share, processes = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    replace = Path.replace

    def fail_marker(path, target):
        if Path(target).name == marker and (marker != ".selected" or app._published_map(job)):
            raise OSError("metadata commit failed")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_marker)
    run(job, check_integrity=True)
    assert app.JOBS["job"]["state"] == "error"
    if marker == ".published":
        assert published(share) == []
        assert (job / "Show/first.mkv").exists()
        assert app._published_map(job) == {}
    else:
        assert published(share) == ["first.mkv"]
        assert app._published_map(job) == {"Show/first.mkv": "first.mkv"}
    monkeypatch.setattr(Path, "replace", replace)
    run(job, None, check_integrity=True)
    assert app.JOBS["job"]["state"] == "done"
    assert published(share) == ["first.mkv"]
    assert app._selected_indices(job) == set()
    assert len(processes) == (2 if marker == ".published" else 1)


@pytest.mark.parametrize("operation", ["mkdir", "copyfile", "popen"])
def test_setup_io_failure_marks_error(torrent, monkeypatch, operation):
    job, _, _ = torrent
    if operation == "mkdir":
        monkeypatch.setattr(Path, "mkdir", Mock(side_effect=OSError("SMB mkdir")))
    elif operation == "popen":
        monkeypatch.setattr(app.subprocess, "Popen", Mock(side_effect=OSError("Popen denied")))
    else:
        monkeypatch.setattr(app.shutil, "copyfile", Mock(side_effect=OSError("SMB copy")))
    source = str(job / "other.torrent") if operation == "copyfile" else str(job / ".meta.torrent")
    app._run_torrent("job", source, select=[1])
    assert app.JOBS["job"]["state"] == "error"
    assert app.JOBS["job"]["error"]
    assert (job / ".meta.torrent").exists()


def test_empty_drain_is_not_success(torrent):
    job, share, _ = torrent
    assert app._drain_torrent_to_library("job", job) is False
    assert published(share) == []


@pytest.mark.parametrize("state", ["queued", "downloading", "processing"])
def test_manual_publication_does_not_move_active_torrent(torrent, state):
    job, share, _ = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    (job / ".selected").write_text("1")
    app.JOBS["job"]["state"] = state
    with pytest.raises(app.HTTPException) as exc:
        app.library_add(app.LibraryAddRequest(job_id="job"))
    assert exc.value.status_code == 409
    assert (job / "Show/first.mkv").exists()
    assert published(share) == []


def test_auto_rest_continues_only_unpublished_files(torrent, monkeypatch):
    job, share, processes = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    (job / "Show/second.mkv").write_bytes(b"b" * 200)
    (job / "Show/third.mkv").write_bytes(b"c" * 300)
    run(job, auto_rest=True)
    assert app.JOBS["job"]["state"] == "done"
    assert published(share) == ["first.mkv", "second.mkv", "third.mkv"]
    assert "--select-file=2,3" in processes[2][0]
    assert len(processes) == 4


def test_same_basename_does_not_mark_another_path_published(torrent):
    job, _, _ = torrent
    (job / ".meta.torrent").write_bytes(make_torrent(
        "Show", [("season1/episode.mkv", 100), ("season2/episode.mkv", 100)]))
    app._add_published(job, "Show/season1/episode.mkv", "episode.mkv")
    assert app._published_indices(job) == {1}


def test_library_list_returns_torrent_group_for_published_files(torrent):
    job, share, _ = torrent
    (job / ".torrent_job").touch()
    (share / "first.mkv").write_bytes(b"a" * 100)
    app._add_published(job, "Show/first.mkv", "first.mkv")

    item = next(f for f in app.library_list()["files"] if f["name"] == "first.mkv")

    assert item["torrent_job_id"] == "job"
    assert item["torrent_title"] == "Show"


def test_add_files_at_completion_does_not_download_published_file_again(torrent, monkeypatch):
    job, share, processes = torrent
    (job / "Show/first.mkv").write_bytes(b"a" * 100)
    (job / "Show/second.mkv").write_bytes(b"b" * 200)
    publish = app._publish_to_share

    def add_while_publishing(path):
        name = publish(path)
        if path.name == "first.mkv":
            app.JOBS["job"].update(add_files={1, 2}, pause=True)
        return name

    monkeypatch.setattr(app, "_publish_to_share", add_while_publishing)
    run(job, check_integrity=True)
    assert app.JOBS["job"]["state"] == "done"
    assert published(share) == ["first.mkv", "second.mkv"]
    assert len(processes) == 3
    assert all("--select-file=2" in cmd for cmd, _ in processes[1:])
    assert app._selected_indices(job) == set()
