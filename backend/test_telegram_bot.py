"""Offline unit tests for the private Telegram worker."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import telegram_bot as bot


class TelegramBotTests(unittest.TestCase):
    def test_only_youtube_video_hosts_are_accepted(self):
        self.assertEqual(bot.validate_youtube_url("https://youtu.be/abc"), "https://youtu.be/abc")
        for url in ("https://example.com/watch?v=abc", "file:///etc/passwd", "https://youtube.com.evil.test/watch?v=x"):
            with self.subTest(url=url), self.assertRaises(bot.BotError):
                bot.validate_youtube_url(url)

    def test_title_normalization_and_legacy_match_are_exact(self):
        files = [
            {"name": "Кошки и собаки (2024).mp4"},
            {"name": "Кошки и собаки обзор.mp4"},
        ]
        self.assertEqual(bot.exact_legacy_matches("Кошки и собаки (2024)", files),
                         ["Кошки и собаки (2024).mp4"])
        self.assertEqual(bot.exact_legacy_matches("КОШКИ И СОБАКИ (2024)", files),
                         ["Кошки и собаки (2024).mp4"])

    def test_sqlite_index_persists_canonical_id_and_update_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "STATE_DB", Path(temp) / "bot.sqlite3"):
                db = bot.db_connect()
                db.execute("INSERT INTO videos(video_id,title,state,chat_id,updated) VALUES(?,?,?,?,?)",
                           ("a1b2", "Video", "done", "42", 1))
                db.execute("INSERT INTO settings(key,value) VALUES('offset','25')")
                db.commit()
                db.close()
                db = bot.db_connect()
                self.assertEqual(db.execute("SELECT state FROM videos WHERE video_id='a1b2'").fetchone()[0], "done")
                self.assertEqual(db.execute("SELECT value FROM settings WHERE key='offset'").fetchone()[0], "25")
                db.close()

    def test_legacy_database_gets_outbox_columns_without_replaying_old_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bot.sqlite3"
            raw = bot.sqlite3.connect(path)
            raw.execute("""CREATE TABLE videos (
                video_id TEXT PRIMARY KEY, title TEXT NOT NULL, state TEXT NOT NULL,
                job_id TEXT, filename TEXT, chat_id TEXT NOT NULL, message_id INTEGER,
                error TEXT, updated REAL NOT NULL)""")
            raw.execute("INSERT INTO videos(video_id,title,state,chat_id,updated) VALUES('old','Old','done','42',1)")
            raw.commit(); raw.close()
            with patch.object(bot, "STATE_DB", path):
                db = bot.db_connect()
                row = db.execute("SELECT notification,notified FROM videos WHERE video_id='old'").fetchone()
                self.assertEqual(tuple(row), (None, 1))
                self.assertIsNone(db.execute(
                    "SELECT status_text FROM videos WHERE video_id='old'").fetchone()[0])
                db.close()

    def test_active_status_edits_same_message_with_download_stats_and_skips_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "STATE_DB", Path(temp) / "bot.sqlite3"), \
                 patch.object(bot, "app_api", return_value={
                     "state": "downloading", "percent": 25, "speed": 2097152,
                     "eta": 61, "downloaded": 5242880, "total": 20971520,
                 }), patch.object(bot, "telegram") as telegram:
                db = bot.db_connect()
                db.execute("""INSERT INTO videos(video_id,title,state,job_id,chat_id,message_id,updated)
                    VALUES('v1','Video','downloading','j1','42',7,1)""")
                db.commit(); db.close()
                bot.poll_jobs()
                bot.poll_jobs()
        self.assertEqual(telegram.call_count, 1)
        method, payload = telegram.call_args.args
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["message_id"], 7)
        self.assertIn("25%", payload["text"])
        self.assertIn("2.0 МБ/с", payload["text"])
        self.assertIn("~1 мин 1 с", payload["text"])
        self.assertIn("5.0 МБ из ~20.0 МБ", payload["text"])
        self.assertEqual(payload["reply_markup"], bot.stop_markup("j1"))

    def test_terminal_status_edits_same_message_and_removes_stop_button(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "STATE_DB", Path(temp) / "bot.sqlite3"), \
                 patch.object(bot, "app_api", return_value={
                     "state": "done", "library_name": "Video.mp4", "size": 3145728,
                 }), patch.object(bot, "telegram") as telegram:
                db = bot.db_connect()
                db.execute("""INSERT INTO videos(video_id,title,state,job_id,chat_id,message_id,updated)
                    VALUES('v1','Video','downloading','j1','42',7,1)""")
                db.commit(); db.close()
                bot.poll_jobs()
        self.assertEqual(telegram.call_count, 1)
        method, payload = telegram.call_args.args
        self.assertEqual(method, "editMessageText")
        self.assertEqual(payload["message_id"], 7)
        self.assertIn("Готово (3.0 МБ)", payload["text"])
        self.assertEqual(payload["reply_markup"], {"inline_keyboard": []})

    def test_already_applied_terminal_edit_counts_as_delivered(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "STATE_DB", Path(temp) / "bot.sqlite3"), \
                 patch.object(bot, "app_api", return_value={"state": "done", "library_name": "Video.mp4"}), \
                 patch.object(bot, "telegram", side_effect=bot.BotError("message is not modified")):
                db = bot.db_connect()
                db.execute("""INSERT INTO videos(video_id,title,state,job_id,chat_id,message_id,updated)
                    VALUES('v1','Video','downloading','j1','42',7,1)""")
                db.commit(); db.close()
                bot.poll_jobs()
                db = bot.db_connect()
                self.assertEqual(db.execute(
                    "SELECT notified FROM videos WHERE video_id='v1'").fetchone()[0], 1)
                db.close()

    def test_failed_terminal_notification_is_retried_after_worker_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bot.sqlite3"
            with patch.object(bot, "STATE_DB", path):
                db = bot.db_connect()
                db.execute("""INSERT INTO videos(video_id,title,state,job_id,chat_id,updated)
                    VALUES('video1','Video','downloading','job1','42',1)""")
                db.commit(); db.close()
                send = patch.object(bot, "telegram", side_effect=[bot.BotError("offline"), {"message_id": 7}])
                status = patch.object(bot, "app_api", return_value={"state": "done", "library_name": "Video.mp4"})
                with send as telegram, status as app_api:
                    bot.poll_jobs()
                    db = bot.db_connect()
                    row = db.execute("SELECT state,notified,notification FROM videos WHERE video_id='video1'").fetchone()
                    self.assertEqual(row["state"], "done")
                    self.assertEqual(row["notified"], 0)
                    self.assertIn("Video.mp4", row["notification"])
                    db.close()

                    # A fresh poll, like a restarted worker, retries delivery and
                    # does not query or submit a second download.
                    bot.poll_jobs()
                    self.assertEqual(telegram.call_count, 2)
                    self.assertEqual(app_api.call_count, 1)
                    db = bot.db_connect()
                    self.assertEqual(db.execute("SELECT notified FROM videos WHERE video_id='video1'").fetchone()[0], 1)
                    db.close()
    def test_unauthorized_message_does_not_read_url_or_call_api(self):
        with patch.object(bot, "ALLOWED_ID", "123"), \
             patch.object(bot, "handle_video") as handle_video, \
             patch.object(bot, "telegram") as telegram:
            bot.handle_update({"message": {"from": {"id": 999},
                "chat": {"id": 999}, "text": "https://youtu.be/abc"}})
        handle_video.assert_not_called()
        telegram.assert_not_called()

    def test_duplicate_active_video_is_not_submitted_again(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(bot, "STATE_DB", Path(temp) / "bot.sqlite3"), \
                 patch.object(bot, "get_video_info", return_value=("same-id", "Same video", "https://www.youtube.com/watch?v=same-id")), \
                 patch.object(bot, "telegram") as telegram, \
                 patch.object(bot, "app_api") as app_api:
                db = bot.db_connect()
                db.execute("""INSERT INTO videos(video_id,title,state,job_id,chat_id,updated)
                    VALUES('same-id','Same video','downloading','job123','42',1)""")
                db.commit()
                db.close()
                bot.handle_video("42", 8, "https://youtu.be/same-id")
        app_api.assert_not_called()
        telegram.assert_called_once()
        self.assertEqual(telegram.call_args.args[0], "sendMessage")
        self.assertIn("Same video", telegram.call_args.args[1]["text"])

    def test_stop_button_calls_shared_cancel_endpoint(self):
        with patch.object(bot, "ALLOWED_ID", "123"), \
             patch.object(bot, "app_api", return_value={"ok": True}) as app_api, \
             patch.object(bot, "telegram") as telegram:
            bot.handle_callback({"id": "q1", "from": {"id": 123}, "data": "stop:job123"})
        app_api.assert_called_once_with("POST", "/api/cancel/job123", {})
        self.assertEqual(telegram.call_args.args[0], "answerCallbackQuery")


if __name__ == "__main__":
    unittest.main()
