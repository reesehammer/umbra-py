# Hosted API probe history

Append-only `healthz.jsonl`, one JSON object per probe of
`https://api.umbra-py.space/healthz`. Written by
`.github/workflows/healthz.yml` on this branch only. `main` is not updated.

`ok` is the uptime bit: HTTP 200 and `degraded` false. `stale` is a separate
freshness bit (`built_at` more than 9 days old) and is not downtime.
`latency_ms` is how long the request took. There is no latency cap.

An uptime percentage is withheld until this file covers 7 days.
