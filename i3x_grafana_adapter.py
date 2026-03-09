"""
i3X → Prometheus Adapter
======================
Connects to the i3X CESMII Smart Manufacturing Platform and exposes
live data as a Prometheus /metrics endpoint.

Usage:
    pip install -r requirements.txt
    python i3x_grafana_adapter.py

Configuration:
    Copy .env.example to .env and edit. All settings are optional —
    defaults connect to i3x.cesmii.net and auto-discover all objects.

    I3X_BASE_URL      – i3X server base URL (default: https://i3x.cesmii.net)
    I3X_API_KEY       – API key for authentication (optional)
    I3X_API_SECRET    – API secret for authentication (optional)
    I3X_POLL_INTERVAL – sync poll frequency in seconds (default: 5)
    I3X_ADAPTER_PORT  – port this adapter listens on (default: 9000)
    I3X_SUBSCRIPTIONS – optional JSON array of subscription objects;
                        leave unset to auto-discover all leaf objects.
"""

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import i3x
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    Info,
    generate_latest,
)

# ─────────────────────────────────────────────
# Configuration (.env file or environment vars)
# ─────────────────────────────────────────────

load_dotenv()


def load_config() -> dict[str, Any]:
    """
    Read configuration from environment variables (populated via .env).

    I3X_BASE_URL      – i3X server base URL (default: https://i3x.cesmii.net)
    I3X_API_KEY       – API key for authentication (optional)
    I3X_API_SECRET    – API secret for authentication (optional)
    I3X_POLL_INTERVAL – sync poll frequency in seconds (default: 5)
    I3X_ADAPTER_PORT  – port this adapter listens on (default: 9000)
    I3X_SUBSCRIPTIONS – optional JSON array of subscription objects;
                        leave unset to auto-discover all leaf objects.
    """
    api_key = os.environ.get("I3X_API_KEY", "")
    api_secret = os.environ.get("I3X_API_SECRET", "")

    raw_subs = os.environ.get("I3X_SUBSCRIPTIONS", "")
    subscriptions = json.loads(raw_subs) if raw_subs.strip() else []

    return {
        "i3x_base_url": os.environ.get("I3X_BASE_URL", "https://i3x.cesmii.net"),
        "adapter_port": int(os.environ.get("I3X_ADAPTER_PORT", "9000")),
        "poll_interval_seconds": int(os.environ.get("I3X_POLL_INTERVAL", "5")),
        "auth": (api_key, api_secret) if api_key and api_secret else None,
        "subscriptions": subscriptions,
    }


CONFIG = load_config()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("i3x-adapter")

# ─────────────────────────────────────────────
# Prometheus metrics
# ─────────────────────────────────────────────

registry = CollectorRegistry()

# Numeric gauges — populated at startup (after discovery or from config)
gauges: dict[str, Gauge] = {}

# State gauges — created lazily for elements whose values are strings.
# Pattern: gauge{state="running"} = 1, all other states = 0
state_gauges: dict[str, Gauge] = {}
_current_states: dict[str, tuple[str, str]] = {}  # element_id -> (state_value, quality)


def ensure_gauges(subscriptions: list[dict]):
    """Create Prometheus Gauges for any subscription not yet registered."""
    for sub in subscriptions:
        eid = sub["id"]
        if eid not in gauges:
            gauges[eid] = Gauge(
                name=sub["label"],
                documentation=sub.get("description", sub["label"]),
                labelnames=["quality", "element_id"],
                registry=registry,
            )


adapter_info = Info("i3x_adapter", "i3X Grafana adapter metadata", registry=registry)
adapter_info.info({"server": CONFIG["i3x_base_url"], "version": "1.0.0"})

last_sync_gauge = Gauge(
    "i3x_last_sync_timestamp_seconds",
    "Unix timestamp of the last successful i3X sync",
    registry=registry,
)
errors_gauge = Gauge(
    "i3x_error_count_total",
    "Total number of i3X errors since startup",
    registry=registry,
)

# ─────────────────────────────────────────────
# Data parsing
# ─────────────────────────────────────────────

def parse_sync_response(raw: list[dict]) -> dict[str, dict]:
    """
    sync returns: [ { "<elementId>": { "data": [{ "value": 1.2, "quality": "GOOD", ... }] } } ]
    Returns a flat dict keyed by elementId with the latest VQT.
    """
    result = {}
    for item in raw:
        for element_id, payload in item.items():
            data_list = payload.get("data", [])
            if data_list:
                latest = data_list[-1]
                result[element_id] = {
                    "value": latest.get("value"),
                    "quality": latest.get("quality", "UNKNOWN"),
                }
    return result


