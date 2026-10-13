import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import app


def test_existing_library_copy_discards_only_staging_source(tmp_path, monkeypatch):
    share = tmp_path / "share"
    staging = share / ".staging"
    staging.mkdir(parents=True)
    monkeypatch.setattr(app, "SHARE_PATH", share)
    monkeypatch.setattr(app, "STAGING_DIR", staging)

    existing = share / "same.mkv"
    existing.write_bytes(b"same content")
    staged = staging / "job" / "same.mkv"
    staged.parent.mkdir()
    staged.write_bytes(b"same content")

    assert app._publish_to_share(staged) == "same.mkv"
    assert not staged.exists()
    assert existing.read_bytes() == b"same content"

    legacy = tmp_path / "downloads" / "same.mkv"
    legacy.parent.mkdir()
    legacy.write_bytes(b"same content")
    assert app._publish_to_share(legacy) == "same.mkv"
    assert legacy.read_bytes() == b"same content"
