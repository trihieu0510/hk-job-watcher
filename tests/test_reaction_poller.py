"""Tests for _reaction_poller.main(): status precedence, forwarding, and expiry.

The Discord layer and the tracker are stubbed at the watcher_lib boundary, so
these run offline with no token and never touch the real state file. Time is
controlled by swapping the module's `datetime` reference for a namespace whose
`date.today()` is fixed -- the real datetime module is left alone.
"""
import datetime
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import watcher_lib as wl  # noqa: E402
import _reaction_poller as rp  # noqa: E402

TODAY = datetime.date(2026, 6, 15)
CONFIG = {"alerts_channel_id": "alerts", "starred_channel_id": "starred", "poll_stale_days": 14}


def days_ago(n):
    return (TODAY - datetime.timedelta(days=n)).isoformat()


def job(key, posted="__today__", **kw):
    entry = {"key": key, "title": key, "url": "https://x", "source": "S",
             "location": "Hong Kong", "data_tech": True}
    if posted == "__today__":
        posted = TODAY.isoformat()
    if posted is not None:
        entry["posted"] = posted
    entry.update(kw)
    return entry


def fixed_today(day):
    """Freeze _reaction_poller's view of today without mutating datetime itself."""
    class FrozenDate(datetime.date):
        @classmethod
        def today(cls):
            return day
    return mock.patch.object(rp, "datetime", types.SimpleNamespace(date=FrozenDate))


class PollerTest(unittest.TestCase):
    """Runs rp.main() against a stubbed Discord + tracker, capturing the effects."""

    def setUp(self):
        self.state = {"seen": [], "pending": {}}
        self.saved = None
        self.statuses = []      # (key, status) written to the tracker
        self.forwarded = []     # content posted to #starred-jobs
        self.reactions = {}     # message_id -> {emoji_kind: [user_ids]}
        self.config = dict(CONFIG)

        def reaction_users(channel, mid, emoji):
            kind = {v: k for k, v in wl.EMOJI.items()}[emoji]
            return self.reactions.get(mid, {}).get(kind, [])

        patches = {
            "load_bot_config": lambda: self.config,
            "ensure_token": lambda cfg=None: "token",
            "bot_user_id": lambda: "bot",
            "reaction_users": reaction_users,
            "load_state": lambda: self.state,
            "save_state": self._save,
            "set_tracker_status": lambda k, s: self.statuses.append((k, s)) or True,
            "post_embed": lambda ch, embed, content=None: self.forwarded.append(content) or "mid",
            "make_embed": lambda j: {"title": j.get("title", "")},
            "log": lambda *a, **k: None,
        }
        self._patches = [mock.patch.object(wl, name, impl) for name, impl in patches.items()]
        self._patches.append(mock.patch.object(rp.time, "sleep", lambda *_: None))
        # check_config reads the token from the environment, which is what the real
        # ensure_token() populates -- the stub above only returns it.
        self._patches.append(mock.patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "token"}))
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _save(self, state):
        self.saved = state

    def run_poll(self, day=TODAY):
        with fixed_today(day):
            rp.main()
        return (self.saved or {}).get("pending", {})


class PrecedenceTests(PollerTest):
    def test_applied_beats_interested_and_skip(self):
        self.state["pending"] = {"m1": job("A")}
        self.reactions["m1"] = {"applied": ["u"], "interested": ["u"], "skip": ["u"]}
        pending = self.run_poll()
        self.assertEqual(self.statuses, [("A", "applied")])
        self.assertEqual(pending, {}, "handled entry should be dropped")

    def test_interested_beats_skip(self):
        self.state["pending"] = {"m1": job("A")}
        self.reactions["m1"] = {"interested": ["u"], "skip": ["u"]}
        self.run_poll()
        self.assertEqual(self.statuses, [("A", "interested")])

    def test_skip_alone_is_recorded(self):
        self.state["pending"] = {"m1": job("A")}
        self.reactions["m1"] = {"skip": ["u"]}
        self.run_poll()
        self.assertEqual(self.statuses, [("A", "skip")])


