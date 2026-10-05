"""Worker HTTP policy. All source adapters share this context, never a new thread."""
import contextvars
import ipaddress
import math
import random
import socket
import time
from contextlib import contextmanager
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
from django.conf import settings
from django.utils import timezone
from pyrate_limiter import BucketFullException, Limiter, RequestRate, SQLiteBucket

from .errors import Blocked, Cancelled, DownloadError
from .models import DownloadJob, UpstreamCooldown

_gate = contextvars.ContextVar("chapter_download_gate", default=None)


def current_gate():
    return _gate.get()


@contextmanager
def download_context(gate):
    token = _gate.set(gate)
    try:
        yield
    finally:
        _gate.reset(token)


def validate_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise DownloadError("Source supplied an invalid image URL.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise DownloadError("Source URL resolves to a non-public network address.")
    except (OSError, ValueError):
        raise DownloadError("Source hostname could not be resolved.") from None


class RequestGate:
    def __init__(self, job):
        self.job = job
        self.config = settings.CHAPTER_CONFIG
        self.session = requests.Session()
        self.session.trust_env = False  # Do not inherit unrelated proxy/credential settings.
        rates = sorted(self.config.rate_limits, key=lambda rate: rate[1])
        self.limiter = Limiter(
            *(RequestRate(count, seconds) for count, seconds in rates),
            bucket_class=SQLiteBucket,
            bucket_kwargs={"path": str(self.config.runtime_dir / "limits.sqlite3")},
            time_function=time.time,  # Persistent timestamps must survive process/host restart.
        )

    def check_cancelled(self):
        if DownloadJob.objects.filter(pk=self.job.pk, cancel_requested=True).exists():
            raise Cancelled("Download cancelled.")

    def wait_until(self, deadline):
        while deadline and deadline > timezone.now():
            self.check_cancelled()
            DownloadJob.objects.filter(pk=self.job.pk).update(state="cooldown", next_eligible_at=deadline)
            time.sleep(max(0, min(0.25, (deadline - timezone.now()).total_seconds())))
        self.check_cancelled()

    def _admit(self, host):
        rows = list(UpstreamCooldown.objects.filter(host__in=("*", host)))
        if any(row.blocked for row in rows):
            raise Blocked("Upstream is paused after a blocking response. Explicit retry is required.")
        deadlines = [row.next_eligible_at for row in rows if row.next_eligible_at]
        self.wait_until(max(deadlines) if deadlines else None)
        while True:
            self.check_cancelled()
            try:
                self.limiter.try_acquire("all-downloads", "host:" + host)
                return
            except BucketFullException as error:
                self.wait_until(timezone.now() + timedelta(seconds=max(0.05, error.meta_info["remaining_time"])))

    def _cooldown(self, host, seconds, blocked=False):
        deadline = timezone.now() + timedelta(seconds=seconds)
        # Host cooldown plus global completion gap persist across task/process restart.
        UpstreamCooldown.objects.update_or_create(host=host, defaults={"next_eligible_at": deadline, "blocked": blocked})
        if blocked:
            UpstreamCooldown.objects.update_or_create(host="*", defaults={"next_eligible_at": deadline, "blocked": True})
        return deadline

    def _retry_delay(self, response, attempt):
        value = response.headers.get("Retry-After") if response is not None else None
        if value:
            try:
                seconds = float(value)
                if math.isfinite(seconds) and 0 <= seconds <= 365 * 86400:
                    return max(self.config.delay, seconds)
            except ValueError:
                try:
                    seconds = (parsedate_to_datetime(value) - timezone.now()).total_seconds()
                    if 0 <= seconds <= 365 * 86400:
                        return max(self.config.delay, seconds)
                except (TypeError, ValueError, OverflowError):
                    pass
        delay = max(self.config.delay, min(300, self.config.delay * 2 ** (attempt + 1)))
        return delay + random.uniform(0, delay * 0.25)

    def _completion_delay(self):
        return self.config.delay + random.uniform(0, self.config.jitter)

    def block(self, url):
        self._cooldown(urlsplit(url).hostname, self.config.delay, blocked=True)

    def request(self, method, url, limit=None, **kwargs):
        limit = limit or min(self.config.max_archive_bytes, 8 * 1024 * 1024)
        kwargs.pop("timeout", None)
        follow_redirects = kwargs.pop("allow_redirects", True)
        kwargs.pop("stream", None)
        initial_headers = dict(kwargs.pop("headers", {}))
        attempts = 0
        redirects = 0
        while True:
            validate_url(url)
            host = urlsplit(url).hostname
            self._admit(host)
            response = None
            try:
                DownloadJob.objects.filter(pk=self.job.pk).update(
                    state="resolving" if self.job.total_pages == 0 else "downloading", next_eligible_at=None
                )
                response = self.session.request(
                    method, url, headers=initial_headers, timeout=self.config.timeout,
                    allow_redirects=False, stream=True, **kwargs
                )
                if response.status_code == 403:
                    self._cooldown(host, self.config.delay, blocked=True)
                    raise Blocked("Upstream refused access (403). Explicit retry is required.")
                if response.status_code == 429 or response.status_code >= 500:
                    delay = self._retry_delay(response, attempts)
                    self._cooldown(host, delay)
                    attempts += 1
                    if attempts >= self.config.max_retries:
                        if response.status_code == 429:
                            self._cooldown(host, delay, blocked=True)
                        raise DownloadError("Upstream request retry limit reached; cooldown is preserved.")
                    continue
                if response.is_redirect:
                    if not follow_redirects:
                        # Legacy git.io resolution inspects Location and performs its next
                        # request explicitly through this same gate.
                        response._content = b""
                        response._content_consumed = True
                        return response
                    redirects += 1
                    if redirects > 5:
                        raise DownloadError("Upstream returned too many redirects.")
                    next_url = urljoin(url, response.headers.get("Location", ""))
                    if urlsplit(next_url).netloc != urlsplit(url).netloc:
                        initial_headers = {k: v for k, v in initial_headers.items()
                                           if k.lower() not in ("authorization", "cookie")}
                    if response.status_code == 303 or (method == "POST" and response.status_code in (301, 302)):
                        method = "GET"
                        kwargs.pop("data", None)
                        kwargs.pop("json", None)
                    url = next_url
                    kwargs.pop("params", None)
                    continue
                if response.status_code != 200:
                    raise DownloadError("Upstream returned an unsuccessful response.")
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > limit:
                    raise DownloadError("Upstream response exceeds the configured byte limit.")
                buffer = bytearray()
                for chunk in response.iter_content(65536):
                    self.check_cancelled()
                    if len(buffer) + len(chunk) > limit:
                        raise DownloadError("Upstream response exceeds the configured byte limit.")
                    buffer.extend(chunk)
                response._content = bytes(buffer)
                response._content_consumed = True
                return response
            except requests.RequestException:
                attempts += 1
                self._cooldown(host, self._retry_delay(None, attempts - 1))
                if attempts >= self.config.max_retries:
                    raise DownloadError("Upstream connection failed; retry limit reached.") from None
            except (ValueError, OverflowError):
                raise DownloadError("Upstream response metadata is invalid.") from None
            finally:
                if response is not None:
                    response.close()
                UpstreamCooldown.objects.update_or_create(
                    host="*", defaults={
                        "next_eligible_at": timezone.now() + timedelta(seconds=self._completion_delay()),
                        # Preserve blocking flags; completion must not unpause a 403/429.
                        "blocked": UpstreamCooldown.objects.filter(host="*", blocked=True).exists(),
                    }
                )

    def close(self):
        self.session.close()
        for bucket in self.limiter.bucket_group.values():
            bucket.close()
