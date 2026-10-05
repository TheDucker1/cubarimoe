# Implementation progress

Design authority: [encrypted-chapter-downloads.md](encrypted-chapter-downloads.md).
Deployment instructions: [chapter-download-setup.md](chapter-download-setup.md).

Branch: `feat/s3-cache`. User authorized implementation and GPT-attributed checkpoint commits. Never commit real secrets or runtime data. Missing required configuration prevents service startup. No live upstream downloads or live S3 writes during verification.

## Checklist

- [x] Configuration validation, dependency pins, application/models/migrations, identity
- [x] Huey single-worker execution, persistent jobs, whole-task restart, request pacing
- [x] In-memory archive, encryption envelope, S3 adapter, encrypted disk LRU
- [x] Authenticated management APIs, cached image serving, source integration
- [x] Frontend selection/bulk actions, polling, queue/cancel/retry/delete/cache maintenance
- [x] Failure/race/security tests and setup documentation
- [x] Final review; GPT-attributed implementation commits

## Current state

Implementation complete. One Huey thread worker and an exclusive consumer lock enforce sequential chapter operations. Source request wrappers and adapter executors share the worker's persisted rate gate. Redirects (including legacy manual Gist redirects), retries, completion gaps, Retry-After, blocking responses, cancellation, and restart cooldowns are covered by fake-clock tests.

Ordered CBZs are constructed in RAM and encrypted with scrypt/HKDF/AES-GCM. Only ciphertext is uploaded/stored on disk. Storage supports unversioned and versioned buckets with exact-key permanent version cleanup, uses single PutObject uploads, and suppresses secret-bearing SDK/source exception details. Archives become ready only after upload and committed acceptance. Interrupted unfinished jobs restart from the beginning and overwrite the same recorded target; accepted, failed, and cancelled jobs remain terminal.

Persistent deletion/cleanup has priority over queued downloads. Cancellation still completes during an upstream block. Acceptance checks cancellation/deletion again after upload. Failed deletion retains deleting state. Old failed download retries cannot replace ready chapters, and delayed cleanup cannot delete a subsequently accepted archive.

The encrypted disk cache coordinates cross-process loads/deletion, uses atomic writes, reconciles owned temporary/orphan files and stale rows, and enforces both file count (20 by default) and configured bytes. Oversized cache objects may be served without retention. One decryption is allowed at a time per web process. Persistent metadata snapshots and local URLs permit accepted chapter reading without upstream requests. Missing/corrupt storage returns 503 with no automatic source fallback; staff can explicitly choose upstream reading via the reader's fallback link.

Frontend: row Store/Remove, explicit group selection, select-visible/all/clear, bounded sequential bulk submissions, skip/partial-failure reporting, reader controls, persistent queue with polling/phase/page/byte/cooldown progress, cancel/retry, and clear-all local cache maintenance. Management requires staff session and CSRF. Metadata availability responses are private and uncached. Source catalog lookups are batched and polling has no upstream effects.

Required configuration is read from a private owner-only JSON file or explicit environment. The empty example contains no real secrets. Password/S3/signing secret/resource budgets must be filled before any normal Django startup. DEBUG=True is refused. Unresolved provider values have no working fallback.

Dependencies installed in the existing Python 3.8 venv: huey 2.5.2, pyrate-limiter 2.10.0, cryptography 44.0.3, boto3 1.37.38. Existing SQLite database is untouched by feature migrations; apply migrations during configured setup. No real upstream/S3 requests made.

## Verification

- `manage.py test downloads.tests --settings=downloads.tests.settings`: 41 tests, fake transport/storage plus Botocore Stubber; configuration/privacy, identity, archive/authentication/wrong password, single-consumer lock, redirect/retry timing, persisted limiter/cooldowns, acceptance/restart/cancel/delete races, cleanup safeguards, concurrent cache misses and restart hits, LRU/byte budgets, image MIME/ETag/unavailability, authentication/CSRF/bulk/polling, and explicit staff-only source fallback.
- `manage.py check --settings=downloads.tests.settings`: no issues.
- `manage.py makemigrations downloads --check --dry-run --settings=downloads.tests.settings`: no changes.
- `node --check downloads/static/downloads/downloads.js` and `node --check proxy/static/js/init.js`: pass.
- `python -m pip check`: no broken requirements.
- `git diff --check`: pass.
- Normal `manage.py check` without private configuration intentionally refuses startup and lists missing setting names without values.

Tests use isolated SQLite/runtime directories and ephemeral generated secrets. Never use test settings to serve the application. Legacy apps have ignored/empty migration packages, so test settings synchronize those models; setup uses `migrate --run-syncdb`.

## Commits

- `8cddf6d`: backend foundation, authored GPT <gpt@users.noreply.github.com>.
- `a9199de`: final implementation, frontend, 41 failure/race/security tests, reviewed recovery safeguards, private setup guide, and updated design; authored GPT <gpt@users.noreply.github.com>.

## Remaining deployment checks

1. Supply real private configuration using the setup guide; preserve password/salt/KDF settings.
2. Run configured startup checks, migrations, read-only storage diagnostic, and create/sign in to a staff account.
3. Start one worker and one web process; perform browser verification of selection/group controls, polling, reading, cancellation, and removal.
4. Use one small chapter first to measure actual request pacing, peak RAM/disk use, provider transfer/encryption/deletion behavior, and source compatibility before bulk jobs.

