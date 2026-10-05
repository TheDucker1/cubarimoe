# Encrypted chapter downloads and local reading cache

Status: implemented on `feat/s3-cache`. Required private configuration and resource limits must be supplied before service startup. See [setup](chapter-download-setup.md) and [implementation progress](implementation-progress.md) for verified behavior and remaining deployment checks.

## Purpose and scope

Add server-side chapter downloads to Cubari for a single user. Downloads run strictly sequentially, produce a CBZ in memory, encrypt it, and upload it to S3-compatible storage. SQLite records which chapters are available. When reading a downloaded chapter, Cubari returns local page URLs and Django serves images from the stored archive.

Use a persistent disk cache of encrypted archives, with least recently used (LRU) eviction and a default limit of 20 files. Support deleting downloaded chapters from object storage and clearing local cache independently.

Keep the system small: one Django web process, one Huey consumer with exactly one worker, SQLite, a local cache directory, and one object-storage bucket. Use established libraries for task execution and request-rate tracking. No Celery, Redis, Docker requirement, or separate reverse proxy for archive entries.

## Decisions already agreed

| Area | Decision |
| --- | --- |
| Upstream downloading | One worker; one chapter at a time; one request at a time, including retries and metadata resolution |
| Request pacing | Configurable delay between requests, cooldowns, and bounded retries |
| Archive | CBZ containing ordered original image files; ZIP_STORED, with no image conversion |
| Archive construction | In memory; no plaintext archive or partial image files on disk |
| Remote storage | Encrypted archives in private S3-compatible storage |
| Encryption | Startup master password; password KDF to master key; chapter-specific derived keys; authenticated encryption |
| Jobs | Huey with SQLite storage; restart any unfinished, non-terminal task from the beginning |
| Completion | Accepted only after successful upload and committed ready state in SQLite |
| Progress updates | Consolidated frontend polling every two seconds while active; no SSE |
| Reading cache | Encrypted archives on disk; default maximum 20 archives; LRU eviction |
| Page serving | Django returns images through URLs based on stable chapter identity and archive version |
| Deletion | Single/bulk remote chapter deletion; optional clear-all archive-cache maintenance action; per-entry eviction stays internal |

## Existing code to integrate with

Paths below are relative to the repository root.

| Location | Existing behavior | Required integration |
| --- | --- | --- |
| `proxy/source/__init__.py` | Shared series/chapter API views and route registration | Replace page URLs for ready downloads; include download status |
| `proxy/source/data.py` | SeriesAPI, ChapterAPI, SeriesPage response structures | Expose optional download metadata without breaking existing responses |
| `proxy/source/helpers.py` | Metadata cache and upstream request wrappers | Reuse source resolution; apply worker pacing to its metadata requests |
| `proxy/sources/*.py` | Source-specific series and chapter resolution | Normalize inline page lists and deferred chapter references |
| `proxy/templates/reader/series.html` | Chapter list; conditional legacy download links | Add chapter/group download actions and status; replace legacy link |
| `proxy/templates/reader/reader.html` | Existing browser download/progress/cancel controls | Add server archive controls; preserve a distinct save-to-device action |
| `proxy/static/js/init.js` | Reader URL resolution and browser-side JSZip downloader | Accept local URLs; implement server job controls without fetching upstream images in parallel |
| `cubarimoe/settings/local.py` | SQLite and memory cache for existing metadata | Add new feature configuration through settings/environment |
| `cubarimoe/urls.py` | Application routes | Mount download-management and cached-page routes |

The current reader downloader runs in the browser and can fetch multiple images concurrently. It is not the new server download worker. The legacy series link `/api/download_chapter/...` has no corresponding implementation in the current routed applications.

## Identity and archive layout

Create one canonical identity from source name, stable series ID, normalized chapter ID, and stable group ID. Use a fixed, versioned, unambiguous serialization, then SHA-256 it to produce a public `chapter_key`.

Do not use individual image URLs, display titles, or expiring query strings as identity. Preserve source IDs where available. Canonicalization must be tested so equivalent chapter notation resolves consistently without merging genuinely different source IDs.

