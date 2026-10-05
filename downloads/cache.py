"""Encrypted disk LRU. A cross-process lock also coordinates worker deletion."""
import fcntl
import os
import re
import tempfile
from contextlib import contextmanager

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import ArchiveCacheEntry, DownloadedChapter
from .errors import DownloadError
from .storage import get_storage

CACHE_FILENAME = re.compile(r"^[a-f0-9]{64}-[a-f0-9-]{36}\.enc$")


def owned_cache_file(path):
    return bool(CACHE_FILENAME.fullmatch(path.name) or path.name.startswith(".load-"))


@contextmanager
def cache_lock():
    config = settings.CHAPTER_CONFIG
    with open(str(config.runtime_dir / "cache.lock"), "a+b") as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _remove(entry):
    (settings.CHAPTER_CONFIG.cache_dir / entry.filename).unlink(missing_ok=True)
    entry.delete()


def encrypted_archive(chapter):
    config = settings.CHAPTER_CONFIG
    filename = "{}-{}.enc".format(chapter.chapter_key, chapter.archive_version)
    path = config.cache_dir / filename
    with cache_lock():
        if not DownloadedChapter.objects.filter(pk=chapter.pk, status="ready", archive_version=chapter.archive_version).exists():
            raise DownloadError("Downloaded chapter is no longer available.")
        # Cleanup interrupted temporary files and files without durable LRU records.
        known = set(ArchiveCacheEntry.objects.values_list("filename", flat=True))
        for candidate in config.cache_dir.iterdir():
            if (owned_cache_file(candidate) and candidate.is_file()
                    and not candidate.is_symlink() and candidate.name not in known):
                candidate.unlink()
        entry = ArchiveCacheEntry.objects.filter(filename=filename).first()
        if entry and path.is_file() and not path.is_symlink() and path.stat().st_size == chapter.encrypted_size:
            data = path.read_bytes()
            ArchiveCacheEntry.objects.filter(pk=entry.pk).update(accessed_at=timezone.now())
            return data
        if entry:
            _remove(entry)
        data = get_storage().fetch(chapter.object_key, chapter.encrypted_size)
        if len(data) > config.cache_max_bytes:
            return data
        while True:
            entries = ArchiveCacheEntry.objects.all()
            total = entries.aggregate(total=Sum("size"))["total"] or 0
            if entries.count() < config.cache_max_files and total + len(data) <= config.cache_max_bytes:
                break
            _remove(entries.order_by("accessed_at", "pk").first())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=str(config.cache_dir), prefix=".load-", delete=False) as stream:
                temporary = stream.name
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(path))
            ArchiveCacheEntry.objects.create(
                chapter_key=chapter.chapter_key, archive_version=chapter.archive_version,
                filename=filename, size=len(data),
            )
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return data


def remove_chapter(chapter_key):
    with cache_lock():
        for entry in ArchiveCacheEntry.objects.filter(chapter_key=chapter_key):
            _remove(entry)


def clear_all():
    with cache_lock():
        for entry in ArchiveCacheEntry.objects.all():
            _remove(entry)
        for path in settings.CHAPTER_CONFIG.cache_dir.iterdir():
            if owned_cache_file(path) and path.is_file() and not path.is_symlink():
                path.unlink()
