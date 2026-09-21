"""
Violetics Solver - Turnstile + CF JS-Challenge HTTP service (aiohttp).
"""

import asyncio
import collections
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import time
import uuid
from urllib.parse import urlparse

from aiohttp import web

from . import db
from .solver import (get_pool, solve_async, solve_challenge_async,
                     solve_recaptcha_v3_async, solve_aws_token_async,
                     _challenge_proxy)


PORT = int(os.environ.get("PORT", 9988))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 8))
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 64 * 1024))  # 64 KB
# API key gate. Empty -> auth disabled (dev). Set API_KEY to require the
# X-API-Key header (or ?api_key=) on /solve and /solve-challenge.
API_KEY = os.environ.get("API_KEY", "").strip()

# Anti-abuse (per client IP). 0 disables each check.
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", 10))   # requests/min/IP
MAX_CONCURRENT_PER_IP = int(os.environ.get("MAX_CONCURRENT_PER_IP", 5))  # in-flight/IP
# Exceeding the rate limit bans the IP for this long (seconds; default 1 day).
BAN_SECONDS = int(os.environ.get("BAN_SECONDS", 86400))
# Dev escape hatch: let siteurl point at loopback / private / link-local
# addresses (blocked by default to stop SSRF into the docker network).
ALLOW_PRIVATE_TARGETS = os.environ.get("ALLOW_PRIVATE_TARGETS", "").lower() in ("1", "true", "yes")

log = logging.getLogger("service")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(_ROOT, "web")
TEMPLATE_DIR = os.path.join(WEB_DIR, "templates")
STATIC_DIR = os.path.join(WEB_DIR, "static")

_PROCESS_STARTED = time.time()


_stats = {"in_flight": 0, "solved": 0, "errors": 0, "challenges": 0}
# Only these survive a restart; in_flight is live state.
_PERSISTED_STATS = ("solved", "errors", "challenges")

# Recent events for the playground's live stats panel. Capped ring buffer so
# an idle process does not accumulate memory. Each entry: {ts, endpoint,
# status, duration, url, summary}.
_events: "collections.deque[dict]" = collections.deque(maxlen=50)
_latency_ms: "collections.deque[float]" = collections.deque(maxlen=100)


def _rid() -> str:
    return uuid.uuid4().hex[:8]


def _classify_error(exc: BaseException) -> tuple[str, str, int]:
    """Map an exception to (error_code, public_message, http_status).

    Hides Playwright/Camoufox internal stack traces from clients while
    keeping enough signal that callers can branch on the result.
    """
    msg = str(exc).strip()
    low = msg.lower()
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in low \
            or "did not respond within" in low:
        # Don't leak Playwright stack text (e.g. "Page.evaluate: ...") — the
        # full detail still goes to the server log via log.exception.
        return "timeout", "solve timeout", 504
    if any(h in low for h in ("connection closed", "browser has been closed",
                               "context has been closed", "net::err",
                               "ns_error", "navigation timeout")):
        return "browser_error", "browser unavailable, please retry", 503
    if "invalid url" in low or "invalid_url" in low:
        return "bad_request", "invalid siteurl", 400
    if isinstance(exc, ValueError):
        return "bad_request", msg or "bad request", 400
    return "solver_error", "internal solver error", 500


def _summary(body: dict) -> str:
    if "error" in body:
        return f"error: {body['error']}"
    if "token" in body:
        t = body["token"]
        return f"token {t[:12]}...{t[-6:]} ({len(t)} chars)"
    if "title" in body:
        parts = [f"title={body.get('title')!r}"]
        if "cookies" in body:
            parts.append(f"cookies={len(body['cookies'])}")
        if "html" in body:
            parts.append(f"html={len(body['html'])}b")
        return " ".join(parts)
    return "ok"


def _record_event(endpoint: str, status: int, duration: float, url: str, body: dict):
    _events.appendleft({
        "ts": time.time(),
        "endpoint": endpoint,
        "status": status,
        "duration": round(duration, 3),
        "url": url,
        "summary": _summary(body)[:180],
    })
    _latency_ms.append(duration * 1000)


