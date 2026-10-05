import uuid
import secrets
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .errors import DownloadError
from .models import DownloadedChapter, DownloadJob, TERMINAL, UpstreamCooldown


def wake_worker():
    from .tasks import drain_queue
    transaction.on_commit(lambda: drain_queue())


def object_key():
    # Independent randomness, never a hash of public chapter identity or a file extension.
    return "{}/{}".format(settings.CHAPTER_CONFIG.prefix, secrets.token_hex(32))


@transaction.atomic
def queue_download(key):
    record = DownloadedChapter.objects.select_for_update().filter(chapter_key=key).first()
    if record is None:
        raise DownloadError("Chapter selection is unknown. Refresh the series first.")
    if record.status == "deleting":
        raise DownloadError("Chapter is being removed.")
    if record.status == "ready":
        return {"key": key, "result": "already-ready"}
    job = record.jobs.exclude(state__in=TERMINAL).filter(kind="download").first()
    if job:
        return {"key": key, "result": "already-queued", "job": job.pk}
    version = uuid.uuid4()
    job = DownloadJob.objects.create(chapter=record, archive_version=version, object_key=object_key())
    wake_worker()
    return {"key": key, "result": "queued", "job": job.pk}


@transaction.atomic
def queue_delete(key):
    record = DownloadedChapter.objects.select_for_update().filter(chapter_key=key).first()
    if record is None:
        return {"key": key, "result": "already-deleted"}
    existing = record.jobs.exclude(state__in=TERMINAL).filter(kind="delete").first()
    if existing:
        return {"key": key, "result": "already-queued", "job": existing.pk}
    targets = record.jobs.filter(kind="download").exclude(state__in=TERMINAL)
    targets.update(cancel_requested=True)
    record.status = "deleting"
    record.save(update_fields=("status", "updated_at"))
    job = DownloadJob.objects.create(chapter=record, kind="delete", object_key=record.object_key,
                                     archive_version=record.archive_version)
    wake_worker()
    return {"key": key, "result": "queued", "job": job.pk}


@transaction.atomic
def cancel_job(job_id):
    job = DownloadJob.objects.select_for_update().get(pk=job_id)
    if job.state in TERMINAL:
        return
    if job.kind != "download":
        raise DownloadError("Only download jobs can be cancelled; removal and cleanup must finish.")
    job.cancel_requested = True
    job.save(update_fields=("cancel_requested", "updated_at"))
    wake_worker()


@transaction.atomic
def retry_job(job_id):
    job = DownloadJob.objects.select_for_update().select_related("chapter").get(pk=job_id)
    if job.state != "failed":
        raise DownloadError("Only failed jobs can be retried.")
    if job.kind == "download" and job.chapter.status == "ready":
        raise DownloadError("Chapter is already downloaded. Remove it before downloading again.")
    if job.chapter.status == "deleting" and job.kind == "download":
        raise DownloadError("Chapter is being removed.")
    if job.chapter.jobs.exclude(pk=job.pk).exclude(state__in=TERMINAL).filter(kind=job.kind).exists():
        raise DownloadError("Chapter already has an active job.")
    job.state, job.error, job.cancel_requested = "queued", "", False
    job.completed_pages = job.transferred_bytes = 0
    job.save()
    # Explicit user retry clears blocking flags, never the persisted time deadlines or limiter history.
    UpstreamCooldown.objects.filter(blocked=True).update(blocked=False)
    wake_worker()


def snapshot(job_id=None):
    jobs = list(DownloadJob.objects.select_related("chapter").exclude(state__in=TERMINAL))
    recent = list(DownloadJob.objects.select_related("chapter").filter(state__in=TERMINAL).order_by("-id")[:50])
    if job_id is not None and not any(job.pk == job_id for job in jobs + recent):
        recent.append(DownloadJob.objects.select_related("chapter").get(pk=job_id))
    waiting = [j for j in jobs if j.kind == "download"]
    positions = {j.pk: i + 1 for i, j in enumerate(waiting)}
    def serialize(job):
        return {
            "id": job.pk, "key": job.chapter.chapter_key, "source": job.chapter.source,
            "series": job.chapter.series, "title": job.chapter.title, "chapter": job.chapter.chapter,
            "group": job.chapter.group, "kind": job.kind, "state": job.state,
            "pages": job.completed_pages, "total_pages": job.total_pages,
            "bytes": job.transferred_bytes, "queue_position": positions.get(job.pk),
            "next_attempt": job.next_eligible_at.isoformat() if job.next_eligible_at else None,
            "error": job.error, "cancel_requested": job.cancel_requested,
        }
    all_jobs = jobs + recent
    return {
        "jobs": [serialize(job) for job in all_jobs], "active": bool(jobs),
        "paused": UpstreamCooldown.objects.filter(blocked=True).exists(),
        "counts": {state: DownloadJob.objects.filter(state=state).count()
                   for state in ("accepted", "failed", "cancelled")},
        "revision": max((j.updated_at.isoformat() for j in all_jobs), default="0"),
        "timestamp": timezone.now().isoformat(), "poll_seconds": settings.CHAPTER_CONFIG.poll_seconds,
        "max_bulk_items": settings.CHAPTER_CONFIG.max_bulk_items,
    }
