import json
import re
import threading
from functools import wraps

from django.conf import settings
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .archive import extract_page
from .cache import clear_all, encrypted_archive
from .crypto import get_cipher
from .errors import DownloadError
from .models import ArchiveCacheEntry, DownloadedChapter, DownloadJob
from .resolver import catalog, source_by_name
from .services import cancel_job, queue_delete, queue_download, retry_job, snapshot

_decrypt_limit = threading.BoundedSemaphore(1)
KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def staff_api(function):
    @wraps(function)
    @never_cache
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Sign in to manage chapter downloads."}, status=401)
        if not request.user.is_staff:
            return JsonResponse({"error": "Chapter management requires a staff account."}, status=403)
        try:
            return function(request, *args, **kwargs)
        except DownloadError as error:
            return JsonResponse({"error": str(error)}, status=400)
        except DownloadJob.DoesNotExist:
            return JsonResponse({"error": "Job not found."}, status=404)
    return wrapped


def selection(request):
    if len(request.body) > 65536:
        raise DownloadError("Selection request is too large.")
    try:
        data = json.loads(request.body)
        keys = data["keys"]
        if (not isinstance(keys, list) or not 1 <= len(keys) <= settings.CHAPTER_CONFIG.max_bulk_items
                or any(not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) for key in keys)):
            raise ValueError()
        return list(dict.fromkeys(keys))
    except (ValueError, KeyError, TypeError):
        raise DownloadError("Provide a bounded list of valid chapter selection keys.") from None


@staff_api
@require_http_methods(["GET", "POST"])
def jobs(request):
    if request.method == "GET":
        result = snapshot()
        records = DownloadedChapter.objects.all()
        if request.GET.get("source") and request.GET.get("series"):
            records = records.filter(source=request.GET["source"], series=request.GET["series"])
        else:
            records = records.filter(status__in=("ready", "deleting"))
        result["chapters"] = list(records.values("chapter_key", "chapter", "group", "status"))
        result["cache"] = {
            "files": ArchiveCacheEntry.objects.count(),
            "bytes": ArchiveCacheEntry.objects.aggregate(total=Sum("size"))["total"] or 0,
            "max_files": settings.CHAPTER_CONFIG.cache_max_files,
        }
        return JsonResponse(result)
    return batch_response(selection(request), queue_download)


def batch_response(keys, action):
    results = []
    for key in keys:
        try:
            results.append(action(key))
        except DownloadError as error:
            results.append({"key": key, "result": "failed", "error": str(error)})
    return JsonResponse({"results": results}, status=202)


@staff_api
@require_POST
def bulk_delete(request):
    return batch_response(selection(request), queue_delete)


@staff_api
@require_http_methods(["DELETE"])
def delete_chapter(request, chapter_key):
    return JsonResponse(queue_delete(chapter_key), status=202)


@staff_api
@require_GET
def job_detail(request, job_id):
    job = next((item for item in snapshot(job_id=job_id)["jobs"] if item["id"] == job_id), None)
    if job is None:
        return JsonResponse({"error": "Job not found."}, status=404)
    return JsonResponse(job)


@staff_api
@require_POST
def job_cancel(request, job_id):
    cancel_job(job_id)
    return JsonResponse({"result": "cancellation-requested"})


@staff_api
@require_POST
def job_retry(request, job_id):
    retry_job(job_id)
    return JsonResponse({"result": "queued"}, status=202)


@staff_api
@require_POST
def cache_clear(request):
    clear_all()
    return JsonResponse({"result": "local-archive-cache-cleared"})


@staff_api
@require_GET
def series_catalog(request, source_name, series_id):
    from .models import SeriesSnapshot
    cached = SeriesSnapshot.objects.filter(source=source_name, series=series_id).first()
    if cached is None:
        return JsonResponse({"error": "Series metadata is unavailable. Reload the series page."}, status=409)
    data = catalog(source_name, series_id, cached.data)
    return JsonResponse({"groups": data["groups"], "downloads": data["downloads"]})


@require_GET
@sensitive_variables()
def image(request, chapter_key, version, page):
    record = get_object_or_404(DownloadedChapter, chapter_key=chapter_key, archive_version=version, status="ready")
    if not 1 <= page <= len(record.manifest):
        return HttpResponse(status=404)
    entry = record.manifest[page - 1]
    etag = '"{}-{}-{}"'.format(chapter_key, version, page)
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponse(status=304)
    else:
        try:
            with _decrypt_limit:
                plaintext = get_cipher().decrypt(encrypted_archive(record), chapter_key, version)
                contents = extract_page(plaintext, entry, settings.CHAPTER_CONFIG.max_image_bytes)
                del plaintext
                if not DownloadedChapter.objects.filter(pk=record.pk, status="ready").exists():
                    return HttpResponse(status=404)
            response = HttpResponse(contents, content_type=entry["type"])
            response["Content-Length"] = str(len(contents))
        except (DownloadError, OSError):
            return JsonResponse({"error": "Stored chapter unavailable. Refresh the chapter or check storage configuration."}, status=503)
    response["ETag"] = etag
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@staff_api
@require_GET
def queue_page(request):
    return render(request, "downloads/queue.html")


@staff_api
@require_GET
def library_page(request):
    from .library import library_titles
    query = request.GET.get("q", "").strip()[:200]
    return render(request, "downloads/library.html", {
        "titles": library_titles(query), "query": query,
        "max_bulk_items": settings.CHAPTER_CONFIG.max_bulk_items,
        "page_title": "Downloaded chapters",
    })


@never_cache
@require_GET
def library_series(request, chapter_key):
    from .library import stored_series
    _, data = stored_series(chapter_key)
    return JsonResponse(data)


@never_cache
@require_GET
def library_reader(request, chapter_key, chapter, page):
    from django.http import Http404
    from .identity import chapter_number
    from .library import stored_series
    anchor, data = stored_series(chapter_key)
    number = chapter_number(chapter)
    if number not in data["chapters"] or page < 1:
        raise Http404("Chapter is not stored for this group.")
    data.update({
        "series_name": data["title"], "chapter_number": number,
        "api_path": "/library/api/series/", "image_proxy_url": "",
        "download_source": anchor.source, "download_series": anchor.series,
        "stored_library": True, "version_query": settings.STATIC_VERSION,
        "relative_url": "/library/read/" + chapter_key,
        "reader_modifier": "library/read",
    })
    return render(request, "reader/reader.html", data)


@require_GET
def library_return(request, chapter_key):
    return redirect("download-library")
