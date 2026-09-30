# Phase 0 Performance + Security Patches (proposal)

**Status:** Draft. Source of truth lives in [`nelc/openedx-2.0`](https://github.com/nelc/openedx-2.0) under `django-patches/`.

This document lists five Django-level patches identified during the Open edX
2.0 study. All five are **independent**, **reversible**, **feature-flagged**,
and are intended to ship **before** any cell-based sharding work. They
address root causes we confirmed in production (see evidence at the bottom
of each section and in the parent PRD).

Each patch below is a spec: exact files, exact change, exact flag, exact
rollback. No code is committed in this PR. Once a spec is agreed, the
author of record opens a second PR with the code and tests.

---

## Patch 1 — defer `auth_user.last_login` writes to Redis

**Problem.** Every authenticated web request (DRF `TokenAuthentication`,
session refresh, SSO callback) triggers `update_last_login` which issues a
synchronous `UPDATE auth_user SET last_login = NOW() WHERE id = ?`. At peak
traffic on `moe.futurex.sa` this is the single hottest write in MySQL slow
query log.

**Change.**

1. New setting `NASSAU_DEFER_LAST_LOGIN_TO_REDIS = False` (default off,
   flip on per-environment once canary'd).
2. Connect a receiver to `django.contrib.auth.signals.user_logged_in` that,
   when the flag is on, writes the timestamp to Redis:
   `HSET last_login:{shard} {user_id} {iso8601}`.
3. A Celery beat task every 5 minutes drains each hash into MySQL with a
   single multi-row `UPDATE ... CASE id WHEN ... END` per shard.
4. Reads of `user.last_login` fall back to Redis if the stored value is
   older than the hash entry.

**Files.**

- `common/djangoapps/student/signals/handlers.py` — signal receiver.
- `common/djangoapps/student/tasks.py` — `flush_last_login` Celery task.
- `lms/envs/common.py`, `cms/envs/common.py` — flag default.
- `openedx/core/djangoapps/nassau/` (new app, optional) — shared helpers.

**Flag.** `NASSAU_DEFER_LAST_LOGIN_TO_REDIS` (env/Tutor config).

**Rollback.** Flip flag to `False`; Celery beat task is idempotent; Redis
keys are TTL 24h so stale state self-expires.

**Expected impact.** Removes ~1 `UPDATE auth_user` per authenticated
request. At current peak that is a few hundred QPS of write contention on
the hottest row in the busiest tenant.

---

## Patch 2 — async + debounced `BlockCompletion.submit_completion`

**Problem.** `BlockCompletion.submit_completion` writes to
`completion_blockcompletion` on the request path every time a learner
progresses through a unit. Slow query log shows this is the #2 write
hotspot; rows are contended because many learners finish similar blocks
within the same minute.

**Change.**

1. New Celery task `completion.tasks.submit_completion_async(user_id,
   course_key, block_key, completion, modified)` on queue `high`.
2. New setting `NASSAU_COMPLETION_ASYNC = False`. When on, the view calls
   `.delay(...)` instead of `BlockCompletion.submit_completion`.
3. Redis-backed debounce: within a 30-second window, coalesce identical
   `(user, block)` updates. Keep the **max** completion value.
4. Preserve strict read-your-writes via the same Redis cache for the
   progress-bar API: the GET path reads pending values from Redis first.

**Files.**

- `openedx/core/djangoapps/completion/tasks.py` — new.
- `openedx/core/djangoapps/completion/models.py` — wrap `submit_completion`.
- `openedx/core/djangoapps/completion/api/v1/views.py` — conditional `.delay`.
- `lms/envs/common.py`, `cms/envs/common.py` — flag default.

**Flag.** `NASSAU_COMPLETION_ASYNC`.

**Rollback.** Flip flag; any in-flight Celery tasks still commit on the
same schema. No DDL.

**Expected impact.** Removes row-lock contention on
`completion_blockcompletion (user_id, block_key)`. Debounce reduces write
rate by ~10x during video playback, since the front-end currently
heartbeats every ~5s.

---

## Patch 3 — `eox_nelp` likedislikeunit 404 storm fix

**Problem.** `GET /api/eox-nelp/likedislikeunit/{block_key}` fires on every
unit render. For blocks that do not yet have a `LikeDislikeUnit` row the
view does `.get_or_create(...)` which hits MySQL with an `INSERT` on cache
miss. Access logs show tens of 404s/s during peak on
`moe.futurex.sa` — the **404 storm** from the PRD.

**Change.**

1. In the view, first check a Redis set `block_exists:{course_key}` to see
   if the block has ever had a LikeDislike write. On miss, skip the
   `get_or_create` and return an empty 200 response.
2. Writes (`POST /like`, `POST /dislike`) go through a Celery task that
   performs the upsert and adds the block to the Redis set.
3. Rebuild the Redis set lazily: if a read path doesn't find the set, it
   queries DB once and populates.

**Files.**

- `eox_nelp/likedislikeunit/api_clients.py` (or the current module in this
  fork).
- `eox_nelp/likedislikeunit/views.py`.
- `eox_nelp/likedislikeunit/tasks.py` — new.

**Flag.** `NASSAU_LIKEDISLIKE_REDIS_GUARD`.

**Rollback.** Flip flag; view falls back to current behaviour.

**Expected impact.** Removes the majority of the 404 storm visible in
nginx access logs. Makes reads O(Redis) instead of O(MySQL round-trip +
possible write).

---

## Patch 4 — `CONN_MAX_AGE=60` + `CONN_HEALTH_CHECKS=True`

**Problem.** Current Tutor config leaves Django's `CONN_MAX_AGE` at 0,
which means every request opens a new TCP + TLS connection to Cloud SQL.
At steady state we see ~3k connections/sec churn. Cloud SQL connection
init dominates the bottom of the latency histogram for a non-trivial
fraction of requests.

**Change.**

1. Tutor config override (not a Django patch per se) setting
   `OPENEDX_DATABASES.*.CONN_MAX_AGE = 60` and
   `OPENEDX_DATABASES.*.CONN_HEALTH_CHECKS = True` for both `default` and
   `read_replica`.
2. Raise Cloud SQL `max_connections` or ensure PgBouncer/ProxySQL not in
   path (FutureX intentionally bypasses ProxySQL at the application
   level — see PRD).

**Files.**

- `nassau/plugins/*` (Tutor plugin) — render `OPENEDX_DATABASES` with new
  values.
- Python app itself needs no change; Django already honours these keys.

**Flag.** Rolled out per environment via Tutor config, not feature-flagged
in-app. Revert by bumping value back.

**Rollback.** Revert Tutor config.

**Expected impact.** Cuts connection-setup overhead per request for hot
paths. Watch for "too many connections" on Cloud SQL — if it appears,
raise Cloud SQL `max_connections` or tune `CONN_MAX_AGE` down to 30.

---

## Patch 5 — route `eox-nelp` signal-driven Celery tasks to a dedicated queue

**Problem.** Several `eox-nelp` signal handlers call `.delay(...)` inline
during the request. The default Celery broker is the same Redis that
handles session cache; a slow broker write blocks the web request. Slow
query log + uWSGI request log show ~50ms extra on affected endpoints.

**Change.**

1. New Celery queue `event_routing` (already configured in FutureX — we're
   just routing to it).
2. For each `eox-nelp` signal that currently does `.delay(...)` on the
   default queue, route to `event_routing`.
3. Wrap each call in a fire-and-forget helper
   `safe_delay(task, *args, **kwargs)` that swallows broker exceptions,
   logs to Sentry, and never raises into the web thread.

**Files.**

- `eox_nelp/signals/handlers.py`.
- New helper `eox_nelp/_celery.py` (or similar).

**Flag.** `NASSAU_EOX_NELP_EVENT_ROUTING_ASYNC`.

**Rollback.** Flip flag; handlers call `.delay(...)` as today.

**Expected impact.** Web-path p95 latency should drop by the fraction of
requests currently blocked on broker writes (estimated 5-10% of
authenticated requests that trigger signals).

---

## Patch 6 — rotate exposed AWS credentials (security, not performance)

**Problem.** `edx-platform-strains/teak/nelp/config.yml` contains
**plaintext AWS access keys** (discovered during the Open edX 2.0 study).
These are live long-term credentials committed in a repo visible to
contractors.

**Change.**

1. Rotate the access key in IAM. Revoke the old one.
2. Move the new credentials to Google Secret Manager.
3. Render them into the Tutor config via a plugin that resolves from
   Secret Manager at build time, never committing them to git.
4. Rewrite git history on `edx-platform-strains` to remove the old key
   (BFG repo-cleaner or `git filter-repo`), force-push, and rotate.

**Files.**

- `edx-platform-strains/teak/nelp/config.yml` (remove).
- `nassau/plugins/<name>/secrets.py` (new, Secret Manager lookup).

**Flag.** n/a — this is a one-shot rollout.

**Rollback.** n/a — do **not** roll back. Old keys must stay revoked.

**Owner.** Security.

---

## Ordering + acceptance

| # | Deploy after | Verify with |
|---|---|---|
| 4 | Tutor config review | `SELECT count(*) FROM information_schema.processlist WHERE db='edxapp'` trend |
| 6 | Immediately | AWS CloudTrail shows old key revoked; no legacy key in repo |
| 1 | 4 | MySQL slow query log: `UPDATE auth_user SET last_login` drops to <1% |
| 3 | 4 | nginx 404 rate on `/api/eox-nelp/likedislikeunit/` drops |
| 2 | 1 | MySQL slow query log: `INSERT ... completion_blockcompletion` drops |
| 5 | 2 | Web p95 latency, uWSGI request log |

Each patch lands behind its own flag. Rollout is per-environment: enable
in sandbox, soak 48h, enable in staging, soak 48h, enable in production.

## Where the full specs live

Source of truth (exact Python snippets, unit-test expectations, migration
notes): <https://github.com/nelc/openedx-2.0/tree/main/django-patches>.

## This PR

This PR **does not change code**. It only introduces this proposal doc so
the team can comment inline, argue with the priorities, and approve (or
reject) each patch before a real code PR is opened.
