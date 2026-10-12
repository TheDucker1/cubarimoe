from django.urls import path
from . import views

urlpatterns = [
    path("library/", views.library_page, name="download-library"),
    path("library/api/series/<str:chapter_key>/", views.library_series),
    path("library/read/<str:chapter_key>/", views.library_return),
    path("library/read/<str:chapter_key>/<str:chapter>/<int:page>/", views.library_reader, name="library-reader"),
    path("downloads/", views.queue_page, name="download-queue"),
    path("downloads/jobs/", views.jobs),
    path("downloads/jobs/<int:job_id>/", views.job_detail),
    path("downloads/jobs/<int:job_id>/cancel/", views.job_cancel),
    path("downloads/jobs/<int:job_id>/retry/", views.job_retry),
    path("downloads/chapters/bulk-delete/", views.bulk_delete),
    path("downloads/chapters/<str:chapter_key>/", views.delete_chapter),
    path("downloads/cache/clear/", views.cache_clear),
    path("downloads/catalog/<str:source_name>/<str:series_id>/", views.series_catalog),
    path("stored-chapters/<str:chapter_key>/<uuid:version>/pages/<int:page>", views.image),
]
