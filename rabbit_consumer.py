"""
RabbitMQ RPC: Go API publishes JSON to a per-organization queue
`hikvision.gateway.requests.<sanitized_org_id>`; this agent runs on the LAN,
executes SADP / ISAPI, replies on reply_to.
No HTTP server — credentials default from .env when message omits them.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Dict

import pika

import sadp_core
from amqp_tls import connection_parameters
from gateway_isapi import forward_isapi_to_device

logger = logging.getLogger("hikvision-rabbit")

_REQUEST_QUEUE_PREFIX = "hikvision.gateway.requests"
_AGENT_ORG_ID = ""

_discover_lock = threading.Lock()


def _sanitize_organization_id_for_queue_suffix(organization_id: str) -> str:
    """Must match Go internal/hikvisiongatewayamqp.SanitizeOrganizationIDForQueueSuffix (ASCII alnum only)."""
    out: list[str] = []
    for ch in organization_id.strip():
        o = ord(ch)
        if (97 <= o <= 122) or (65 <= o <= 90) or (48 <= o <= 57) or ch in "-_.":
            out.append(ch)
        else:
            out.append("_")
    s = "".join(out)
    if len(s) > 200:
        s = s[:200]
    return s or "org"


def request_queue_for_organization_id(organization_id: str) -> str:
    oid = organization_id.strip()
    if not oid:
        raise ValueError("organization_id is required")
    return _REQUEST_QUEUE_PREFIX + "." + _sanitize_organization_id_for_queue_suffix(oid)


def _env_default_user() -> str:
    return str(os.environ.get("HIKVISION_DEVICE_USERNAME", "") or "").strip()


def _env_default_password() -> str:
    return str(os.environ.get("HIKVISION_DEVICE_PASSWORD", "") or "")


def _env_default_insecure() -> bool:
    v = os.environ.get("HIKVISION_ISAPI_INSECURE_SKIP_VERIFY", "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def _coerce_bool(v: object) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _reply(ch: pika.channel.Channel, reply_to: str, corr_id: str, body: Dict[str, Any]) -> None:
    props = pika.BasicProperties(correlation_id=corr_id, content_type="application/json")
    ch.basic_publish(
        exchange="",
        routing_key=reply_to,
        properties=props,
        body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
    )


def _handle(ch: pika.channel.Channel, delivery: Any, props: Any, raw: bytes) -> None:
    reply_to = props.reply_to
    corr_id = props.correlation_id or ""
    if not reply_to or not corr_id:
        ch.basic_nack(delivery_tag=delivery.delivery_tag, requeue=False)
        return
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        _reply(ch, reply_to, corr_id, {"v": 1, "id": corr_id, "ok": False, "error": f"bad json: {exc}"})
        ch.basic_ack(delivery_tag=delivery.delivery_tag)
        return

    rid = str(req.get("id") or corr_id)
    action = str(req.get("action") or "").lower()
    if _AGENT_ORG_ID:
        msg_org = str(req.get("organization_id") or "").strip()
        if msg_org and msg_org != _AGENT_ORG_ID:
            logger.warning(
                "RPC organization_id=%r does not match agent HIKVISION_GATEWAY_ORG_ID=%r",
                msg_org,
                _AGENT_ORG_ID,
            )

    try:
        if action == "ping":
            out: Dict[str, Any] = {
                "v": 1,
                "id": rid,
                "ok": True,
                "payload": {"service": "hikvision-lan-agent", "transport": "rabbitmq"},
            }
        elif action == "devices":
            with sadp_core.state_lock:
                devices = [d.to_dict() for d in sadp_core.state.devices]
                scanning = sadp_core.state.scanning
                last = sadp_core.state.last_scan_at.isoformat() if sadp_core.state.last_scan_at else None
                scan_err = sadp_core.state.scan_error
            out = {
                "v": 1,
                "id": rid,
                "ok": True,
                "payload": {
                    "count": len(devices),
                    "scanning": scanning,
                    "last_scan_at": last,
                    "scan_error": scan_err,
                    "devices": devices,
                },
            }
        elif action == "discover":
            p = req.get("discover") or {}
            tmo = float(p.get("timeout") or 3.0)
            tmo = max(1.0, min(tmo, 30.0))
            devices: list[sadp_core.SADPDevice] = []
            with _discover_lock:
                with sadp_core.state_lock:
                    sadp_core.state.scanning = True
                    sadp_core.state.scan_error = None
                try:
                    devices = sadp_core.discover_devices_sync(tmo, None)
                    sadp_core.state.update(devices)
                except Exception as scan_exc:  # noqa: BLE001
                    with sadp_core.state_lock:
                        sadp_core.state.scan_error = str(scan_exc)
                    raise
                finally:
                    with sadp_core.state_lock:
                        sadp_core.state.scanning = False
            out = {
                "v": 1,
                "id": rid,
                "ok": True,
                "payload": {"count": len(devices), "devices": [d.to_dict() for d in devices]},
            }
        elif action == "isapi":
            p = req.get("isapi") or {}
            host = str(p.get("device_host") or "").strip()
            port = int(p.get("device_port") or 443)
            http_method = str(p.get("method") or "GET").upper()
            path = str(p.get("path") or "").strip()
            msg_user = str(p.get("username") or "").strip()
            msg_pass = str(p.get("password") or "")
            if msg_user:
                username = msg_user
                password = msg_pass or _env_default_password()
            else:
                username = _env_default_user()
                password = _env_default_password()
            # Go JSON often includes insecure_skip_verify:false explicitly, which would ignore .env.
            # Skip TLS verify if either agent env or the RPC message requests it.
            insecure = _env_default_insecure() or _coerce_bool(p.get("insecure_skip_verify"))
            ts = float(p.get("timeout_seconds") or 45.0)
            raw_body = None
            b64in = p.get("body_b64")
            if b64in:
                raw_body = base64.b64decode(str(b64in), validate=True)
            st, data, ct = forward_isapi_to_device(
                host, port, http_method, path, raw_body, username, password, insecure, ts
            )
            out = {
                "v": 1,
                "id": rid,
                "ok": True,
                "status_code": st,
                "content_type": ct,
                "body_b64": base64.b64encode(data or b"").decode("ascii"),
            }
        else:
            out = {"v": 1, "id": rid, "ok": False, "error": f"unknown action: {action}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("rabbit request failed")
        out = {"v": 1, "id": rid, "ok": False, "error": str(exc)}

    _reply(ch, reply_to, rid, out)
    ch.basic_ack(delivery_tag=delivery.delivery_tag)


def run_forever(amqp_url: str, request_queue: str, agent_organization_id: str) -> None:
    """Blocking reconnect loop; intended as main process entry."""
    global _AGENT_ORG_ID
    _AGENT_ORG_ID = agent_organization_id.strip()
    rq = request_queue.strip()
    if not rq:
        raise ValueError("request_queue is required")
    while True:
        try:
            conn = pika.BlockingConnection(connection_parameters(amqp_url))
            ch = conn.channel()
            ch.queue_declare(queue=rq, durable=True)
            ch.basic_qos(prefetch_count=1)

            def cb(ch_: pika.channel.Channel, delivery: Any, props: Any, body_b: bytes) -> None:
                _handle(ch_, delivery, props, body_b)

            ch.basic_consume(queue=rq, on_message_callback=cb, auto_ack=False)
            logger.info(
                "RabbitMQ consumer organization_id=%r listening on queue=%r (Hikvision LAN agent)",
                _AGENT_ORG_ID,
                rq,
            )
            ch.start_consuming()
        except KeyboardInterrupt:
            logger.info("Shutdown requested.")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("RabbitMQ consumer error, reconnect in 3s: %s", exc)
            time.sleep(3)
