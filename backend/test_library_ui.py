from pathlib import Path
import unittest

HTML = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")

class LibraryUiContract(unittest.TestCase):
    def test_video_and_torrent_forms_are_not_hidden_in_a_dialog(self):
        self.assertNotIn('id="actionOpen"', HTML)
        self.assertNotIn('id="actionPanel"', HTML)
        self.assertLess(HTML.index('id="tabVideo"'), HTML.index('id="tabTorrent"'))
        self.assertLess(HTML.index('id="ytdlpLine"'), HTML.index('id="gameCard"'))

    def test_sort_and_collapse_state_are_persisted(self):
        for key in ("name", "date", "size", "status"):
            self.assertIn(f'<option value="{key}">', HTML)
        self.assertIn('localStorage.setItem("library_sort", value)', HTML)
        self.assertIn('localStorage.setItem("library_collapsed"', HTML)
        self.assertIn('toggleCompletedGroups()', HTML)

    def test_task_groups_use_job_ids_and_playlist_group_ids(self):
        self.assertIn('PLAYLIST_GROUPS[job_id] = groupId', HTML)
        self.assertIn('PLAYLIST_FILES[libraryName] = groupId', HTML)
        self.assertIn('el.dataset.groupId = PLAYLIST_GROUPS[f.job_id] || f.job_id', HTML)
        self.assertIn('el.dataset.jobId = f.job_id', HTML)
        self.assertIn('ACTIVE_JOBS.set(jobId, job)', HTML)
        self.assertIn('const id = row.dataset.groupId || row.dataset.jobId', HTML)

    def test_playlist_id_works_in_http_context(self):
        self.assertIn('Math.random().toString(36).slice(2)', HTML)
        self.assertNotIn('crypto.randomUUID()', HTML)

    def test_poll_updates_only_changed_progress_markup(self):
        self.assertIn('if (meta.textContent !== progressText) meta.textContent = progressText', HTML)
        self.assertIn('setMarkupIfChanged(controls', HTML)
        self.assertIn('list.replaceChildren(fragment)', HTML)

if __name__ == "__main__":
    unittest.main()
