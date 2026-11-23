import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import jobs_receiver as jobs


class JobsReceiverTests(unittest.TestCase):
    def setUp(self):
        self.job = {
            "company": "Example", "title": "Python Backend",
            "url": "https://example.com/jobs/123",
            "location": "Bilbao", "match_score": 90,
        }
        self.feed = {"schema_version": 1, "jobs": [self.job]}

    def test_validation(self):
        self.assertEqual(len(jobs.validate_feed(self.feed)), 1)
        with self.assertRaises(ValueError):
            jobs.validate_feed({"schema_version": 1, "jobs": [{**self.job, "url": "http://example.com"}]})
        with self.assertRaises(ValueError):
            jobs.validate_feed({"schema_version": 1, "jobs": [{**self.job, "match_score": True}]})

    def test_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "jobs.json"
            delivered = []
            with patch.object(jobs, "_load_json_url", return_value={"schema_version": 1, "jobs": [self.job, self.job]}):
                self.assertEqual(jobs.run_once(jobs.DEFAULT_FEED, state, sender=lambda *args: delivered.append(args)), 1)
                self.assertEqual(jobs.run_once(jobs.DEFAULT_FEED, state, sender=lambda *args: delivered.append(args)), 0)
            self.assertEqual(len(delivered), 1)

    def test_failure_does_not_mark_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "jobs.json"
            def fail(*args):
                raise RuntimeError("Telegram unavailable")
            with patch.object(jobs, "_load_json_url", return_value=self.feed):
                with self.assertRaises(RuntimeError):
                    jobs.run_once(jobs.DEFAULT_FEED, state, sender=fail)
            self.assertFalse(state.exists())

    def test_markup_escaped(self):
        self.assertIn("&lt;Backend&gt;", jobs.render_job({**self.job, "title": "<Backend>"}))


if __name__ == "__main__":
    unittest.main()
