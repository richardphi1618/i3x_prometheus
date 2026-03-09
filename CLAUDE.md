# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the adapter (Python)
python i3x_grafana_adapter.py

# Run full stack (adapter + Prometheus + Grafana)
docker compose up -d

# Build the adapter image only
docker compose build

# Check adapter health (while running)
curl http://localhost:9000/health
curl http://localhost:9000/metrics
```

No tests exist in this project currently.

## Architecture

Single-file Python service (`i3x_grafana_adapter.py`) that bridges the **i3X CESMII Smart Manufacturing Platform** to **Grafana** via a Prometheus scrape endpoint.

```
i3X API  ──(subscribe/sync REST)──►  adapter  ──(Prometheus /metrics)──►  Grafana
```

**Data flow:**
1. `_run_client()` runs in a background `threading.Thread`, keeping the synchronous `i3x.Client` off the asyncio event loop
2. On connect, calls `i3x_client.get_objects()` to discover all non-composition leaf nodes (unless `I3X_SUBSCRIPTIONS` is set)
3. Calls `client.create_subscription()` + `client.register_items()` — uses the low-level API to avoid SSE (the SSE stream has a bug in `i3x-client` 0.1.5 where it calls `response.stream.iter_text()` which was removed in httpx 0.28)
4. Polls `client.sync_subscription()` every `I3X_POLL_INTERVAL` seconds; uses `threading.Event.wait()` so shutdown is immediate
5. `parse_sync_response()` extracts the latest VQT from the raw sync list
6. `update_metrics()` routes values to `gauges` (numeric) or `state_gauges` (string), created lazily
7. FastAPI serves `/metrics`, `/health`, and `/`

**Metric types:**
- **Numeric values** → `gauges` dict, standard `Gauge` with `quality` and `element_id` labels
- **String/enum values** → `state_gauges` dict, `Gauge` with `state`, `quality`, and `element_id` labels; metric name gets `_status` suffix; active state = 1, previous state = 0; `_current_states` tracks last known state per element to zero out stale label sets

**Key globals:** `i3x_client`, `i3x_subscription`, `_stop_event`, `gauges`, `state_gauges`, `_current_states`

## Configuration

All config via environment variables (loaded from `.env` by `python-dotenv`):

| Variable | Default | Notes |
|---|---|---|
| `I3X_BASE_URL` | `https://i3x.cesmii.net` | |
| `I3X_POLL_INTERVAL` | `5` | seconds |
| `I3X_ADAPTER_PORT` | `9000` | |
| `I3X_API_KEY` / `I3X_API_SECRET` | — | passed as `auth=` tuple to `i3x.Client` |
| `I3X_SUBSCRIPTIONS` | — | JSON array of `{id, label, description, unit}`; omit for auto-discovery |

Copy `.env.example` to `.env` to configure.

## Docker

Three services defined in `docker-compose.yml`:
- `adapter` — built from `Dockerfile`, reads `.env` via `env_file`
- `prometheus` — `prom/prometheus`, scrapes `adapter:9000`, config in `prometheus.yml`
- `grafana` — `grafana/grafana`, Prometheus datasource auto-provisioned from `grafana/provisioning/datasources/prometheus.yml`

Named volumes `prometheus_data` and `grafana_data` persist state across restarts.

## Known issues

- `i3x-client` 0.1.5 SSE streaming is broken with httpx 0.28 (`BoundSyncStream` has no `iter_text()`). We work around this by using `sync_subscription()` polling instead of `client.subscribe()`. Monitor for a fix in a future `i3x-client` release.