# ANSI colors, disabled when NO_COLOR is set or output isn't a TTY.
_USE_COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


# Per-request context stashed at start, read + cleared at end, so the single
# completion line can show endpoint / origin / target without threading extra
# args through every call site.
_req_ctx: "dict[str, dict]" = {}


def _short_ep(path: str) -> str:
    return {"/solve": "turnstile", "/solve-challenge": "challenge",
            "/recaptcha-v3": "recaptcha", "/aws-token": "aws-waf"}.get(path, path)


def _emit_start(rid: str, method: str, path: str, url: str, key: str, peer: str):
    _req_ctx[rid] = {"ep": _short_ep(path), "url": url or "-",
                     "key": key or "", "from": peer}
    if log.isEnabledFor(logging.DEBUG):
        log.debug("start %s %s %s url=%s key=%s from=%s",
                  rid, method, path, url or "-",
                  (key[:14] + "…") if len(key) > 14 else (key or "-"), peer)


def _emit_end(rid: str, elapsed: float, status: int, body: dict):
    ctx = _req_ctx.pop(rid, {})
    ok = 200 <= status < 400
    icon = _c("32", "✓") if ok else _c("31", "✗")
    stat = _c("32" if ok else "31", str(status))
    dur = _c("33", f"{elapsed:6.2f}s")
    ep = _c("36", f"{ctx.get('ep', '-'):9}")          # cyan endpoint
    origin = _c("90", ctx.get("from", "-"))            # dim client IP
    url = ctx.get("url", "-")
    key = ctx.get("key", "")
    tail = f" key={key[:10]}…" if len(key) > 10 else (f" key={key}" if key else "")
    # ✓ 1a2b3c4d turnstile  200   3.21s  from=1.2.3.4  https://site/  key=0x4AAA…  → token …
    print(f"{icon} {rid} {ep} {stat} {dur}  {origin}  {url}{tail}  → {_summary(body)}",
          flush=True)


# ---------- Input validation (trust boundary) ----------

# Turnstile keys look like 0x4AAAAAAA..., reCAPTCHA keys like 6Lc...; both are
# URL-safe base64-ish. Also what ends up in a data-* attribute of the host
# page, so keep it tight.
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# Turnstile action / cdata: alphanumeric, _ and -, <=255 chars (CF's own rule).
_ATTR_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
# reCAPTCHA v3 action: alphanumeric, slash, underscore.
_RC_ACTION_RE = re.compile(r"^[A-Za-z0-9_/]{1,64}$")


def _field(payload: dict, name: str, pattern: "re.Pattern", required: bool = False) -> str:
    v = payload.get(name)
    if v is None or v == "":
        if required:
            raise ValueError(f"{name} required")
        return ""
    if not isinstance(v, str):
        raise ValueError(f"{name} must be a string")
    v = v.strip()
    if not pattern.match(v):
        raise ValueError(f"invalid {name}")
    return v


def _timeout(payload: dict) -> int:
    try:
        return max(5, min(180, int(payload.get("timeout", 45))))
    except (TypeError, ValueError):
        return 45


def _is_private_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _validate_siteurl(siteurl: str) -> None:
    """Guard against empty / non-http(s) / no-host URLs, and against targets
    that resolve into loopback / private / link-local space (SSRF into the
    docker network via the headless browser or Byparr)."""
    if not siteurl:
        raise ValueError("siteurl required")
    if len(siteurl) > 2048:
        raise ValueError("siteurl too long")
    try:
        u = urlparse(siteurl)
    except Exception:
        raise ValueError("invalid siteurl")
    if u.scheme not in ("http", "https"):
        raise ValueError("siteurl scheme must be http or https")
    host = u.hostname
    if not host:
        raise ValueError("siteurl missing host")
    if ALLOW_PRIVATE_TARGETS:
        return
    if host == "localhost" or host.endswith(".localhost") or _is_private_ip(host):
        raise ValueError("siteurl host not allowed")
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("siteurl host does not resolve")
    for info in infos:
        if _is_private_ip(info[4][0]):
            raise ValueError("siteurl host not allowed")


