"""Unit checks for exclusive Turnstile page reuse and bounded eviction."""

import unittest

from app.solver import BrowserSingleton


class FakePage:
    def __init__(self):
        self.closed = False
        self.unrouted = False

    def is_closed(self):
        return self.closed

    async def evaluate(self, _script):
        return True

    async def unroute_all(self):
        self.unrouted = True

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.created = []

    async def new_page(self):
        page = FakePage()
        self.created.append(page)
        return page


class TurnstilePagePoolTests(unittest.IsolatedAsyncioTestCase):
    def make_pool(self, size=2):
        pool = BrowserSingleton(max_concurrent=4)
        pool.browser = FakeContext()
        pool.stopped = False
        pool._turnstile_cache_size = size
        return pool

    async def test_page_is_exclusively_leased(self):
        pool = self.make_pool()
        key = ("https://example.test/", "sitekey", "", "")
        page = FakePage()

        await pool.release_turnstile_page(key, page)
        leased, reused = await pool.acquire_turnstile_page(key)
        concurrent, concurrent_reused = await pool.acquire_turnstile_page(key)

        self.assertIs(leased, page)
        self.assertTrue(reused)
        self.assertIsNot(concurrent, page)
        self.assertFalse(concurrent_reused)

    async def test_lru_eviction_closes_oldest_page(self):
        pool = self.make_pool(size=1)
        old = FakePage()
        new = FakePage()
        old_key = ("https://old.test/", "old", "", "")
        new_key = ("https://new.test/", "new", "", "")

        await pool.release_turnstile_page(old_key, old)
        await pool.release_turnstile_page(new_key, new)

        self.assertTrue(old.closed)
        self.assertTrue(old.unrouted)
        self.assertFalse(new.closed)
        self.assertNotIn(old_key, pool._turnstile_pages)
        self.assertIn(new_key, pool._turnstile_pages)


if __name__ == "__main__":
    unittest.main()
