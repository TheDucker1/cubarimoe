import base64
import json
import os
import struct
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .errors import DownloadError

MAGIC = b"ENCOBJ02"
LEGACY_MAGIC = b"CUBARIC1"


class Cipher:
    @sensitive_variables()
    def __init__(self, config):
        self.config = config
        self._master = Scrypt(
            salt=config.salt, length=32, n=config.scrypt_n, r=config.scrypt_r, p=config.scrypt_p
        ).derive(config.password.encode("utf-8"))

    def __repr__(self):
        return "<ChapterCipher: redacted>"

    @sensitive_variables()
    def _key(self, chapter_key):
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=b"cubari-chapter-v1:" + chapter_key.encode("ascii")).derive(self._master)

    def _header(self):
        return {
            "format": 2, "algorithm": "AES-256-GCM", "kdf": "scrypt+HKDF-SHA256",
            "salt": base64.b64encode(self.config.salt).decode("ascii"),
            "n": self.config.scrypt_n, "r": self.config.scrypt_r, "p": self.config.scrypt_p,
        }

    def _legacy_header(self, chapter_key, version):
        header = self._header()
        header.update(format=1, chapter=chapter_key, version=str(version))
        return header

    def _aad(self, header, chapter_key, version):
        # Identity comes from SQLite, authenticated without exposing it in the envelope.
        context = json.dumps([chapter_key, str(version)], separators=(",", ":")).encode()
        return header + b"\x00" + context

    @sensitive_variables()
    def encrypt(self, plaintext, chapter_key, version):
        if len(plaintext) > self.config.max_archive_bytes:
            raise DownloadError("Chapter exceeds the configured archive limit.")
        header = json.dumps(self._header(), sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        return MAGIC + struct.pack(">I", len(header)) + header + nonce + AESGCM(
            self._key(chapter_key)
        ).encrypt(nonce, plaintext, self._aad(header, chapter_key, version))

    @sensitive_variables()
    def decrypt(self, envelope, chapter_key, version):
        try:
            legacy = envelope[:8] == LEGACY_MAGIC
            if len(envelope) > self.config.max_archive_bytes + 4096 or envelope[:8] not in (MAGIC, LEGACY_MAGIC):
                raise ValueError()
            length = struct.unpack(">I", envelope[8:12])[0]
            if not 0 < length <= 2048 or len(envelope) < 12 + length + 28:
                raise ValueError()
            header = envelope[12:12 + length]
            expected = self._legacy_header(chapter_key, version) if legacy else self._header()
            if json.loads(header) != expected:
                raise ValueError()
            nonce = envelope[12 + length:24 + length]
            aad = header if legacy else self._aad(header, chapter_key, version)
            plaintext = AESGCM(self._key(chapter_key)).decrypt(nonce, envelope[24 + length:], aad)
            if len(plaintext) > self.config.max_archive_bytes:
                raise ValueError()
            return plaintext
        except (ValueError, InvalidTag, UnicodeError, struct.error):
            raise DownloadError("Stored chapter could not be authenticated. Check configuration or storage integrity.") from None


@lru_cache(maxsize=4)
def _cipher(config):
    return Cipher(config)


def get_cipher():
    return _cipher(settings.CHAPTER_CONFIG)
