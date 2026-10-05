# Implementation progress

Design authority: [encrypted-chapter-downloads.md](encrypted-chapter-downloads.md).

Branch: `feat/s3-cache`. User authorized implementation and GPT-attributed checkpoint commits. Never commit real secrets or runtime data. Missing required configuration must prevent service startup. No live upstream downloads or live S3 writes during verification without actual configured storage.

## Checklist

- [x] Configuration validation, dependency pins, application/models/migrations, identity
- [ ] Huey single-worker execution, persistent jobs, whole-task restart, request pacing
- [ ] In-memory archive, encryption envelope, S3 adapter, encrypted disk LRU
- [ ] Authenticated management APIs, cached image serving, source integration
- [ ] Frontend selection/bulk actions, polling, queue/cancel/retry/delete/cache maintenance
- [ ] Failure/race/security tests and setup documentation
- [ ] Final review and GPT-attributed commits

## Current state

Core implementation checkpoint ready: downloads app, validated configuration, identity/models/initial migration, Huey integration, rate-gated source resolution, in-memory CBZ/authenticated encryption, S3 adapter, encrypted disk LRU, management APIs, and initial source integration are implemented. Most still require comprehensive integration/race tests. Frontend is next, followed by failure tests/review.

Dependencies installed in the existing Python 3.8 venv: huey 2.5.2, pyrate-limiter 2.10.0, cryptography 44.0.3, boto3 1.37.38. Existing SQLite database is untouched by feature migrations. No real upstream/S3 requests made.

Verification: `manage.py makemigrations downloads --settings=downloads.tests.settings` generated the initial migration. `manage.py test downloads.tests.test_core --settings=downloads.tests.settings` passes 6 configuration/identity/archive/encryption tests. Test settings generate ephemeral secrets and use isolated temporary paths; never use them for deployment.

Potential follow-up issues to verify: source snapshot/catalog avoids upstream reading for ready chapters; Huey management command enforces one consumer; deletion/cancel/cleanup precedence; source concurrency context; cache/crypto resource bounds; frontend metadata integration; migration tracking and ignored static files. DEBUG is now false in local settings to prevent secret-bearing debug tracebacks (development static serving needs `runserver --insecure`). Missing required private configuration intentionally prevents normal service startup.

Existing Python 3.8 venv is at `.venv`; interpreter is in `../.runtime-python38`. Reader assets are tracked under `proxy/static` (ordinary `rg --files` skips them due to the legacy `static/` ignore rule).

## Resume instructions

Read this file, the design, and `git status` first. Continue the next unchecked step. Check implementation and tests rather than assuming a checked step needs no review. Update this log after each milestone with commands/results, deviations, remaining issues, and commit IDs. Do not expose configuration values in output.