Some adapters assign temporary numeric group IDs while building responses. Map those to a stable source group identifier or canonical group name before hashing; a rebuilt series response must not change a downloaded chapter's identity.

Use `chapter_key` for database uniqueness, key derivation context, cache lookup, and page URLs. Never use it in remote object names. It is an identifier, not a secret or an authorization token.

Each successful download receives a new immutable `archive_version`. The remote target is an independent cryptographically random 256-bit token with no extension, source, chapter hash, or archive version. The default generic prefix is `objects`. Example object-key shape:

`objects/<64 random hex characters>`

Only SQLite maps this opaque target to the chapter; preserve that database. Retries/restarts reuse the recorded random target. Configuring a descriptive prefix or bucket can reveal application context, so choose generic names.

Example reader URL:

`/stored-chapters/<chapter_key>/<archive_version>/pages/0001`

Inside the CBZ, use `0001.jpg`, `0002.png`, etc., preserving source order and validated image types. Include an internal versioned manifest with identity, archive version, ordered entry names, content types, and optional page descriptions. Use generated filenames rather than upstream paths.

Assign an archive version and target object key when creating a job, and reuse that target for attempts of the same job. Retain one accepted archive per chapter/group. Downloading an already-ready chapter is a no-op. For the initial release, replacement means delete the downloaded chapter and then download again; automatic refresh/version history is outside scope.

## Encryption format

1. Load the master password from startup configuration.
2. Use scrypt with a random installation salt and explicit parameters to derive a 256-bit master key. Derive once per process at startup, not once per page request.
3. Use HKDF-SHA256 on the master key, with a versioned application context and `chapter_key`, to derive the chapter encryption key.
4. Encrypt the completed CBZ with AES-256-GCM and a fresh random 96-bit nonce for every encryption attempt.
5. Authenticate the chapter identity, archive version, and envelope format metadata as associated data.

New objects use a generic version-2 encryption envelope containing public algorithm/KDF metadata, nonce, and authenticated ciphertext. Chapter identity and archive version are included in associated data reconstructed from SQLite, not the visible header. The internal CBZ manifest is encrypted. The original version-1 envelope remains readable for compatibility; existing object names/headers are not rewritten automatically. Object size and transfer times are not hidden. Salt and nonce are not secrets. Never reuse a nonce with a key, including retries or repeated chapter downloads; retry an upload using the same already-produced encrypted bytes rather than re-encrypting needlessly.

Preserve password, installation salt, and derivation parameters. Include enough public derivation information in the envelope for recovery; validate supported algorithms and bound KDF parameters before processing an envelope. Never store the password or derived keys in SQLite, logs, object names, or client responses.

Changing the master password is not a settings-only operation: existing objects require the old key or explicit re-encryption. Password rotation is outside the initial implementation. Authentication failure must be reported as a storage/decryption failure; do not silently download upstream.

## Persistent data

Prefer a new `downloads` Django app so legacy source adapters and reader models remain focused on their current responsibilities.

| Model | Minimum data |
| --- | --- |
| DownloadedChapter | Canonical source/series/chapter/group identity, chapter_key, display labels, availability/deletion state, current archive version, object key, encrypted size, page count, ordered page manifest, timestamps, last error |
| DownloadJob | Chapter reference, job type, queue order, state, accepted timestamp, attempts, next eligible time, progress/page count, cancellation request, target archive version/object key, timestamps, last error |
| ArchiveCacheEntry | Chapter key/version, generated local filename, byte size, last-access time |
| UpstreamCooldown | Host/global service, next eligible request time, persistent blocking flag |
| SeriesSnapshot | Source/series identity and durable source metadata for offline archive reading |

Do not overload archive availability with job status: queued or uploading does not mean readable. Avoid storing image bytes or keys in the database. A deletion job has its own completion state.

Use uniqueness constraints to prevent duplicate chapter identities and duplicate active jobs. Store progress for the UI, but progress does not mean partial image data is durable.

