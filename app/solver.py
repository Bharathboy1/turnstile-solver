"""
Cloudflare Turnstile + JS-Challenge ("Just a moment...") solver.

Design notes:
  - Backend: Camoufox (stealth Firefox build with realistic OS
    fingerprints) running headed under a virtual X server (Xvfb).
    Real Cloudflare deployments fingerprint --headless=new builds
    (HeadlessChrome UA + missing GPU/audio signals) and refuse to
    mount the Turnstile iframe; running headed under Xvfb keeps the
    UA clean and lets the widget render.
  - HTML template + click strategy borrowed from Boterdrop-Solver:
    minimal host page that loads the CF api.js, plus a CSS shrink
    hack on the .cf-turnstile div so click coordinates land on the
    invisible-mode hit area predictably. Completion is callback-driven and
    clicks are paced to avoid repeatedly hammering the widget.
  - Single warm browser, persistent profile. Solves are serialised
    per-sitekey (CF escalates difficulty when the same sitekey is hit by
    concurrent tabs of one profile); different keys run in parallel up to
    MAX_WORKERS via a semaphore.
  - Idle API-loaded pages are reused by exact origin. Challenge iframes are
    removed before caching; each exclusive lease renders its own
    sitekey/action/cdata, preserving concurrency and isolation guarantees.
  - solve_challenge_async optionally delegates to a Byparr /
    FlareSolverr proxy via CHALLENGE_PROXY_URL; the in-process browser
    is the fallback.
"""

import asyncio
import contextlib
import json
import logging
import os
import random
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp
from camoufox import DefaultAddons
from camoufox.async_api import AsyncCamoufox


log = logging.getLogger("solver")


def _step(req_id: str, msg: str):
    """Per-request progress trace. Only emitted at DEBUG so INFO stays to one
    line per request even under heavy concurrency."""
    if log.isEnabledFor(logging.DEBUG):
        log.debug("[%s] %s", req_id, msg)


# ---------- Profile + headless mode ----------

def _get_profile_dir() -> str:
    if os.environ.get("TS_PROFILE_DIR"):
        return os.environ["TS_PROFILE_DIR"]
    return "/tmp/ts_profile"


def _solver_proxy() -> Optional[str]:
    """Outbound proxy for the in-process browser (e.g. WARP HTTP proxy).
    Empty -> direct connection."""
    return (os.environ.get("SOLVER_PROXY") or "").strip() or None


def _headless_mode():
    """Default to headed under the entrypoint-managed Xvfb. Headless
    Camoufox/Firefox can still be requested via HEADLESS=true but real
    CF widgets refuse to mount on it."""
    mode = os.environ.get("HEADLESS", "false").lower()
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("virtual",):
        return "virtual"
    return False


# ---------- Singleton browser ----------

