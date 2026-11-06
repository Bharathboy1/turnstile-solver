"""Smoke test for RATE_LIMIT_BYPASS_IPS: stub the browser module, exercise the
real ratelimit middleware. Run: python test_bypass.py (no docker needed)."""
import asyncio
import os
import sys
import types

os.environ["RATE_LIMIT_BYPASS_IPS"] = "1.2.3.4, 10.0.0.0/8"

# app.solver pulls in camoufox (docker-only); the HTTP layer does not need it here
fake = types.ModuleType("app.solver")


async def _noop(*a, **k):
    return {}


fake.get_pool = _noop
fake.solve_async = _noop
fake.solve_challenge_async = _noop
fake.solve_recaptcha_v3_async = _noop
fake.solve_aws_token_async = _noop
fake._challenge_proxy = lambda: (None, None)
fake._solver_proxy = lambda: None
sys.modules["app.solver"] = fake

import app.service as svc  # noqa: E402

# ---- _bypassed
assert svc._bypassed("1.2.3.4") is True, "exact ip"
assert svc._bypassed("10.99.2.1") is True, "cidr member"
assert svc._bypassed("8.8.8.8") is False, "outside list"
assert svc._bypassed("not-an-ip") is False, "garbage"


class Req:
    path = "/solve"
    remote = "1.2.3.4"
    headers: dict = {}


async def handler(request):
    return "handled"


# a bypassed ip sails through even with an old ban on record
svc._ip_banned["1.2.3.4"] = 9_999_999_999.0
assert asyncio.run(svc.ratelimit_middleware(Req(), handler)) == "handled"

# and it never accrues window hits, so it can never be marched into a ban
for _ in range(25):
    assert asyncio.run(svc.ratelimit_middleware(Req(), handler)) == "handled"
assert "1.2.3.4" not in svc._ip_hits, "bypassed ip must not be counted"

class Req2(Req):
    remote = "8.8.8.8"


# default: limits off, so even an old persisted ban is ignored
svc._ip_banned["8.8.8.8"] = 9_999_999_999.0
assert asyncio.run(svc.ratelimit_middleware(Req2(), handler)) == "handled"

# limit on: a normal ip with an active ban is rejected
svc.RATE_LIMIT_PER_MIN = 3
resp = asyncio.run(svc.ratelimit_middleware(Req2(), handler))
assert getattr(resp, "status", None) == 429, resp

# a normal ip over the minute limit still earns a ban
svc._ip_banned.pop("8.8.8.8", None)
for _ in range(3):
    asyncio.run(svc.ratelimit_middleware(Req2(), handler))
resp = asyncio.run(svc.ratelimit_middleware(Req2(), handler))
assert getattr(resp, "status", None) == 429, "4th request in a minute must ban"

print("bypass tests: ok")