## Proven libraries, download worker, and restart behavior

Use Huey's Django integration and SqliteHuey instead of implementing a queue scheduler, consumer loop, delayed-task engine, or generic task-retry system. Run `manage.py run_huey` separately from the web process and outside Django's autoreloader, with exactly one thread worker. Explicitly disable immediate/eager execution even when Django DEBUG is true. Hold a process lock for the consumer lifetime to prevent accidental second consumers. Do not hold database transactions during network calls or sleeps.

Use PyrateLimiter with persistent SQLite-backed rate tracking for request admission. Keep a small application layer for the minimum delay after completion, HTTP Retry-After, source-specific cooldowns, and stop conditions. A queue library does not implement HTTP throttling, and a limiter cannot discover an unknown upstream's safe rate. Configure capacity-one short-term pacing so bulk submissions cannot cause bursts, alongside any hourly/daily budgets.

Use pinned library releases compatible with Python 3.8 and the installed Django version. Current documentation describes capabilities, not a promise that every current API exists in the compatible release. Verify the selected versions' shutdown, storage, retry, and limiter behavior before implementation is finalized.

Retain DownloadJob as the durable application record; Huey tasks contain job IDs, not image bytes or secrets. Record the job before enqueueing after transaction commit. On consumer startup, requeue unfinished jobs that are not accepted, cancelled, or terminally failed, preserving retry budgets and cooldown deadlines. This also covers a committed job that was never enqueued. Huey explicitly does not guarantee at-least-once delivery for interrupted running tasks, so the job table supplies this small restart scan. Duplicate task messages must check the durable state and become no-ops once their job is accepted or terminal; one consumer/worker and its process lock prevent overlapping execution.

Accepted means the full archive has been validated, encrypted, uploaded successfully, and both the chapter's ready state and the job's accepted timestamp have been committed in one SQLite transaction. It does not mean the queue accepted a submission or the worker started it. The UI labels this state Ready/Completed.

For deletion jobs, accepted means remote removal and local cache cleanup succeeded and deleted state was committed. Repeating an unaccepted deletion is safe because already-missing objects count as success.

Avoid nested retry systems. Let the HTTP policy decide if/when another request is allowed, and let Huey schedule chapter-level attempts from the persisted deadline. Do not enable immediate generic whole-chapter retries that repeatedly re-download successful pages after throttling. Graceful shutdown should finish the active task where possible; hard-crash recovery still restarts an unfinished in-memory chapter.

Job flow:

`queued -> resolving -> downloading -> encrypting -> uploading -> accepted`

Additional outcomes: `cooldown`, `failed`, and `cancelled`. Persist state transitions, next eligible time, retry counts, and errors.

Resolve the selected chapter/group into an ordered list of page URLs. Inputs can be inline strings, objects with `src`/description, or a deferred chapter endpoint. Reuse source adapters rather than maintaining a second scraper. Some source adapters issue concurrent metadata requests internally: audit those paths and provide a sequential worker resolution path before claiming strict sequencing.

Fetch one page completely before requesting the next. Validate HTTP status, image type, byte limits, and page count, then append to an in-memory CBZ. After all pages succeed, close the archive, encrypt, upload, and publish ready state in a short database transaction.

After a restart, every unfinished non-terminal job restarts its whole operation. A download begins again at page one, including a task interrupted during upload. A deletion retries its whole idempotent deletion operation. There is no page checkpoint, partial CBZ recovery, upload continuation, or adoption of an unaccepted uploaded object. Queue state, retry counts, and cooldown deadlines survive. A web reload alone does not restart the separate worker or its active job.

If upload succeeds but the process exits before the acceptance transaction, restart the task and overwrite the same recorded target object. Each new encryption uses a fresh nonce. The object stays unavailable to the reader until accepted. Use single-object uploads within the configured archive limit. Support unversioned buckets and versioned providers such as Backblaze B2. On a versioned target, purge old unaccepted versions before replacement, then upload; the persisted target remains the same. An attempt that is cancelled or terminally fails must remove any unaccepted target object; record cleanup as a persistent deletion job if that operation fails. Version-aware cleanup is implemented because Backblaze B2 requires it; multipart uploads remain deferred.

