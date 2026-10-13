from pathlib import Path
import unittest

HTML = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")

class LibraryUiContract(unittest.TestCase):
    def test_actions_live_in_a_dialog_with_a_persistent_entry_point(self):
        self.assertIn('<dialog class="action-panel" id="actionPanel"', HTML)
        self.assertIn('id="actionOpen" onclick="openActionPanel()"', HTML)
        self.assertIn('["tabVideo", "tabTorrent"].forEach(id => body.appendChild', HTML)

    def test_sort_and_collapse_state_are_persisted(self):
        for key in ("name", "date", "size", "status"):
            self.assertIn(f'<option value="{key}">', HTML)
        self.assertIn('localStorage.setItem("library_sort", value)', HTML)
        self.assertIn('localStorage.setItem("library_collapsed"', HTML)
        self.assertIn('toggleCompletedGroups()', HTML)

    def test_task_groups_use_job_ids_and_playlist_group_ids(self):
        self.assertIn('PLAYLIST_GROUPS[job_id] = groupId', HTML)
        self.assertIn('PLAYLIST_FILES[libraryName] = groupId', HTML)
        self.assertIn('PLAYLIST_FILES[f.name] || ("library:" + f.name)', HTML)
        self.assertIn('el.dataset.groupId = PLAYLIST_GROUPS[jobId] || jobId', HTML)
        self.assertIn('knownIds.has(el.dataset.jobId)', HTML)
        self.assertIn('const id = row.dataset.groupId || row.dataset.jobId', HTML)

    def test_poll_updates_only_changed_progress_markup(self):
        self.assertIn('if (meta.textContent !== progressText) meta.textContent = progressText', HTML)
        self.assertIn('setMarkupIfChanged(controls', HTML)
        self.assertIn('list.replaceChildren(fragment)', HTML)

if __name__ == "__main__":
    unittest.main()
