import io
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from pyrate_limiter import BucketFullException

from downloads.config import prepare_directories
from downloads.errors import Blocked, Cancelled, DownloadError
from downloads.models import DownloadedChapter, DownloadJob, UpstreamCooldown
from downloads.transport import RequestGate, current_gate, download_context, validate_url
from proxy.source.helpers import source_executor


class Clock:
    def __init__(self):
        self.seconds = 1700000000.0

    def now(self):
        return datetime.fromtimestamp(self.seconds, timezone.utc)

    def sleep(self, seconds):
        self.seconds += seconds


def response(status=200, body=b"image", headers=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result.raw = io.BytesIO(body)
    result.url = "https://images.example/page"
    return result


class RequestPolicyTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name)
        self.config = replace(settings.CHAPTER_CONFIG, runtime_dir=path, cache_dir=path / "cache")
        override = override_settings(CHAPTER_CONFIG=self.config)
        override.enable()
        self.addCleanup(override.disable)
        prepare_directories(self.config)
        chapter = DownloadedChapter.objects.create(chapter_key="a" * 64, source="gist", series="s", chapter="1", group="g")
        self.job = DownloadJob.objects.create(chapter=chapter, object_key="target")
        self.clock = Clock()
        for name, value in (("downloads.transport.timezone.now", self.clock.now),
                            ("downloads.transport.time.time", lambda: self.clock.seconds),
                            ("downloads.transport.time.sleep", self.clock.sleep),
                            ("downloads.transport.validate_url", lambda _: None)):
            p = patch(name, value)
            p.start()
            self.addCleanup(p.stop)
        self.gate = RequestGate(self.job)
        self.addCleanup(self.gate.close)

    def test_completion_gap_redirect_and_retry_all_pass_the_gate(self):
        calls = []
        responses = [response(302, headers={"Location": "https://other.example/image"}),
                     response(429, headers={"Retry-After": "10"}), response(), response()]
        def send(*args, **kwargs):
            calls.append((self.clock.seconds, args, kwargs))
            # Simulated one-second response duration.
            self.clock.seconds += 1
            return responses.pop(0)
        self.gate.session.request = send
        self.gate.request("GET", "https://images.example/first", headers={"Authorization": "private"})
        self.gate.request("GET", "https://other.example/second")
        self.assertEqual(len(calls), 4)
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 3)
        self.assertGreaterEqual(calls[2][0] - calls[1][0], 11)
        self.assertGreaterEqual(calls[3][0] - calls[2][0], 3)
        self.assertNotIn("Authorization", calls[1][2]["headers"])
        self.assertTrue(all(not item[2]["allow_redirects"] for item in calls))

    def test_403_stops_immediately_and_blocks_later_jobs(self):
        send = Mock(return_value=response(403))
        self.gate.session.request = send
        with self.assertRaises(Blocked):
            self.gate.request("GET", "https://images.example/image")
        self.assertEqual(send.call_count, 1)
        self.assertTrue(UpstreamCooldown.objects.get(host="*").blocked)
        with self.assertRaises(Blocked):
            self.gate.request("GET", "https://another.example/image")
        self.assertEqual(send.call_count, 1)

    def test_additive_jitter_never_shortens_completion_gap_and_is_persisted(self):
        self.gate.config = replace(self.config, delay=60, jitter=15)
        self.gate.session.request = Mock(side_effect=lambda *args, **kwargs: response())
        for extra in (0, 15):
            now = self.clock.now()
            with patch("downloads.transport.random.uniform", return_value=extra) as randomize:
                self.gate.request("GET", "https://images.example/page")
            deadline = UpstreamCooldown.objects.get(host="*").next_eligible_at
            self.assertGreaterEqual((deadline - now).total_seconds(), 60 + extra)
            randomize.assert_called_once_with(0, 15)
            self.clock.seconds = deadline.timestamp()
    def test_rate_history_survives_a_new_gate(self):
        self.gate.limiter.try_acquire("all-downloads")
        other = RequestGate(self.job)
        try:
            with self.assertRaises(BucketFullException):
                other.limiter.try_acquire("all-downloads")
        finally:
            other.close()

    def test_persisted_cooldown_is_respected_after_restart(self):
        UpstreamCooldown.objects.create(host="images.example", next_eligible_at=self.clock.now() + timedelta(seconds=20))
        times = []
        def send(*args, **kwargs):
            times.append(self.clock.seconds)
            return response()
        self.gate.session.request = send
        original = self.clock.seconds
        self.gate.request("GET", "https://images.example/page")
        self.assertGreaterEqual(times[0] - original, 20)

    def test_cancellation_interrupts_cooldown_without_a_request(self):
        DownloadJob.objects.filter(pk=self.job.pk).update(cancel_requested=True)
        self.gate.session.request = Mock()
        with self.assertRaises(Cancelled):
            self.gate.request("GET", "https://images.example/page")
        self.gate.session.request.assert_not_called()

    def test_response_body_limit_and_retry_budget(self):
        self.gate.session.request = Mock(return_value=response(body=b"too-many-bytes"))
        with self.assertRaises(DownloadError):
            self.gate.request("GET", "https://images.example/page", limit=3)
        self.gate.session.request = Mock(side_effect=lambda *args, **kwargs: response(429))
        with self.assertRaises(DownloadError):
            self.gate.request("GET", "https://images.example/page")
        self.assertEqual(self.gate.session.request.call_count, self.config.max_retries)

    def test_source_executors_remain_in_worker_context(self):
        with download_context(self.gate):
            with source_executor(3) as executor:
                result = executor.map(lambda _: current_gate(), range(3))
            self.assertEqual(result, [self.gate] * 3)
        self.assertIsNone(current_gate())

    def test_legacy_gist_manual_redirect_keeps_source_semantics_and_pacing(self):
        from proxy.sources.gist import Gist
        calls = []
        responses = [response(302, headers={"location": "https://raw.example/series.json"}), response()]
        def send(*args, **kwargs):
            calls.append(self.clock.seconds)
            return responses.pop(0)
        self.gate.session.request = send
        with download_context(self.gate):
            original, result = Gist.request_handler("abc")
        self.assertEqual(original, "https://git.io/abc")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1] - calls[0], self.config.delay)


class URLBoundaryTests(TestCase):
    def test_non_public_redirect_target_is_rejected(self):
        addresses = [(None, None, None, None, ("127.0.0.1", 80))]
        with patch("downloads.transport.socket.getaddrinfo", return_value=addresses):
            with self.assertRaises(DownloadError):
                validate_url("http://example.test/private")
