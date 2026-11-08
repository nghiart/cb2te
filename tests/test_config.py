import unittest
import tempfile
from pathlib import Path
from core.config import Config, load_config, load_favorites, save_favorites, add_favorite, remove_favorite


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        self.settings_file = self.tmp_dir / "settings.txt"
        self.fav_file = self.tmp_dir / "favorites.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_config_defaults_and_parsing(self):
        self.settings_file.write_text(
            "TELEGRAM_API_ID = 123456\n"
            "TELEGRAM_API_HASH = abcdef123456\n"
            "TELEGRAM_BOT_TOKEN = 1234:xyz\n"
            "TELEGRAM_CHAT_ID = -100123456789\n"
            "TELEGRAM_ADMIN_IDS = 111, 222\n"
            "RETENTION_HOURS = 48\n"
            "MAX_CONCURRENT = 2\n"
            "MIN_FREE_MB = 500\n"
        )
        cfg = load_config(self.settings_file)
        self.assertEqual(cfg.api_id, 123456)
        self.assertEqual(cfg.api_hash, "abcdef123456")
        self.assertEqual(cfg.bot_token, "1234:xyz")
        self.assertEqual(cfg.chat_id, -100123456789)
        self.assertEqual(cfg.admin_ids, [111, 222])
        self.assertEqual(cfg.retention_hours, 48)
        self.assertEqual(cfg.max_concurrent, 2)
        self.assertEqual(cfg.min_free_mb, 500)

    def test_load_config_defaults_when_empty_or_missing(self):
        # Non-existent file
        cfg_missing = load_config(self.tmp_dir / "nonexistent.txt")
        self.assertEqual(cfg_missing.api_id, 0)
        self.assertEqual(cfg_missing.api_hash, "")
        self.assertEqual(cfg_missing.bot_token, "")
        self.assertEqual(cfg_missing.chat_id, 0)
        self.assertEqual(cfg_missing.admin_ids, [])
        self.assertEqual(cfg_missing.retention_hours, 48)
        self.assertEqual(cfg_missing.max_concurrent, 2)
        self.assertEqual(cfg_missing.min_free_mb, 400)
        self.assertEqual(cfg_missing.proxy, "")

        # Empty file
        empty_file = self.tmp_dir / "empty.txt"
        empty_file.write_text("# Only comments\n\n")
        cfg_empty = load_config(empty_file)
        self.assertEqual(cfg_empty.api_id, 0)
        self.assertEqual(cfg_empty.admin_ids, [])

    def test_load_config_alternate_keys_and_proxy(self):
        self.settings_file.write_text(
            "API_ID = 9999\n"
            "API_HASH = hash999\n"
            "BOT_TOKEN = token999\n"
            "CHAT_ID = -100999\n"
            "ADMIN_IDS = 333\n"
            "PROXY = socks5://127.0.0.1:9050\n"
        )
        cfg = load_config(self.settings_file)
        self.assertEqual(cfg.api_id, 9999)
        self.assertEqual(cfg.api_hash, "hash999")
        self.assertEqual(cfg.bot_token, "token999")
        self.assertEqual(cfg.chat_id, -100999)
        self.assertEqual(cfg.admin_ids, [333])
        self.assertEqual(cfg.proxy, "socks5://127.0.0.1:9050")

    def test_favorites_crud(self):
        self.fav_file.write_text("streamer1  Note 1\n# streamer2  Disabled\n")
        favs = load_favorites(self.fav_file)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["room_id"], "streamer1")

        add_favorite("streamer3", "New note", self.fav_file)
        favs_after_add = load_favorites(self.fav_file)
        self.assertEqual(len(favs_after_add), 2)
        self.assertTrue(any(f["room_id"] == "streamer3" for f in favs_after_add))

        remove_favorite("streamer1", self.fav_file)
        favs_after_del = load_favorites(self.fav_file)
        self.assertEqual(len(favs_after_del), 1)
        self.assertEqual(favs_after_del[0]["room_id"], "streamer3")

    def test_favorites_include_disabled_and_urls(self):
        self.fav_file.write_text(
            "# Chaturbate favorites\n"
            "streamer1  Note 1\n"
            "# streamer2  Disabled note\n"
            "https://chaturbate.com/streamer3/  URL note\n"
        )
        # load without disabled
        active_favs = load_favorites(self.fav_file)
        self.assertEqual(len(active_favs), 2)
        self.assertEqual({f["room_id"] for f in active_favs}, {"streamer1", "streamer3"})

        # load with disabled
        all_favs = load_favorites(self.fav_file, include_disabled=True)
        self.assertEqual(len(all_favs), 3)
        disabled = [f for f in all_favs if not f.get("enabled")]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0]["room_id"], "streamer2")

    def test_add_and_remove_favorite_edge_cases(self):
        new_fav_file = self.tmp_dir / "new_favs.txt"
        self.assertTrue(add_favorite("streamer_new", "A note", new_fav_file))
        favs = load_favorites(new_fav_file)
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["room_id"], "streamer_new")

        # Adding invalid / empty room
        self.assertFalse(add_favorite("", "No room", new_fav_file))

        # Adding existing room updates note
        self.assertTrue(add_favorite("streamer_new", "Updated note", new_fav_file))
        favs_updated = load_favorites(new_fav_file)
        self.assertEqual(len(favs_updated), 1)
        self.assertEqual(favs_updated[0]["note"], "Updated note")

        # Removing non-existent room returns False
        self.assertFalse(remove_favorite("non_existent", new_fav_file))
        self.assertTrue(remove_favorite("streamer_new", new_fav_file))
        self.assertEqual(len(load_favorites(new_fav_file)), 0)


if __name__ == "__main__":
    unittest.main()
