from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .archive import ArchiveBuilder, image_type
from .cache import remove_chapter
from .crypto import get_cipher
from .errors import Blocked, Cancelled, DownloadError
from .models import DownloadedChapter, DownloadJob, TERMINAL
from .resolver import resolve_pages
from .storage import get_storage
from .transport import RequestGate, download_context


def progress(job, **values):
    values["updated_at"] = timezone.now()
    DownloadJob.objects.filter(pk=job.pk).update(**values)
    for key, value in values.items():
        setattr(job, key, value)


def delete_download(job):
    record = job.chapter
    keys = set(record.jobs.filter(kind="download").values_list("object_key", flat=True))
    keys.update((record.object_key, job.object_key))
    for key in sorted(keys - {""}):
        get_storage().delete(key)
    remove_chapter(record.chapter_key)
    with transaction.atomic():
        # Download tasks that were queued before deletion must never publish later.
        record.jobs.filter(kind="download").exclude(state__in=TERMINAL).update(state="cancelled", cancel_requested=True)
        DownloadedChapter.objects.filter(pk=record.pk).update(status="missing", manifest=[], object_key="", encrypted_size=0)
        progress(job, state="accepted", accepted_at=timezone.now(), error="")


def cleanup_target(job):
    if job.kind != "download":
        return
    try:
        get_storage().delete(job.object_key)
    except Exception:
        # Preserve cleanup across restart without deleting an accepted chapter archive.
        record = job.chapter
        if not record.jobs.exclude(state__in=TERMINAL).filter(kind="cleanup", object_key=job.object_key).exists():
            DownloadJob.objects.create(chapter=record, kind="cleanup", object_key=job.object_key,
                                       archive_version=job.archive_version)


def run_job(job):
    if job.state in TERMINAL:
        return
    progress(job, state="resolving", attempts=job.attempts + 1, error="", completed_pages=0,
             total_pages=0, transferred_bytes=0)
    builder = None
    gate = None
    try:
        if job.kind == "cleanup":
            # A delayed cleanup retry must not remove a target subsequently accepted.
            accepted = DownloadedChapter.objects.filter(
                pk=job.chapter_id, status="ready", object_key=job.object_key
            ).exists()
            if not accepted:
                get_storage().delete(job.object_key)
            progress(job, state="accepted", accepted_at=timezone.now())
            return
        if job.kind == "delete":
            delete_download(job)
            return
        gate = RequestGate(job)
        gate.check_cancelled()
        # Verify storage before making any upstream download requests.
        get_storage().verify()
        with download_context(gate):
            pages = resolve_pages(job.chapter)
            progress(job, state="downloading", total_pages=len(pages))
            builder = ArchiveBuilder(settings.CHAPTER_CONFIG, job.chapter.chapter_key, job.archive_version)
            for index, page in enumerate(pages):
                gate.check_cancelled()
                response = gate.request("GET", page["src"], limit=settings.CHAPTER_CONFIG.max_image_bytes)
                try:
                    image_type(response.content)
                except DownloadError:
                    gate.block(page["src"])
                    raise Blocked("Upstream returned a non-image or unsupported image. Downloads paused; explicit retry is required.") from None
                builder.append(response.content, page.get("description"))
                progress(job, state="downloading", completed_pages=index + 1,
                         transferred_bytes=job.transferred_bytes + len(response.content))
                del response
            plaintext, manifest = builder.finish()
            builder.close()
            builder = None
            gate.check_cancelled()
            progress(job, state="encrypting")
            encrypted = get_cipher().encrypt(plaintext, job.chapter.chapter_key, job.archive_version)
            del plaintext
            gate.check_cancelled()
            progress(job, state="uploading")
            get_storage().upload(job.object_key, encrypted)
            gate.check_cancelled()
            with transaction.atomic():
                current = DownloadJob.objects.select_for_update().get(pk=job.pk)
                record = DownloadedChapter.objects.select_for_update().get(pk=job.chapter_id)
                if current.cancel_requested or record.status == "deleting":
                    raise Cancelled("Download cancelled before acceptance.")
                record.status, record.manifest = "ready", manifest
                record.object_key, record.archive_version = job.object_key, job.archive_version
                record.encrypted_size = len(encrypted)
                record.save()
                progress(job, state="accepted", accepted_at=timezone.now(), next_eligible_at=None)
    except Cancelled:
        progress(job, state="cancelled", error="", next_eligible_at=None)
        cleanup_target(job)
    except (DownloadError, Blocked) as error:
        progress(job, state="failed", error=str(error))
        cleanup_target(job)
    except Exception:
        # No traceback/exception repr: source/SDK exceptions can contain signed URLs or credentials.
        progress(job, state="failed", error="Chapter operation failed. Check configuration and source compatibility.")
        cleanup_target(job)
    finally:
        if builder:
            builder.close()
        if gate:
            gate.close()


def recover_jobs():
    # Whole operation restart only. No partial archive or remote-object adoption.
    DownloadJob.objects.exclude(state__in=TERMINAL).update(
        state="queued", completed_pages=0, transferred_bytes=0, updated_at=timezone.now()
    )