def update_metrics(parsed: dict[str, dict]):
    """Push parsed VQT data into Prometheus gauges."""
    for element_id, vqt in parsed.items():
        raw_value = vqt["value"]
        quality = vqt["quality"]

        if isinstance(raw_value, (int, float)):
            gauge = gauges.get(element_id)
            if gauge is not None:
                gauge.labels(quality=quality, element_id=element_id).set(raw_value)

        elif isinstance(raw_value, str):
            # String/enum state — expose as gauge{state="<value>"} = 1 (active), 0 (inactive)
            if element_id not in state_gauges:
                name = element_id.replace("-", "_") + "_status"
                state_gauges[element_id] = Gauge(
                    name=name,
                    documentation=f"Active state of {element_id} (1 = current state)",
                    labelnames=["state", "quality", "element_id"],
                    registry=registry,
                )
            sg = state_gauges[element_id]
            prev = _current_states.get(element_id)
            if prev is not None and prev != (raw_value, quality):
                sg.labels(state=prev[0], quality=prev[1], element_id=element_id).set(0)
            sg.labels(state=raw_value, quality=quality, element_id=element_id).set(1)
            _current_states[element_id] = (raw_value, quality)
            log.debug(f"Updated {element_id} state = {raw_value!r} ({quality})")

        elif raw_value is None:
            log.warning(f"No value for {element_id}, skipping")

# ─────────────────────────────────────────────
# Background polling thread
# ─────────────────────────────────────────────

i3x_client: i3x.Client | None = None
i3x_subscription: i3x.Subscription | None = None
_stop_event = threading.Event()
_error_count = 0


def _run_client():
    """Connect, discover, subscribe, and poll — runs in a background thread."""
    global i3x_client, i3x_subscription, _error_count

    client = i3x.Client(CONFIG["i3x_base_url"], auth=CONFIG["auth"])
    i3x_client = client

    try:
        client.connect()
        log.info(f"Connected to {CONFIG['i3x_base_url']}")
    except i3x.I3XError as exc:
        log.error(f"Failed to connect to i3X: {exc}")
        return

    # Discover or use configured subscriptions
    subscriptions = CONFIG.get("subscriptions") or []
    if not subscriptions:
        log.info("No subscriptions configured — discovering objects from i3X …")
        try:
            objects = client.get_objects()
        except i3x.I3XError as exc:
            log.error(f"Failed to discover objects: {exc}")
            client.disconnect()
            return
        subscriptions = [
            {
                "id": obj.element_id,
                "label": obj.element_id.replace("-", "_"),
                "description": obj.display_name,
                "unit": "",
            }
            for obj in objects
            if not obj.is_composition
        ]
        log.info(f"Discovered {len(subscriptions)} subscribable objects")

    ensure_gauges(subscriptions)
    element_ids = [s["id"] for s in subscriptions]

    # Create subscription using the low-level API (avoids SSE)
    try:
        sub_id = client.create_subscription()
        client.register_items(sub_id, element_ids)
        i3x_subscription = client.get_subscription(sub_id)
        log.info(f"Subscribed to {len(element_ids)} objects (id={sub_id})")
    except i3x.I3XError as exc:
        log.error(f"Failed to create subscription: {exc}")
        client.disconnect()
        return

    # Poll loop
    log.info("Polling loop started.")
    while not _stop_event.is_set():
        try:
            raw = client.sync_subscription(sub_id)
            parsed = parse_sync_response(raw)
            update_metrics(parsed)
            last_sync_gauge.set(time.time())
            if parsed:
                log.debug(f"Synced {len(parsed)} objects.")
            else:
                log.info("Sync returned no data (empty response from i3X).")
        except i3x.NotFoundError:
            log.warning("Subscription expired, recreating …")
            try:
                sub_id = client.create_subscription()
                client.register_items(sub_id, element_ids)
                i3x_subscription = client.get_subscription(sub_id)
                log.info(f"Subscription recreated (id={sub_id})")
            except i3x.I3XError as exc:
                log.error(f"Failed to recreate subscription: {exc}")
        except i3x.I3XError as exc:
            _error_count += 1
            errors_gauge.set(_error_count)
            log.error(f"i3x error during sync: {exc}")

        _stop_event.wait(CONFIG["poll_interval_seconds"])

    client.disconnect()
    log.info("i3x client disconnected.")


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _stop_event.clear()
    thread = threading.Thread(target=_run_client, daemon=True, name="i3x-poller")
    thread.start()
    yield
    _stop_event.set()
    thread.join(timeout=10)


app = FastAPI(
    title="i3X → Grafana Adapter",
    description="Exposes i3X Smart Manufacturing data as Prometheus metrics",
    lifespan=lifespan,
)


@app.get("/metrics", response_class=Response)
async def metrics():
    """Prometheus scrape endpoint."""
    data = generate_latest(registry)
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health():
    """Simple liveness check."""
    return {
        "status": "ok",
        "connected": i3x_client.is_connected if i3x_client else False,
        "subscription_id": i3x_subscription.subscription_id if i3x_subscription else None,
        "i3x_server": CONFIG["i3x_base_url"],
    }


@app.get("/")
async def root():
    return {
        "message": "i3X Grafana Adapter running",
        "endpoints": {
            "/metrics": "Prometheus scrape endpoint",
            "/health": "Liveness check",
        },
    }


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "i3x_grafana_adapter:app",
        host="0.0.0.0",
        port=CONFIG["adapter_port"],
        log_level="info",
    )
