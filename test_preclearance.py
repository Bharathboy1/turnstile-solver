"""Unit checks for the pre-clearance endpoint contract (no browser needed)."""

import os
import sys
import types

from app.solver import _valid_preclearance_url


assert _valid_preclearance_url(
    "https://vplink.in/cdn-cgi/challenge-platform/h/b/c/abc123",
    "https://vplink.in/ALT8bj",
)
assert not _valid_preclearance_url(
    "https://attacker.example/cdn-cgi/challenge-platform/h/b/c/abc123",
    "https://vplink.in/ALT8bj",
)
assert not _valid_preclearance_url(
    "http://vplink.in/cdn-cgi/challenge-platform/h/b/c/abc123",
    "https://vplink.in/ALT8bj",
)
assert not _valid_preclearance_url(
    "https://vplink.in/not-a-clearance-endpoint",
    "https://vplink.in/ALT8bj",
)

# app.service imports Camoufox-facing functions at module load. Replace that
# module to test its HTTP payload parser without launching a browser.
fake = types.ModuleType("app.solver")


async def _noop(*args, **kwargs):
    return {}


fake.get_pool = _noop
fake.solve_async = _noop
fake.solve_preclearance_async = _noop
fake.solve_challenge_async = _noop
fake.solve_recaptcha_v3_async = _noop
fake.solve_aws_token_async = _noop
fake._challenge_proxy = lambda: (None, None)
fake._solver_proxy = lambda: None
sys.modules["app.solver"] = fake
os.environ.setdefault("RATE_LIMIT_PER_MIN", "0")

import app.service as service  # noqa: E402


siteurl, sitekey, run = service._parse_preclearance(
    {
        "siteurl": "https://vplink.in/ALT8bj",
        "sitekey": "0x4AAAAAAFTiirsi0MrdSv3r",
        "action": "ip_1-2-3-4",
        "cdata": "abc123",
    }
)
assert siteurl == "https://vplink.in/ALT8bj"
assert sitekey == "0x4AAAAAAFTiirsi0MrdSv3r"
assert callable(run)
assert "/solve-preclearance" in service._ENDPOINTS
assert "/solve-preclearance" in service._SOLVE_PATHS

print("pre-clearance tests: ok")
