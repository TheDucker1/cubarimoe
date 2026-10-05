import base64
import hashlib
import threading

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from .errors import DownloadError


class Storage:
    def __init__(self, config):
        self.config = config
        self._verified = False
        self._lock = threading.Lock()
        self.client = boto3.client(
            "s3", endpoint_url=config.endpoint, region_name=config.region,
            aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": config.addressing_style},
                          retries={"max_attempts": 0}, connect_timeout=config.timeout,
                          read_timeout=config.timeout),
        )

    def verify(self):
        with self._lock:
            if self._verified:
                return
            try:
                response = self.client.get_bucket_versioning(Bucket=self.config.bucket)
                # Suspended buckets can still retain versions; require a clean unversioned bucket.
                if response.get("Status"):
                    raise DownloadError("Storage bucket must have versioning disabled for chapter downloads.")
                self._verified = True
            except (BotoCoreError, ClientError):
                raise DownloadError("Storage configuration could not be verified. Check endpoint, bucket, and permissions.") from None

    @sensitive_variables()
    def upload(self, key, data):
        self.verify()
        try:
            self.client.put_object(
                Bucket=self.config.bucket, Key=key, Body=data, ContentType="application/octet-stream",
                ContentMD5=base64.b64encode(hashlib.md5(data).digest()).decode("ascii"),
            )
        except (BotoCoreError, ClientError):
            raise DownloadError("Encrypted chapter upload failed.") from None

    def fetch(self, key, expected_size):
        self.verify()
        response = None
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            if response["ContentLength"] != expected_size or expected_size > self.config.max_archive_bytes + 4096:
                raise DownloadError("Stored chapter size does not match its record.")
            # Bound the read even if a provider sends an incorrect ContentLength.
            data = response["Body"].read(expected_size + 1)
            if len(data) != expected_size:
                raise DownloadError("Stored chapter transfer was incomplete.")
            return data
        except (BotoCoreError, ClientError, OSError):
            raise DownloadError("Stored chapter could not be retrieved.") from None
        finally:
            if response:
                response["Body"].close()

    def delete(self, key):
        self.verify()
        try:
            self.client.delete_object(Bucket=self.config.bucket, Key=key)
        except (BotoCoreError, ClientError):
            raise DownloadError("Stored chapter could not be deleted.") from None


_storage = None
_config = None
_storage_lock = threading.Lock()


def get_storage():
    global _storage, _config
    with _storage_lock:
        if _config != settings.CHAPTER_CONFIG:
            _config = settings.CHAPTER_CONFIG
            _storage = Storage(_config)
        return _storage
