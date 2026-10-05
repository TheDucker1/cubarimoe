from django.core.management.base import BaseCommand, CommandError
from downloads.errors import DownloadError
from downloads.storage import get_storage


class Command(BaseCommand):
    help = "Verify chapter storage configuration without uploading or deleting anything."

    def handle(self, *args, **options):
        try:
            get_storage().verify()
        except DownloadError as error:
            raise CommandError(str(error)) from None
        self.stdout.write("Chapter storage configuration verified.")
