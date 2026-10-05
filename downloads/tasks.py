from huey.contrib.djhuey import db_task

from .models import DownloadJob, TERMINAL, UpstreamCooldown
from .worker import run_job


@db_task(retries=0)
def drain_queue():
    # Huey runs precisely one worker. Deletions/cleanup take precedence over the download backlog.
    waiting = DownloadJob.objects.exclude(state__in=TERMINAL).select_related("chapter")
    job = waiting.filter(kind__in=("delete", "cleanup")).first()
    if job is None:
        if UpstreamCooldown.objects.filter(blocked=True).exists():
            return
        job = waiting.filter(kind="download").first()
    if job:
        run_job(job)
        drain_queue()
