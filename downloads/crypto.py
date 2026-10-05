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

MAGIC = b"CUBARIC1"


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

    def _header(self, chapter_key, version):
        return {
            "format": 1, "algorithm": "AES-256-GCM", "chapter": chapter_key,
            "version": str(version), "kdf": "scrypt+HKDF-SHA256",
            "salt": base64.b64encode(self.config.salt).decode("ascii"),
            "n": self.config.scrypt_n, "r": self.config.scrypt_r, "p": self.config.scrypt_p,
        }

    @sensitive_variables()
    def encrypt(self, plaintext, chapter_key, version):
        if len(plaintext) > self.config.max_archive_bytes:
            raise DownloadError("Chapter exceeds the configured archive limit.")
        header = json.dumps(self._header(chapter_key, version), sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        return MAGIC + struct.pack(">I", len(header)) + header + nonce + AESGCM(
            self._key(chapter_key)
        ).encrypt(nonce, plaintext, header)

    @sensitive_variables()
    def decrypt(self, envelope, chapter_key, version):
        try:
            if len(envelope) > self.config.max_archive_bytes + 4096 or envelope[:8] != MAGIC:
                raise ValueError()
            length = struct.unpack(">I", envelope[8:12])[0]
            if not 0 < length <= 2048 or len(envelope) < 12 + length + 28:
                raise ValueError()
            header = envelope[12:12 + length]
            if json.loads(header) != self._header(chapter_key, version):
                raise ValueError()
            nonce = envelope[12 + length:24 + length]
            plaintext = AESGCM(self._key(chapter_key)).decrypt(nonce, envelope[24 + length:], header)
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
