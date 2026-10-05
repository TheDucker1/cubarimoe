# Configure and run encrypted chapter downloads

The design is in [encrypted-chapter-downloads.md](encrypted-chapter-downloads.md). Progress and verification are recorded in [implementation-progress.md](implementation-progress.md).

## Required private configuration

Normal Django startup, including management commands, deliberately fails until required configuration is provided. There is no fallback password, S3 credential, or feature-disable escape hatch. Error messages name missing settings and never echo their values. DEBUG must remain false while these credentials are loaded.

The supported deployment is one web process and one Huey consumer with exactly one thread worker. Use the existing Python 3.8 environment and install the repository requirements. The additional packages are pinned to huey 2.5.2, pyrate-limiter 2.10.0, cryptography 44.0.3, and boto3 1.37.38.

From `/root/manga-reader/cubarimoe`, create the private configuration once. This generates the signing secret, master password, and salt directly into a 0600 file without printing them. It refuses to overwrite an existing file. The initial resource values below cap an archive at 64 MiB, an image at 8 MiB, and the disk cache at 256 MiB (also at most 20 files); adjust for chapter sizes and available memory/disk.

```bash
.venv/bin/python - <<'PYCONFIG'
import base64, json, os, secrets
from pathlib import Path
values = json.loads(Path("chapter-config.example.json").read_text())
values.update(
    SECRET_KEY=secrets.token_urlsafe(48),
    CHAPTER_MASTER_PASSWORD=secrets.token_urlsafe(32),
    CHAPTER_KDF_SALT=base64.b64encode(secrets.token_bytes(32)).decode(),
    CHAPTER_MAX_ARCHIVE_BYTES=67108864,
    CHAPTER_MAX_IMAGE_BYTES=8388608,
    CHAPTER_CACHE_MAX_BYTES=268435456,
)
fd = os.open("chapter-config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as file:
    json.dump(values, file, indent=2)
    file.write("\n")
PYCONFIG
nano chapter-config.json
```

Fill the private file using an editor, not a command line containing credentials. Keep it out of version control; `chapter-config*.json` is ignored except for the empty example. The loader rejects symlinks, non-regular files, files owned by another user, and group/world permissions. You may keep it outside the checkout. Set only its path in the environment:

```bash
export CHAPTER_CONFIG_FILE=/absolute/path/to/chapter-config.json
```

Individual environment settings can override file settings. Values are not copied into the process environment when loaded from a file. Do not enable SDK debug logging or log the private file/environment.

Required fields:

| Field | Format |
| --- | --- |
| `SECRET_KEY` | Random Django session/signing secret of at least 32 characters |
| `CHAPTER_MASTER_PASSWORD` | Strong private password of at least 16 characters |
| `CHAPTER_KDF_SALT` | Base64-encoded random bytes, at least 16 decoded bytes; preserve permanently |
| `CHAPTER_S3_ENDPOINT_URL` | HTTPS endpoint without embedded credentials, query, or fragment |
| `CHAPTER_S3_BUCKET` | Private, unversioned bucket with no retention lock |
| `CHAPTER_S3_ACCESS_KEY_ID`, `CHAPTER_S3_SECRET_ACCESS_KEY` | Credentials limited to this bucket/prefix |
| `CHAPTER_S3_REGION` | Region string accepted by the provider for SigV4 signing |
| `CHAPTER_S3_ADDRESSING_STYLE` | `path` or `virtual`, according to the provider |
| `CHAPTER_MAX_ARCHIVE_BYTES` | Positive maximum CBZ size, at most 536870912 bytes |
| `CHAPTER_MAX_IMAGE_BYTES` | Positive maximum image size, no greater than the CBZ limit |
| `CHAPTER_CACHE_MAX_BYTES` | Positive disk cache budget |

Generate secrets with a password manager or a local random generator and save them directly to the private file. Never paste real values into this documentation, commit messages, issue comments, or chat output. Changing the password, salt, or KDF parameters without re-encrypting stored objects makes them unreadable. Back up these configuration values and SQLite together.

Archive limits require RAM headroom: CBZ/ciphertext can coexist during encryption, and one web decryption may run while a download is being built. The disk cache stores only encrypted bytes. Its default 20-file limit also respects the configured byte budget. An object larger than the cache budget is served without being retained.

