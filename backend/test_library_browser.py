"""Browser regressions against the real page with offline mocked APIs."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


HTML = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")
BASE = "http://192.0.2.1:8000"


def open_page(page, state):
    def route_handler(route, request):
        path = request.url.removeprefix(BASE)
        if path == "/":
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HTML)
        elif path == "/api/config":
            route.fulfill(json={"password_required": False, "rutracker": "no_network"})
        elif path == "/api/files":
            route.fulfill(json={"files": state["files"], "disk": {"free": 0, "total": 0}})
        elif path == "/api/library":
            route.fulfill(json={"enabled": True, "files": state["library"]})
        elif path == "/api/jobs":
            route.fulfill(json={"jobs": []})
        elif path == "/api/torrents":
            route.fulfill(json={"torrents": state.get("torrents", [])})
        elif path == "/api/torrent/manage123/files":
            route.fulfill(json={"files": [state["torrent_file"]]})
        elif path in ("/api/torrent/manage123/pause-files", "/api/torrent/manage123/add-files"):
            data = request.post_data_json
            state["calls"].append((path, data))
            paused = path.endswith("pause-files")
            state["torrent_file"].update(paused=paused, selected=not paused,
                                         state="paused" if paused else "downloading")
            route.fulfill(json={"ok": True})
        elif path in ("/api/torrent/manage123/delete", "/api/torrent/second123/delete"):
            data = request.post_data_json
            state["calls"].append((path, data))
            job_id = path.split("/")[3]
            state["torrents"][:] = [t for t in state["torrents"] if t["job_id"] != job_id]
            route.fulfill(json={"ok": True, "warnings": []})
        elif path == "/api/download":
            route.fulfill(json={"job_id": "download123"})
        elif path == "/api/torrent/resume123/resume":
            route.fulfill(json={"ok": True})
        elif path == "/api/status/download123":
            route.fulfill(json={"job_id": "download123", "state": "downloading", "percent": 1})
        elif path == "/api/status/resume123":
            route.fulfill(json={"job_id": "resume123", "type": "torrent", "title": "Show",
                                "state": "downloading", "percent": 25})
        elif path == "/api/torrent/resume123/files":
            route.fulfill(json={"files": [{"index": 1, "path": "Show/E01.mkv", "size": 100,
                                            "downloaded": 25, "percent": 25, "selected": True,
                                            "done": False, "state": "downloading"}]})
        else:
            route.fulfill(status=200, json={"ok": True, "files": []})

    page.route("**/*", route_handler)
    page.goto(BASE, wait_until="domcontentloaded")
    page.locator("#url").wait_for()
    page.wait_for_function("window.RT_STATE !== 'checking'")


def test_playlist_download_starts_over_plain_http():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        open_page(page, {"files": [], "library": []})
        assert page.locator("#url").is_visible()
        assert page.locator("#actionOpen").count() == 0
        assert page.locator("#gameCard").evaluate("el => el.parentElement.lastElementChild === el")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.evaluate("""() => {
          PARSED = {type: 'playlist', title: 'Playlist', entries: [
            {url: 'https://example.invalid/one', title: 'One'}]};
          document.getElementById('epList').innerHTML =
            '<input type="checkbox" checked data-i="0">';
          GAME.start = () => {};
          startDownload();
        }""")
        page.get_by_text("One", exact=True).wait_for()
        assert not errors
        browser.close()


def test_torrent_tab_file_controls_and_delete_options():
    state = {
        "files": [], "library": [], "calls": [],
        "torrents": [
            {"job_id": "manage123", "title": "Active torrent", "state": "downloading", "percent": 25, "speed": 1048576},
            {"job_id": "second123", "title": "Finished torrent", "state": "done", "percent": 100},
        ],
        "torrent_file": {"index": 1, "path": "Show/E01.mkv", "size": 100,
                         "downloaded": 25, "percent": 25, "selected": True,
                         "done": False, "state": "downloading", "paused": False},
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        open_page(page, state)
        page.get_by_role("button", name="Торренты", exact=True).click()
        active = page.locator('#torrentJobs .job[data-job-id="manage123"]')
        finished = page.locator('#torrentJobs .job[data-job-id="second123"]')
        active.get_by_text("Скачивание · 25% · 1.0 МБ/с").wait_for()
        finished.get_by_text("Готово · 100%").wait_for()
        active.get_by_text("Файлы в раздаче").click()
        active.locator('[data-act="pause"]').click()
        active.get_by_text("На паузе 25%", exact=True).wait_for()
        active.locator('[data-act="add"]').get_by_text("Продолжить").click()
        active.get_by_text("качается… 25%", exact=True).wait_for()
        active.locator('[data-torrent-act="delete"]').click()
        active.get_by_role("button", name="Удалить раздачу, сохранить файлы").click()
        active.wait_for(state="detached")
        finished.locator('[data-torrent-act="delete"]').click()
        finished.get_by_role("button", name="Удалить раздачу и файлы").click()
        finished.wait_for(state="detached")
        assert state["calls"] == [
            ("/api/torrent/manage123/pause-files", {"files": [1]}),
            ("/api/torrent/manage123/add-files", {"files": [1]}),
            ("/api/torrent/manage123/delete", {"delete_files": False}),
            ("/api/torrent/second123/delete", {"delete_files": True}),
        ]
        browser.close()


def test_resumed_job_survives_library_refresh_and_library_files_remain_visible():
    state = {
        "files": [{"job_id": "resume123", "filename": None, "title": "Show", "torrent": True,
                   "interrupted": True, "progress": {"percent": 20, "done_files": 0, "total_files": 2},
                   "mtime": 1}],
        "library": [
            {"name": "E01.mkv", "size": 10, "mtime": 1,
             "torrent_job_id": "resume123", "torrent_title": "Show"},
            {"name": "E02.mkv", "size": 20, "mtime": 2,
             "torrent_job_id": "resume123", "torrent_title": "Show"},
            {"name": "Other.mkv", "size": 5, "mtime": 3,
             "torrent_job_id": "other123", "torrent_title": "Other"},
        ],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        open_page(page, state)
        page.locator("#serverList .job button").get_by_text("Возобновить").click()
        page.get_by_text("Скачивание 25%", exact=False).wait_for()
        page.evaluate("loadFiles()")
        page.wait_for_function("ACTIVE_JOBS.has('resume123')")
        assert page.locator('#serverList .job[data-job-id="resume123"]').count() == 1

        # SMB files are displayed in the separate library table.
        page.wait_for_function("document.querySelectorAll('#libraryTable tr').length === 3")
        for name in ("E01.mkv", "E02.mkv", "Other.mkv"):
            assert page.locator("#libraryTable tr").filter(has_text=name).count() == 1
        browser.close()
