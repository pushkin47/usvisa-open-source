import argparse
import json
import os
import re
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from config_store import load_config_to_env


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROXY_LISTEN = "127.0.0.1"
DEFAULT_PROXY_PORT = 2080
DEFAULT_PROXY_COUNT = 2


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_filters(value: str) -> list[str]:
    return [item.strip().casefold() for item in value.split(",") if item.strip()]


def _safe_tag(value: str, index: int) -> str:
    cleaned = re.sub(r"\s+", "-", value.strip().casefold())
    cleaned = re.sub(r"[^a-z0-9а-яё_-]+", "", cleaned)
    return f"proxy-{index}-{cleaned or 'node'}"


def _fetch_subscription(url: str, hwid: str) -> list[dict[str, Any]]:
    request = Request(
        url,
        headers={
            "User-Agent": os.getenv("USVISA_VPN_USER_AGENT", "Happ/1.0"),
            "x-hwid": hwid,
            "x-device-os": os.getenv("USVISA_VPN_DEVICE_OS", "Linux"),
            "x-device-model": os.getenv("USVISA_VPN_DEVICE_MODEL", "USVisa Monitor VPS"),
        },
    )
    context = None
    if not _truthy(os.getenv("USVISA_VPN_SUBSCRIPTION_SSL_VERIFY", "true")):
        context = ssl._create_unverified_context()
    with urlopen(
        request,
        timeout=int(os.getenv("USVISA_VPN_SUBSCRIPTION_TIMEOUT_SECONDS", "20")),
        context=context,
    ) as response:
        payload = response.read().decode("utf-8")

    data = json.loads(payload)
    if not isinstance(data, list):
        raise RuntimeError("VPN subscription did not return a JSON profile list")
    return [item for item in data if isinstance(item, dict)]


def _profile_allowed(remarks: str, include_filters: list[str], exclude_filters: list[str]) -> bool:
    normalized = remarks.casefold()
    if include_filters and not any(item in normalized for item in include_filters):
        return False
    if exclude_filters and any(item in normalized for item in exclude_filters):
        return False
    return True


def _vless_outbound(profile: dict[str, Any], index: int) -> dict[str, Any] | None:
    remarks = str(profile.get("remarks") or f"node-{index}")
    outbounds = profile.get("outbounds")
    if not isinstance(outbounds, list) or not outbounds:
        return None

    outbound = outbounds[0]
    if not isinstance(outbound, dict) or outbound.get("protocol") != "vless":
        return None

    settings = outbound.get("settings")
    stream_settings = outbound.get("streamSettings")
    if not isinstance(settings, dict) or not isinstance(stream_settings, dict):
        return None

    vnext = settings.get("vnext")
    if not isinstance(vnext, list) or not vnext:
        return None
    server_config = vnext[0]
    if not isinstance(server_config, dict):
        return None

    users = server_config.get("users")
    if not isinstance(users, list) or not users:
        return None
    user = users[0]
    if not isinstance(user, dict):
        return None

    reality = stream_settings.get("realitySettings") or {}
    grpc = stream_settings.get("grpcSettings") or {}
    if not isinstance(reality, dict) or not isinstance(grpc, dict):
        return None

    address = str(server_config.get("address") or "").strip()
    uuid = str(user.get("id") or "").strip()
    public_key = str(reality.get("publicKey") or "").strip()
    if not address or not uuid or not public_key:
        return None

    try:
        port = int(server_config.get("port"))
    except (TypeError, ValueError):
        return None

    return {
        "type": "vless",
        "tag": _safe_tag(remarks, index),
        "server": address,
        "server_port": port,
        "uuid": uuid,
        "network": "tcp",
        "tls": {
            "enabled": True,
            "server_name": str(reality.get("serverName") or address),
            "utls": {
                "enabled": True,
                "fingerprint": str(reality.get("fingerprint") or "chrome"),
            },
            "reality": {
                "enabled": True,
                "public_key": public_key,
                "short_id": str(reality.get("shortId") or ""),
            },
        },
        "transport": {
            "type": "grpc",
            "service_name": str(grpc.get("serviceName") or "api"),
        },
    }


