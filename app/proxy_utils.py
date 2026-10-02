from urllib.parse import urlparse


def has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def safe_proxy_label(proxy_url: str | None) -> str:
    if not proxy_url:
        return "-"
    try:
        parsed = urlparse(proxy_url.strip())
        host = parsed.hostname
        port = parsed.port
        scheme = (parsed.scheme or "").lower()
        if host and port and scheme:
            return f"{scheme}://{host}:{port}"
    except Exception:
        pass
    return "<redacted>"