Use low-level sequential object operations or explicitly disable SDK transfer concurrency. No automatic background download thread per request.

## Request pacing and limits

Use one worker-wide upstream request gate backed by PyrateLimiter, plus persisted completion-delay and cooldown deadlines. Count metadata requests, images, redirects, retries, and any external image-proxy requests toward the sequential policy. Account for requests issued inside source adapters. Redirects and automatic HTTP retries must pass the same gate; default library behavior must not bypass it.

- Configurable minimum delay after each request finishes, plus nonnegative additive uniform jitter. Initial base default: two seconds, jitter defaults to 25% of the base. Persist the sampled completion deadline; never shorten it on restart.
- Honor `Retry-After` on 429; otherwise use increasing backoff with jitter and a bounded attempt count.
- Pause immediately on 403 or a source-level blocking response; require explicit user retry. Honor 429 cooldowns and pause after the bounded retry budget rather than hammering the upstream.
- Timeouts and per-image/per-chapter size limits prevent unbounded jobs.
- Check cancellation between pages and before upload/publishing. Bound in-flight request duration; cancellation may wait for that request to finish.
- Do not reset cooldowns on web/worker restart.

Sequential downloading does not guarantee an upstream will not block the IP. Existing browser reading/preloading of undownloaded chapters is outside this worker's gate. Where browser and server share an outbound IP, that traffic adds to downloads.

## Reading and disk cache

Check DownloadedChapter for the selected chapter/group before upstream chapter-page resolution. A ready archive returns its persisted ordered manifest as local URLs without refetching upstream chapter/image data. Also overlay local URLs into inline image arrays in cached series responses. Series-list discovery may still use the existing source metadata fetch/cache; downloaded chapter page serving must not depend on a live upstream. Otherwise retain current upstream behavior.

The page endpoint validates identity/version and page number against the manifest. On a cache miss, fetch the encrypted object to a temporary local file and atomically rename it after a complete transfer. Coordinate concurrent misses for the same object so the browser's parallel page requests do not fetch it repeatedly.

On a hit, update last access, read the encrypted archive, authenticate/decrypt in memory, and serve the requested ZIP entry with the correct image content type. Do not unpack the chapter to disk. Use independent archive readers for concurrent responses.

Default cache limit: 20 encrypted archives. Support an additional byte budget and evict least recently used eligible entries until both limits hold. An object larger than the disk cache budget may be served without being retained, subject to the memory/archive-size limit. Expired temporary files and stale cache rows are reconciled at worker startup and before archive loads; filesystem bytes are included in disk accounting.

Cache eviction is local only and never changes remote availability. Coordinate active reads/loads with eviction; release file references after responses complete. Lock and LRU implementation can remain simple for one web process.

The initial design decrypts an archive per page request. This trades CPU and transient RAM for minimal persistent plaintext and a simple disk cache. Limit concurrent archive decryption; otherwise one user's preload requests can multiply memory use. Measure before adding a decrypted memory LRU or remote range access.

Set browser cache headers and an ETag based on immutable archive version and page. Local URLs do not contain encryption secrets and must respect the application's access boundary. Re-check ready/deleting state before serving; browser copies already cached cannot be revoked by remote deletion.

## Deletion and cache clearing

Keep per-entry cache eviction an internal implementation detail. Expose chapter removal and, if useful, one maintenance action to clear the entire archive cache:

| Action | Semantics |
| --- | --- |
| Clear all local archive cache | Optional settings/maintenance action; remove all local encrypted archive-cache files and rows, retaining remote archives and downloaded records; do not clear unrelated Django metadata caches |
| Delete downloaded chapter(s) | Single or bulk action; remove remote archive(s), local cache, and ready availability; retain minimal identity/job history if useful |

Remote deletion is a persistent job. Mark the chapter deleting immediately so new page requests stop using it, cancel queued downloads, and coordinate with any active download before deletion proceeds. The worker must re-check cancellation/deletion before publishing an upload. If cancellation races with an upload, delete the resulting object too.

