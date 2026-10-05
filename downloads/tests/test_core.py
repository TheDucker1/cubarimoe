import base64
import io
import json
import os
import tempfile
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from downloads.archive import ArchiveBuilder, extract_page
from downloads.config import load_configuration
from downloads.crypto import Cipher, get_cipher
from downloads.errors import DownloadError
from downloads.identity import identity
from downloads.resolver import group_identity

PNG = b"\x89PNG\r\n\x1a\n" + b"test-image-bytes"


class ConfigurationTests(TestCase):
    def test_missing_configuration_refuses_startup(self):
        with self.assertRaises(ImproperlyConfigured) as caught:
            load_configuration(Path("/tmp"), {})
        self.assertIn("CHAPTER_MASTER_PASSWORD", str(caught.exception))

    def test_invalid_private_file_error_does_not_contain_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            path.write_text(json.dumps({"CHAPTER_MASTER_PASSWORD": "never-disclose-this"}))
            path.chmod(0o644)
            with self.assertRaises(ImproperlyConfigured) as caught:
                load_configuration(Path(directory), {"CHAPTER_CONFIG_FILE": str(path)})
            self.assertNotIn("never-disclose-this", str(caught.exception))

    def test_configuration_repr_is_redacted(self):
        self.assertNotIn(settings.CHAPTER_CONFIG.password, repr(settings.CHAPTER_CONFIG))
        self.assertNotIn(settings.CHAPTER_CONFIG.secret_key, repr(settings.CHAPTER_CONFIG))

    def test_reported_request_settings_are_valid(self):
        from .settings import test_values
        values = dict(test_values, CHAPTER_REQUEST_DELAY_SECONDS=60,
                      CHAPTER_REQUEST_TIMEOUT_SECONDS=300, CHAPTER_MAX_RETRIES=3,
                      CHAPTER_MAX_BULK_ITEMS=100, CHAPTER_REQUEST_RATE_LIMITS=[[1, 2], [300, 3600]])
        config = load_configuration(Path("/tmp"), values)
        self.assertEqual((config.delay, config.timeout, config.max_retries, config.max_bulk_items), (60, 300, 3, 100))

    def test_validation_names_the_setting_without_disclosing_values(self):
        from .settings import test_values
        invalid_values = {
            "CHAPTER_MASTER_PASSWORD": "secret-marker",
            "SECRET_KEY": "secret-marker",
            "CHAPTER_KDF_SALT": "secret-marker",
            "CHAPTER_S3_ENDPOINT_URL": "http://secret-marker.invalid",
            "CHAPTER_S3_ADDRESSING_STYLE": "secret-marker",
            "CHAPTER_RUNTIME_DIR": "secret-marker",
            "CHAPTER_CACHE_DIR": "secret-marker",
            "CHAPTER_REQUEST_DELAY_SECONDS": "secret-marker",
            "CHAPTER_REQUEST_TIMEOUT_SECONDS": "secret-marker",
            "CHAPTER_REQUEST_RATE_LIMITS": "secret-marker",
            "CHAPTER_KDF_N": "secret-marker",
            "CHAPTER_KDF_R": 7, "CHAPTER_KDF_P": 2,
            "CHAPTER_S3_PREFIX": "secret-marker/../",
            "CHAPTER_MAX_ARCHIVE_BYTES": 536870913,
            "CHAPTER_MAX_IMAGE_BYTES": 1048577,
            "CHAPTER_CACHE_MAX_BYTES": "secret-marker",
            "CHAPTER_MAX_RETRIES": 11, "CHAPTER_MAX_BULK_ITEMS": 1001,
            "CHAPTER_CACHE_MAX_FILES": 10001,
        }
        for name, value in invalid_values.items():
            with self.subTest(setting=name), self.assertRaises(ImproperlyConfigured) as caught:
                load_configuration(Path("/tmp"), dict(test_values, **{name: value}))
            self.assertIn(name, str(caught.exception))
            self.assertNotIn("secret-marker", str(caught.exception))

    def test_private_config_loads_without_copying_secrets_into_environment(self):
        from .settings import test_values
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.json"
            values = dict(test_values)
            values["CHAPTER_CACHE_MAX_FILES"] = 5
            path.write_text(json.dumps(values))
            path.chmod(0o600)
            environment = {"CHAPTER_CONFIG_FILE": str(path), "CHAPTER_CACHE_MAX_FILES": "7"}
            result = load_configuration(Path(directory), environment)
            self.assertEqual(result.cache_max_files, 7)
            self.assertEqual(result.password, values["CHAPTER_MASTER_PASSWORD"])
            self.assertNotIn("CHAPTER_MASTER_PASSWORD", environment)
            values["CHAPTER_S3_BUCKET"] = None
            path.write_text(json.dumps(values))
            with self.assertRaises(ImproperlyConfigured):
                load_configuration(Path(directory), environment)


