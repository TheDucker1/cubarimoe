"""Constrain Huey's existing consumer; do not implement another queue engine."""
import fcntl
import os

from django.conf import settings
from django.core.management.base import CommandError
from huey.contrib.djhuey.management.commands.run_huey import Command as HueyCommand


class Command(HueyCommand):
    def handle(self, *args, **options):
        if options.get("workers") not in (None, 1) or options.get("worker_type") not in (None, "thread"):
            raise CommandError("Chapter downloads require exactly one thread worker.")
        if options.get("disable_autoload"):
            raise CommandError("Chapter tasks must be autoloaded.")
        options.update(workers=1, worker_type="thread")
        path = settings.CHAPTER_CONFIG.runtime_dir / "worker.lock"
        with open(str(path), "a+b") as lock:
            os.chmod(str(path), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError("A chapter download worker is already running.") from None
            from downloads.worker import recover_jobs
            from downloads.tasks import drain_queue
            from downloads.cache import reconcile
            reconcile()
            recover_jobs()
            drain_queue()
            super().handle(*args, **options)
