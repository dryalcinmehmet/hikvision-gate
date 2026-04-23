"""
SADP (UDP 37020) keşfi ve bellek içi cihaz önbelleği — HTTP sunucusu yok.
RabbitMQ tüketicisi bu modülü kullanır.
"""

from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger("hikvision-lan-agent.sadp")

STARTUP_TIMEOUT = float(os.environ.get("HIK_SADP_STARTUP_TIMEOUT", "3.0"))

SADP_UDP_PORT = 37020
SADP_MULTICAST = "239.255.255.250"
SADP_BROADCAST = "255.255.255.255"

SADP_PROBE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<Probe><Uuid>{uuid}</Uuid><Types>inquiry</Types></Probe>"
)


@dataclass
class SADPDevice:
    ip: str
    mac: str
    serial_number: str
    device_type: str
    device_description: str
    http_port: int = 80
    https_port: int = 443
    firmware_version: Optional[str] = None
    software_version: Optional[str] = None
    activated: bool = False
    dhcp_enabled: bool = False
    subnet_mask: Optional[str] = None
    gateway: Optional[str] = None
    ipv6_address: Optional[str] = None
    discovered_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get_broadcast_addresses() -> set[str]:
    broadcasts: set[str] = set()
    try:
        import netifaces  # type: ignore[import]

        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    bcast = addr.get("broadcast")
                    if bcast and bcast != "127.255.255.255":
                        broadcasts.add(bcast)
    except ImportError:
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            parts = local_ip.rsplit(".", 1)
            if len(parts) == 2:
                broadcasts.add(f"{parts[0]}.255")
        except OSError:
            pass
    return broadcasts


def _parse_sadp_response(data: bytes, src_ip: str) -> Optional[SADPDevice]:
    try:
        text = data.decode("utf-8", errors="replace").strip()
        if not text.startswith("<"):
            return None

        root = ET.fromstring(text)
        ns = ""
        if "}" in root.tag:
            ns = root.tag[: root.tag.index("}") + 1]

        def _find(path: str) -> Optional[str]:
            el = root.find(f"{ns}{path}")
            return el.text.strip() if el is not None and el.text else None

        serial = _find("DeviceSN") or _find("SerialNumber") or ""
        if not serial:
            return None

        ip_addr = (_find("IPv4Address") or _find("IPAddress") or src_ip).split("/")[0].strip()
        mac = _find("MAC") or _find("MacAddress") or ""
        device_type = _find("DeviceType") or ""
        description = _find("DeviceDescription") or device_type

        http_port = 80
        try:
            http_port = int(_find("HttpPort") or "80")
        except (ValueError, TypeError):
            pass

        https_port = 443
        try:
            https_port = int(_find("HttpsPort") or "443")
        except (ValueError, TypeError):
            pass

        activated = (_find("Activated") or "").lower() in ("true", "1", "yes")
        dhcp = (_find("DHCP") or "").lower() in ("true", "1", "yes")

        return SADPDevice(
            ip=ip_addr,
            mac=mac,
            serial_number=serial,
            device_type=device_type,
            device_description=description,
            http_port=http_port,
            https_port=https_port,
            firmware_version=_find("FirmwareVersion") or _find("Firmware"),
            software_version=_find("SoftwareVersion"),
            activated=activated,
            dhcp_enabled=dhcp,
            subnet_mask=_find("IPv4SubnetMask") or _find("SubnetMask"),
            gateway=_find("IPv4Gateway") or _find("Gateway"),
            ipv6_address=_find("IPv6Address"),
        )
    except ET.ParseError as exc:
        logger.debug("SADP parse hatası (%s): %s", src_ip, exc)
        return None
    except Exception as exc:
        logger.warning("SADP yanıtı işlenemedi (%s): %s", src_ip, exc)
        return None


def discover_devices_sync(timeout: float = 3.0, stop_event: Optional[threading.Event] = None) -> list[SADPDevice]:
    """
    Yerel ağdaki Hikvision cihazlarını SADP ile bulur (blocking).
    """
    probe = SADP_PROBE_XML.format(uuid=str(uuid4()).upper()).encode("utf-8")
    devices: dict[str, SADPDevice] = {}

    broadcast_targets = _get_broadcast_addresses()
    broadcast_targets.add(SADP_BROADCAST)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.5)

    try:
        sock.bind(("", 0))

        try:
            mreq = struct.pack("4sL", socket.inet_aton(SADP_MULTICAST), socket.INADDR_ANY)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.sendto(probe, (SADP_MULTICAST, SADP_UDP_PORT))
            logger.debug("SADP multicast probe → %s:%d", SADP_MULTICAST, SADP_UDP_PORT)
        except OSError as exc:
            logger.debug("Multicast gönderilemedi: %s", exc)

        for target in broadcast_targets:
            try:
                sock.sendto(probe, (target, SADP_UDP_PORT))
                logger.debug("SADP broadcast probe → %s:%d", target, SADP_UDP_PORT)
            except OSError as exc:
                logger.warning("Broadcast gönderilemedi (%s): %s", target, exc)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop_event and stop_event.is_set():
                logger.debug("Tarama stop_event ile iptal edildi.")
                break
            try:
                data, (src_ip, _) = sock.recvfrom(65535)
                device = _parse_sadp_response(data, src_ip)
                if device and device.serial_number not in devices:
                    devices[device.serial_number] = device
                    logger.info(
                        "Cihaz bulundu: %s  tip=%-20s  SN=%s  aktif=%s",
                        device.ip,
                        device.device_type,
                        device.serial_number,
                        device.activated,
                    )
            except socket.timeout:
                pass
            except OSError:
                break
    finally:
        sock.close()

    result = list(devices.values())
    logger.info("Tarama tamamlandı — %d cihaz bulundu.", len(result))
    return result


state_lock = threading.RLock()


class _State:
    devices: list[SADPDevice] = []
    last_scan_at: Optional[datetime] = None
    scanning: bool = False
    scan_error: Optional[str] = None

    def update(self, devices: list[SADPDevice]) -> None:
        with state_lock:
            self.devices = devices
            self.last_scan_at = datetime.utcnow()
            self.scan_error = None

    def clear(self) -> None:
        with state_lock:
            self.devices = []
            self.last_scan_at = None
            self.scan_error = None


state = _State()
