import json
import unittest
from unittest.mock import AsyncMock, patch

from app import service, solver
from app.proxy_utils import safe_proxy_label


class _FakeLoop:
    def __init__(self, infos):
        self._infos = infos

    async def getaddrinfo(self, *_args, **_kwargs):
        return self._infos


def _addrinfo(ip: str):
    return [(None, None, None, None, (ip, 0))]


class ProxyValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_allowlist = service.REQUEST_PROXY_ALLOWLIST
        self._orig_allow_private = service.ALLOW_PRIVATE_PROXY_TARGETS

    def tearDown(self):
        service.REQUEST_PROXY_ALLOWLIST = self._orig_allowlist
        service.ALLOW_PRIVATE_PROXY_TARGETS = self._orig_allow_private

    async def test_valid_authenticated_proxy(self):
        service.REQUEST_PROXY_ALLOWLIST = "proxy.example.com"
        with patch.object(service.asyncio, "get_event_loop",
                          return_value=_FakeLoop(_addrinfo("93.184.216.34"))):
            value = await service._validate_request_proxy(
                "http://user@proxy.example.com:8080")
        self.assertEqual(value, "http://user@proxy.example.com:8080")

    async def test_invalid_scheme(self):
        service.REQUEST_PROXY_ALLOWLIST = "proxy.example.com"
        with self.assertRaisesRegex(ValueError, "scheme"):
            await service._validate_request_proxy("socks5://proxy.example.com:8080")

    async def test_missing_host_or_port(self):
        service.REQUEST_PROXY_ALLOWLIST = "proxy.example.com"
        with self.assertRaisesRegex(ValueError, "host and port"):
            await service._validate_request_proxy("http://proxy.example.com")

    async def test_private_or_loopback_blocked(self):
        service.REQUEST_PROXY_ALLOWLIST = "127.0.0.1/32"
        with patch.object(service.asyncio, "get_event_loop",
                          return_value=_FakeLoop(_addrinfo("127.0.0.1"))):
            with self.assertRaisesRegex(ValueError, "not allowed"):
                await service._validate_request_proxy("http://127.0.0.1:8080")

    async def test_allowlisted_proxy_by_cidr(self):
        service.REQUEST_PROXY_ALLOWLIST = "8.8.8.0/24"
        with patch.object(service.asyncio, "get_event_loop",
                          return_value=_FakeLoop(_addrinfo("8.8.8.8"))):
            value = await service._validate_request_proxy("https://proxy.lab:8443")
        self.assertEqual(value, "https://proxy.lab:8443")


class _FakeResponse:
    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return json.dumps({
            "status": "ok",
            "solution": {
                "url": "https://example.com/",
                "cookies": [],
                "response": "<title>ok</title>",
                "userAgent": "ua",
            },
        })


class _FakeSession:
    def __init__(self, sink, **_kwargs):
        self._sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, _url, json=None):
        self._sink.append(json)
        return _FakeResponse()


class SolverProxyPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_to_server_proxy_when_request_proxy_absent(self):
        payloads = []
        with patch.object(solver, "_challenge_proxy",
                          return_value=("http://byparr:8191", "byparr")), \
                patch.object(solver, "_solver_proxy", return_value="http://warp:8080"), \
                patch.object(solver.aiohttp, "ClientSession",
                             side_effect=lambda **kwargs: _FakeSession(payloads, **kwargs)):
            await solver._solve_via_proxy("https://example.com", "rid", timeout=10, proxy=None)
        self.assertEqual(payloads[0]["proxy"]["url"], "http://warp:8080")

    async def test_propagates_request_proxy_to_byparr(self):
        mock_delegate = AsyncMock(return_value={"url": "https://ok", "title": "",
                                                "user_agent": "ua", "cookies": [], "html": ""})
        with patch.object(solver, "_challenge_proxy",
                          return_value=("http://byparr:8191", "byparr")), \
                patch.object(solver, "_solve_via_proxy", mock_delegate):
            await solver.solve_challenge_async(
                "https://example.com",
                req_id="rid",
                timeout=10,
                proxy="http://user@proxy.example.com:8080",
            )
        self.assertEqual(
            mock_delegate.await_args.kwargs["proxy"],
            "http://user@proxy.example.com:8080",
        )

    async def test_propagates_request_proxy_to_camoufox_path(self):
        isolated = AsyncMock(return_value={"url": "https://ok", "title": "",
                                           "user_agent": "ua", "cookies": [], "html": ""})
        shared = AsyncMock()
        with patch.object(solver, "_challenge_proxy", return_value=(None, "")), \
                patch.object(solver, "_solve_challenge_with_proxy", isolated), \
                patch.object(solver, "_run_in_tab", shared):
            await solver.solve_challenge_async(
                "https://example.com",
                req_id="rid",
                timeout=10,
                proxy="http://proxy.example.com:8080",
            )
        isolated.assert_awaited_once()
        shared.assert_not_called()

    async def test_shared_proxy_path_used_when_request_proxy_absent(self):
        shared = AsyncMock(return_value={"url": "https://ok", "title": "",
                                         "user_agent": "ua", "cookies": [], "html": ""})
        isolated = AsyncMock()
        with patch.object(solver, "_challenge_proxy", return_value=(None, "")), \
                patch.object(solver, "_run_in_tab", shared), \
                patch.object(solver, "_solve_challenge_with_proxy", isolated):
            await solver.solve_challenge_async(
                "https://example.com",
                req_id="rid",
                timeout=10,
                proxy=None,
            )
        shared.assert_awaited_once()
        isolated.assert_not_called()


class ProxyRedactionTests(unittest.TestCase):
    def test_proxy_label_redacts_credentials(self):
        label = safe_proxy_label("http://user@proxy.example.com:8080")
        self.assertEqual(label, "http://proxy.example.com:8080")


if __name__ == "__main__":
    unittest.main()