Delete remote objects, then local cache, then finalize deleted state. Treat an already absent object as successful deletion. Retain deleting/error state and retry information on failure; never report completed deletion while the remote operation failed. Already-running image responses may finish.

After deletion completes, fresh chapter responses use upstream URLs. Requests to stale local URLs return a clear missing/unavailable response, without silently fetching upstream. Use short-lived availability metadata or explicit frontend refresh so previous API caches do not keep advertising deleted archives.

Delete the accepted object and every recorded unaccepted target for the chapter. On unversioned storage use ordinary delete; on versioned/suspended storage list all data versions and delete markers for each exact key, gather pagination before modifying it, delete by explicit VersionId, and verify the exact key is absent. Treat an absent key as success. Never delete neighboring prefix-matching keys or claim success while retained versions remain. Listing/count bounds prevent runaway cleanup. Retention protections are not bypassed.

## Backend API additions

Suggested routes; exact naming can follow repository conventions:

| Route | Purpose |
| --- | --- |
| `POST /downloads/jobs/` | Queue one or multiple explicit chapter/group identities; return per-item queued/already-ready/already-queued/invalid outcomes |
| `GET /downloads/jobs/` | One queue snapshot with active jobs, totals, queue positions, progress, cooldowns, errors, and recent results |
| `GET /downloads/jobs/<id>/` | Progress, status, retry/cooldown deadline, error |
| `POST /downloads/jobs/<id>/cancel/` | Persist cancellation request |
| `POST /downloads/jobs/<id>/retry/` | Retry a failed job while preserving applicable cooldowns |
| `DELETE /downloads/chapters/<chapter_key>/` | Queue persistent remote deletion |
| `POST /downloads/chapters/bulk-delete/` | Queue deletion for a bounded explicit selection of chapter/group identities; return per-item outcomes |
| `POST /downloads/cache/clear/` | Optional maintenance action clearing all local archive cache; no per-entry public deletion |
| `GET /stored-chapters/<chapter_key>/<version>/pages/<page>/` | Serve an image from the encrypted archive |
| `GET /downloads/chapters/<chapter_key>/file/` | Deferred convenience endpoint; decrypted CBZ export is outside the initial feature |

Mutations require POST/DELETE, CSRF protection, and a single-user access rule. Validate source/chapter/group against resolved metadata; do not turn job creation into an unrestricted arbitrary-URL fetch endpoint. Credentials and internal errors must not leak in API responses.

Use frontend polling; SSE and WebSockets are outside this design. Poll one consolidated queue snapshot roughly every two seconds while work is active; prevent overlapping polls, back off on errors/hidden tabs, and stop when idle. Opening the queue view, submitting a mutation, or making the tab visible triggers an immediate refresh. Update fields from snapshot differences, without a full page reload.

Snapshots include a revision or ETag, timestamp, per-job phase, completed/total pages, transferred bytes where known, queue position, cooldown deadline, and errors. Show `Downloading 12/30 pages`, `Encrypting`, and `Uploading` distinctly. Show upload percentage only if measured. Polling has no upstream side effects.

No event log, SSE cursor, or streaming progress connection is required. SQLite snapshots provide the progress detail and survive page reloads.

Bulk submissions create one independent job per selected chapter/group and remain strictly sequential. Validate bounded explicit selections before upstream fetching. Return per-item results, tolerate already-ready/queued/deleted items, and make retries idempotent. Partial failures must not hide successes. Keep downloads in submission order initially: the active chapter's cooldown pauses later downloads too. Deletion of a queued/active chapter first persists cancellation and is executed when the current request yields; it must not wait for the full download backlog. Batch downloaded-state lookups and invalidate/overlay cached source responses when archive state changes.

## Frontend changes