class ForwardingTests(PollerTest):
    def test_interested_and_applied_are_forwarded(self):
        self.state["pending"] = {"m1": job("A"), "m2": job("B")}
        self.reactions = {"m1": {"interested": ["u"]}, "m2": {"applied": ["u"]}}
        self.run_poll()
        self.assertEqual(len(self.forwarded), 2)
        self.assertTrue(any("Starred" in c for c in self.forwarded))
        self.assertTrue(any("Applied" in c for c in self.forwarded))

    def test_skip_is_not_forwarded(self):
        self.state["pending"] = {"m1": job("A")}
        self.reactions["m1"] = {"skip": ["u"]}
        self.run_poll()
        self.assertEqual(self.forwarded, [])


class BotReactionTests(PollerTest):
    def test_bots_own_prefilled_reactions_are_ignored(self):
        """The watcher pre-adds all three emoji; those must not count as a human vote."""
        self.state["pending"] = {"m1": job("A")}
        self.reactions["m1"] = {"interested": ["bot"], "applied": ["bot"], "skip": ["bot"]}
        pending = self.run_poll()
        self.assertEqual(self.statuses, [])
        self.assertIn("m1", pending, "unreacted entry must stay pending")

    def test_human_alongside_bot_still_counts(self):
        self.state["pending"] = {"m1": job("A")}
        self.reactions["m1"] = {"interested": ["bot", "human"]}
        self.run_poll()
        self.assertEqual(self.statuses, [("A", "interested")])


class ExpiryTests(PollerTest):
    def test_fresh_unreacted_entry_is_kept(self):
        self.state["pending"] = {"m1": job("A", posted=days_ago(3))}
        self.assertIn("m1", self.run_poll())

    def test_entry_past_stale_days_is_dropped(self):
        self.state["pending"] = {"m1": job("A", posted=days_ago(40))}
        self.assertEqual(self.run_poll(), {})

    def test_boundary_is_strictly_greater_than(self):
        self.state["pending"] = {"m1": job("A", posted=days_ago(14))}
        self.assertIn("m1", self.run_poll(), "exactly stale_days old is not yet stale")

    def test_undated_entries_are_backfilled_not_stranded(self):
        self.state["pending"] = {"m1": job("A", posted=""), "m2": job("B", posted="garbage"),
                                 "m3": job("C", posted=None)}
        pending = self.run_poll()
        self.assertEqual(sorted(pending), ["m1", "m2", "m3"])
        for mid in pending:
            self.assertEqual(pending[mid]["posted"], TODAY.isoformat())

    def test_backfilled_entries_do_eventually_expire(self):
        self.state["pending"] = {"m1": job("A", posted="")}
        self.state["pending"] = self.run_poll()
        self.saved = None
        self.assertEqual(self.run_poll(day=TODAY + datetime.timedelta(days=15)), {})


class RobustnessTests(PollerTest):
    def test_no_pending_work_returns_without_saving(self):
        self.run_poll()
        self.assertIsNone(self.saved, "must not rewrite state when there is nothing to do")

    def test_missing_channel_config_aborts_early(self):
        self.config["starred_channel_id"] = ""
        self.state["pending"] = {"m1": job("A")}
        self.run_poll()
        self.assertIsNone(self.saved)
        self.assertEqual(self.statuses, [])

    def test_one_failing_message_does_not_abort_the_rest(self):
        self.state["pending"] = {"m1": job("A"), "m2": job("B")}
        self.reactions["m2"] = {"applied": ["u"]}

        real = wl.reaction_users

        def flaky(channel, mid, emoji):
            if mid == "m1":
                raise RuntimeError("discord blew up")
            return real(channel, mid, emoji)

        with mock.patch.object(wl, "reaction_users", flaky):
            pending = self.run_poll()
        self.assertEqual(self.statuses, [("B", "applied")], "m2 still processed")
        self.assertIn("m1", pending, "failed message stays pending for a retry")


if __name__ == "__main__":
    unittest.main()
