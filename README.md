> [!NOTE]
> This was "vibe coded" in like an hour and mostly as a simple project for me to explore: coding with claude / working with the [i3x](https://www.i3x.dev/) api and sdk kit. **Do not expect much from this**. Maybe I will give this more thought with time but for now felt like something fun to buid / play with. 

# i3X → Prometheus Adapter

Bridges the **i3X CESMII Smart Manufacturing Platform** to **Prometheus** metrics endpoint.

```
i3X API  ──(subscribe/sync)──►  adapter  ──(Prometheus /metrics)──►  Grafana
```

---

## Quick Start (Docker)

The easiest way to run the full stack — adapter, Prometheus, and Grafana — is with Docker Compose:

```bash
cp .env.example .env   # edit as needed
docker compose up -d
```

| Service | URL |
|---|---|
| Adapter metrics | http://localhost:9000/metrics |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 (admin / admin) |

Prometheus is pre-configured as the default Grafana datasource — no manual setup required.

---

## Quick Start (Python)

```bash
pip install -r requirements.txt
cp .env.example .env   # edit as needed
python i3x_grafana_adapter.py
```

---

## Configuration

All configuration is via environment variables (or a `.env` file in the working directory).

| Variable | Default | Description |
|---|---|---|
| `I3X_BASE_URL` | `https://i3x.cesmii.net` | i3X server URL |
| `I3X_POLL_INTERVAL` | `5` | Sync poll frequency (seconds) |
| `I3X_ADAPTER_PORT` | `9000` | Port for Prometheus to scrape |
| `I3X_API_KEY` | — | API key (if server requires auth) |
| `I3X_API_SECRET` | — | API secret (if server requires auth) |
| `I3X_SUBSCRIPTIONS` | — | JSON array of objects to subscribe to (see below) |

### Auto-discovery

If `I3X_SUBSCRIPTIONS` is unset, the adapter queries `GET /objects` on startup and subscribes to all non-composition leaf objects automatically. No configuration needed.

### Pinning specific metrics

Set `I3X_SUBSCRIPTIONS` to a JSON array if you want to subscribe to a specific subset:

```bash
I3X_SUBSCRIPTIONS='[
  {"id": "pump-101-measurements-bearing-temperature-value",
   "label": "pump_101_bearing_temperature",
   "description": "Pump 101 Bearing Temperature",
   "unit": "celsius"}
]'
```

`id` is the i3X `elementId` (find these in i3X Explorer). `label` becomes the Prometheus metric name and must be a valid PromQL identifier.

---

## Metrics

### Numeric values

Numeric element values are exposed as standard Prometheus gauges:

```promql
# Latest value
pump_101_measurements_bearing_temperature_value

# Only GOOD quality readings
pump_101_measurements_bearing_temperature_value{quality="GOOD"}

# Rate of change
rate(pump_101_measurements_bearing_temperature_value[1m])
```

Labels on every numeric metric: `quality`, `element_id`.

### String / enum states

String-valued elements (e.g. `pump-101-state`) are exposed using the active-state pattern — the current state label is set to `1`, all others to `0`:

```promql
# Current state as a label
pump_101_state_status{state="running"} 1.0
pump_101_state_status{state="stopped"} 0.0

# Filter to active state only
pump_101_state_status == 1
```

### Adapter health metrics

| Metric | Description |
|---|---|
| `i3x_last_sync_timestamp_seconds` | Unix time of last successful sync |
| `i3x_error_count_total` | Cumulative error count since startup |
| `i3x_adapter_info` | Adapter metadata (`server`, `version` labels) |

---

## How it Works

The adapter uses the [i3x-client](https://github.com/cesmii/python-i3x-client) Python package to communicate with the i3X REST API.

On startup:
1. Connects to the i3X server via `i3x.Client`
2. Discovers subscribable objects from `GET /objects` (or uses `I3X_SUBSCRIPTIONS`)
3. Creates a subscription and registers all element IDs
4. Polls `POST /subscriptions/{id}/sync` every `I3X_POLL_INTERVAL` seconds
5. Parses VQT (Value, Quality, Timestamp) responses and updates Prometheus gauges
6. Serves `/metrics` for Prometheus to scrape

If the subscription expires (HTTP 404), it is automatically recreated.

---

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /metrics` | Prometheus scrape endpoint |
| `GET /health` | Liveness check — returns connection status and subscription ID |
| `GET /` | Lists available endpoints |

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Connection refused` on Grafana | Check adapter is running: `curl http://localhost:9000/health` |
| `connected: false` in `/health` | Check `I3X_BASE_URL` and network connectivity to the i3X server |
| No metric values in Grafana | Check `curl http://localhost:9000/metrics` — gauges appear but have no samples until i3X sends data |
| Subscription 404 errors | Adapter auto-recreates the subscription — check logs for repeated failures |
