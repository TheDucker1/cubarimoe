"""Database-only library and stored reader metadata; never resolve upstream URLs."""
from decimal import Decimal, InvalidOperation

from django.shortcuts import get_object_or_404

from .identity import chapter_number
from .models import DownloadedChapter, SeriesSnapshot
from .resolver import page_urls


def chapter_order(record):
    try:
        number = Decimal(record.chapter)
        if number.is_finite():
            return (0, number, record.group)
    except InvalidOperation:
        pass
    return (1, record.chapter, record.group)


def library_titles(query=""):
    records = DownloadedChapter.objects.filter(status="ready")
    if query:
        records = records.filter(title__icontains=query)
    titles = {}
    for record in records:
        key = (record.source, record.series)
        title = titles.setdefault(key, {"title": record.title or "Untitled", "source": record.source, "chapters": []})
        title["chapters"].append(record)
    for title in titles.values():
        title["chapters"].sort(key=chapter_order)
    return sorted(titles.values(), key=lambda title: (title["title"].casefold(), title["source"]))


def stored_series(anchor_key):
    anchor = get_object_or_404(DownloadedChapter, chapter_key=anchor_key)
    records = list(DownloadedChapter.objects.filter(
        source=anchor.source, series=anchor.series, group=anchor.group, status="ready"))
    if not records:
        from django.http import Http404
        raise Http404("No stored chapters remain in this group.")
    cached = SeriesSnapshot.objects.filter(source=anchor.source, series=anchor.series).first()
    metadata = {chapter_number(number): chapter for number, chapter in
                (cached.data.get("chapters", {}) if cached else {}).items()}
    data = {"slug": anchor_key, "title": records[0].title or "Untitled", "description": "Stored chapters",
            "author": "", "artist": "", "cover": "", "groups": {"0": anchor.group}, "chapters": {}, "downloads": {}}
    for record in sorted(records, key=chapter_order):
        chapter = metadata.get(record.chapter, {})
        data["chapters"][record.chapter] = {
            "title": str(chapter.get("title", "")), "volume": str(chapter.get("volume", "1")),
            "groups": {"0": page_urls(record)}, "release_date": {"0": 0},
        }
        data["downloads"][record.chapter] = {"0": {"key": record.chapter_key, "status": "ready", "group": record.group}}
    return anchor, data
