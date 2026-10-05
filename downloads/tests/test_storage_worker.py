import fcntl
import io
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from botocore.response import StreamingBody
from botocore.stub import Stubber
from django.conf import settings
from django.core.management import get_commands
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings

from downloads.config import prepare_directories
from downloads.errors import DownloadError
from downloads.management.commands.run_huey import Command, HueyCommand
from downloads.models import DownloadedChapter, DownloadJob
from downloads.storage import Storage


class StorageTests(TestCase):
    def setUp(self):
        self.storage = Storage(settings.CHAPTER_CONFIG)
        self.stub = Stubber(self.storage.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.addCleanup(self.storage.client.close)
        self.bucket = settings.CHAPTER_CONFIG.bucket

    def test_single_object_upload_fetch_delete_with_no_public_urls(self):
        import base64
        import hashlib
        body = b"already-encrypted-envelope"
        self.stub.add_response("get_bucket_versioning", {}, {"Bucket": self.bucket})
        self.stub.add_response("put_object", {}, {
            "Bucket": self.bucket, "Key": "target", "Body": body,
            "ContentType": "application/octet-stream",
            "ContentMD5": base64.b64encode(hashlib.md5(body).digest()).decode(),
        })
        stream = StreamingBody(io.BytesIO(body), len(body))
        self.stub.add_response("get_object", {"ContentLength": len(body), "Body": stream},
                               {"Bucket": self.bucket, "Key": "target"})
        self.stub.add_response("delete_object", {}, {"Bucket": self.bucket, "Key": "target"})
        self.storage.upload("target", body)
        self.assertEqual(self.storage.fetch("target", len(body)), body)
        self.assertTrue(stream._raw_stream.closed)
        self.storage.delete("target")
        self.stub.assert_no_pending_responses()

    def test_versioned_and_suspended_buckets_rejected_before_upload(self):
        for status in ("Enabled", "Suspended"):
            self.storage._verified = False
            self.stub.add_response("get_bucket_versioning", {"Status": status}, {"Bucket": self.bucket})
            with self.assertRaises(DownloadError):
                self.storage.upload("target", b"encrypted")
        self.stub.assert_no_pending_responses()

    def test_wrong_size_closes_stream_and_sdk_error_is_sanitized(self):
        self.storage._verified = True
        stream = StreamingBody(io.BytesIO(b"too-large"), 9)
        self.stub.add_response("get_object", {"ContentLength": 9, "Body": stream},
                               {"Bucket": self.bucket, "Key": "target"})
        with self.assertRaises(DownloadError):
            self.storage.fetch("target", 1)
        self.assertTrue(stream._raw_stream.closed)
        self.stub.add_client_error("delete_object", service_error_code="AccessDenied",
                                   service_message="never-disclose-this-signed-url-token",
                                   expected_params={"Bucket": self.bucket, "Key": "target"})
        with self.assertRaises(DownloadError) as caught:
            self.storage.delete("target")
        self.assertNotIn("never-disclose", str(caught.exception))
        self.stub.assert_no_pending_responses()


class ConsumerTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = replace(settings.CHAPTER_CONFIG, runtime_dir=root, cache_dir=root / "cache")
        self.override = override_settings(CHAPTER_CONFIG=self.config)
        self.override.enable()
        prepare_directories(self.config)
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp.cleanup)

    def test_custom_command_selected_and_parallel_modes_rejected(self):
        self.assertEqual(get_commands()["run_huey"], "downloads")
        for options in ({"workers": 2}, {"worker_type": "process"}, {"disable_autoload": True}):
            with self.assertRaises(CommandError):
                Command().handle(**options)

    def test_second_consumer_refused_and_restart_only_resets_unfinished(self):
        record = DownloadedChapter.objects.create(chapter_key="a" * 64, source="gist", series="series",
                                                   chapter="1", group="Team")
        pending = DownloadJob.objects.create(chapter=record, object_key="pending", state="uploading", completed_pages=4)
        terminal = DownloadJob.objects.create(chapter=record, object_key="done", state="accepted")
        with open(str(self.config.runtime_dir / "worker.lock"), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(CommandError):
                Command().handle()
        with patch.object(HueyCommand, "handle") as consumer, patch("downloads.tasks.drain_queue") as wake:
            Command().handle()
            consumer.assert_called_once_with(workers=1, worker_type="thread")
            wake.assert_called_once()
        pending.refresh_from_db()
        terminal.refresh_from_db()
        self.assertEqual((pending.state, pending.completed_pages), ("queued", 0))
        self.assertEqual(terminal.state, "accepted")


class ConcurrentCacheTests(TransactionTestCase):
    def test_parallel_misses_fetch_once_and_cache_survives_restart(self):
        from concurrent.futures import ThreadPoolExecutor
        from django.db import connections
        from threading import Barrier
        from downloads.cache import encrypted_archive
        from .test_workflows import FakeStorage
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(settings.CHAPTER_CONFIG, runtime_dir=root, cache_dir=root / "cache")
            prepare_directories(config)
            record = DownloadedChapter.objects.create(
                chapter_key="b" * 64, source="gist", series="parallel", chapter="1", group="Team",
                status="ready", object_key="cached", encrypted_size=20,
            )
            storage = FakeStorage()
            storage.objects["cached"] = b"encrypted-test-bytes"
            barrier = Barrier(2)
            def load():
                barrier.wait(timeout=5)
                try:
                    return encrypted_archive(record)
                finally:
                    connections.close_all()
            with override_settings(CHAPTER_CONFIG=config), patch("downloads.cache.get_storage", return_value=storage):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: load(), range(2)))
                self.assertEqual(results, [storage.objects["cached"]] * 2)
                self.assertEqual(storage.fetches, ["cached"])
                # A fresh storage client is not used when the durable cache row/file exists.
                with patch("downloads.cache.get_storage", side_effect=AssertionError("Unexpected remote request")):
                    self.assertEqual(encrypted_archive(record), storage.objects["cached"])
