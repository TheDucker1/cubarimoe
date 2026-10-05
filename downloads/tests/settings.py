"""Isolated test-only configuration. Never use this module to serve the application."""
import base64
import os
import tempfile
from unittest.mock import patch

_test_runtime = tempfile.TemporaryDirectory(prefix="cubari-download-tests-")
TEST_RUNTIME = _test_runtime.name
test_values = {
    "CHAPTER_MASTER_PASSWORD": base64.b64encode(os.urandom(32)).decode(),
    "CHAPTER_KDF_SALT": base64.b64encode(os.urandom(16)).decode(),
    "SECRET_KEY": base64.b64encode(os.urandom(32)).decode(),
    "CHAPTER_S3_ENDPOINT_URL": "https://storage.invalid",
    "CHAPTER_S3_BUCKET": "test-only",
    "CHAPTER_S3_ACCESS_KEY_ID": "test-only",
    "CHAPTER_S3_SECRET_ACCESS_KEY": "test-only",
    "CHAPTER_S3_REGION": "test-only",
    "CHAPTER_S3_ADDRESSING_STYLE": "path",
    "CHAPTER_MAX_ARCHIVE_BYTES": "1048576",
    "CHAPTER_MAX_IMAGE_BYTES": "65536",
    "CHAPTER_CACHE_MAX_BYTES": "2097152",
    "CHAPTER_RUNTIME_DIR": TEST_RUNTIME,
    "CHAPTER_KDF_N": "16384",
}
with patch.dict(os.environ, test_values, clear=True):
    from cubarimoe.settings.base import *  # noqa: F403

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CHAPTER_CONFIG_FILE = None
# Legacy apps have empty/ignored migration packages in this checkout.
MIGRATION_MODULES = {"reader": None, "misc": None, "homepage": None}