class BrowserSingleton:
    def __init__(self, max_concurrent: int):
        self._camoufox: Optional[AsyncCamoufox] = None
        self.browser = None  # playwright BrowserContext when launched with user_data_dir
        self.sem = asyncio.Semaphore(max_concurrent)
        # Per-key locks: CF escalates difficulty when the SAME sitekey is hit
        # by concurrent tabs of one profile, so we serialise per key — but let
        # different keys/sites run in parallel up to the semaphore. Locks are
        # refcounted and dropped when idle so unique keys can't grow the dict.
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_refs: dict[str, int] = {}
        self.max_concurrent = max_concurrent
        self._start_lock = asyncio.Lock()
        self.stopped = False
        # Bumped on every launch. A solve that saw the browser die passes the
        # generation it was using to shutdown(); if another solve already
        # relaunched, the request is ignored instead of killing the new browser.
        self.generation = 0
        # Idle API-loaded Turnstile pages. A page is removed while leased, so it
        # can never serve two requests concurrently. Challenge iframes are torn
        # down before caching; exact request parameters are rendered on lease.
        self._turnstile_pages: OrderedDict[tuple[str, str, str, str], object] = OrderedDict()
        self._turnstile_cache_size = max(
            0, int(os.environ.get("TURNSTILE_PAGE_CACHE_SIZE", min(4, max_concurrent)))
        )
        self.turnstile_cache_hits = 0
        self.turnstile_cache_misses = 0

    @contextlib.asynccontextmanager
    async def keyed(self, key: str):
        """Serialise same-key solves; different keys run concurrently."""
        lock = self._key_locks.setdefault(key, asyncio.Lock())
        self._key_refs[key] = self._key_refs.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            self._key_refs[key] -= 1
            if self._key_refs[key] == 0:
                del self._key_refs[key]
                del self._key_locks[key]

    def _is_alive(self) -> bool:
        # Persistent contexts have no .browser handle, so liveness comes from
        # the context "close" event wired up in ensure().
        return self.browser is not None and not self.stopped

    async def ensure(self):
        async with self._start_lock:
            if self._is_alive():
                return
            if self.browser is not None or self._camoufox is not None:
                log.warning("browser handle stale, relaunching camoufox")
                await self._close_turnstile_pages()
                try:
                    if self._camoufox is not None:
                        await self._camoufox.__aexit__(None, None, None)
                except Exception:
                    pass
                self._camoufox = None
                self.browser = None
            profile = _get_profile_dir()
            os.makedirs(profile, exist_ok=True)
            proxy = _solver_proxy()
            log.info("launching camoufox profile=%s headless=%s proxy=%s",
                     profile, _headless_mode(), proxy or "-")
            # exclude_addons=[DefaultAddons.UBO] — uBlock Origin sometimes
            # blocks the CF api.js script, which kills widget mount.
            kwargs = dict(
                headless=_headless_mode(),
                humanize=True,
                persistent_context=True,
                user_data_dir=profile,
                os="windows",
                locale="en-US",
                exclude_addons=[DefaultAddons.UBO],
            )
            if proxy:
                # Playwright/Firefox requires credentials in separate dictionary keys
                u = urlparse(proxy)
                proxy_dict = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
                if u.username:
                    proxy_dict["username"] = u.username
                if u.password:
                    proxy_dict["password"] = u.password
                kwargs["proxy"] = proxy_dict
                kwargs["geoip"] = True
            self._camoufox = AsyncCamoufox(**kwargs)
            self.browser = await self._camoufox.__aenter__()
            self.stopped = False
            self.generation += 1
            gen = self.generation

            def _on_close(_ctx):
                if self.generation == gen:
                    log.warning("browser context closed (gen %d)", gen)
                    self.stopped = True

            self.browser.on("close", _on_close)
            log.info("camoufox ready (gen %d)", gen)

    async def new_page(self, url: str = ""):
        await self.ensure()
        page = await self.browser.new_page()
        if url:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                log.warning("initial goto failed: %s", e)
        return page

    async def acquire_turnstile_page(
        self, key: tuple[str, str, str, str]
    ) -> tuple[object, bool]:
        """Exclusively lease an idle page for an exact widget configuration."""
        await self.ensure()
        page = self._turnstile_pages.pop(key, None)
        if page is not None:
            try:
                if not page.is_closed():
                    self.turnstile_cache_hits += 1
                    return page, True
            except Exception:
                pass
            with contextlib.suppress(Exception):
                await page.close()

        self.turnstile_cache_misses += 1
        return await self.new_page(), False

    async def release_turnstile_page(
        self, key: tuple[str, str, str, str], page
    ) -> None:
        """Return a healthy page to the bounded LRU cache, or close it."""
        try:
            closed = page.is_closed()
        except Exception:
            closed = True
        if closed or self._turnstile_cache_size <= 0 or self.stopped:
            with contextlib.suppress(Exception):
                await page.close()
            return

        try:
            reusable = await page.evaluate(
                "() => Boolean(window.__tsTeardown && window.__tsTeardown())"
            )
        except Exception:
            reusable = False
        if not reusable:
            await self.discard_turnstile_page(page)
            return

        replaced = self._turnstile_pages.pop(key, None)
        if replaced is not None and replaced is not page:
            await self.discard_turnstile_page(replaced)
        self._turnstile_pages[key] = page

        while len(self._turnstile_pages) > self._turnstile_cache_size:
            _, stale = self._turnstile_pages.popitem(last=False)
            await self.discard_turnstile_page(stale)

    async def discard_turnstile_page(self, page) -> None:
        with contextlib.suppress(Exception):
            await page.unroute_all()
        with contextlib.suppress(Exception):
            await page.close()

    async def _close_turnstile_pages(self) -> None:
        pages = list(self._turnstile_pages.values())
        self._turnstile_pages.clear()
        for page in pages:
            await self.discard_turnstile_page(page)

    async def shutdown(self, generation: Optional[int] = None):
        """Tear the browser down. With `generation`, only if that generation
        is still the live one (a dead-browser report about an old launch must
        not kill the relaunched browser others are already using)."""
        if generation is not None and generation != self.generation:
            return
        if self.stopped and self.browser is None:
            return
        self.stopped = True
        await self._close_turnstile_pages()
        if self._camoufox is not None:
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception:
                pass
        self._camoufox = None
        self.browser = None


