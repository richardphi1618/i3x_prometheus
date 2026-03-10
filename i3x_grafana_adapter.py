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

import base64
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger

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

# ─────────────────────────────────────────────
# Prometheus metrics
# ─────────────────────────────────────────────

registry = CollectorRegistry()

# Numeric gauges — keyed by element_id.
gauges: dict[str, Gauge] = {}

# State gauges — keyed by element_id.
# Pattern: gauge{state="running"} = 1, all other states = 0
state_gauges: dict[str, Gauge] = {}
_current_states: dict[str, tuple[str, str]] = {}  # element_id -> (state_value, quality)

# Composite gauges — for object elements whose every leaf property is numeric.
# element_id -> {property_path: Gauge}
composite_gauges: dict[str, dict[str, Gauge]] = {}


def _sanitize_name(name: str) -> str:
    """Convert any string to a valid Prometheus metric name segment."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_")


def _readable_id(element_id: str) -> str:
    """
    Decode a base64 element_id and strip the leading [provider] prefix.
    e.g. 'W2RlZmF1bHRdTW9kZWxlZC9UZXN0IFVEVCAx' → 'Modeled/Test UDT 1'
    Falls back to the raw element_id if decoding fails.
    """
    try:
        decoded = base64.b64decode(element_id + "==").decode("utf-8")
        return re.sub(r"^\[[^\]]*\]", "", decoded)
    except Exception:
        return element_id


def _all_leaves_numeric(schema: dict) -> bool:
    """
    Return True only when every leaf value in the schema is numeric.
    Used to distinguish measurement UDTs (all-int/float) from state objects
    that mix numerics with strings/enums.
    """
    schema_type = schema.get("type", "")
    if schema_type in ("number", "integer"):
        return True
    if schema_type in ("string", "boolean", "null"):
        return False
    if schema_type == "object":
        props = schema.get("properties", {})
        return bool(props) and all(_all_leaves_numeric(p) for p in props.values())
    return False


def _classify_metric_kind(schema: dict) -> str:
    """
    Classify an object type schema as 'numeric', 'composite', or 'state'.

      - type "number" or "integer"               → numeric gauge
      - type "object" where all leaves numeric   → composite (one gauge per leaf)
      - anything else                            → state gauge
    """
    schema_type = schema.get("type", "")
    if schema_type in ("number", "integer"):
        return "numeric"
    if schema_type == "object" and _all_leaves_numeric(schema):
        return "composite"
    return "state"


def _flatten_numeric_leaves(schema: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Walk schema properties and return [(path, json_type)] for every numeric leaf."""
    results = []
    schema_type = schema.get("type", "")
    if schema_type in ("number", "integer"):
        results.append((prefix, schema_type))
    elif schema_type == "object":
        for prop_name, prop_schema in schema.get("properties", {}).items():
            child = f"{prefix}_{prop_name}" if prefix else prop_name
            results.extend(_flatten_numeric_leaves(prop_schema, child))
    return results


def _flatten_dict_values(value: dict, prefix: str = "") -> list[tuple[str, Any]]:
    """Walk a nested dict and return [(path, value)] for every leaf."""
    results = []
    for key, val in value.items():
        child = f"{prefix}_{key}" if prefix else key
        if isinstance(val, dict):
            results.extend(_flatten_dict_values(val, child))
        else:
            results.append((child, val))
    return results


def _create_numeric_gauge(label: str, description: str) -> Gauge:
    return Gauge(
        name=label,
        documentation=description,
        labelnames=["quality", "path"],
        registry=registry,
    )


