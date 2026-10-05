import uuid
from django.db import models
from django.db.models import Q

TERMINAL = ("accepted", "failed", "cancelled")


class SeriesSnapshot(models.Model):
    source = models.CharField(max_length=64)
    series = models.CharField(max_length=2048)
    data = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("source", "series"), name="one_series_snapshot")]


class DownloadedChapter(models.Model):
    chapter_key = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=64)
    series = models.CharField(max_length=2048)
    chapter = models.CharField(max_length=128)
    group = models.CharField(max_length=512)
    title = models.CharField(max_length=512, blank=True)
    status = models.CharField(max_length=32, default="missing")
    archive_version = models.UUIDField(default=uuid.uuid4)
    object_key = models.CharField(max_length=1024, blank=True)
    encrypted_size = models.PositiveIntegerField(default=0)
    manifest = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)


class DownloadJob(models.Model):
    chapter = models.ForeignKey(DownloadedChapter, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(max_length=16, default="download")
    state = models.CharField(max_length=32, default="queued")
    attempts = models.PositiveIntegerField(default=0)
    completed_pages = models.PositiveIntegerField(default=0)
    total_pages = models.PositiveIntegerField(default=0)
    transferred_bytes = models.PositiveIntegerField(default=0)
    next_eligible_at = models.DateTimeField(null=True, blank=True)
    cancel_requested = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    archive_version = models.UUIDField(default=uuid.uuid4)
    object_key = models.CharField(max_length=1024)
    error = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("id",)
        constraints = [models.UniqueConstraint(
            fields=("chapter", "kind"), condition=~Q(state__in=TERMINAL), name="one_active_chapter_job"
        )]


class ArchiveCacheEntry(models.Model):
    chapter_key = models.CharField(max_length=64)
    archive_version = models.UUIDField()
    filename = models.CharField(max_length=128, unique=True)
    size = models.PositiveIntegerField()
    accessed_at = models.DateTimeField(auto_now=True)


class UpstreamCooldown(models.Model):
    host = models.CharField(max_length=256, unique=True)
    next_eligible_at = models.DateTimeField(null=True)
    blocked = models.BooleanField(default=False)
