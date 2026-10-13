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
