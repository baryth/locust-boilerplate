# API Profiling Suite (Locust)

A small, extensible **profiling** harness built on [Locust](https://locust.io).

This is deliberately *not* a heavy load-testing rig. The goal is to run a
modest, steady number of users against a handful of endpoints and answer
three questions per endpoint:

1. **Baseline** — is the response time in line with what we declared as
   acceptable (p50 / p95)?
2. **Stability** — is the response time consistent, or jittery
   (coefficient of variation)?
3. **Bottlenecks** — does response time creep upward over the course of
   the run (a sign of leaks, saturation, connection exhaustion, etc.)?

Everything needed to add a new endpoint is: one small task function, one
line in the User class, one entry in `profiling/baselines.yml`.

## Project layout

```
src/
  config.py           # env-driven settings
  auth.py             # OAuth2 client-credentials token fetch + cache
  locustfile.py        # the Locust User definition (entry point)
  tasks/
    topics.py           # GET /api/v1/topics
    subscriptions.py     # POST /api/v1/subscriptions/subscribe
profiling/
  baselines.yml         # declared baseline response times, per endpoint
  baseline.py           # loads baselines.yml
  collector.py          # in-memory per-endpoint sample collector
  report.py             # compares samples vs baseline, flags issues
  listeners.py          # wires the collector into Locust's events
reports/                 # generated profiling reports land here (git-ignored)
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then fill in .env with your values
```

### `.env` values

| Variable | Meaning |
|---|---|
| `API_HOST` | Base URL of the API under test |
| `TENANT_ID` | Tenant identifier for your auth provider |
| `CLIENT_ID` / `CLIENT_SECRET` | OAuth2 client credentials |
| `SCOPE` | OAuth2 scope requested for the token |
| `TOKEN_URL` | Token endpoint. May contain a `{tenant_id}` placeholder, e.g. `https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token` |
| `SUBSCRIBE_SAMPLE_SIZE` | How many topics to include per subscribe call |

The token is fetched **once** per test run and cached/shared across all
simulated users (with a lock so concurrent greenlets don't refetch), then
refreshed automatically shortly before it expires. Each request gets a
fresh `Authorization: Bearer <token>` header.

> If your auth provider expects a JSON body instead of form-encoded
> (some Auth0 tenants, for example), swap `data=payload` for `json=payload`
> in `src/auth.py` — it's a one-line change.

## Running it

Interactive (with the web UI):

```bash
locust -f src/locustfile.py --host "$API_HOST"
```

Headless, profiling-style (a handful of users, run for a fixed window):

```bash
locust -f src/locustfile.py --headless -u 5 -r 1 -t 10m --host "$API_HOST"
```

Sensible defaults for the above (`users`, `spawn-rate`, `run-time`) are
already in `locust.conf`, so `locust -f src/locustfile.py --host "$API_HOST"`
picks them up automatically — override any of them with CLI flags.

When the run finishes, in addition to Locust's own stats you'll see a
console summary like:

```
======================================================================
PROFILING REPORT
======================================================================

[OK] GET /api/v1/topics
  samples=612  mean=142ms  p50=138ms  p95=210ms  stdev=38ms  cv=0.27

[ATTENTION] POST /api/v1/subscriptions/subscribe
  samples=201  mean=310ms  p50=290ms  p95=640ms  stdev=210ms  cv=0.68
  ⚠ p95 640ms exceeds baseline 600ms
  ⚠ unstable: CV 0.68 exceeds max 0.60
  ⚠ possible bottleneck: response time drifted x1.72 over the run
======================================================================
```

A JSON copy is written to `reports/profile-<timestamp>.json` for
diffing between runs or archiving in CI.

## Declaring baselines

Edit `profiling/baselines.yml`:

```yaml
"GET /api/v1/topics":
  p50_ms: 150
  p95_ms: 400
  max_cv: 0.5

"POST /api/v1/subscriptions/subscribe":
  p50_ms: 250
  p95_ms: 600
  max_cv: 0.6
```

The key must match the `name=` used in the corresponding `self.get(...)`
/ `self.post(...)` call in the task file. No baseline entry for an
endpoint just means it's reported but never flagged.

## Adding a new endpoint

1. Add a small function to a file under `src/tasks/` (or a new file), taking
   `user` as its argument and calling `user.get(...)` / `user.post(...)`
   with a `name=` you'll reuse everywhere.
2. Register it as a `@task` on `ApiUser` in `src/locustfile.py`.
3. Add a matching entry to `profiling/baselines.yml`.

That's the whole extension surface — no framework to learn.

## Distributed runs

The bundled collector aggregates in-memory, per-process. That's a good
fit for the low-concurrency, single-process style this suite is meant
for. If you run Locust in master/worker mode, each process will emit its
own report; either profile from a single worker, or extend
`profiling/listeners.py` to forward samples to the master via Locust's
custom messaging (`runner.register_message`) and aggregate there.

## CI

`.github/workflows/profiling.yml` runs the suite headless on a schedule
(and on manual dispatch), reading credentials from repository secrets,
and uploads the `reports/` directory (including Locust's own HTML/CSV
reports) as a workflow artifact.
