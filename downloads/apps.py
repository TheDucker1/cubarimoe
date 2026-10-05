from django.apps import AppConfig


class DownloadsConfig(AppConfig):
    name = "downloads"

    def ready(self):
        from django.conf import settings
        from django.core.exceptions import ImproperlyConfigured
        from .config import prepare_directories
        from .crypto import get_cipher
        if settings.DEBUG:
            raise ImproperlyConfigured("DEBUG must be false when chapter storage credentials are loaded.")
        prepare_directories(settings.CHAPTER_CONFIG)
        get_cipher()  # Derive once at startup. Fail without displaying secret values.
