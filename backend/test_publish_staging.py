import ctypes
import errno
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import app


@pytest.fixture
def library(tmp_path, monkeypatch):
    share = tmp_path / "share"
    staging = share / ".staging"
    staging.mkdir(parents=True)
    monkeypatch.setattr(app, "SHARE_PATH", share)
    monkeypatch.setattr(app, "STAGING_DIR", staging)
    return share, staging


@pytest.mark.parametrize("content", [b"different!!!", b"same content"])
@pytest.mark.parametrize("legacy", [False, True])
def test_existing_name_preserves_both_files(library, tmp_path, content, legacy):
    share, staging = library
    existing = share / "same.mkv"
    existing.write_bytes(b"same content")
    source = (tmp_path / "downloads" if legacy else staging / "job") / existing.name
    source.parent.mkdir()
    source.write_bytes(content)
    assert source.stat().st_size == existing.stat().st_size

    name = app._publish_to_share(source)

    assert name != existing.name
    assert (share / name).read_bytes() == content
    assert existing.read_bytes() == b"same content"
    assert source.exists() == legacy
    if legacy:
        assert source.read_bytes() == content


def test_repeated_legacy_publication_keeps_separate_copies(library, tmp_path):
    share, _ = library
    source = tmp_path / "video.mkv"
    source.write_bytes(b"video")
    first = app._publish_to_share(source)
    second = app._publish_to_share(source)
    assert first == source.name
    assert first != second
    assert (share / first).read_bytes() == (share / second).read_bytes() == source.read_bytes()


@pytest.mark.parametrize("legacy", [False, True])
def test_concurrent_publications_are_atomic_and_do_not_overwrite(
        library, tmp_path, monkeypatch, legacy):
    share, staging = library
    sources = []
    for i in range(2):
        source = (tmp_path if legacy else staging) / str(i) / "same.mkv"
        source.parent.mkdir()
        source.write_bytes(bytes([i]) * 100)
        sources.append(source)
    unrelated_part = share / "same.mkv.part"
    unrelated_part.write_bytes(b"another publisher")
    barrier = threading.Barrier(2)
    rename = app._rename_no_replace

    def simultaneous_rename(source, target):
        if target.name == "same.mkv":
            # Both workers reach the final operation before either publishes.
            assert not target.exists()
            assert source.read_bytes() in (bytes([0]) * 100, bytes([1]) * 100)
            barrier.wait(timeout=5)
        rename(source, target)

    monkeypatch.setattr(app, "_rename_no_replace", simultaneous_rename)
    with ThreadPoolExecutor(max_workers=2) as workers:
        names = list(workers.map(app._publish_to_share, sources))

    assert len(set(names)) == 2
    assert {(share / name).read_bytes() for name in names} == {bytes([0]) * 100, bytes([1]) * 100}
    assert all(source.exists() == legacy for source in sources)
    assert unrelated_part.read_bytes() == b"another publisher"
    assert list(share.glob(".publish-*.part")) == []


def test_suffix_collision_retries_without_overwrite(library, monkeypatch):
    share, staging = library
    (share / "same.mkv").write_bytes(b"first")
    (share / "same (aaaaaa).mkv").write_bytes(b"second")
    source = staging / "same.mkv"
    source.write_bytes(b"third")
    ids = iter(["aaaaaa", "bbbbbb"])
    monkeypatch.setattr(app.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    assert app._publish_to_share(source) == "same (bbbbbb).mkv"
    assert (share / "same.mkv").read_bytes() == b"first"
    assert (share / "same (aaaaaa).mkv").read_bytes() == b"second"
    assert (share / "same (bbbbbb).mkv").read_bytes() == b"third"


@pytest.mark.parametrize("legacy", [False, True])
def test_failed_publication_preserves_source(library, tmp_path, monkeypatch, legacy):
    share, staging = library
    source = (tmp_path if legacy else staging) / "video.mkv"
    source.write_bytes(b"keep this")

    def fail(source, target):
        raise OSError(errno.EIO, "SMB unavailable")

    monkeypatch.setattr(app, "_rename_no_replace", fail)
    with pytest.raises(OSError, match="SMB unavailable"):
        app._publish_to_share(source)
    assert source.read_bytes() == b"keep this"
    assert list(share.iterdir()) == [staging]


def test_failed_legacy_copy_cleans_only_its_temporary_file(library, tmp_path, monkeypatch):
    share, staging = library
    source = tmp_path / "video.mkv"
    source.write_bytes(b"keep this")
    unrelated_part = share / "video.mkv.part"
    unrelated_part.write_bytes(b"other writer")

    def fail(source, target):
        target.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(app.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="full"):
        app._publish_to_share(source)
    assert source.read_bytes() == b"keep this"
    assert unrelated_part.read_bytes() == b"other writer"
    assert set(share.iterdir()) == {staging, unrelated_part}


@pytest.mark.parametrize("error", [0, errno.EEXIST, errno.EOPNOTSUPP])
def test_linux_rename_uses_no_replace_and_propagates_errno(monkeypatch, error):
    source, target = Path("source.mkv"), Path("target.mkv")
    renameat2 = Mock(return_value=-1 if error else 0)
    monkeypatch.setattr(app.ctypes, "CDLL", Mock(return_value=SimpleNamespace(renameat2=renameat2)))
    ctypes.set_errno(error)
    with monkeypatch.context() as context:
        context.setattr(app.os, "name", "posix")
        if error:
            with pytest.raises(OSError) as exc:
                app._rename_no_replace(source, target)
            assert exc.value.errno == error
            if error == errno.EEXIST:
                assert isinstance(exc.value, FileExistsError)
        else:
            app._rename_no_replace(source, target)
    renameat2.assert_called_once_with(-100, b"source.mkv", -100, b"target.mkv", 1)


def test_missing_renameat2_fails_without_unsafe_fallback(monkeypatch):
    source, target = Path("source.mkv"), Path("target.mkv")
    monkeypatch.setattr(app.ctypes, "CDLL", Mock(return_value=SimpleNamespace()))
    with monkeypatch.context() as context:
        context.setattr(app.os, "name", "posix")
        with pytest.raises(OSError, match="renameat2") as exc:
            app._rename_no_replace(source, target)
        assert exc.value.errno == errno.ENOTSUP
