import copy
from urllib.parse import unquote, urlsplit

from django.conf import settings

from .errors import DownloadError
from .identity import chapter_number, identity
from .models import DownloadedChapter


def source_by_name(name):
    from proxy import sources
    for source in sources:
        if source.get_reader_prefix() == name:
            return source
    raise DownloadError("Source is not supported.")


def group_identity(group_id, group_label):
    # Several adapters enumerate a set in arbitrary order. Never hash the enumeration.
    return str(group_label) if str(group_id).isdigit() else str(group_id)


def page_urls(record):
    return [dict({"src": "/stored-chapters/{}/{}/pages/{:04d}".format(
        record.chapter_key, record.archive_version, i + 1)},
        **({"description": entry["description"]} if entry.get("description") else {}))
        for i, entry in enumerate(record.manifest)]


def catalog(source_name, series_id, data, use_archives=True):
    """Register only server-resolved identities; never accept arbitrary URLs in job APIs."""
    result = copy.deepcopy(data)
    downloads = {}
    selections = []
    for number, chapter in result["chapters"].items():
        for group_id in chapter["groups"]:
            label = str(result["groups"].get(str(group_id), group_id))
            group = group_identity(group_id, label)
            key = identity(source_name, series_id, number, group)
            selections.append((number, group_id, label, key, group))
    keys = {item[3] for item in selections}
    existing = set(DownloadedChapter.objects.filter(chapter_key__in=keys).values_list("chapter_key", flat=True))
    DownloadedChapter.objects.bulk_create([
        DownloadedChapter(chapter_key=key, source=source_name, series=series_id,
                          chapter=chapter_number(number), group=group,
                          title=str(result.get("title", ""))[:512])
        for number, group_id, label, key, group in selections if key not in existing
    ], ignore_conflicts=True)
    records = {record.chapter_key: record for record in DownloadedChapter.objects.filter(chapter_key__in=keys)}
    for number, chapter in result["chapters"].items():
        chapter_downloads = {}
        for group_id in chapter["groups"]:
            label = str(result["groups"].get(str(group_id), group_id))
            group = group_identity(group_id, label)
            key = identity(source_name, series_id, number, group)
            record = records[key]
            chapter_downloads[str(group_id)] = {"key": key, "status": record.status, "group": label}
            if use_archives and record.status == "ready":
                chapter["groups"][group_id] = page_urls(record)
        downloads[str(number)] = chapter_downloads
    result["downloads"] = downloads
    return result


def resolve_pages(record):
    source = source_by_name(record.source)
    response = source.series_api_handler(record.series)
    if not response:
        raise DownloadError("Source series could not be resolved.")
    data = response.objectify()
    locator = None
    for number, chapter in data["chapters"].items():
        if chapter_number(number) != record.chapter:
            continue
        for group_id, value in chapter["groups"].items():
            label = data["groups"].get(str(group_id), group_id)
            if group_identity(group_id, label) == record.group:
                locator = value
                break
    if locator is None:
        raise DownloadError("Selected chapter or group is no longer available.")
    if not isinstance(locator, list):
        if not isinstance(locator, str):
            raise DownloadError("Chapter page reference is invalid.")
        parsed = urlsplit(locator)
        prefix = "/{}/api/".format(settings.PROXY_BASE_PATH)
        if parsed.netloc or parsed.query or not parsed.path.startswith(prefix):
            raise DownloadError("Deferred chapters must use a registered local chapter API.")
        parts = parsed.path[len(prefix):].split("/")
        if len(parts) < 3 or parts[1] != "chapter":
            raise DownloadError("Deferred chapter API is invalid.")
        adapter = source_by_name(parts[0])
        meta_id = unquote("/".join(parts[2:]).rstrip("/"))
        response = adapter.chapter_api_handler(meta_id)
        if not response:
            raise DownloadError("Chapter pages could not be resolved.")
        locator = response.objectify()["pages"]
    if not isinstance(locator, list) or not locator or len(locator) > 10000:
        raise DownloadError("Chapter page list is empty or too large.")
    pages = []
    for value in locator:
        if isinstance(value, str):
            pages.append({"src": value})
        elif isinstance(value, dict) and isinstance(value.get("src"), str):
            pages.append({"src": value["src"], "description": str(value.get("description", ""))[:2048]})
        else:
            raise DownloadError("Chapter page list contains an invalid entry.")
    return pages
