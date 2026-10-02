# Violetics Solver

HTTP service that solves Cloudflare Turnstile, clears Cloudflare JS /
interactive challenges, mints reCAPTCHA v3 tokens and fetches AWS WAF tokens.

- Browser: [Camoufox](https://github.com/daijro/camoufox) (stealth Firefox),
  headed under Xvfb, one warm instance with a persistent profile.
- JS challenges are delegated to [Byparr](https://github.com/ThePhaseless/Byparr)
  when configured; Camoufox is the fallback.
- Egress through Cloudflare WARP (HTTP proxy) in the compose stack.

## Quick start

```bash
git clone git@github.com:cv3inx/turnstile-solver.git
cd turnstile-solver
cp .env.example .env      # set API_KEY before exposing it
docker compose up -d
curl http://127.0.0.1:9988/health
```

Containers: `violetics-solver` (bound to `127.0.0.1:9988`), `violetics-byparr`
and `violetics-warp` (internal network only). The solver starts after WARP and
Byparr report healthy. Playground UI at `http://127.0.0.1:9988/`
(append `?api_key=...` when `API_KEY` is set).

Without Docker:

```bash
sudo apt install xvfb
pip install -r requirements.txt && python -m camoufox fetch
python -m app
```

## Configuration

All values are environment variables. Defaults in `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `9988` | HTTP port |
| `MAX_WORKERS` | `8` | Max concurrent browser tabs. Same sitekey is serialised; different sitekeys run in parallel. |
| `MAX_BODY_BYTES` | `65536` | Max request body |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds per-step solve progress |
| `API_KEY` | unset | When set, every path except `/health` needs it via `X-API-Key` header or `?api_key=`. |
| `RATE_LIMIT_PER_MIN` | `10` | Admitted solve requests per minute per client IP. Exceeding it bans the IP. `0` = off. |
| `MAX_CONCURRENT_PER_IP` | `5` | In-flight solves per client IP. `0` = off. |
| `BAN_SECONDS` | `86400` | Ban length. Bans persist in SQLite. |
| `DB_PATH` | `/data/solver.db` | SQLite file for bans and counters |
| `ALLOW_PRIVATE_TARGETS` | unset | `1` allows `siteurl` on loopback / private / link-local hosts (dev only). |
| `SOLVER_PROXY` | unset | Outbound HTTP proxy for the browser and Byparr, e.g. `http://warp:8080` |
| `REQUEST_PROXY_ALLOWLIST` | unset | Comma-separated allowlist for `/solve-challenge` request `proxy` values (exact hosts / `*.` wildcard hosts and/or CIDR/IP ranges). Empty disables per-request proxies. |
| `ALLOW_PRIVATE_PROXY_TARGETS` | unset | Dev-only override. `1` allows request `proxy` hosts that resolve to loopback/private/link-local/etc addresses. |
| `CHALLENGE_PROXY_URL` | unset | Byparr / FlareSolverr base URL. When set, `/solve-challenge` delegates to it. |
| `CHALLENGE_PROXY_KIND` | `byparr` | `byparr` (timeouts in s) or `flaresolverr` (timeouts in ms) |
| `TS_PROFILE_DIR` | `/tmp/ts_profile` | Persistent Camoufox profile |
| `HEADLESS` | `false` | `false` = headed under Xvfb. `true` = headless; real CF widgets usually refuse to mount. |

## API reference

All endpoints take and return JSON. Send `X-API-Key: <key>` when `API_KEY` is set.

Common request fields:

| Field | Type | Notes |
|---|---|---|
| `siteurl` | string | Required. `http`/`https`, public host. |
| `sitekey` | string | Required on `/solve`, `/recaptcha-v3`. `[A-Za-z0-9_-]{1,128}` |
| `proxy` | string | Optional on `/solve-challenge`. Must be `http://`, `https://`, or `socks5://` with host+port, and pass `REQUEST_PROXY_ALLOWLIST`. |
| `timeout` | int | Seconds, clamped to `5..180`, default `45`. Covers the whole request including queueing. Server aborts at `timeout + 15` s. Set your HTTP client timeout above that. |

Every response carries `elapsed` (seconds). Errors:

```json
{ "error": "solve timeout", "error_code": "timeout", "elapsed": 45.2 }
```

| `error_code` | HTTP | Meaning |
|---|---|---|
| `bad_request` | 400 | Invalid JSON, missing or malformed field, disallowed `siteurl` |
| `unauthorized` | 401 | `API_KEY` set and missing or wrong |
| `banned` | 429 | IP exceeded `RATE_LIMIT_PER_MIN`. `retry_after` in seconds. |
| `too_many_concurrent` | 429 | IP at `MAX_CONCURRENT_PER_IP` in-flight solves |
| `browser_error` | 503 | Browser closed or navigation failed. Retry. |
| `timeout` | 504 | Not solved within `timeout` |
| `solver_error` | 500 | Internal failure. Detail in server log only. |

### `POST /solve` — Turnstile token

```json
{ "sitekey": "0x4AAAAAAC3x1HiBz5IFyj7s", "siteurl": "https://www.example.com/",
  "timeout": 45, "action": "login", "cdata": "optional" }
```

`action`, `cdata`: optional, `[A-Za-z0-9_-]{1,255}`. The widget is rendered on
a host page served under the `siteurl` origin (route interception; the real
site is never fetched), so pass the URL the sitekey is bound to.

```json
{ "token": "1.abc...xyz", "elapsed": 6.14 }
```

### `POST /solve-challenge` — clear "Just a moment..."

```json
{ "siteurl": "https://api.example.com/docs", "timeout": 45,
  "proxy": "******proxy.example.net:8080" }
```

```json
{
  "url": "https://api.example.com/docs/",
  "title": "Example API",
  "user_agent": "Mozilla/5.0 ...",
  "cookies": [ { "name": "cf_clearance", "value": "...", "domain": ".example.com",
                 "path": "/", "expires": 1811226000 } ],
  "html": "<!doctype html>...",
  "elapsed": 15.52
}
```

Reuse `cf_clearance` together with `user_agent`; Cloudflare rejects the cookie
under a different UA. Extensionless paths are retried with a trailing slash.
Per-request proxy usage is disabled by default; set `REQUEST_PROXY_ALLOWLIST`
to explicitly permit trusted proxy hosts/ranges. Request proxies are validated
before any solve network activity, must include host+port, and cannot target
private/loopback/link-local/multicast/reserved/unspecified addresses unless
`ALLOW_PRIVATE_PROXY_TARGETS=1` is explicitly set for local development.

### `POST /recaptcha-v3` — reCAPTCHA v3 token

```json
{ "sitekey": "6Lc...", "siteurl": "https://www.example.com/", "action": "verify", "timeout": 45 }
```

`action`: optional, `[A-Za-z0-9_/]{1,64}`, default `verify`.

```json
{ "token": "03AFcWeA...", "elapsed": 4.02 }
```

### `POST /aws-token` — AWS WAF token

```json
{ "siteurl": "https://www.example.com/", "timeout": 45 }
```

```json
{ "token": "...", "cookie": "aws-waf-token=...", "user_agent": "Mozilla/5.0 ...",
  "url": "https://www.example.com/", "elapsed": 5.31 }
```

### `GET /health` — no auth

```json
{ "status": "ok", "warp": "on", "mode": "byparr", "proxy_url": "http://byparr:8191",
  "in_flight": 0, "solved": 30, "errors": 1, "challenges": 11 }
```

`mode` is `camoufox` when no challenge proxy is configured. `warp` is
`on` / `off` / `unknown`, refreshed every 30 s.

### `GET /stats`

Counters, uptime, success rate, latency (`avg`, `p50`, `p95` in ms) and the
last 50 request events. Used by the playground.

## Logging

One line per request on stdout:

```
✓ 1a2b3c4d turnstile  200   3.21s  1.2.3.4  https://www.example.com/ key=0x4AAAAAAA…  → token 1.1Tqrqdr...26cb55 (538 chars)
✗ cdd14513 challenge  504  45.10s  1.2.3.4  https://api.example.com/docs  → error: solve timeout
```

Failed solves log the full trace server-side; clients get the sanitised
`error_code` only. `LOG_LEVEL=DEBUG` adds per-step progress.
Proxy credentials are never logged; proxy values are redacted to safe labels
(`scheme://host:port`) where shown.

## Production notes

- Set `API_KEY`. Without it solve endpoints and `/stats` are open to anyone
  reaching the port.
- Keep the port on loopback (compose default) behind a reverse proxy for TLS.
  `X-Forwarded-For` is trusted only when the direct peer is a private or
  loopback address.
- `--shm-size=1gb` is required; Firefox crashes on the default 64 MB `/dev/shm`.
- `/tmp/ts_profile` holds Cloudflare cookies. Treat the volume as sensitive.
- `playwright` is pinned to 1.54.0; newer drivers crash on real Cloudflare pages.

## Layout

```
app/service.py       aiohttp HTTP layer: auth, rate limit, validation, logging
app/solver.py        Camoufox automation + Byparr delegation
app/db.py            SQLite persistence (bans, counters)
web/templates/       Playground UI
docker-compose.yml   warp + solver + byparr
```

## License

MIT
