"""Validated private configuration; values never appear in error messages."""
import base64
import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.debug import sensitive_variables


@dataclass(frozen=True, repr=False)
class Configuration:
    password: str = field(repr=False)
    salt: bytes = field(repr=False)
    django_secret: str = field(repr=False)
    endpoint: str
    bucket: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    region: str
    prefix: str
    addressing_style: str
    cache_dir: Path
    runtime_dir: Path
    max_archive_bytes: int
    max_image_bytes: int
    cache_max_bytes: int
    cache_max_files: int = 20
    delay: float = 2.0
    timeout: float = 30.0
    max_retries: int = 3
    poll_seconds: int = 2
    max_bulk_items: int = 100
    scrypt_n: int = 32768
    scrypt_r: int = 8
    scrypt_p: int = 1
    rate_limits: tuple = ((1, 2), (300, 3600))

    def __repr__(self):
        return "<ChapterConfiguration: redacted>"


@sensitive_variables()
def load_configuration(base_dir, environ=None):
    environ = os.environ if environ is None else environ
    values = {}
    filename = environ.get("CHAPTER_CONFIG_FILE")
    if filename:
        try:
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as stream:
                metadata = os.fstat(stream.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                        or metadata.st_uid != os.getuid()):
                    raise ValueError()
                values = json.load(stream)
                if not isinstance(values, dict):
                    raise ValueError()
        except (OSError, ValueError):
            raise ImproperlyConfigured(
                "CHAPTER_CONFIG_FILE must be valid JSON in an owner-only regular file (mode 0600)."
            ) from None
    # Explicit environment overrides. Do not copy values into os.environ.
    values.update({k: v for k, v in environ.items() if k.startswith("CHAPTER_") or k == "SECRET_KEY"})
    required = (
        "CHAPTER_MASTER_PASSWORD", "CHAPTER_KDF_SALT", "SECRET_KEY",
        "CHAPTER_S3_ENDPOINT_URL", "CHAPTER_S3_BUCKET", "CHAPTER_S3_ACCESS_KEY_ID",
        "CHAPTER_S3_SECRET_ACCESS_KEY", "CHAPTER_S3_REGION", "CHAPTER_S3_ADDRESSING_STYLE",
        "CHAPTER_MAX_ARCHIVE_BYTES", "CHAPTER_MAX_IMAGE_BYTES", "CHAPTER_CACHE_MAX_BYTES",
    )
    missing = [name for name in required if values.get(name) is None or not str(values.get(name, "")).strip()]
    if missing:
        raise ImproperlyConfigured("Required configuration missing: " + ", ".join(missing))
    try:
        password = str(values["CHAPTER_MASTER_PASSWORD"])
        django_secret = str(values["SECRET_KEY"])
        salt = base64.b64decode(values["CHAPTER_KDF_SALT"], validate=True)
        endpoint = str(values["CHAPTER_S3_ENDPOINT_URL"])
        parsed = urlsplit(endpoint)
        if (len(password) < 16 or len(django_secret) < 32 or len(salt) < 16
                or parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError()
        style = values["CHAPTER_S3_ADDRESSING_STYLE"]
        if style not in ("path", "virtual"):
            raise ValueError()
        def integer(name, default=None):
            value = int(values.get(name, default))
            if value <= 0:
                raise ValueError()
            return value
        runtime = Path(values.get("CHAPTER_RUNTIME_DIR", str(Path(base_dir) / ".chapter-data")))
        cache_dir = Path(values.get("CHAPTER_CACHE_DIR", str(runtime / "cache")))
        if not runtime.is_absolute() or not cache_dir.is_absolute() or cache_dir == runtime:
            raise ValueError()
        delay = float(values.get("CHAPTER_REQUEST_DELAY_SECONDS", 2))
        timeout = float(values.get("CHAPTER_REQUEST_TIMEOUT_SECONDS", 30))
        rates = values.get("CHAPTER_REQUEST_RATE_LIMITS", [[1, 2], [300, 3600]])
        if isinstance(rates, str):
            rates = json.loads(rates)
        rates = tuple((int(count), int(seconds)) for count, seconds in rates)
        rates = tuple(sorted(rates, key=lambda rate: rate[1]))
        if (not math.isfinite(delay) or not math.isfinite(timeout) or delay < 1
                or not 1 <= timeout <= 300 or not rates or any(c <= 0 or s <= 0 for c, s in rates)
                or any(rates[i][0] >= rates[i + 1][0] or rates[i][1] >= rates[i + 1][1]
                       for i in range(len(rates) - 1))):
            raise ValueError()
        n = integer("CHAPTER_KDF_N", 32768)
        r, p = integer("CHAPTER_KDF_R", 8), integer("CHAPTER_KDF_P", 1)
        if n < 16384 or n > 131072 or n & (n - 1) or r != 8 or p != 1:
            raise ValueError()
        config = Configuration(
            password=password, salt=salt, django_secret=django_secret,
            endpoint=endpoint, bucket=str(values["CHAPTER_S3_BUCKET"]),
            access_key=str(values["CHAPTER_S3_ACCESS_KEY_ID"]),
            secret_key=str(values["CHAPTER_S3_SECRET_ACCESS_KEY"]),
            region=str(values["CHAPTER_S3_REGION"]),
            prefix=str(values.get("CHAPTER_S3_PREFIX", "chapters")).strip("/"),
            addressing_style=style, runtime_dir=runtime, cache_dir=cache_dir,
            max_archive_bytes=integer("CHAPTER_MAX_ARCHIVE_BYTES"),
            max_image_bytes=integer("CHAPTER_MAX_IMAGE_BYTES"),
            cache_max_bytes=integer("CHAPTER_CACHE_MAX_BYTES"),
            cache_max_files=integer("CHAPTER_CACHE_MAX_FILES", 20),
            delay=delay, timeout=timeout, max_retries=integer("CHAPTER_MAX_RETRIES", 3),
            max_bulk_items=integer("CHAPTER_MAX_BULK_ITEMS", 100),
            scrypt_n=n, scrypt_r=r, scrypt_p=p, rate_limits=rates,
        )
        if (config.max_image_bytes > config.max_archive_bytes or config.max_archive_bytes > 512 * 1024 * 1024
                or config.max_retries > 10 or config.max_bulk_items > 1000 or config.cache_max_files > 10000
                or not config.prefix or len(config.prefix) > 900 or ".." in config.prefix.split("/")):
            raise ValueError()
        return config
    except (ValueError, TypeError, KeyError):
        raise ImproperlyConfigured(
            "Invalid chapter configuration. Check the documented formats and resource limits; values are redacted."
        ) from None


def prepare_directories(config):
    try:
        for path in (config.runtime_dir, config.cache_dir):
            if path.is_symlink():
                raise ImproperlyConfigured("Chapter runtime directories must not be symbolic links.")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise ImproperlyConfigured("Chapter runtime directories must be owner-only (mode 0700).")
    except OSError:
        raise ImproperlyConfigured("Chapter runtime directories could not be prepared. Check paths and permissions.") from None