_pool: Optional[BrowserSingleton] = None
_pool_lock: Optional[asyncio.Lock] = None


async def get_pool(size: Optional[int] = None) -> BrowserSingleton:
    global _pool, _pool_lock
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if _pool is None:
            n = size if size is not None else int(os.environ.get("MAX_WORKERS", 8))
            _pool = BrowserSingleton(n)
            await _pool.ensure()
        return _pool


_DEAD_BROWSER_HINTS = (
    "connection closed",
    "browser has been closed",
    "target page, context or browser has been closed",
    "browser context has been closed",
)


def _is_dead_browser_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in _DEAD_BROWSER_HINTS)


async def _run_in_tab(key: str, url: str, req_id: str,
                      fn: Callable[[object, BrowserSingleton], Awaitable],
                      proxy: Optional[str] = None):
    """Common tab lifecycle for every solve: key lock (outer, so same-key
    waiters don't hold worker slots), semaphore (inner), one retry on a dead
    browser, page cleanup."""
    pool = await get_pool()
    async with pool.keyed(key):
        async with pool.sem:
            # If an explicit proxy is requested and it differs from the warm browser's proxy,
            # launch a dedicated Camoufox tab for that proxy.
            if proxy and proxy.strip() and proxy.strip() != _solver_proxy():
                u = urlparse(proxy)
                proxy_dict = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
                if u.username:
                    proxy_dict["username"] = u.username
                if u.password:
                    proxy_dict["password"] = u.password
                _step(req_id, f"launching dedicated camoufox tab with proxy: {proxy_dict['server']}")
                async with AsyncCamoufox(
                    headless=_headless_mode(),
                    humanize=True,
                    os="windows",
                    locale="en-US",
                    exclude_addons=[DefaultAddons.UBO],
                    proxy=proxy_dict,
                    geoip=True,
                ) as custom_browser:
                    page = await custom_browser.new_page()
                    if url:
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        except Exception as e:
                            log.warning("initial goto failed: %s", e)
                    try:
                        return await fn(page, pool)
                    finally:
                        with contextlib.suppress(Exception):
                            await page.unroute_all()
                        with contextlib.suppress(Exception):
                            await page.close()

            # Otherwise, use the warm, instant persistent browser on default proxy (WARP)
            for attempt in (1, 2):
                page = None
                gen = pool.generation
                try:
                    page = await pool.new_page(url)
                    gen = pool.generation
                    return await fn(page, pool)
                except Exception as exc:
                    if attempt == 1 and _is_dead_browser_error(exc):
                        _step(req_id, f"browser dead, relaunching: {exc}")
                        await pool.shutdown(gen)
                        continue
                    raise
                finally:
                    if page is not None:
                        try:
                            await page.unroute_all()
                        except Exception:
                            pass
                        try:
                            await page.close()
                        except Exception:
                            pass
            raise RuntimeError("_run_in_tab: unreachable")


