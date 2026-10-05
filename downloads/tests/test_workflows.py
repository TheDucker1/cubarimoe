import copy
import io
import tempfile
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.utils import timezone

from downloads.archive import ArchiveBuilder
from downloads.cache import clear_all, encrypted_archive
from downloads.config import prepare_directories
from downloads.crypto import get_cipher
from downloads.errors import DownloadError
from downloads.identity import identity
from downloads.integration import ready_chapter_for_locator, series_data
from downloads.models import ArchiveCacheEntry, DownloadedChapter, DownloadJob, SeriesSnapshot, UpstreamCooldown
from downloads.resolver import catalog
from downloads.services import queue_delete, queue_download, retry_job
from downloads.worker import recover_jobs, run_job
from .test_core import PNG


class FakeStorage:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fetches = []
        self.deletes = []
        self.on_upload = None

    def verify(self):
        pass

    def upload(self, key, data):
        self.uploads.append(key)
        self.objects[key] = data
        if self.on_upload:
            self.on_upload()

    def fetch(self, key, expected_size):
        self.fetches.append(key)
        if key not in self.objects:
            raise DownloadError("Stored chapter could not be retrieved.")
        return self.objects[key]

    def delete(self, key):
        self.deletes.append(key)
        self.objects.pop(key, None)


def dataset(locator=None):
    return {
        "slug": "series", "title": "Example", "description": "Description", "author": "Author",
        "artist": "Artist", "cover": "", "groups": {"0": "Team"},
        "chapters": {"1": {"title": "First", "volume": "1", "groups": {
            "0": locator if locator is not None else ["https://images.example/1.png", "https://images.example/2.png"]
        }}},
    }


class WorkflowTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        self.config = replace(settings.CHAPTER_CONFIG, runtime_dir=path, cache_dir=path / "cache", cache_max_files=2)
        self.override = override_settings(CHAPTER_CONFIG=self.config)
        self.override.enable()
        prepare_directories(self.config)
        self.addCleanup(self.override.disable)
        self.addCleanup(self.temp.cleanup)
        self.storage = FakeStorage()
        for module in ("downloads.worker", "downloads.cache"):
            p = patch(module + ".get_storage", return_value=self.storage)
            p.start()
            self.addCleanup(p.stop)
        p = patch("downloads.services.wake_worker")
        p.start()
        self.addCleanup(p.stop)
        self.data = dataset()
        SeriesSnapshot.objects.create(source="gist", series="series", data=self.data)
        self.selection = catalog("gist", "series", self.data)
        self.record = DownloadedChapter.objects.get()

    def queue(self):
        result = queue_download(self.record.chapter_key)
        return DownloadJob.objects.select_related("chapter").get(pk=result["job"])

    def download(self, job=None):
        job = job or self.queue()
        gate = Mock()
        gate.request.return_value = SimpleNamespace(content=PNG)
        with patch("downloads.worker.RequestGate", return_value=gate), patch(
            "downloads.worker.resolve_pages", return_value=[{"src": "https://images.example/1.png"},
                                                            {"src": "https://images.example/2.png"}]
        ):
            run_job(job)
        job.refresh_from_db()
        self.record.refresh_from_db()
        return job

    def test_download_accepts_only_after_upload_and_duplicate_is_noop(self):
        self.storage.on_upload = lambda: self.assertFalse(DownloadJob.objects.filter(accepted_at__isnull=False).exists())
        job = self.download()
        self.assertEqual(job.state, "accepted")
        self.assertIsNotNone(job.accepted_at)
        self.assertEqual(self.record.status, "ready")
        self.assertEqual(len(self.record.manifest), 2)
        self.assertEqual(queue_download(self.record.chapter_key)["result"], "already-ready")
        self.assertNotIn(PNG, self.storage.objects[job.object_key])

    def test_duplicate_pending_job_is_reused(self):
        first = queue_download(self.record.chapter_key)
        second = queue_download(self.record.chapter_key)
        self.assertEqual(second["result"], "already-queued")
        self.assertEqual(first["job"], second["job"])

    def test_restart_resets_unaccepted_job_and_overwrites_same_target(self):
        job = self.queue()
        deadline = timezone.now() + timedelta(hours=1)
        job.state, job.completed_pages, job.next_eligible_at = "uploading", 9, deadline
        job.save()
        self.storage.objects[job.object_key] = b"unaccepted-old-ciphertext"
        UpstreamCooldown.objects.create(host="*", next_eligible_at=deadline)
        recover_jobs()
        job.refresh_from_db()
        self.assertEqual(job.state, "queued")
        self.assertEqual(job.completed_pages, 0)
        self.assertEqual(job.next_eligible_at, deadline)
        target = job.object_key
        self.download(job)
        self.assertEqual(job.object_key, target)
        self.assertNotEqual(self.storage.objects[target], b"unaccepted-old-ciphertext")
        self.assertEqual(UpstreamCooldown.objects.get(host="*").next_eligible_at, deadline)

    def test_terminal_jobs_not_restarted(self):
        job = self.queue()
        for state in ("failed", "cancelled", "accepted"):
            job.state = state
            job.save()
            recover_jobs()
            job.refresh_from_db()
            self.assertEqual(job.state, state)

    def test_acceptance_cancel_race_does_not_publish_uploaded_archive(self):
        job = self.queue()
        self.storage.on_upload = lambda: DownloadJob.objects.filter(pk=job.pk).update(cancel_requested=True)
        self.download(job)
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(self.record.status, "missing")
        self.assertNotIn(job.object_key, self.storage.objects)

    def test_delete_cancels_pending_download_and_removes_remote_and_cache(self):
        job = self.download()
        encrypted_archive(self.record)
        result = queue_delete(self.record.chapter_key)
        deletion = DownloadJob.objects.select_related("chapter").get(pk=result["job"])
        run_job(deletion)
        deletion.refresh_from_db()
        self.record.refresh_from_db()
        self.assertEqual(deletion.state, "accepted")
        self.assertEqual(self.record.status, "missing")
        self.assertEqual(ArchiveCacheEntry.objects.count(), 0)
        self.assertNotIn(job.object_key, self.storage.objects)
        # Idempotent remote removal remains successful.
        result = queue_delete(self.record.chapter_key)
        run_job(DownloadJob.objects.select_related("chapter").get(pk=result["job"]))

    def test_delete_prevents_queued_download_from_publishing(self):
        job = self.queue()
        result = queue_delete(self.record.chapter_key)
        self.assertTrue(DownloadJob.objects.get(pk=job.pk).cancel_requested)
        deletion = DownloadJob.objects.select_related("chapter").get(pk=result["job"])
        run_job(deletion)
        self.assertEqual(DownloadJob.objects.get(pk=job.pk).state, "cancelled")

    def test_failed_delete_stays_deleting_until_explicit_retry(self):
        self.download()
        result = queue_delete(self.record.chapter_key)
        deletion = DownloadJob.objects.select_related("chapter").get(pk=result["job"])
        with patch.object(self.storage, "delete", side_effect=DownloadError("Stored chapter could not be deleted.")):
            run_job(deletion)
        self.record.refresh_from_db()
        deletion.refresh_from_db()
        self.assertEqual(self.record.status, "deleting")
        self.assertEqual(deletion.state, "failed")
        retry_job(deletion.pk)
        deletion.refresh_from_db()
        run_job(deletion)
        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "missing")

    def test_ready_series_and_deferred_chapter_do_not_contact_upstream(self):
        self.download()
        source = Mock()
        source.get_reader_prefix.return_value = "gist"
        source.series_api_handler.side_effect = AssertionError("Must not contact upstream")
        data = series_data(source, "series")
        self.assertTrue(data["chapters"]["1"]["groups"]["0"][0]["src"].startswith("/stored-chapters/"))
        source.series_api_handler.assert_not_called()
        upstream = series_data(source, "series", use_archives=False)
        self.assertEqual(upstream["chapters"]["1"]["groups"]["0"], self.data["chapters"]["1"]["groups"]["0"])
        cached = SeriesSnapshot.objects.get()
        cached.data = dataset("/read/api/gist/chapter/chapter-id/")
        cached.save()
        self.assertTrue(ready_chapter_for_locator("gist", "chapter-id")[0]["src"].startswith("/stored-chapters/"))

    def test_cache_lru_and_clear_keep_remote_archives(self):
        records = []
        for number in ("1", "2", "3"):
            key = identity("gist", "series", number, "Team")
            record, _ = DownloadedChapter.objects.get_or_create(chapter_key=key, defaults={
                "source": "gist", "series": "series", "chapter": number, "group": "Team",
            })
            record.status, record.object_key = "ready", "object-" + number
            record.encrypted_size = 20
            record.save()
            self.storage.objects[record.object_key] = b"encrypted-test-bytes"
            records.append(record)
        encrypted_archive(records[0])
        encrypted_archive(records[1])
        encrypted_archive(records[0])  # First is now the most recently used.
        encrypted_archive(records[2])
        self.assertTrue(ArchiveCacheEntry.objects.filter(chapter_key=records[0].chapter_key).exists())
        self.assertFalse(ArchiveCacheEntry.objects.filter(chapter_key=records[1].chapter_key).exists())
        self.assertEqual(len(self.storage.fetches), 3)
        clear_all()
        self.assertEqual(ArchiveCacheEntry.objects.count(), 0)
        self.assertEqual(len(self.storage.objects), 3)

    def test_image_response_mime_etag_and_no_silent_upstream_fallback(self):
        self.download()
        path = "/stored-chapters/{}/{}/pages/0001".format(self.record.chapter_key, self.record.archive_version)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PNG)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(self.client.get(path, HTTP_IF_NONE_MATCH=response["ETag"]).status_code, 304)
        self.assertEqual(len(self.storage.fetches), 1)
        self.record.status = "deleting"
        self.record.save()
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_management_auth_csrf_bulk_and_polling_do_not_fetch_upstream(self):
        self.assertEqual(self.client.get("/downloads/jobs/").status_code, 401)
        user = get_user_model().objects.create_user("owner", is_staff=True)
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        self.assertEqual(client.get("/downloads/").status_code, 200)
        token = client.cookies["csrftoken"].value
        payload = {"keys": [self.record.chapter_key]}
        self.assertEqual(client.post("/downloads/jobs/", payload, content_type="application/json").status_code, 403)
        response = client.post("/downloads/jobs/", payload, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 202)
        with patch("downloads.resolver.source_by_name", side_effect=AssertionError("Polling must not resolve upstream")):
            snapshot = client.get("/downloads/jobs/?source=gist&series=series")
            catalog_response = client.get("/downloads/catalog/gist/series/")
        self.assertEqual(snapshot.status_code, 200)
        self.assertEqual(catalog_response.status_code, 200)
        self.assertEqual(snapshot.json()["jobs"][0]["state"], "queued")
        self.assertNotIn(settings.CHAPTER_CONFIG.password, snapshot.content.decode())

    def test_worker_error_does_not_leak_source_or_sdk_secrets(self):
        job = self.queue()
        with patch("downloads.worker.RequestGate", side_effect=RuntimeError("secret-signed-url-token")):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertNotIn("secret-signed-url-token", job.error)

    def test_stale_cleanup_and_download_retries_cannot_remove_ready_archive(self):
        old = self.queue()
        old.state = "failed"
        old.save()
        accepted = self.download()
        cleanup = DownloadJob.objects.create(chapter=self.record, kind="cleanup", object_key=accepted.object_key)
        run_job(cleanup)
        self.assertIn(accepted.object_key, self.storage.objects)
        with self.assertRaises(DownloadError):
            retry_job(old.pk)

    def test_queue_prioritizes_deletion_and_cancel_during_global_block(self):
        from downloads.tasks import drain_queue
        invoke = drain_queue.call_local
        cancelled = self.queue()
        DownloadJob.objects.filter(pk=cancelled.pk).update(cancel_requested=True)
        UpstreamCooldown.objects.create(host="*", blocked=True)
        deletion = DownloadJob.objects.create(chapter=self.record, kind="delete")
        with patch("downloads.tasks.run_job") as runner, patch("downloads.tasks.drain_queue"):
            invoke()
            self.assertEqual(runner.call_args[0][0].pk, deletion.pk)
            deletion.state = "accepted"
            deletion.save()
            invoke()
            self.assertEqual(runner.call_args[0][0].pk, cancelled.pk)
            DownloadJob.objects.filter(pk=cancelled.pk).update(cancel_requested=False)
            runner.reset_mock()
            invoke()
            runner.assert_not_called()

    def test_worker_pauses_on_non_image_and_does_not_request_next_page(self):
        job = self.queue()
        gate = Mock()
        gate.request.return_value = SimpleNamespace(content=b"<html>Access blocked</html>")
        with patch("downloads.worker.RequestGate", return_value=gate), patch(
            "downloads.worker.resolve_pages", return_value=[{"src": "https://images.example/1.png"}] * 2
        ):
            run_job(job)
        job.refresh_from_db()
        self.assertEqual(job.state, "failed")
        self.assertEqual(gate.request.call_count, 1)
        gate.block.assert_called_once()

    def test_missing_storage_and_corruption_never_fall_back_to_upstream(self):
        self.download()
        path = "/stored-chapters/{}/{}/pages/0001".format(self.record.chapter_key, self.record.archive_version)
        self.storage.objects[self.record.object_key] = b"corrupt"
        self.assertEqual(self.client.get(path).status_code, 503)
        clear_all()
        self.storage.objects.clear()
        self.assertEqual(self.client.get(path).status_code, 503)

    def test_cache_byte_budget_and_unrelated_files(self):
        self.download()
        unrelated = self.config.cache_dir / "keep.txt"
        unrelated.write_text("unrelated")
        with override_settings(CHAPTER_CONFIG=replace(self.config, cache_max_bytes=1)):
            self.assertTrue(encrypted_archive(self.record))
        self.assertFalse(ArchiveCacheEntry.objects.exists())
        clear_all()
        self.assertTrue(unrelated.exists())

    def test_source_fallback_requires_explicit_staff_opt_in(self):
        import json
        from proxy.sources.gist import Gist
        self.download()
        source = Gist()
        for staff, requested, local in ((True, False, True), (False, True, True), (True, True, False)):
            request = RequestFactory().get("/read/api/gist/series/series/", {"upstream": "1"} if requested else {})
            request.user = SimpleNamespace(is_staff=staff)
            with patch.object(source, "series_api_handler", side_effect=AssertionError("Unexpected metadata request")):
                response = source.series_api_view(request, "series")
            pages = json.loads(response.content)["chapters"]["1"]["groups"]["0"]
            if local:
                self.assertTrue(pages[0]["src"].startswith("/stored-chapters/"))
            else:
                self.assertEqual(pages, self.data["chapters"]["1"]["groups"]["0"])
