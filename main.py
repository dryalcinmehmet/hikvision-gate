"""
Hikvision LAN agent — yalnızca RabbitMQ (HTTP sunucusu yok).

Go backend `HIKVISION_GATEWAY_AMQP_URL` ile aynı broker'a, bu kuruluş için
`hikvision.gateway.requests.<HIKVISION_GATEWAY_ORG_ID>` kuyruğuna RPC yazar;
bu süreç LAN'da çalışır, SADP ile tarama yapar ve ISAPI'yi cihazlara iletir.

Çalıştırma:
  cd cmd/hikvision-sadp-proxy
  cp .env.example .env
  pip install -r requirements.txt
  python main.py

Ortam: `HIKVISION_GATEWAY_AMQP_URL` ve kuruluş kimliği `HIKVISION_GATEWAY_ORG_ID` zorunlu
(Go API'deki organization UUID string ile aynı olmalı; kuyruk adı buna göre üretilir).
`amqps://` = TLS, bkz. `amqp_tls.py` ve `docs/RABBIT_HIKVISION_GATEWAY_KILAVUZU.md` §9. Cihaz Digest için `HIKVISION_DEVICE_USERNAME` /
`HIKVISION_DEVICE_PASSWORD` (Go mesajında user/pass yoksa veya şifre boşsa kullanılır).

UDP 37020 (SADP) için macOS/Linux'ta bazen sudo gerekir; cihazlarla aynı L2 segmentinde çalıştırın.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass

logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hikvision-lan-agent")


def main() -> None:
    amqp_url = os.environ.get("HIKVISION_GATEWAY_AMQP_URL", "").strip()
    if not amqp_url:
        logger.error("HIKVISION_GATEWAY_AMQP_URL is required (RabbitMQ connection string).")
        sys.exit(1)

    org_id = os.environ.get("HIKVISION_GATEWAY_ORG_ID", "").strip()
    if not org_id:
        logger.error(
            "HIKVISION_GATEWAY_ORG_ID is required — organization this agent serves "
            "(same string as Go API/JWT organization id, typically a UUID)."
        )
        sys.exit(1)

    import sadp_core
    from rabbit_consumer import request_queue_for_organization_id, run_forever

    try:
        request_queue = request_queue_for_organization_id(org_id)
    except ValueError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  Hikvision LAN agent (RabbitMQ only, no HTTP)")
    logger.info("  organization_id=%s", org_id)
    logger.info("  request_queue=%s", request_queue)
    logger.info("  Startup SADP scan: %.1f s", sadp_core.STARTUP_TIMEOUT)
    logger.info("=" * 60)
    try:
        devices = sadp_core.discover_devices_sync(sadp_core.STARTUP_TIMEOUT, None)
        sadp_core.state.update(devices)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup SADP scan failed (continuing): %s", exc)

    run_forever(amqp_url, request_queue, org_id)


if __name__ == "__main__":
    main()