- Add a server-download action to each chapter row, including an explicit group choice when needed.
- Add checkboxes, select-all-visible, select-all-series, clear selection, and selected-item counts. Clearly distinguish filtered/visible rows from every chapter in the series.
- Add Download selected/all and Remove downloaded selected/all actions. Download all uses an explicit group policy, never every translation group implicitly. Disable ineligible actions and report skipped/already-ready items.
- Default bulk download to the currently selected group. Chapters lacking that group are skipped with a visible reason; do not silently choose another translation. Bulk removal explicitly lists the downloaded chapter/group entries it will remove.
- Confirm bulk removal with the chapter/group count and remote-deletion meaning. Report partial failures per item and retain selection for items needing retry.
- Show queued, downloading (page count), cooling down (next attempt), uploading, ready, failed, deleting, and cancelled states.
- Add a persistent queue view with phase/page/byte progress, queue positions, aggregate completed/failed counts, and cancel/retry controls. Poll a consolidated snapshot. Reloading reconnects to job state; it does not restart the job.
- Show a downloaded badge and a delete action with confirmation identifying the chapter/group.
- Keep cache mechanics out of chapter actions. Offer only optional Clear all local archive cache under maintenance/settings, with usage/file counts.
- Connect reader server-download controls to the same backend jobs. Keep save-to-device wording distinct from download-to-storage.
- Preserve reader layout, descriptions, navigation, and group selection when local image URLs are substituted.
- Present storage/decryption failures clearly; offer explicit upstream fallback instead of silently creating upstream traffic.
- Treat old cached page URLs after deletion as unavailable and refresh chapter metadata.

## Additional modules and dependencies

Suggested files within the new downloads app:

| Module | Responsibility |
| --- | --- |
| `models.py` + migrations | Chapter state, persistent jobs, cache metadata, cooldowns |
| `identity.py` | Canonical identity, chapter hash, archive version |
| `resolver.py` | Source integration and page normalization |
| `archive.py` | In-memory CBZ construction, manifest validation, page extraction |
| `crypto.py` | KDFs, versioned envelope, authenticated encryption/decryption |
| `storage.py` | S3 endpoint/client, sequential upload/fetch/delete, overwrite and unaccepted-object cleanup |
| `cache.py` | Disk files, atomic writes, LRU, size accounting, concurrent-load coordination |
| `tasks.py` | Huey task definitions referencing application job IDs |
| `worker.py` | Download/delete workflow, HTTP policy, simple unfinished-job restart scan, cancellation ordering; no custom queue engine |
| `transport.py` | PyrateLimiter integration, persisted completion gaps/cooldowns, HTTP status policy |
| `views.py` / `urls.py` | Management endpoints, image serving, optional CBZ export |
| Huey Django integration | Existing `run_huey` management command; one consumer/worker |

New third-party dependencies: `huey`, `pyrate-limiter`, `cryptography`, and `boto3`. Use existing requests, Django, SQLite, and standard-library zipfile/hashlib/io/file-locking facilities. Pin versions whose Python 3.8/Django support and Linux wheels are verified; do not install current releases blindly. PyrateLimiter 2.10 documents SQLite-backed persistent tracking; verify clock/restart behavior and selected release compatibility. No Pillow or JSZip dependency is needed for server archive construction.

## Additional configuration

Configuration is validated at startup, loaded from an owner-only JSON file selected by `CHAPTER_CONFIG_FILE`, with explicit environment overrides. No real credentials belong in documentation or version control. The earlier feature-disable proposal is superseded by the user's requirement: service startup always refuses incomplete configuration.