# ---------- Turnstile injection ----------
# Minimal host page modelled after Boterdrop. The api.js loads with an
# explicit onload callback and an <p> element keeps the body non-empty
# so layout settles before the widget renders.
_HOST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>.</title>
<script>
window.__tsToken = '';
window.__tsWidgetId = null;
window.__tsApiReady = false;
window.__tsSolved = function(token) { window.__tsToken = token || ''; };
window.__tsExpired = function() { window.__tsToken = ''; };
window.__tsError = function() { window.__tsToken = ''; };
window.onloadTurnstileCallback = function() { window.__tsApiReady = true; };
window.__tsTeardown = function() {
    window.__tsToken = '';
    if (window.__tsWidgetId !== null && window.turnstile) {
        try { window.turnstile.remove(window.__tsWidgetId); } catch (_) {}
    }
    window.__tsWidgetId = null;
    var host = document.getElementById('cf-turnstile-host');
    if (host) host.replaceChildren();
    return true;
};
window.__tsRender = function(config) {
    window.__tsTeardown();
    var options = {
        sitekey: config.sitekey,
        callback: window.__tsSolved,
        'expired-callback': window.__tsExpired,
        'error-callback': window.__tsError
    };
    if (config.action) options.action = config.action;
    if (config.cdata) options.cData = config.cdata;
    window.__tsWidgetId = window.turnstile.render('#cf-turnstile-host', options);
    return window.__tsWidgetId;
};
</script>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&amp;onload=onloadTurnstileCallback" async defer></script>
</head>
<body>
<div id="cf-turnstile-host" style="background:white;width:70px"></div>
<p id="ip-display"></p>
</body>
</html>"""


_IS_CHALLENGE_JS = """
(() => {
    if (document.title.toLowerCase().includes('just a moment')) return true;
    if (document.querySelector('div.challenge-form, #challenge-form, .ray-id')) return true;
    if (document.querySelector('iframe[src*="challenges.cloudflare.com/cdn-cgi"]')) return true;
    return false;
})()
"""


async def _turnstile_on_page(page, sitekey: str, siteurl: str, req_id: str,
                              timeout: int, action: Optional[str] = None,
                              cdata: Optional[str] = None,
                              reuse: bool = False) -> str:
    """Solve Turnstile on a fresh or exclusively leased intercepted page."""
    loop = asyncio.get_event_loop()
    t0 = loop.time()

    target = siteurl if siteurl.endswith("/") else siteurl + "/"

    if reuse and page.url != target:
        reuse = False

    if not reuse:
        with contextlib.suppress(Exception):
            await page.unroute_all()
        await page.route(target, lambda route: route.fulfill(
            body=_HOST_HTML, status=200, content_type="text/html; charset=utf-8"))
        await page.goto(target, wait_until="domcontentloaded")
        _step(req_id, f"route intercepted {target}")
    else:
        _step(req_id, f"reused API-loaded Turnstile page {target}")

    api_timeout = max(1, min(15_000, int((t0 + timeout - loop.time()) * 1000)))
    await page.wait_for_function(
        "() => window.__tsApiReady && typeof window.turnstile !== 'undefined'",
        timeout=api_timeout,
    )
    await page.evaluate(
        "config => window.__tsRender(config)",
        {
            "sitekey": sitekey,
            "action": action or "",
            "cdata": cdata or "",
        },
    )

    deadline = t0 + timeout

    async def paced_clicks() -> None:
        widget = page.locator("#cf-turnstile-host")
        # Wait for Cloudflare's iframe instead of clicking an empty host div.
        # Token waiting runs concurrently, so invisible widgets can complete
        # without being delayed by this readiness check.
        try:
            await page.locator(
                'iframe[src*="challenges.cloudflare.com"]'
            ).first.wait_for(state="visible", timeout=3_000)
        except Exception:
            pass
        for _ in range(4):
            if loop.time() >= deadline:
                return
            try:
                value = await page.evaluate(
                    """() => window.__tsToken ||
                    (document.querySelector('[name="cf-turnstile-response"]') || {}).value || ''"""
                )
                if value:
                    return
                await widget.click(timeout=1_200)
            except Exception:
                pass
            await asyncio.sleep(4.0)

    click_task = asyncio.create_task(paced_clicks())
    try:
        remaining_ms = max(1, int((deadline - loop.time()) * 1000))
        await page.wait_for_function(
            """() => Boolean(window.__tsToken ||
            (document.querySelector('[name="cf-turnstile-response"]') || {}).value)""",
            timeout=remaining_ms,
        )
        value = await page.evaluate(
            """() => window.__tsToken ||
            (document.querySelector('[name="cf-turnstile-response"]') || {}).value || ''"""
        )
        if value:
            _step(req_id, f"token obtained ({loop.time() - t0:.1f}s, reused={reuse})")
            return value
    except Exception as exc:
        if loop.time() < deadline:
            raise
        raise TimeoutError(f"turnstile timeout after {timeout}s") from exc
    finally:
        click_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await click_task

    raise TimeoutError(f"turnstile timeout after {timeout}s")


async def solve_async(sitekey: str, siteurl: str, req_id: str = "-",
                      timeout: int = 45, action: Optional[str] = None,
                      cdata: Optional[str] = None,
                      proxy: Optional[str] = None) -> str:
    _step(req_id, f"opening tab for {siteurl}")

    # Explicit per-request proxies launch isolated Camoufox instances and keep
    # the close-after-solve lifecycle. Reuse is limited to the warm default
    # browser, where origin, profile, UA and egress remain identical.
    if proxy and proxy.strip() and proxy.strip() != _solver_proxy():
        async def run(page, _pool):
            return await _turnstile_on_page(
                page, sitekey, siteurl, req_id, timeout, action, cdata
            )

        return await _run_in_tab(sitekey, "", req_id, run, proxy=proxy)

    pool = await get_pool()
    target = siteurl if siteurl.endswith("/") else siteurl + "/"
    # The idle page retains only the loaded API. The challenge iframe is
    # removed on release and exact sitekey/action/cdata are rendered per lease.
    page_key = (target, "", "", "")

    async with pool.keyed(sitekey):
        async with pool.sem:
            for attempt in (1, 2):
                page = None
                gen = pool.generation
                try:
                    page, reused = await pool.acquire_turnstile_page(page_key)
                    gen = pool.generation
                    token = await _turnstile_on_page(
                        page,
                        sitekey,
                        siteurl,
                        req_id,
                        timeout,
                        action,
                        cdata,
                        reuse=reused,
                    )
                    await pool.release_turnstile_page(page_key, page)
                    page = None
                    return token
                except Exception as exc:
                    if page is not None:
                        await pool.discard_turnstile_page(page)
                        page = None
                    if attempt == 1 and _is_dead_browser_error(exc):
                        _step(req_id, f"browser dead, relaunching: {exc}")
                        await pool.shutdown(gen)
                        continue
                    raise

    raise RuntimeError("solve_async: unreachable")


# ---------- JS Challenge ("Just a moment...") ----------

_CF_WIDGET_RECT_JS = """
(() => {
    for (const f of document.querySelectorAll('iframe')) {
        const src = f.src || f.getAttribute('src') || '';
        if (!src.includes('challenges.cloudflare.com')) continue;
        const r = f.getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
    }
    const el = document.querySelector('#hQLfM7, .main-wrapper .ch-title-zone + div');
    if (el) {
        const r = el.getBoundingClientRect();
        if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
    }
    return null;
})()
"""


def _match_host(target_host: str, cdomain: str) -> bool:
    d = (cdomain or "").lstrip(".").lower()
    h = (target_host or "").lower()
    return bool(h) and bool(d) and (h == d or h.endswith("." + d))


def _challenge_proxy() -> tuple[Optional[str], str]:
    """Return (base_url, kind) for the configured challenge proxy."""
    url = os.environ.get("CHALLENGE_PROXY_URL") or os.environ.get("FLARESOLVERR_URL") or ""
    url = url.rstrip("/")
    if not url:
        return None, ""
    kind = (os.environ.get("CHALLENGE_PROXY_KIND")
            or ("flaresolverr" if os.environ.get("FLARESOLVERR_URL") else "byparr")).lower()
    return url, kind


async def _solve_via_proxy(siteurl: str, req_id: str, timeout: int, proxy: Optional[str] = None) -> Optional[dict]:
    url, kind = _challenge_proxy()
    if not url:
        return None
    _step(req_id, f"delegating to {kind} -> {url}")

    candidates = [siteurl]
    try:
        u = urlparse(siteurl)
        if u.path and not u.path.endswith("/") and "." not in u.path.rsplit("/", 1)[-1] and not u.query:
            fixed = siteurl.rstrip() + "/"
            if fixed != siteurl:
                candidates.append(fixed)
    except Exception:
        pass

    if kind == "byparr":
        payload_base = {"cmd": "request.get", "max_timeout": max(5, timeout)}
    else:
        payload_base = {"cmd": "request.get", "maxTimeout": max(5000, timeout * 1000)}

    # Route the challenge fetch through requested proxy or default egress proxy.
    egress_proxy = proxy or _solver_proxy()
    req_headers = {}
    if egress_proxy:
        payload_base["proxy"] = {"url": egress_proxy}
        try:
            u = urlparse(egress_proxy)
            req_headers["X-Proxy-Server"] = f"{u.scheme}://{u.hostname}:{u.port}"
            if u.username:
                req_headers["X-Proxy-Username"] = u.username
            if u.password:
                req_headers["X-Proxy-Password"] = u.password
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    data = None
    last_err = None
    for i, try_url in enumerate(candidates):
        if i:
            _step(req_id, f"retrying with trailing slash -> {try_url}")
        payload = {**payload_base, "url": try_url}
        try:
            conn_timeout = aiohttp.ClientTimeout(total=timeout + 15)
            async with aiohttp.ClientSession(timeout=conn_timeout) as s:
                async with s.post(f"{url}/v1", json=payload, headers=req_headers) as resp:
                    body_text = await resp.text()
                    if resp.status == 200:
                        try:
                            parsed = json.loads(body_text)
                        except ValueError:
                            parsed = None
                        if not isinstance(parsed, dict):
                            last_err = f"{kind}: non-JSON response: {body_text[:200]}"
                            continue
                        if (parsed.get("status") or "").lower() == "ok":
                            data = parsed
                            break
                        last_err = f"{kind}: {parsed.get('message') or parsed}"
                        continue
                    last_err = f"{kind} HTTP {resp.status}: {body_text[:200]}"
        except asyncio.TimeoutError:
            last_err = f"{kind} did not respond within {timeout + 15}s"
        except aiohttp.ClientError as e:
            last_err = f"{kind} connection error: {e}"

    if data is None:
        raise RuntimeError(last_err or f"{kind}: unknown failure")

    sol = data.get("solution") or {}
    final_url = sol.get("url") or siteurl
    target_host = urlparse(final_url).hostname or ""
    raw_cookies = sol.get("cookies") or []
    cookies = []
    for c in raw_cookies:
        if not _match_host(target_host, c.get("domain", "")):
            continue
        cookies.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain"),
            "path": c.get("path", "/"),
            "expires": c.get("expiry") if c.get("expiry") is not None else c.get("expires", -1),
        })

    html_ = sol.get("response") or ""
    title = ""
    low = html_.lower()
    a = low.find("<title")
    if a != -1:
        b = low.find(">", a)
        c_ = low.find("</title>", b)
        if b != -1 and c_ != -1:
            title = html_[b + 1:c_].strip()

    user_agent = sol.get("userAgent") or sol.get("user_agent") or ""
    _step(req_id, f"{kind} cleared ({loop.time() - t0:.1f}s, cookies={len(cookies)})")
    return {
        "url": final_url,
        "title": title,
        "user_agent": user_agent,
        "cookies": cookies,
        "html": html_,
    }


async def solve_challenge_async(siteurl: str, req_id: str = "-",
                                 timeout: int = 45, proxy: Optional[str] = None) -> dict:
    """Open page, wait for CF challenge to clear, return cookies + final html."""
    proxy_url, proxy_kind = _challenge_proxy()
    if proxy_url:
        try:
            result = await _solve_via_proxy(siteurl, req_id, timeout, proxy=proxy)
            if result is not None:
                return result
        except Exception as e:
            log.warning("[%s] %s failed, falling back to camoufox: %s",
                        req_id, proxy_kind or "proxy", e)

    _step(req_id, f"opening tab -> {siteurl}")

    async def run(page, pool):
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        deadline = t0 + timeout
        cleared = False
        attempts = 0
        clicks = 0
        last_click = 0.0

        while loop.time() < deadline:
            is_challenge = await page.evaluate(_IS_CHALLENGE_JS)
            if not is_challenge:
                cleared = True
                break
            attempts += 1
            if attempts == 1:
                _step(req_id, "CF challenge detected, waiting for clear...")

            now = loop.time()
            if clicks < 3 and (clicks == 0 or now - last_click > 6):
                rect = await page.evaluate(_CF_WIDGET_RECT_JS)
                if rect:
                    cx = rect["x"] + 28 + random.uniform(-3, 3)
                    cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
                    _step(req_id, f"interactive click #{clicks + 1} at ({cx:.0f},{cy:.0f})")
                    try:
                        await page.mouse.move(cx - 60, cy - 20)
                        await asyncio.sleep(0.05)
                        await page.mouse.move(cx, cy)
                        await asyncio.sleep(0.03)
                        await page.mouse.click(cx, cy)
                    except Exception as e:
                        _step(req_id, f"click error: {e}")
                    last_click = now
                    clicks += 1
            await asyncio.sleep(0.3)

        if not cleared:
            raise TimeoutError(f"challenge did not clear within {timeout}s")

        final_url = page.url
        title = await page.title()
        user_agent = await page.evaluate("navigator.userAgent")
        html_ = await page.content()
        target_host = urlparse(final_url or siteurl).hostname or ""
        try:
            raw_cookies = await pool.browser.cookies(final_url or siteurl)
            cookies = [
                {"name": c["name"], "value": c["value"], "domain": c["domain"],
                 "path": c["path"], "expires": c.get("expires", -1)}
                for c in raw_cookies
                if _match_host(target_host, c.get("domain", ""))
            ]
        except Exception as e:
            _step(req_id, f"cookie fetch failed: {e}")
            cookies = []

        _step(req_id, f"challenge cleared ({loop.time() - t0:.1f}s, attempts={attempts})")
        return {
            "url": final_url,
            "title": title,
            "user_agent": user_agent,
            "cookies": cookies,
            "html": html_,
        }

    return await _run_in_tab(siteurl, siteurl, req_id, run, proxy=proxy)


# ---------- reCAPTCHA v3 (Boterdrop pattern) ----------

_RECAPTCHA_READY_JS = """
() => typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'
"""

_RECAPTCHA_INJECT_JS = """
(key) => new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/api.js?render=' + key;
    s.onload = () => {
        const i = setInterval(() => {
            if (typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function') {
                clearInterval(i); res(true);
            }
        }, 100);
        setTimeout(() => { clearInterval(i); rej(new Error('recaptcha inject timeout')); }, 10000);
    };
    s.onerror = () => rej(new Error('recaptcha script load failed'));
    document.head.appendChild(s);
})
"""

_RECAPTCHA_EXEC_JS = """
([key, act]) => new Promise((res, rej) => {
    grecaptcha.ready(() => {
        grecaptcha.execute(key, { action: act }).then(res)
            .catch(e => rej(new Error(e.message || 'recaptcha execute failed')));
    });
})
"""


async def solve_recaptcha_v3_async(sitekey: str, siteurl: str, req_id: str = "-",
                                    timeout: int = 45, action: str = "verify",
                                    proxy: Optional[str] = None) -> str:
    """Load the target page, ensure grecaptcha is present (inject api.js if
    not), then run grecaptcha.execute to mint a v3 token. Score-based, so a
    clean Camoufox fingerprint + WARP egress is what makes it pass."""
    _step(req_id, f"recaptcha v3 -> {siteurl} (action={action})")

    async def run(page, _pool):
        if not await page.evaluate(_RECAPTCHA_READY_JS):
            _step(req_id, "grecaptcha absent, injecting api.js")
            await page.evaluate(_RECAPTCHA_INJECT_JS, sitekey)
        # grecaptcha.ready never firing would otherwise hang this evaluate
        # forever and pin a worker slot.
        token = await asyncio.wait_for(
            page.evaluate(_RECAPTCHA_EXEC_JS, [sitekey, action]), timeout)
        if not token:
            raise RuntimeError("recaptcha returned empty token")
        _step(req_id, "recaptcha token obtained")
        return token

    return await _run_in_tab(sitekey, siteurl, req_id, run, proxy=proxy)


# ---------- AWS WAF token (Boterdrop pattern) ----------

async def solve_aws_token_async(siteurl: str, req_id: str = "-",
                                 timeout: int = 45,
                                 proxy: Optional[str] = None) -> dict:
    """Navigate to the target and poll for the `aws-waf-token` cookie that
    the AWS WAF challenge JS sets once it clears. Returns the cookie + UA."""
    _step(req_id, f"aws-waf -> {siteurl}")

    async def run(page, pool):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        token = None
        while loop.time() < deadline:
            # Scoped to the page URL: the profile is shared, so an unscoped
            # cookies() call would return a stale token from another site.
            cookies = await pool.browser.cookies(page.url or siteurl)
            token = next((c for c in cookies
                          if c.get("name") == "aws-waf-token"), None)
            if token:
                break
            await asyncio.sleep(1)
        if not token:
            raise TimeoutError(f"aws-waf-token not set within {timeout}s")
        user_agent = await page.evaluate("navigator.userAgent")
        _step(req_id, "aws-waf-token obtained")
        return {
            "token": token.get("value"),
            "cookie": f"aws-waf-token={token.get('value')}",
            "user_agent": user_agent,
            "url": page.url,
        }

    return await _run_in_tab(siteurl, siteurl, req_id, run, proxy=proxy)


def solve(sitekey: str, siteurl: str, timeout: int = 45) -> str:
    """Legacy sync wrapper."""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return asyncio.run(solve_async(sitekey, siteurl, timeout=timeout))


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stdout)
    if len(sys.argv) < 3:
        print("Usage: python solver.py <sitekey> <siteurl>")
        sys.exit(1)
    t0 = time.time()
    tok = solve(sys.argv[1], sys.argv[2])
    print(f"{tok}\nelapsed: {time.time()-t0:.2f}s")
