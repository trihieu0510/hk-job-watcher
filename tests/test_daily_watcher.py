"""Tests for _daily_job_watcher: the HTTP retry layer, role filtering, and dedup.

Importing this module pulls in playwright (a module-level import), but no
browser is ever launched -- the render path is not exercised here and every
network call is stubbed.
"""
import io
import os
import sys
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _daily_job_watcher as w  # noqa: E402


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(body))


def responder(seq, body=b'{"ok":true}'):
    """urlopen stub: each entry is either an exception to raise or None to succeed."""
    it = iter(seq)
    calls = []

    def fake(req, timeout=None):
        calls.append(req)
        item = next(it)
        if isinstance(item, Exception):
            raise item

        class R:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()
    fake.calls = calls
    return fake


class HttpRetryTests(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(w.time, "sleep", lambda *_: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def get(self, seq):
        stub = responder(seq)
        with mock.patch.object(urllib.request, "urlopen", stub):
            return w.http_get("https://example.test/jobs"), stub

    def test_succeeds_first_try(self):
        body, stub = self.get([None])
        self.assertEqual(body, '{"ok":true}')
        self.assertEqual(len(stub.calls), 1)

    def test_retries_timeout_then_succeeds(self):
        body, stub = self.get([TimeoutError(), None])
        self.assertEqual(body, '{"ok":true}')
        self.assertEqual(len(stub.calls), 2)

    def test_retries_server_error(self):
        body, stub = self.get([http_error(502), None])
        self.assertEqual(body, '{"ok":true}')

    def test_client_error_is_not_retried(self):
        stub = responder([http_error(404)])
        with mock.patch.object(urllib.request, "urlopen", stub):
            with self.assertRaises(urllib.error.HTTPError):
                w.http_get("https://example.test/jobs")
        self.assertEqual(len(stub.calls), 1, "a 404 must not burn retries")

    def test_gives_up_after_three_attempts(self):
        stub = responder([TimeoutError()] * 3)
        with mock.patch.object(urllib.request, "urlopen", stub):
            with self.assertRaises(OSError):
                w.http_get("https://example.test/jobs")
        self.assertEqual(len(stub.calls), 3)

    def test_post_sends_a_json_body(self):
        stub = responder([None])
        with mock.patch.object(urllib.request, "urlopen", stub):
            w.http_post("https://example.test/s", {"limit": 20})
        req = stub.calls[0]
        self.assertEqual(req.data, b'{"limit": 20}')
        self.assertEqual(req.get_header("Content-type"), "application/json")


class IsHkStudentTests(unittest.TestCase):
    @staticmethod
    def entry(title, location="", source="Greenhouse"):
        return {"title": title, "location": location, "source": source}

    def test_accepts_hk_internship(self):
        self.assertTrue(w.is_hk_student(self.entry("Summer Intern", "Hong Kong")))

    def test_accepts_graduate_and_campus_wording(self):
        for title in ("Graduate Analyst", "Campus Hire Programme", "2027 Summer Analyst"):
            self.assertTrue(w.is_hk_student(self.entry(title, "Hong Kong")), title)

    def test_rejects_non_student_role(self):
        self.assertFalse(w.is_hk_student(self.entry("Senior Data Engineer", "Hong Kong")))

    def test_rejects_senior_titles_even_when_student_words_appear(self):
        self.assertFalse(w.is_hk_student(self.entry("Head of Graduate Recruitment", "Hong Kong")))
        self.assertFalse(w.is_hk_student(self.entry("Vice President, Campus", "Hong Kong")))

    def test_rejects_other_cities(self):
        self.assertFalse(w.is_hk_student(self.entry("Summer Intern", "Singapore")))

    def test_hk_implied_sources_pass_without_an_explicit_location(self):
        self.assertTrue(w.is_hk_student(self.entry("Summer Intern", "", source="JobsDB")))

    def test_hk_implied_sources_still_reject_a_named_other_city(self):
        self.assertFalse(w.is_hk_student(self.entry("Summer Intern", "London", source="JobsDB")))


class DiscoverDedupTests(unittest.TestCase):
    """discover() with no render sources: exercises dedup, keying and data/tech tagging."""

    def run_discover(self, fetched):
        src = {"name": "Greenhouse", "type": "greenhouse", "token": "t"}
        with mock.patch.object(w, "SOURCES", [src]), \
             mock.patch.object(w, "fetch_greenhouse", lambda s: fetched), \
             mock.patch.object(w, "log", lambda *a, **k: None):
            return w.discover()

    def hit(self, title, location="Hong Kong"):
        return {"title": title, "location": location, "url": "https://x", "source": "Greenhouse"}

    def test_dedups_on_normalised_title(self):
        out = self.run_discover([self.hit("Data  Intern"), self.hit("data intern"),
                                 self.hit("Data Intern")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["_key"], "Greenhouse|data intern")

    def test_tags_data_tech(self):
        out = self.run_discover([self.hit("Data Science Intern"), self.hit("Marketing Intern")])
        tags = {e["title"]: e["data_tech"] for e in out}
        self.assertTrue(tags["Data Science Intern"])
        self.assertFalse(tags["Marketing Intern"])

    def test_filters_out_non_matching_roles(self):
        self.assertEqual(self.run_discover([self.hit("Managing Director")]), [])

    def test_a_failing_source_does_not_abort_the_run(self):
        good = {"name": "Greenhouse", "type": "greenhouse", "token": "t"}
        bad = {"name": "Lever", "type": "lever", "token": "t"}

        def boom(s):
            raise RuntimeError("board is down")

        with mock.patch.object(w, "SOURCES", [bad, good]), \
             mock.patch.object(w, "fetch_lever", boom), \
             mock.patch.object(w, "fetch_greenhouse", lambda s: [self.hit("Data Intern")]), \
             mock.patch.object(w, "log", lambda *a, **k: None):
            out = w.discover()
        self.assertEqual([e["title"] for e in out], ["Data Intern"])


if __name__ == "__main__":
    unittest.main()
