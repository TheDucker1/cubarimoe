import base64
import io
import json
import os
import tempfile
import uuid
import zipfile
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


class ArchiveEncryptionTests(TestCase):
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
