from test_instance_staging import instances, make_job


def test_torrent_list_includes_memory_and_local_disk_only(instances):
    a, b = instances
    make_job(a.STAGING_DIR, "saved", torrent=True)
    make_job(a.DOWNLOAD_DIR, "legacy", torrent=True)
    make_job(b.STAGING_DIR, "foreign", torrent=True)
    a.JOBS.update({
        "saved": {"type": "torrent", "state": "error", "title": "Saved", "error": "failed"},
        "starting": {"type": "torrent", "state": "queued", "title": "Starting"},
        "video": {"type": "video", "state": "downloading"},
        "removed": {"type": "torrent", "state": "deleted"},
    })
    rows = {row["job_id"]: row for row in a.list_torrents()["torrents"]}
    assert set(rows) == {"saved", "legacy", "starting"}
    assert rows["saved"]["state"] == "error"
    assert rows["saved"]["error"] == "failed"
    assert rows["starting"]["state"] == "queued"


def test_torrent_list_restores_paused_file_state(instances):
    app, _ = instances
    directory = make_job(app.STAGING_DIR, "paused", torrent=True)
    (directory / ".selected").write_text("")
    (directory / ".paused_files").write_text("1")
    rows = app.list_torrents()["torrents"]
    assert rows[0]["state"] == "paused"
    assert rows[0]["percent"] is not None