def build_config(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    include_filters = _split_filters(os.getenv("USVISA_VPN_LOCATION_FILTER", ""))
    exclude_filters = _split_filters(os.getenv("USVISA_VPN_EXCLUDE_FILTER", "Россия"))
    max_outbounds = max(1, int(os.getenv("USVISA_VPN_MAX_OUTBOUNDS", "12")))
    proxy_port = int(os.getenv("USVISA_VPN_PROXY_PORT", str(DEFAULT_PROXY_PORT)))
    proxy_count = max(1, int(os.getenv("USVISA_VPN_PROXY_COUNT", str(DEFAULT_PROXY_COUNT))))

    proxy_outbounds: list[dict[str, Any]] = []
    for profile in profiles:
        remarks = str(profile.get("remarks") or "")
        if not _profile_allowed(remarks, include_filters, exclude_filters):
            continue
        outbound = _vless_outbound(profile, len(proxy_outbounds) + 1)
        if outbound is not None:
            proxy_outbounds.append(outbound)
        if len(proxy_outbounds) >= max_outbounds:
            break

    if not proxy_outbounds and include_filters:
        os.environ["USVISA_VPN_LOCATION_FILTER"] = ""
        return build_config(profiles)

    if not proxy_outbounds:
        raise RuntimeError("No usable VLESS Reality nodes were found in VPN subscription")

    selector_tag = "auto"
    dedicated_outbounds = proxy_outbounds[:proxy_count]
    outbounds = proxy_outbounds + [
        {
            "type": "urltest",
            "tag": selector_tag,
            "outbounds": [outbound["tag"] for outbound in proxy_outbounds],
            "url": os.getenv("USVISA_VPN_HEALTHCHECK_URL", "https://www.gstatic.com/generate_204"),
            "interval": os.getenv("USVISA_VPN_HEALTHCHECK_INTERVAL", "3m"),
            "tolerance": int(os.getenv("USVISA_VPN_HEALTHCHECK_TOLERANCE_MS", "150")),
            "interrupt_exist_connections": _truthy(os.getenv("USVISA_VPN_INTERRUPT_EXISTING", "true")),
        },
        {"type": "direct", "tag": "direct"},
    ]
    inbounds = [
        {
            "type": "mixed",
            "tag": f"mixed-in-{index}",
            "listen": os.getenv("USVISA_VPN_PROXY_LISTEN", DEFAULT_PROXY_LISTEN),
            "listen_port": proxy_port + index - 1,
        }
        for index, _ in enumerate(dedicated_outbounds, start=1)
    ]
    route_rules = [
        {"inbound": [f"mixed-in-{index}"], "outbound": outbound["tag"]}
        for index, outbound in enumerate(dedicated_outbounds, start=1)
    ]

    return {
        "log": {"level": os.getenv("USVISA_VPN_LOG_LEVEL", "info"), "timestamp": True},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"auto_detect_interface": True, "rules": route_rules, "final": selector_tag},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build sing-box config from the USVisa VPN subscription.")
    parser.add_argument("--output", default=os.getenv("USVISA_VPN_SINGBOX_CONFIG", "/etc/sing-box/config.json"))
    parser.add_argument("--check", action="store_true", help="Validate generated config with sing-box check.")
    parser.add_argument("--restart", action="store_true", help="Restart sing-box after writing config.")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    load_config_to_env(override=True)

    subscription_url = os.getenv("USVISA_VPN_SUBSCRIPTION_URL", "").strip()
    hwid = os.getenv("USVISA_VPN_HWID", "").strip()
    if not subscription_url:
        raise RuntimeError("USVISA_VPN_SUBSCRIPTION_URL is not set")
    if not hwid:
        raise RuntimeError("USVISA_VPN_HWID is not set")

    profiles = _fetch_subscription(subscription_url, hwid)
    config = build_config(profiles)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    output.chmod(0o600)

    if args.check:
        subprocess.run(["sing-box", "check", "-c", str(output)], check=True)
    if args.restart:
        subprocess.run(["systemctl", "restart", "sing-box"], check=True)

    proxy_count = sum(1 for outbound in config["outbounds"] if outbound.get("type") == "vless")
    print(f"Wrote {output} with {proxy_count} VPN outbounds")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"VPN config update failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