Real-provider behavior, browser interaction, and live compatibility of every legacy source are unverified. Existing browser upstream reading/preloading is outside the worker gate. No automatic partial resume, SSE, decrypted CBZ export, multipart upload, or password rotation is implemented. Versioned-bucket permanent deletion was subsequently implemented for Backblaze B2; see the latest follow-up.

## Resume instructions

Read this file, the design, setup guide, and `git status` first. Implementation is complete; continue with configuration/deployment only when authorized values are available. Do not rerun live downloads as a substitute for fake tests. If changes are requested, update this log and make GPT-attributed checkpoint commits without staging private configuration/runtime data.

Python 3.8 venv: `.venv`; interpreter: `../.runtime-python38`. DEBUG is false; local static serving needs `runserver --insecure`. New static assets and the download migration are explicitly tracked despite legacy ignore rules.

## Follow-up: opaque remote naming

User requested remote names contain no content clues. New jobs now choose an independent 256-bit random token under a generic `objects` prefix, with no extension, chapter hash, or archive version. SQLite persists the mapping/target so retry/restart/deletion behavior remains intact. New version-2 encryption headers remove chapter identity/version; those values remain authenticated using AAD reconstructed from SQLite. Version-1 decryption remains supported; existing targets are not renamed automatically. Size/timing concealment is outside this change.

Setup guide now includes a one-time, exclusive 0600 private-file generator that writes random signing/password/salt values directly to disk without printing them, plus initial 64 MiB archive / 8 MiB image / 256 MiB disk bounds. User must still fill S3 values. No private configuration or real remote data was created during this follow-up.

Follow-up verification: all 43 isolated tests pass; no migration changes; diff whitespace check passes. The documented private-file generator was exercised in a temporary directory: silent generation, mode 0600, and refusal to overwrite verified.

## Follow-up: private configuration and unified service

The real configuration is now recommended outside the repository at `~/.config/cubarimoe/chapter-config.json`; `.gitignore` already excludes `chapter-config*.json` except the empty example. The new executable `./service` uses the existing venv and a stdlib foreground supervisor. `init-config` creates secrets privately without printing/overwriting; `setup` performs one-time validation, migrations, storage diagnostic and account creation; ordinary startup validates/applies migrations and runs one worker plus one web process in one terminal. Existing configuration must be moved/preserved instead of regenerating password/salt.

Supervisor handles Ctrl+C/SIGTERM, child failure, startup failure, and bounded shutdown; both children are reaped. An exclusive launcher lock complements the worker lock. Comma-separated ALLOWED_HOSTS/repeated launcher --allowed-host support multiple explicit names. The web component remains the existing local Django runserver; no extra dependency/environment is introduced. `deploy/cubarimoe.service.example` documents running the same launcher under systemd later; no live unit was installed or service started.

Lifecycle/config tests use temporary private files and benign local subprocesses, never real S3/upstream calls or the existing database. Setup guide distinguishes one-time initialization from one-command daily startup. Ordinary startup does not require live S3 connectivity for cached reading; setup and worker validate storage.

Unified-service verification: all 48 isolated tests pass, including real local subprocess lifecycle tests for Ctrl+C/SIGTERM/failure cleanup and preflight/lock tests. systemd-analyze verify passes for the example unit; Bash/Python syntax, ignore rules, missing-config refusal, migration consistency, and diff checks pass. No real private configuration, live service, database migration, upstream request, or S3 operation was performed.

## Follow-up: actionable configuration validation

The user's posted delay=60, timeout=300, retry budget=3, bulk limit=100, rates=[[1,2],[300,3600]] are valid. A private, read-only check identified CHAPTER_S3_ENDPOINT_URL as the current failure: neither HTTPS scheme nor parsed hostname. No actual setting values were displayed or changed, and no remote requests were made.

Replaced generic invalid-configuration errors with fixed setting names/rules, preserving value redaction and existing bounds. Added tests for the posted settings and field-specific errors across secret, URL, path, timing/rate/KDF, and resource inputs. Setup guide explains request semantics and environment precedence.

Validation follow-up verification: all 50 isolated tests pass and git diff --check passes. Private configuration was read only for redacted diagnostics; it remains unchanged.

## Follow-up: additive jitter and Backblaze versioning

User configured a Backblaze B2 bucket with “Keep only the last version” and hit the original unversioned-only guard. Official docs confirm this is lifecycle cleanup (previous versions hidden for one day before deletion), not disabled versioning. Adapted storage to accept Enabled/Suspended versioning, verify version-list permissions read-only, and permanently remove every data version/delete marker for an exact DB-recorded key. Gather pagination before deletes, filter exact key, bound listing/counts, verify absence, and preserve failed/deleting state on retention/permission/incomplete cleanup. Purge an unaccepted target's old versions before restart replacement; no new schema needed.

Normal completion pacing now includes nonnegative uniform additive jitter, configurable through CHAPTER_REQUEST_JITTER_SECONDS, defaulting to 25% of the base delay. Base=60 therefore means a 60–75 second persisted completion gap by default. No existing minimum, Retry-After deadline, or rate history is shortened. Connection retry backoff also uses additive jitter.

All 56 isolated tests pass, including paginated exact-key version/delete-marker removal, partial failures/idempotence, incomplete removal, restart replacement, permission/error redaction, and jitter boundary/persistence checks. Setup/design docs updated. No live upload or deletion performed; read-only provider permissions check is being recorded separately.

Read-only Backblaze GetBucketVersioning/ListObjectVersions permission check passed after rerunning outside network sandbox. No objects were uploaded or deleted, no private settings were changed, and no live consumer/web service was started. Migration consistency and diff checks pass.