def _create_state_gauge(element_id: str) -> Gauge:
    readable = _readable_id(element_id)
    metric_name = _sanitize_name(readable) + "_status"
    return Gauge(
        name=metric_name,
        documentation=f"Active state of {readable} (1 = current state)",
        labelnames=["state", "quality", "path"],
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


def _set_state_gauge(element_id: str, state_label: str, quality: str, readable: str):
    """Set the active state to 1, zeroing out any previous state."""
    sg = state_gauges[element_id]
    prev = _current_states.get(element_id)
    if prev is not None and prev != (state_label, quality):
        sg.labels(state=prev[0], quality=prev[1], path=readable).set(0)
    sg.labels(state=state_label, quality=quality, path=readable).set(1)
    _current_states[element_id] = (state_label, quality)


def _extract_state_label(raw_value) -> str:
    """Extract a human-readable state label from a string or complex dict value."""
    if isinstance(raw_value, str):
        return raw_value
    if isinstance(raw_value, dict):
        type_obj = raw_value.get("type", {})
        return type_obj.get("name") or raw_value.get("description") or str(raw_value)
    return str(raw_value)


def update_metrics(parsed: dict[str, dict]):
    """Push parsed VQT data into Prometheus gauges."""
    for element_id, vqt in parsed.items():
        raw_value = vqt["value"]
        quality = vqt["quality"]

        if raw_value is None:
            logger.warning(f"No value for {element_id}, skipping")
            continue

        readable = _readable_id(element_id)

        if element_id in gauges:
            if isinstance(raw_value, (int, float)):
                gauges[element_id].labels(quality=quality, path=readable).set(raw_value)
            else:
                logger.warning(f"Expected numeric for {element_id}, got {type(raw_value).__name__}")

        elif element_id in composite_gauges:
            if isinstance(raw_value, dict):
                sub_gauges = composite_gauges[element_id]
                for path, val in _flatten_dict_values(raw_value):
                    if path in sub_gauges and isinstance(val, (int, float)):
                        sub_gauges[path].labels(quality=quality, path=readable).set(val)
            else:
                logger.warning(f"Expected dict for composite {element_id}, got {type(raw_value).__name__}")

        elif element_id in state_gauges:
            state_label = _extract_state_label(raw_value)
            _set_state_gauge(element_id, state_label, quality, readable)

        else:
            logger.debug(f"No gauge registered for {element_id}, skipping")

# ─────────────────────────────────────────────
# Background polling thread
# ─────────────────────────────────────────────

i3x_client: i3x.Client | None = None
i3x_subscription: i3x.Subscription | None = None
_stop_event = threading.Event()
_error_count = 0


def _run_client():
    """Connect, discover, subscribe, and poll — runs in a background thread."""
    try:
        _run_client_inner()
    except Exception:
        logger.exception("Unhandled exception in i3x polling thread — thread is exiting")


def _run_client_inner():
    """Inner implementation — separated so the outer function can catch all escaping exceptions."""
    global i3x_client, i3x_subscription, _error_count

    client = i3x.Client(CONFIG["i3x_base_url"], auth=CONFIG["auth"])
    i3x_client = client

    try:
        client.connect()
        logger.info(f"Connected to {CONFIG['i3x_base_url']}")
    except i3x.I3XError as exc:
        logger.error(f"Failed to connect: {exc}")
        return

    # Discover objects and classify by schema type
    logger.info("Discovering objects from i3X …")
    try:
        objects = client.get_objects()
    except i3x.I3XError as exc:
        logger.error(f"Failed to discover objects: {exc}")
        client.disconnect()
        return

    leaf_objects = [
        obj for obj in objects
        if not obj.is_composition and _readable_id(obj.element_id)
    ]
    logger.info(f"Discovered {len(leaf_objects)} leaf objects")

    unique_type_ids = list({obj.type_id for obj in leaf_objects if obj.type_id})
    try:
        object_types = client.query_object_types(unique_type_ids)
    except i3x.I3XError as exc:
        logger.error(f"Failed to query object types: {exc}")
        client.disconnect()
        return
    schema_by_type_id = {ot.element_id: ot.schema for ot in object_types}

    config_subs = CONFIG.get("subscriptions") or []
    if config_subs:
        configured_ids = {s["id"] for s in config_subs}
        label_map = {s["id"]: s["label"] for s in config_subs}
        desc_map = {s["id"]: s.get("description", s["label"]) for s in config_subs}
        leaf_objects = [obj for obj in leaf_objects if obj.element_id in configured_ids]
    else:
        label_map = {}
        desc_map = {}

    element_ids = []
    for obj in leaf_objects:
        schema = schema_by_type_id.get(obj.type_id, {})
        kind = _classify_metric_kind(schema)
        label = label_map.get(obj.element_id, obj.element_id.replace("-", "_"))
        description = desc_map.get(obj.element_id, obj.display_name or label)

        if kind == "numeric":
            gauges[obj.element_id] = _create_numeric_gauge(label, description)
            element_ids.append(obj.element_id)

        elif kind == "composite":
            base = _sanitize_name(obj.display_name or label)
            leaves = _flatten_numeric_leaves(schema)
            sub_gauges = {}
            for path, _ in leaves:
                sub_gauges[path] = Gauge(
                    name=f"{base}_{path}",
                    documentation=f"{obj.display_name} – {path}",
                    labelnames=["quality", "path"],
                    registry=registry,
                )
            composite_gauges[obj.element_id] = sub_gauges
            element_ids.append(obj.element_id)
            logger.info(f"Composite '{obj.display_name}': {[f'{base}_{p}' for p, _ in leaves]}")

        else:
            state_gauges[obj.element_id] = _create_state_gauge(obj.element_id)
            element_ids.append(obj.element_id)

    logger.info(
        f"Registered {len(gauges)} numeric, {len(composite_gauges)} composite, "
        f"{len(state_gauges)} state gauge(s)"
    )

    # Create subscription using the low-level API
    # Note: high-level client.subscribe() uses SSE streaming which is broken in i3x-client 0.1.5
    # (response.stream.iter_text() was never a valid httpx public API). Using sync polling instead
    # until the upstream fix is released.
    try:
        sub_id = client.create_subscription()
        client.register_items(sub_id, element_ids)
        i3x_subscription = client.get_subscription(sub_id)
        logger.info(f"Subscribed to {len(element_ids)} objects (id={sub_id})")
    except i3x.I3XError as exc:
        logger.error(f"Failed to create subscription: {exc}")
        client.disconnect()
        return

    # Poll loop
    logger.info("Polling loop started.")
    while not _stop_event.is_set():
        try:
            raw = client.sync_subscription(sub_id)
            parsed = parse_sync_response(raw)
            update_metrics(parsed)
            last_sync_gauge.set(time.time())
            if parsed:
                logger.debug(f"Synced {len(parsed)} objects.")
            else:
                logger.info("Sync returned no data (empty response from i3X).")
        except i3x.NotFoundError:
            logger.warning("Subscription expired, recreating …")
            try:
                sub_id = client.create_subscription()
                client.register_items(sub_id, element_ids)
                i3x_subscription = client.get_subscription(sub_id)
                logger.info(f"Subscription recreated (id={sub_id})")
            except i3x.I3XError as exc:
                logger.error(f"Failed to recreate subscription: {exc}")
        except i3x.I3XError as exc:
            _error_count += 1
            errors_gauge.set(_error_count)
            logger.error(f"i3x error during sync: {exc}")
        except Exception:
            logger.exception("Unexpected error in poll loop — continuing")

        _stop_event.wait(CONFIG["poll_interval_seconds"])

    client.disconnect()
    logger.info("i3x client disconnected.")


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
    import logging

    # Route uvicorn's standard logging through loguru
    logging.getLogger("uvicorn").handlers = []
    logging.getLogger("uvicorn.access").handlers = []

    uvicorn.run(
        "i3x_grafana_adapter:app",
        host="0.0.0.0",
        port=CONFIG["adapter_port"],
        log_config=None,
    )
