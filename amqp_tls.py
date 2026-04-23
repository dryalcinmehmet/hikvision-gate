"""
AMQP URI → pika ``URLParameters``; ``amqps://`` için TLS bağlamı ortam değişkenlerinden.

Ortam:
  HIKVISION_GATEWAY_AMQP_SSL_VERIFY        true/false (varsayılan: true)
  HIKVISION_GATEWAY_AMQP_SSL_CAFILE        CA PEM yolu (özel CA için)
  HIKVISION_GATEWAY_AMQP_SSL_RELAX_X509    true ise OpenSSL 3 / Py 3.13+ için STRICT+PARTIAL bayraklarını kapatır
                                           (CA’da keyUsage yokken “CA cert does not include key usage extension” hatasına);
                                           üretimde tercihen CA’yı openssl ile keyUsage ile yeniden üretin.
"""

from __future__ import annotations

import logging
import os
import ssl
from urllib.parse import urlparse

import pika
from pika import SSLOptions

logger = logging.getLogger("hikvision-lan-agent.amqp_tls")


def _ssl_verify_enabled() -> bool:
    v = os.environ.get("HIKVISION_GATEWAY_AMQP_SSL_VERIFY", "true").strip().lower()
    return v not in ("0", "false", "no", "off")


def _ssl_cafile() -> str | None:
    p = os.environ.get("HIKVISION_GATEWAY_AMQP_SSL_CAFILE", "").strip()
    return p or None


def _relax_x509_for_private_ca() -> bool:
    v = os.environ.get("HIKVISION_GATEWAY_AMQP_SSL_RELAX_X509", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _strip_strict_x509(ctx: ssl.SSLContext) -> None:
    """OpenSSL 3: minimal self-signed CA PEMs often lack keyUsage; default verify_flags then fail at handshake."""
    for name in ("VERIFY_X509_STRICT", "VERIFY_X509_PARTIAL_CHAIN"):
        bit = getattr(ssl, name, 0)
        if bit:
            ctx.verify_flags &= ~bit


def connection_parameters(url: str) -> pika.URLParameters:
    """
    ``amqp://`` için doğrudan ``URLParameters``;
    ``amqps://`` için ``SSLOptions`` eklenir (sertifika doğrulama .env ile).
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("empty AMQP URL")

    parsed = urlparse(url)
    scheme = (parsed.scheme or "amqp").lower()

    if scheme != "amqps":
        return pika.URLParameters(url)

    params = pika.URLParameters(url)
    verify = _ssl_verify_enabled()
    cafile = _ssl_cafile()

    if verify:
        if cafile:
            ctx = ssl.create_default_context(cafile=cafile)
            if _relax_x509_for_private_ca():
                _strip_strict_x509(ctx)
                logger.warning(
                    "AMQP TLS: HIKVISION_GATEWAY_AMQP_SSL_RELAX_X509=true — "
                    "X509 STRICT/PARTIAL relaxed; for production regenerate CA with keyUsage (keyCertSign, cRLSign)."
                )
        else:
            ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if cafile:
            ctx.load_verify_locations(cafile=cafile)

    host = params.host or parsed.hostname
    if not host:
        raise ValueError("amqps URL must include a host")

    params.ssl_options = SSLOptions(ctx, server_hostname=host)
    logger.info(
        "AMQP TLS (amqps) host=%s port=%s verify=%s cafile=%s",
        host,
        params.port,
        verify,
        cafile or "-",
    )
    return params