| Implemented setting | Purpose / default |
| --- | --- |
| `CHAPTER_CONFIG_FILE` | Private owner-only JSON configuration path; optional when all required values are supplied through environment |
| `SECRET_KEY` | Required private Django signing/session secret |
| `CHAPTER_MASTER_PASSWORD`, `CHAPTER_KDF_SALT` | Required password and persistent base64 installation salt |
| `CHAPTER_KDF_N`, `CHAPTER_KDF_R`, `CHAPTER_KDF_P` | Validated scrypt parameters; 32768, 8, 1 |
| `CHAPTER_S3_ENDPOINT_URL`, `CHAPTER_S3_BUCKET` | Required HTTPS provider endpoint and private bucket (versioned or unversioned) |
| `CHAPTER_S3_ACCESS_KEY_ID`, `CHAPTER_S3_SECRET_ACCESS_KEY` | Required private credentials |
| `CHAPTER_S3_REGION`, `CHAPTER_S3_ADDRESSING_STYLE` | Required provider region and `path`/`virtual` style |
| `CHAPTER_S3_PREFIX` | `objects`; generic namespace, with opaque extensionless random object names |
| `CHAPTER_RUNTIME_DIR` | Dedicated owner-only directory; default checkout `.chapter-data`; contains worker/cache locks, Huey and limiter SQLite files |
| `CHAPTER_CACHE_DIR` | Default runtime `cache` subdirectory |
| `CHAPTER_CACHE_MAX_FILES` | 20 |
| `CHAPTER_CACHE_MAX_BYTES` | Required disk byte budget |
| `CHAPTER_MAX_ARCHIVE_BYTES`, `CHAPTER_MAX_IMAGE_BYTES` | Required finite limits; archive at most 512 MiB |
| `CHAPTER_REQUEST_DELAY_SECONDS` | Two seconds after completion; minimum one |
| `CHAPTER_REQUEST_JITTER_SECONDS` | Additive random range; default 25% of base delay; nonnegative |
| `CHAPTER_REQUEST_RATE_LIMITS` | `[[1, 2], [300, 3600]]`; persisted PyrateLimiter budgets |
| `CHAPTER_REQUEST_TIMEOUT_SECONDS`, `CHAPTER_MAX_RETRIES` | 30 seconds, three attempts |
| `CHAPTER_MAX_BULK_ITEMS` | 100; larger frontend selections use sequential batches |

Fixed initial policies: two-second polling, one archive decryption per web process, one thread worker with an exclusive local lock, single-object uploads, permanent exact-key deletion, including all versions/delete markers on versioned storage, and staff-session authentication plus CSRF for management. Reading follows the existing reader boundary. These policies do not have placeholder settings requiring later decisions. `DEBUG=True` is refused while private configuration is loaded.

Storage permissions need object read/write/delete for the configured prefix. Validate that the initial provider supports the selected single-object upload size and simple overwrite/delete semantics. Keep TLS verification enabled. Add startup validation and a read-only diagnostic command so incorrect credentials, salt encoding, cache permissions, or unsupported configuration fail clearly.

## Implementation order and acceptance checks

1. Identity, models/migrations, settings validation, Huey/SqliteHuey integration, and simple restart scan. Verify stable identity despite adapter group-ID changes, duplicate prevention, exactly one worker/consumer, immediate mode disabled, queued/unaccepted jobs restarting from the beginning, accepted/cancelled/failed jobs staying terminal, and cooldown persistence.
2. PyrateLimiter integration, sequential resolver, and in-memory CBZ builder. Before contacting real sources, use a local fake upstream to verify request timestamps, no bursts/concurrency, redirects/retries passing the gate, Retry-After handling, immediate-403 stop behavior, restart cooldowns, and bulk jobs retaining sequential execution. Verify ordered pages/groups, inline/deferred URLs, bounded memory, status validation, and cancellation.
3. Crypto envelope and storage adapter. Verify round-trip, tampering/wrong-password rejection, fresh nonces, sequential transfers, restart overwrite after upload-before-acceptance failure, unaccepted-object cleanup, and secret-free logs.
4. Disk cache and image endpoint. Verify 20-file LRU, byte-limit behavior, cache survival across restart, one remote fetch for concurrent misses, bounded decryptions, correct MIME/order, and atomic writes.
5. Series/reader integration, bulk actions, and polling queue UI. Verify ready archives use local URLs for all page representations, state survives reload, and undownloaded groups retain upstream behavior. Verify visible/all selection, group choice, duplicates, partial failures, polling backoff, and no upstream fetches triggered by status polls.
6. Persistent deletion, cache clearing, and races. Verify upload/delete ordering, deletion without waiting for the full download backlog, failed deletion retries, stale URL behavior, and simple permanent deletion semantics.

