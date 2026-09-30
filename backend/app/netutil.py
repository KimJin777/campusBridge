"""실제 접속 IP 판별(채팅·사용 기록·제보 공용) — GPT5 #722·#746.

Cloud Run 앞단(구글 프런트엔드)은 실제 접속 IP를 X-Forwarded-For **오른쪽**에 붙인다.
앞쪽 값은 사용자가 꾸밀 수 있으므로, 오른쪽부터 구글 프록시·사설망을 건너뛴 첫 공인 주소를 쓴다.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request

# 구글 프런트엔드·부하분산 프록시 대역(이 주소는 사용자가 아님)
GOOGLE_PROXIES = tuple(ipaddress.ip_network(n) for n in ("35.191.0.0/16", "130.211.0.0/22"))


def _trusted_proxy(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or any(ip in net for net in GOOGLE_PROXIES if ip.version == net.version)
    )


def client_ip_from(xff: str | None, peer: str | None) -> str:
    """'client, google-proxy' → client, '위조값, client' → client."""
    for part in reversed([p.strip() for p in (xff or "").split(",") if p.strip()]):
        try:
            ip = ipaddress.ip_address(part)
        except ValueError:
            continue
        if not _trusted_proxy(ip):
            return str(ip)
    return peer or "unknown"


def request_ip(request: Request) -> str:
    return client_ip_from(
        request.headers.get("x-forwarded-for"), request.client.host if request.client else None
    )