class ArchiveEncryptionTests(TestCase):
    def test_legacy_envelope_remains_readable(self):
        import struct
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = identity("gist", "series", "1", "group")
        version = uuid.uuid4()
        cipher = get_cipher()
        header = json.dumps(cipher._legacy_header(key, version), sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        legacy = b"CUBARIC1" + struct.pack(">I", len(header)) + header + nonce + AESGCM(
            cipher._key(key)
        ).encrypt(nonce, b"legacy-archive", header)
        self.assertEqual(cipher.decrypt(legacy, key, version), b"legacy-archive")

    def test_order_and_authenticated_round_trip(self):
        key = identity("gist", "series", "1.0", "group")
        version = uuid.uuid4()
        builder = ArchiveBuilder(settings.CHAPTER_CONFIG, key, version)
        builder.append(PNG, "Page description")
        builder.append(b"\xff\xd8\xff" + b"test-jpeg")
        plaintext, manifest = builder.finish()
        builder.close()
        with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
            self.assertEqual(archive.namelist(), ["0001.png", "0002.jpg", "manifest.json"])
        cipher = get_cipher()
        encrypted = cipher.encrypt(plaintext, key, version)
        self.assertNotIn(PNG, encrypted)
        self.assertNotIn(key.encode(), encrypted)
        self.assertNotIn(str(version).encode(), encrypted)
        self.assertEqual(cipher.decrypt(encrypted, key, version), plaintext)
        self.assertEqual(extract_page(plaintext, manifest[0], 65536), PNG)
        self.assertNotEqual(cipher.encrypt(plaintext, key, version), encrypted)
        for damaged in (encrypted[:-1] + bytes([encrypted[-1] ^ 1]), encrypted[:15], b"not-an-envelope"):
            with self.assertRaises(DownloadError):
                cipher.decrypt(damaged, key, version)
        with self.assertRaises(DownloadError):
            cipher.decrypt(encrypted, identity("gist", "series", "2", "group"), version)
        with self.assertRaises(DownloadError):
            cipher.decrypt(encrypted, key, uuid.uuid4())
        wrong_password = Cipher(replace(settings.CHAPTER_CONFIG, password="incorrect-master-password"))
        with self.assertRaises(DownloadError):
            wrong_password.decrypt(encrypted, key, version)

    def test_rejects_html_and_oversized_image(self):
        builder = ArchiveBuilder(settings.CHAPTER_CONFIG, "a" * 64, uuid.uuid4())
        with self.assertRaises(DownloadError):
            builder.append(b"<html>blocked</html>")
        with self.assertRaises(DownloadError):
            builder.append(PNG + b"x" * 65536)
        builder.close()

    def test_identity_survives_group_reenumeration(self):
        first = identity("gist", "series", "1.0", group_identity("0", "Team"))
        second = identity("gist", "series", "1", group_identity("99", "Team"))
        self.assertEqual(first, second)
        self.assertNotEqual(first, identity("gist", "series", "1", "Other Team"))