Remote names use `objects/<64 random hex characters>`, without extensions or chapter identifiers. New encrypted headers also exclude chapter identity/version. SQLite holds the mapping; back up it together with the private configuration. Choose a generic bucket name and keep the prefix `objects` (or another generic prefix). Object sizes/timestamps remain visible to the storage provider. Previously created objects retain their old names/format until explicitly replaced.

Optional settings use conservative defaults. `CHAPTER_RUNTIME_DIR` defaults to `.chapter-data` under the checkout; `CHAPTER_CACHE_DIR` defaults to its `cache` subdirectory. Overrides must be absolute, distinct paths in owner-only directories (0700). Keep this directory dedicated to chapter runtime data. The worker lock, Huey queue, and persistent limiter database reside under the runtime directory. Polling is every two seconds and archive decryption is limited to one request at a time per web process.

Request pacing defaults to a two-second gap after completion, one request per two seconds, and 300 requests per hour. Configure `CHAPTER_REQUEST_RATE_LIMITS` as increasing `[request_count, seconds]` pairs. Configure `CHAPTER_REQUEST_DELAY_SECONDS` for longer completion gaps. A 403 pauses upstream downloads immediately; an explicit Retry action clears blocking flags but preserves time deadlines and rate history. Retries and redirects pass through the same gate.

## Initialize and start

With `CHAPTER_CONFIG_FILE` exported in every service shell:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py migrate --run-syncdb --noinput
.venv/bin/python manage.py check_chapter_storage
.venv/bin/python manage.py createsuperuser
```

The read-only storage check verifies credentials can inspect bucket versioning. Give storage credentials GetBucketVersioning plus GetObject, PutObject, and DeleteObject permissions for the configured prefix. Suspended/versioned buckets are rejected because ordinary deletion would retain old versions. Single-object uploads and ordinary permanent deletion are the supported initial mode.

Start the worker in its own shell/process:

```bash
.venv/bin/python manage.py run_huey
```

The command refuses more than one worker, alternate worker types, or a second consumer. It uses Huey's consumer; no custom queue engine is implemented. A graceful stop lets the current task finish where possible. After a hard stop, unfinished jobs restart the whole operation; accepted/cancelled/terminally failed jobs stay terminal. If stopped after upload but before database acceptance, the task downloads again and overwrites its recorded object key. No partial task resume is implemented.

For local development, start the web process separately:

```bash
ALLOWED_HOSTS=10.121.15.2 .venv/bin/python manage.py runserver 0.0.0.0:8010 --noreload --insecure
```

Replace that host with your actual server address. `--insecure` permits development static-file serving with DEBUG disabled; it does not turn on debug tracebacks. Use a normal static-file server and one web process for deployment. Never use the test settings module to serve real traffic.

Sign in with the staff/superuser account via `/admin/login/`. Open a series to populate server-resolved selection keys. Its table offers row Store/Remove buttons, checkboxes, a group selector, select-visible/all controls, and bulk actions. Choose the group explicitly; unavailable groups are skipped. The reader offers Store current chapter and Remove stored chapter. `/downloads/` shows the persistent queue, progress, cooldowns, cancellation, retry, and optional clear-all local archive cache maintenance.

Stored images are served by Django at `/stored-chapters/<chapter-key>/<archive-version>/pages/<page>`. Accepted chapter metadata replaces upstream URLs with these local URLs. Storage/decryption failures return an error rather than silently contacting upstream. The reader displays a stored-image failure message with a staff-only “Read from source instead” link; choosing it explicitly uses upstream pages without deleting the stored archive. Browser source reads retain their existing preload behavior and are outside the worker rate gate. Reading access follows the existing reader boundary; management endpoints require a staff session and CSRF checks. The private bucket never needs public image URLs.

## Verification without real credentials or upstream traffic

```bash
.venv/bin/python manage.py test downloads.tests --settings=downloads.tests.settings
node --check downloads/static/downloads/downloads.js
```

Test settings generate ephemeral secrets and isolated SQLite/runtime directories. Tests use fake image transport and object storage, so they do not touch the real bucket or upstream image hosts. They verify request timestamps, retry/redirect gating, persistent cooldowns, encryption authentication, acceptance/cancellation races, deletion, LRU behavior, and authenticated APIs.

Configuration can be verified locally, but actual provider behavior cannot be confirmed until the user supplies storage configuration. No live remote archive download/upload/delete is required to run this test suite.