Test failures and limits, not just the happy path. Initial rollout should use one small chapter and verify request timing, peak RAM, cache usage, object encryption, and deletion on the actual storage provider.

## Implementation defaults and setup inputs

The architecture is concluded. Compatible dependency pins and source integration are implemented. Remaining deployment work is supplying private values and checking the actual provider, browser flow, and one small chapter before broader use.

- User supplies S3 provider, endpoint, bucket, credentials, and addressing style later. Backblaze B2 compatibility includes version-aware permanent deletion; do not configure retention that prevents requested removal.
- Set finite RAM/archive/image limits and a disk byte cap after checking deployment resources. File-count default remains 20; polling interval remains two seconds.
- Audit existing source adapters for sequential resolution and test each supported source against a fake upstream before live use. Start with a two-second completion gap, configurable per deployment, and pause on blocking responses.
- Keep the existing single-user access boundary. Mutating download/delete/clear actions require an explicit authenticated session by default; existing reading access need not change.
- Select Python 3.8-compatible dependency pins during implementation. Bulk download uses the selected group and reports unavailable groups; submit at most 100 identities per request, batching larger UI selections.
- Terminally failed jobs require an explicit Retry action after bounded retries. Restart requeues unfinished work, not terminal failures. Cancelled jobs remain cancelled.
- Replacement is delete then download again. Decrypted CBZ export, archive version history, password rotation, multipart uploads, and partial-task resume are deferred.

## References

- [Huey Django integration](https://huey.readthedocs.io/en/latest/contrib.html): SqliteHuey and the existing Django consumer command.
- [Huey consumer lifecycle](https://huey.readthedocs.io/en/latest/consumer.html): one-worker configuration, graceful shutdown, and lack of at-least-once delivery for interrupted tasks.
- [PyrateLimiter 2.10 documentation](https://pypi.org/project/pyrate-limiter/2.10.0/): configurable rate budgets and persistent SQLite buckets.
- [Python ZIP archive handling](https://docs.python.org/3/library/zipfile.html): ordered entries, ZIP_STORED, seekable reading, and in-memory archive access.
- [Cryptography key derivation](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/): password KDFs and HKDF.
- [Cryptography authenticated encryption](https://cryptography.io/en/latest/hazmat/primitives/aead/): AES-GCM, associated data, nonce uniqueness, and authentication failure.
- [S3 GetObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html) and [DeleteObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html): object access and versioned deletion semantics; confirm compatibility with the selected provider.

## Implementation notes

The implemented catalog route is `GET /downloads/catalog/<source>/<series>/`; it reads the durable metadata snapshot and performs batched chapter-state lookups, with no upstream polling. Explicit series-page browsing refreshes source metadata; accepted chapter reading uses durable snapshots. Source adapters share the worker HTTP context, and their existing executor calls switch to sequential execution inside that context.

Cleanup tasks have priority alongside remote deletion. Cancellation still finishes while upstream downloads are paused. Old download retries are rejected when a chapter is already ready, and delayed cleanup retries skip objects subsequently accepted. Unsupported/non-image page responses also pause upstream downloads conservatively.

The existing browser save-to-device implementation remains separate. Stored pages use ordinary local image URLs compatible with the reader. Metadata availability responses are private and uncached so deletion cannot leave shared caches advertising stale local URLs.

Tests use isolated configuration, fake transport/storage, and Botocore Stubber. No live S3 permissions, provider transfer/deletion behavior, browser interaction, or live compatibility of each legacy source has been verified. Setup and the initial small-chapter check remain required before production use.

Backblaze references: [lifecycle rules](https://www.backblaze.com/docs/cloud-storage-lifecycle-rules), [Get Bucket Versioning](https://www.backblaze.com/apidocs/s3-get-bucket-versioning), [List Object Versions](https://www.backblaze.com/apidocs/s3-list-object-versions), and [Delete Object](https://www.backblaze.com/apidocs/s3-delete-object).
