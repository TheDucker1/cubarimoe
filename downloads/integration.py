from .errors import DownloadError
from .models import DownloadedChapter, SeriesSnapshot
from .resolver import catalog, page_urls


def series_data(source, meta_id, refresh=False):
    name = source.get_reader_prefix()
    cached = SeriesSnapshot.objects.filter(source=name, series=meta_id).first()
    # Reading accepted archives can proceed with the durable metadata snapshot.
    has_archive = DownloadedChapter.objects.filter(source=name, series=meta_id, status="ready").exists()
    if cached and has_archive and not refresh:
        data = cached.data
    else:
        result = source.series_api_handler(meta_id)
        if not result:
            raise DownloadError("Series metadata is unavailable.")
        data = result.objectify()
        SeriesSnapshot.objects.update_or_create(source=name, series=meta_id, defaults={"data": data})
    return catalog(name, meta_id, data)


def ready_chapter_for_locator(source_name, meta_id):
    from django.conf import settings
    locator = "/{}/api/{}/chapter/{}/".format(settings.PROXY_BASE_PATH, source_name, meta_id)
    records = list(DownloadedChapter.objects.filter(status="ready"))
    for cached in SeriesSnapshot.objects.all():
        if not records:
            break
        for number, chapter in cached.data["chapters"].items():
            for group_id, value in chapter["groups"].items():
                if value != locator:
                    continue
                from .identity import chapter_number
                from .resolver import group_identity
                group = group_identity(group_id, cached.data["groups"].get(str(group_id), group_id))
                for record in records:
                    if (record.source == cached.source and record.series == cached.series
                            and record.chapter == chapter_number(number) and record.group == group):
                        return page_urls(record)
    return None