# Paths reachable without a key even when API_KEY is set. /health stays open
# for container/uptime probes.
_PUBLIC_PATHS = frozenset({"/health"})


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Require the API key on every path except _PUBLIC_PATHS. No-op when
    API_KEY is unset (dev). Key accepted via X-API-Key header or ?api_key=
    so the browser playground can pass it in the URL."""
    if API_KEY and request.path not in _PUBLIC_PATHS:
        given = request.headers.get("X-API-Key") or request.query.get("api_key") or ""
        if not hmac.compare_digest(given.encode(), API_KEY.encode()):
            return web.json_response(
                {"error": "unauthorized", "error_code": "unauthorized"}, status=401)
    return await handler(request)


# Solve endpoints are the expensive ones worth protecting from abuse.
_SOLVE_PATHS = frozenset({"/solve", "/solve-challenge", "/recaptcha-v3", "/aws-token"})

# Per-IP state: sliding-window request timestamps, in-flight count, ban expiry.
_ip_hits: "dict[str, collections.deque[float]]" = collections.defaultdict(collections.deque)
_ip_inflight: "dict[str, int]" = collections.defaultdict(int)
_ip_banned: "dict[str, float]" = {}  # ip -> unix ts when the ban lifts
# In-flight asyncio tasks per IP, so a ban can cancel everything that IP has
# running (not just reject new requests).
_ip_tasks: "dict[str, set]" = collections.defaultdict(set)


def _kill_ip_tasks(ip: str) -> int:
    """Cancel every in-flight request task from an IP. Returns how many."""
    tasks = _ip_tasks.get(ip)
    if not tasks:
        return 0
    n = 0
    for t in list(tasks):
        if not t.done():
            t.cancel()
            n += 1
    return n


def _client_ip(request: web.Request) -> str:
    """Real client IP. X-Forwarded-For is only honoured when the direct peer
    is a private/loopback address (our reverse proxy or docker network); a
    public peer hitting the port directly could otherwise spoof it to dodge
    bans or ban someone else."""
    remote = request.remote or "-"
    xff = request.headers.get("X-Forwarded-For")
    if xff and _is_private_ip(remote):
        return xff.split(",")[0].strip() or remote
    return remote


@web.middleware
async def ratelimit_middleware(request: web.Request, handler):
    """Per-IP sliding-window rate limit + concurrent-solve cap on the solve
    endpoints. In-memory; both checks skipped when their env var is 0."""
    if request.path not in _SOLVE_PATHS:
        return await handler(request)

    ip = _client_ip(request)
    now = time.time()

    # Already banned? Reject until the ban lifts.
    ban_until = _ip_banned.get(ip)
    if ban_until:
        if now < ban_until:
            retry = int(ban_until - now)
            return web.json_response(
                {"error": "temporarily banned for abuse", "error_code": "banned",
                 "retry_after": retry},
                status=429, headers={"Retry-After": str(retry)})
        _ip_banned.pop(ip, None)  # expired
        db.clear_ban(ip)

    if RATE_LIMIT_PER_MIN > 0:
        hits = _ip_hits[ip]
        cutoff = now - 60
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_PER_MIN:
            # Over the limit -> ban the IP for BAN_SECONDS (persisted so it
            # survives a restart) AND cancel everything it has in flight.
            until = now + BAN_SECONDS
            _ip_banned[ip] = until
            db.save_ban(ip, until)
            _ip_hits.pop(ip, None)
            killed = _kill_ip_tasks(ip)
            log.warning("banned ip=%s for %ds (>%d req/min), killed %d in-flight",
                        ip, BAN_SECONDS, RATE_LIMIT_PER_MIN, killed)
            return web.json_response(
                {"error": "temporarily banned for abuse", "error_code": "banned",
                 "retry_after": BAN_SECONDS},
                status=429, headers={"Retry-After": str(BAN_SECONDS)})

    if MAX_CONCURRENT_PER_IP > 0 and _ip_inflight[ip] >= MAX_CONCURRENT_PER_IP:
        log.warning("concurrency cap hit ip=%s (%d)", ip, MAX_CONCURRENT_PER_IP)
        return web.json_response(
            {"error": "too many concurrent requests", "error_code": "too_many_concurrent"},
            status=429)

    # Count only admitted requests, so a client backing off after a
    # too_many_concurrent 429 is not marched into a day-long ban.
    if RATE_LIMIT_PER_MIN > 0:
        _ip_hits[ip].append(now)

    _ip_inflight[ip] += 1
    # Run the handler as a tracked task so a mid-flight ban can cancel it.
    task = asyncio.ensure_future(handler(request))
    _ip_tasks[ip].add(task)
    try:
        return await task
    except asyncio.CancelledError:
        # Cancelled by a ban on this IP (not a client disconnect during a
        # normal request — those don't register bans).
        if _ip_banned.get(ip, 0) > time.time():
            return web.json_response(
                {"error": "request killed: IP banned for abuse",
                 "error_code": "banned"}, status=429)
        raise
    finally:
        _ip_tasks[ip].discard(task)
        if not _ip_tasks[ip]:
            _ip_tasks.pop(ip, None)
        _ip_inflight[ip] -= 1
        if _ip_inflight[ip] <= 0:
            _ip_inflight.pop(ip, None)


async def _read_payload(request: web.Request) -> dict:
    """Bounded-size JSON object body. Raises ValueError on bad input.
    aiohttp enforces client_max_size (=MAX_BODY_BYTES) inside read()."""
    try:
        raw = await request.read()
    except web.HTTPRequestEntityTooLarge:
        raise ValueError("request body too large")
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("invalid JSON")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


# ---------- Solve endpoints ----------
# Each endpoint parses its payload into (siteurl, key, coroutine factory); the
# shared _handle() does validation, stats, logging, timeout and error mapping.

def _parse_solve(p: dict):
    sitekey = _field(p, "sitekey", _KEY_RE, required=True)
    siteurl = (p.get("siteurl") or "")
    action = _field(p, "action", _ATTR_RE) or None
    cdata = _field(p, "cdata", _ATTR_RE) or None

    def run(rid, timeout):
        return solve_async(sitekey, siteurl, req_id=rid, timeout=timeout,
                           action=action, cdata=cdata)
    return siteurl, sitekey, run


def _parse_challenge(p: dict):
    siteurl = (p.get("siteurl") or "")

    def run(rid, timeout):
        return solve_challenge_async(siteurl, req_id=rid, timeout=timeout)
    return siteurl, "", run


def _parse_recaptcha(p: dict):
    sitekey = _field(p, "sitekey", _KEY_RE, required=True)
    siteurl = (p.get("siteurl") or "")
    action = _field(p, "action", _RC_ACTION_RE) or "verify"

    def run(rid, timeout):
        return solve_recaptcha_v3_async(sitekey, siteurl, req_id=rid,
                                        timeout=timeout, action=action)
    return siteurl, sitekey, run


def _parse_aws(p: dict):
    siteurl = (p.get("siteurl") or "")

    def run(rid, timeout):
        return solve_aws_token_async(siteurl, req_id=rid, timeout=timeout)
    return siteurl, "", run


# path -> (stats counter bumped on success, payload parser)
_ENDPOINTS = {
    "/solve": ("solved", _parse_solve),
    "/solve-challenge": ("challenges", _parse_challenge),
    "/recaptcha-v3": ("solved", _parse_recaptcha),
    "/aws-token": ("challenges", _parse_aws),
}


async def handle_solve(request: web.Request) -> web.Response:
    rid = _rid()
    t0 = time.time()
    path = request.path
    peer = _client_ip(request)
    counter, parse = _ENDPOINTS[path]
    siteurl = ""

    def fail(status: int, body: dict) -> web.Response:
        elapsed = time.time() - t0
        _emit_end(rid, elapsed, status, body)
        _record_event(path, status, elapsed, siteurl, body)
        return web.json_response(body, status=status)

    try:
        payload = await _read_payload(request)
        siteurl_raw = payload.get("siteurl")
        if siteurl_raw is not None and not isinstance(siteurl_raw, str):
            raise ValueError("siteurl must be a string")
        payload["siteurl"] = (siteurl_raw or "").strip()
        siteurl, key, run = parse(payload)
        _emit_start(rid, request.method, path, siteurl, key, peer)
        await _validate_siteurl(siteurl)
    except ValueError as ve:
        if rid not in _req_ctx:
            _emit_start(rid, request.method, path, siteurl, "", peer)
        return fail(400, {"error": str(ve), "error_code": "bad_request"})

    timeout = _timeout(payload)
    _stats["in_flight"] += 1
    try:
        # Hard wall-clock cap on the whole solve, including queueing behind
        # the per-key lock / semaphore and the initial navigation. Without it
        # the client gives up while the server keeps a worker slot busy.
        result = await asyncio.wait_for(run(rid, timeout), timeout + 15)
        elapsed = time.time() - t0
        _stats[counter] += 1
        body = result if isinstance(result, dict) else {"token": result}
        body = {**body, "elapsed": round(elapsed, 2)}
        _emit_end(rid, elapsed, 200, body)
        _record_event(path, 200, elapsed, siteurl, body)
        return web.json_response(body)
    except asyncio.CancelledError:
        # Killed by a ban (or the connection went away). Close out the log
        # line so _req_ctx doesn't leak, then let the middleware answer.
        _stats["errors"] += 1
        fail(499, {"error": "cancelled", "error_code": "cancelled"})
        raise
    except Exception as exc:
        elapsed = time.time() - t0
        _stats["errors"] += 1
        code, public_msg, status = _classify_error(exc)
        # Full detail to the server log; sanitised body to the client.
        log.exception("%s failed rid=%s code=%s", _short_ep(path), rid, code)
        return fail(status, {"error": public_msg, "error_code": code,
                             "elapsed": round(elapsed, 2)})
    finally:
        _stats["in_flight"] -= 1


async def handle_playground(request: web.Request) -> web.Response:
    index_path = os.path.join(TEMPLATE_DIR, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return web.Response(text="playground template missing", status=500)
    return web.Response(text=html, content_type="text/html")


_warp_state = "unknown"


async def _refresh_warp_state() -> None:
    """warp=on/off from Cloudflare's trace endpoint, fetched through the same
    egress proxy the browser uses so it reflects real solve traffic. Runs in
    the housekeeping loop, never on the /health request path, so a slow WARP
    can't make the docker healthcheck time out."""
    global _warp_state
    import aiohttp
    from .solver import _solver_proxy
    state = "unknown"
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get("https://www.cloudflare.com/cdn-cgi/trace",
                             proxy=_solver_proxy()) as r:
                text = await r.text()
        for line in text.splitlines():
            if line.startswith("warp="):
                state = line.split("=", 1)[1] or "off"
                break
    except Exception:
        state = "unknown"
    _warp_state = state


