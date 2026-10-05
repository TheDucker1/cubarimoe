import io
import json
import zipfile

from .errors import DownloadError


def image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"avif", b"avis"):
        return "image/avif", ".avif"
    raise DownloadError("Upstream returned an unsupported image or non-image response.")


class ArchiveBuilder:
    def __init__(self, config, chapter_key, version):
        self.config = config
        self.chapter_key, self.version = chapter_key, str(version)
        self.buffer = io.BytesIO()
        self.zip = zipfile.ZipFile(self.buffer, "w", compression=zipfile.ZIP_STORED)
        self.manifest = []
        self.size = 0

    def append(self, data, description=None):
        if not data or len(data) > self.config.max_image_bytes:
            raise DownloadError("Image exceeds the configured limit or is empty.")
        mime, extension = image_type(data)
        # Reserve directory and manifest overhead before allocating the next entry.
        if self.size + len(data) + (len(self.manifest) + 1) * 4096 > self.config.max_archive_bytes:
            raise DownloadError("Chapter exceeds the configured archive limit.")
        name = "{:04d}{}".format(len(self.manifest) + 1, extension)
        self.zip.writestr(name, data)
        self.size += len(data)
        entry = {"name": name, "type": mime, "size": len(data)}
        if description:
            entry["description"] = str(description)[:2048]
        self.manifest.append(entry)

    def finish(self):
        if not self.manifest:
            raise DownloadError("Chapter has no images.")
        self.zip.writestr("manifest.json", json.dumps({
            "format": 1, "chapter": self.chapter_key, "version": self.version, "pages": self.manifest,
        }, ensure_ascii=False))
        self.zip.close()
        if self.buffer.tell() > self.config.max_archive_bytes:
            raise DownloadError("Chapter exceeds the configured archive limit.")
        return self.buffer.getvalue(), self.manifest

    def close(self):
        self.zip.close()
        self.buffer.close()


def extract_page(plaintext, entry, max_image_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
            info = archive.getinfo(entry["name"])
            if (info.file_size != entry["size"] or info.file_size > max_image_bytes
                    or info.compress_type != zipfile.ZIP_STORED):
                raise ValueError()
            data = archive.read(info)
            if image_type(data)[0] != entry["type"]:
                raise ValueError()
            return data
    except (KeyError, ValueError, zipfile.BadZipFile, RuntimeError):
        raise DownloadError("Stored chapter page is invalid.") from None
