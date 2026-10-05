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

Ordered CBZs are constructed in RAM and encrypted with scrypt/HKDF/AES-GCM. Only ciphertext is uploaded/stored on disk. Storage requires an unversioned bucket, uses single PutObject uploads, and suppresses secret-bearing SDK/source exception details. Archives become ready only after upload and committed acceptance. Interrupted unfinished jobs restart from the beginning and overwrite the same recorded target; accepted, failed, and cancelled jobs remain terminal.

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
- Final implementation commit: frontend, failure/race tests, reviewed recovery safeguards, private setup guide, and updated design. See `git log` for its identifier; authored GPT <gpt@users.noreply.github.com>.

## Remaining deployment checks

1. Supply real private configuration using the setup guide; preserve password/salt/KDF settings.
2. Run configured startup checks, migrations, read-only storage diagnostic, and create/sign in to a staff account.
3. Start one worker and one web process; perform browser verification of selection/group controls, polling, reading, cancellation, and removal.
4. Use one small chapter first to measure actual request pacing, peak RAM/disk use, provider transfer/encryption/deletion behavior, and source compatibility before bulk jobs.

Real-provider behavior, browser interaction, and live compatibility of every legacy source are unverified. Existing browser upstream reading/preloading is outside the worker gate. No automatic partial resume, SSE, decrypted CBZ export, multipart upload, password rotation, or versioned-bucket deletion is implemented, as agreed.

## Resume instructions

Read this file, the design, setup guide, and `git status` first. Implementation is complete; continue with configuration/deployment only when authorized values are available. Do not rerun live downloads as a substitute for fake tests. If changes are requested, update this log and make GPT-attributed checkpoint commits without staging private configuration/runtime data.

Python 3.8 venv: `.venv`; interpreter: `../.runtime-python38`. DEBUG is false; local static serving needs `runserver --insecure`. New static assets and the download migration are explicitly tracked despite legacy ignore rules.