async def handle_health(request: web.Request) -> web.Response:
    # Never launches or touches the browser: the healthcheck must stay cheap
    # and independent of solver state (that caused restart loops previously).
    proxy_url, proxy_kind = _challenge_proxy()
    body = {"status": "ok", "warp": _warp_state, **_stats}
    if proxy_url:
        body.update(mode=proxy_kind, proxy_url=proxy_url)
    else:
        body.update(mode="camoufox", max_concurrent=MAX_WORKERS)
    return web.json_response(body)


async def handle_stats(request: web.Request) -> web.Response:
    proxy_url, proxy_kind = _challenge_proxy()
    lat = list(_latency_ms)
    lat_sorted = sorted(lat)
    def pct(arr, p):
        if not arr:
            return 0
        k = min(len(arr) - 1, int(round((p / 100) * (len(arr) - 1))))
        return round(arr[k], 0)
    avg = round(sum(lat) / len(lat), 0) if lat else 0
    total = _stats["solved"] + _stats["challenges"] + _stats["errors"]
    success_rate = round(((_stats["solved"] + _stats["challenges"]) / total) * 100, 1) if total else 0.0
    return web.json_response({
        "uptime": round(time.time() - _PROCESS_STARTED, 1),
        "mode": proxy_kind or "camoufox",
        "proxy_url": proxy_url or None,
        **_stats,
        "total_requests": total,
        "success_rate": success_rate,
        "latency_ms": {"avg": avg, "p50": pct(lat_sorted, 50), "p95": pct(lat_sorted, 95), "samples": len(lat)},
        "events": list(_events),
    })


