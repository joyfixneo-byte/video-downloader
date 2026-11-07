import hashlib
import struct

from test_instance_staging import instances
from test_torrent_files import bencode


def test_control_file_progress_and_boundaries(instances):
    app, _ = instances
    job = app.STAGING_DIR / "piecejob"
    job.mkdir()
    info = {b"name": b"Show", b"piece length": 4, b"pieces": b"x" * 60,
            b"files": [{b"path": [b"a"], b"length": 6},
                       {b"path": [b"b"], b"length": 4}]}
    (job / ".meta.torrent").write_bytes(bencode({b"info": info}))
    (job / ".torrent_job").touch()
    (job / ".selected").write_text("1,2")
    control = job / "Show.aria2"
    header = struct.pack(">HII", 1, 1, 20) + hashlib.sha1(bencode(info)).digest()
    control.write_bytes(header + struct.pack(">IQQI", 4, 10, 0, 1) + b"\xa0" + b"\0" * 4)
    rows = app.torrent_job_files("piecejob")["files"]
    assert [(row["downloaded"], row["percent"]) for row in rows] == [(4, 66), (2, 50)]
    assert not any(row["done"] for row in rows)
    control.write_bytes(header[:10] + b"bad hash" + b"\0" * 12 + control.read_bytes()[30:])
    assert app._torrent_piece_progress(job, rows) == {}
