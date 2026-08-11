"""Tests for watcher_lib: atomic file writes, tracker IO, Discord retry policy.

stdlib only (unittest + mock), matching watcher_lib itself, so CI needs no
install step. Every test redirects watcher_lib's module-level paths at a
tempdir -- none of them touch the real tracker or state file.
"""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watcher_lib as wl  # noqa: E402

HEADER = "key,date_found,source,title,location,data_tech,status,url"


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class TempPaths(unittest.TestCase):
    """Point watcher_lib's module-level paths at a scratch directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._saved = (wl.TRACKER_CSV, wl.TRACKER_MD, wl.STATE_FILE)
        wl.TRACKER_CSV = os.path.join(self.dir, "applications_tracker.csv")
        wl.TRACKER_MD = os.path.join(self.dir, "JOB_TRACKER.md")
        wl.STATE_FILE = os.path.join(self.dir, "_watcher_state.json")
        self.log_patch = mock.patch.object(wl, "log", lambda *a, **k: None)
        self.log_patch.start()

    def tearDown(self):
        self.log_patch.stop()
        wl.TRACKER_CSV, wl.TRACKER_MD, wl.STATE_FILE = self._saved

    def write_csv(self, *lines):
        with open(wl.TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
            f.write("\r\n".join((HEADER,) + lines) + "\r\n")

    def leftovers(self):
        return [f for f in os.listdir(self.dir) if ".tmp." in f]


class AtomicWriteTests(TempPaths):
    def test_writes_content(self):
        target = os.path.join(self.dir, "out.txt")
        wl._atomic_write(target, "hello")
        self.assertEqual(read_text(target), "hello")

    def test_leaves_original_intact_when_write_fails(self):
        target = os.path.join(self.dir, "out.txt")
        wl._atomic_write(target, "original")
        with self.assertRaises(TypeError):
            wl._atomic_write(target, None)          # blows up inside the temp write
        self.assertEqual(read_text(target), "original")
        self.assertEqual(self.leftovers(), [], "temp file was not cleaned up")

    def test_no_temp_file_survives_success(self):
        wl._atomic_write(os.path.join(self.dir, "out.txt"), "x")
        self.assertEqual(self.leftovers(), [])

    def test_replaces_rather_than_truncates(self):
        """The target must never be observable as empty part-way through a write."""
        target = os.path.join(self.dir, "out.txt")
        wl._atomic_write(target, "first")
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(read_text(dst))   # target just before the swap
            return real_replace(src, dst)

        with mock.patch.object(wl.os, "replace", spy):
            wl._atomic_write(target, "second")
        self.assertEqual(seen, ["first"], "target was modified before the atomic swap")
        self.assertEqual(read_text(target), "second")


class LoadTrackerTests(TempPaths):
    def test_missing_file_returns_empty(self):
        self.assertEqual(wl.load_tracker(), {})

    def test_reads_rows_by_key(self):
        self.write_csv("k1,2026-01-01,S,T,HK,yes,new,u", "k2,2026-01-02,S,T2,HK,no,applied,u")
        rows = wl.load_tracker()
        self.assertEqual(sorted(rows), ["k1", "k2"])
        self.assertEqual(rows["k2"]["status"], "applied")

    def test_skips_row_with_empty_key(self):
        self.write_csv("k1,2026-01-01,S,T,HK,yes,new,u", ",2026-01-01,S,Orphan,HK,yes,new,u")
        self.assertEqual(sorted(wl.load_tracker()), ["k1"])

    def test_damaged_header_does_not_raise(self):
        with open(wl.TRACKER_CSV, "w", newline="", encoding="utf-8") as f:
            f.write("date_found,source,title\r\n2026-01-01,S,T\r\n")
        self.assertEqual(wl.load_tracker(), {})     # must not raise KeyError


class TrackerRoundTripTests(TempPaths):
    ENTRY = {"_key": "S|a", "source": "S", "title": "A", "location": "Hong Kong",
             "data_tech": True, "url": "https://x"}

    def test_update_is_keyed_and_idempotent(self):
        self.assertEqual(wl.update_tracker([self.ENTRY], "2026-01-01"), (1, 1))
        self.assertEqual(wl.update_tracker([self.ENTRY], "2026-01-02"), (0, 1))

    def test_status_update_persists_and_reports_miss(self):
        wl.update_tracker([self.ENTRY], "2026-01-01")
        self.assertTrue(wl.set_tracker_status("S|a", "applied"))
        self.assertEqual(wl.load_tracker()["S|a"]["status"], "applied")
        self.assertFalse(wl.set_tracker_status("nope", "applied"))

    def test_rewrite_is_byte_stable(self):
        self.write_csv("k1,2026-01-01,S,T,HK,yes,new,u", "k2,2026-01-02,S,T2,HK,no,skip,u")
        wl._write_tracker(wl.load_tracker())
        first = read_bytes(wl.TRACKER_CSV)
        wl._write_tracker(wl.load_tracker())
        self.assertEqual(first, read_bytes(wl.TRACKER_CSV))

    def test_markdown_dashboard_is_regenerated(self):
        self.write_csv("k1,2026-01-01,S,DataRole,HK,yes,interested,u")
        wl._write_tracker(wl.load_tracker())
        md = read_text(wl.TRACKER_MD)
        self.assertIn("DataRole", md)
        self.assertIn("Shortlist", md)


class StateTests(TempPaths):
    def test_defaults_when_absent(self):
        state = wl.load_state()
        self.assertEqual(state["seen"], [])
        self.assertEqual(state["pending"], {})

    def test_round_trip_and_stamps_last_run(self):
        wl.save_state({"seen": ["a"], "pending": {"1": {"key": "a"}}})
        state = wl.load_state()
        self.assertEqual(state["seen"], ["a"])
        self.assertIn("last_run", state)

    def test_corrupt_state_falls_back_to_defaults(self):
        with open(wl.STATE_FILE, "w", encoding="utf-8") as f:
            f.write("{ truncated")
        self.assertEqual(wl.load_state()["seen"], [])


class DiscordRetryTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "t"})
        self.env.start()
        self.sleep = mock.patch.object(wl.time, "sleep", lambda *_: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.env.stop()

    @staticmethod
    def _http(code, body=b"{}"):
        return urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(body))

    @staticmethod
    def _responses(seq):
        it = iter(seq)

        def fake(req, timeout=None):
            item = next(it)
            if isinstance(item, Exception):
                raise item

            class R:
                def read(self):
                    return b'{"id":"42"}'

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()
        return fake

    def call(self, seq):
        with mock.patch.object(urllib.request, "urlopen", self._responses(seq)):
            return wl._discord("GET", "/x")

    def test_succeeds_without_retry(self):
        self.assertEqual(self.call([None]), {"id": "42"})

    def test_recovers_from_timeout(self):
        self.assertEqual(self.call([TimeoutError(), None]), {"id": "42"})

    def test_recovers_from_connection_reset(self):
        self.assertEqual(self.call([ConnectionResetError(), None]), {"id": "42"})

    def test_recovers_from_server_errors(self):
        self.assertEqual(self.call([self._http(503), self._http(500), None]), {"id": "42"})

    def test_honours_rate_limit_then_succeeds(self):
        self.assertEqual(self.call([self._http(429, b'{"retry_after":0.1}'), None]), {"id": "42"})

    def test_client_error_fails_fast(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.call([self._http(404)])
        self.assertEqual(cm.exception.code, 404)

    def test_gives_up_after_four_attempts(self):
        with self.assertRaises(RuntimeError):
            self.call([TimeoutError()] * 4)

    def test_requires_a_token(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}):
            with self.assertRaises(RuntimeError):
                wl._discord("GET", "/x")


class EmbedTests(unittest.TestCase):
    def test_data_tech_role_is_green_and_labelled(self):
        e = wl.make_embed({"title": "Data Intern", "url": "https://x", "source": "S",
                           "location": "Hong Kong", "data_tech": True})
        self.assertEqual(e["color"], wl.GREEN)
        self.assertIn("Data / Tech", e["fields"][2]["value"])

    def test_other_role_is_grey(self):
        e = wl.make_embed({"title": "Ops Intern", "url": "https://x", "source": "S",
                           "location": "Hong Kong", "data_tech": False})
        self.assertEqual(e["color"], wl.GREY)

    def test_defaults_location_when_blank(self):
        e = wl.make_embed({"title": "T", "url": "u", "source": "S", "location": "  "})
        self.assertEqual(e["fields"][1]["value"], "Hong Kong")

    def test_truncates_overlong_title(self):
        e = wl.make_embed({"title": "x" * 400, "url": "u", "source": "S"})
        self.assertLessEqual(len(e["title"]), 250)


class CheckConfigTests(unittest.TestCase):
    GOOD = {"alerts_channel_id": "1", "starred_channel_id": "2"}

    def test_complete_config_has_no_problems(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "t"}):
            self.assertEqual(wl.check_config(self.GOOD), [])

    def test_names_the_missing_channel(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "t"}):
            problems = wl.check_config({})
        self.assertEqual(len(problems), 1)
        self.assertIn("alerts_channel_id", problems[0])

    def test_names_the_missing_token(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}):
            problems = wl.check_config(self.GOOD)
        self.assertEqual(len(problems), 1)
        self.assertIn("DISCORD_BOT_TOKEN", problems[0])

    def test_reports_every_problem_at_once(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": ""}):
            problems = wl.check_config({}, ("alerts_channel_id", "starred_channel_id"))
        self.assertEqual(len(problems), 3)

    def test_blank_values_count_as_missing(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "   "}):
            problems = wl.check_config({"alerts_channel_id": "   "})
        self.assertEqual(len(problems), 2)

    def test_poller_requires_the_starred_channel_too(self):
        with mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "t"}):
            problems = wl.check_config({"alerts_channel_id": "1"},
                                       ("alerts_channel_id", "starred_channel_id"))
        self.assertEqual(len(problems), 1)
        self.assertIn("starred_channel_id", problems[0])


class CommittedTokenGuardTests(unittest.TestCase):
    """bot_config.json is tracked by git, so a token in it is a published secret."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = mock.patch.object(wl, "ROOT", self.dir)
        self.root.start()
        self.logged = []
        self.log = mock.patch.object(wl, "log", self.logged.append)
        self.log.start()

    def tearDown(self):
        self.log.stop()
        self.root.stop()

    def write(self, name, obj):
        import json as _json
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            _json.dump(obj, f)

    def test_warns_when_token_is_in_the_committed_file(self):
        self.write("bot_config.json", {"alerts_channel_id": "1", "bot_token": "secret"})
        wl.load_bot_config()
        self.assertTrue(any("SECURITY" in m for m in self.logged), self.logged)

    def test_silent_when_the_committed_file_has_no_token(self):
        self.write("bot_config.json", {"alerts_channel_id": "1"})
        wl.load_bot_config()
        self.assertEqual(self.logged, [])

    def test_silent_when_the_token_is_only_in_the_gitignored_file(self):
        self.write("bot_config.json", {"alerts_channel_id": "1"})
        self.write("_watcher_config.json", {"bot_token": "secret"})
        cfg = wl.load_bot_config()
        self.assertEqual(self.logged, [])
        self.assertEqual(cfg["bot_token"], "secret", "local overrides must still apply")

    def test_local_overrides_win_but_blanks_do_not_clobber(self):
        self.write("bot_config.json", {"alerts_channel_id": "committed"})
        self.write("_watcher_config.json", {"alerts_channel_id": "", "starred_channel_id": "local"})
        cfg = wl.load_bot_config()
        self.assertEqual(cfg["alerts_channel_id"], "committed")
        self.assertEqual(cfg["starred_channel_id"], "local")


if __name__ == "__main__":
    unittest.main()