def _persisted_stats() -> dict:
    return {k: _stats[k] for k in _PERSISTED_STATS}


async def _housekeeping_loop():
    """Every 30s: persist counters (so a restart resumes near where it left
    off, without disk writes on the request hot path), drop idle per-IP
    rate-limit windows, refresh the cached WARP state."""
    while True:
        try:
            await asyncio.sleep(30)
            db.save_stats(_persisted_stats())
            cutoff = time.time() - 60
            for ip in [ip for ip, h in _ip_hits.items() if not h or h[-1] < cutoff]:
                _ip_hits.pop(ip, None)
            await _refresh_warp_state()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("housekeeping failed")


async def on_startup(app):
    # Persistence: load bans + prior counters so both survive a restart.
    db.init()
    _ip_banned.update(db.load_bans())
    for k, v in db.load_stats().items():
        if k in _PERSISTED_STATS:
            _stats[k] = v
    log.info("db loaded: %d ban(s), stats=%s", len(_ip_banned), _stats)
    await _refresh_warp_state()
    app["housekeeping_task"] = asyncio.ensure_future(_housekeeping_loop())

    proxy_url, proxy_kind = _challenge_proxy()
    # Always warm the browser at startup — /solve still routes through
    # Camoufox even when a challenge proxy is configured. Lazy-loading the
    # browser meant the first /solve paid a ~30s cold-start tax.
    pool = await get_pool(MAX_WORKERS)
    if proxy_url:
        print(f"[solver] {proxy_kind} delegation enabled ({proxy_url}); browser warm, "
              f"MAX_WORKERS={pool.max_concurrent}", flush=True)
    else:
        print(f"[solver] browser warm, MAX_WORKERS={pool.max_concurrent}", flush=True)


