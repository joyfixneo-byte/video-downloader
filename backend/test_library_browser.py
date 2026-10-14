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
    page.locator("#actionOpen").wait_for()
    page.wait_for_function("window.RT_STATE !== 'checking'")


def test_playlist_download_starts_over_plain_http():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        open_page(page, {"files": [], "library": []})
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


def test_resumed_job_survives_library_refresh_and_torrent_files_share_group():
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

        # The parent task is explicit in the API response, so equal file names
        # under different jobs stay separate while a single torrent stays grouped.
        groups = page.locator("#serverList .library-group")
        page.wait_for_function("document.querySelectorAll('#serverList .library-group').length >= 2")
        assert page.locator('#serverList .library-group[data-group-id="resume123"] .job').count() == 3
        assert page.locator('#serverList .library-group[data-group-id="resume123"] [data-sort-name="E01.mkv"]').count() == 1
        assert page.locator('#serverList .library-group[data-group-id="resume123"] [data-sort-name="E02.mkv"]').count() == 1
        assert page.locator('#serverList .library-group[data-group-id="other123"] .job').count() == 1
        browser.close()