async def on_cleanup(app):
    task = app.get("housekeeping_task")
    if task:
        task.cancel()
    db.save_stats(_persisted_stats())   # final flush
    db.close()
    from . import solver as _s
    if _s._pool is None:
        return
    await _s._pool.shutdown()


def main():
    import warnings
    warnings.filterwarnings("ignore")
    os.environ.setdefault("PYTHONWARNINGS", "ignore")
    sys.stderr = sys.stdout

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Keep the noisy libs quiet but let our own loggers through so
    # `log.exception` is actually visible for debugging.
    for name in ("aiohttp.access", "aiohttp.server", "aiohttp.web",
                 "camoufox", "playwright"):
        logging.getLogger(name).setLevel(logging.WARNING)

    app = web.Application(client_max_size=MAX_BODY_BYTES,
                          middlewares=[auth_middleware, ratelimit_middleware])
    log.info("anti-abuse: rate=%s/min/ip concurrency=%s/ip",
             RATE_LIMIT_PER_MIN or "off", MAX_CONCURRENT_PER_IP or "off")
    if API_KEY:
        print("[solver] API key auth ENABLED", flush=True)
    else:
        log.warning("API_KEY unset: solve endpoints and /stats are open to anyone who can reach this port")
    app.router.add_get("/", handle_playground)
    for path in _ENDPOINTS:
        app.router.add_post(path, handle_solve)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/stats", handle_stats)
    if os.path.isdir(STATIC_DIR):
        app.router.add_static("/static/", STATIC_DIR, show_index=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    print(f"[solver] listening on http://0.0.0.0:{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, print=None, access_log=None)


if __name__ == "__main__":
    main()
