"""Tests for the new channel-last-message check (added 2026-06-08 per Arcurus #openworld)."""
import sys
import unittest
sys.path.insert(0, '/home/openclaw/openclaw/workspace/selena-project/code')
from worker_trigger import _channel_last_message_at, _CHANNEL_LAST_MESSAGE_CACHE, _DISCORD_BOT_TOKEN_CACHE, _DISCORD_BOT_TOKEN_RESOLVED


class TestChannelLastMessage(unittest.TestCase):

    def setUp(self):
        # Clear the in-memory cache so each test gets a fresh attempt
        _CHANNEL_LAST_MESSAGE_CACHE.clear()
        global _DISCORD_BOT_TOKEN_CACHE, _DISCORD_BOT_TOKEN_RESOLVED
        _DISCORD_BOT_TOKEN_CACHE = None
        _DISCORD_BOT_TOKEN_RESOLVED = False

    def test_fake_channel_returns_none(self):
        # No real channel; the function should return None on API failure
        # rather than raising (graceful degradation, no skip on error)
        result = _channel_last_message_at('999999999999999999')
        self.assertIsNone(result)

    def test_empty_channel_id_returns_none(self):
        # Defensive: empty / None channel IDs should not call the API
        self.assertIsNone(_channel_last_message_at(''))
        self.assertIsNone(_channel_last_message_at(None))

    def test_caches_failed_calls(self):
        # If the API call fails, the None is cached for the cache window
        # so we don't retry on every call.
        r1 = _channel_last_message_at('888888888888888888')
        r2 = _channel_last_message_at('888888888888888888')
        self.assertIsNone(r1)
        self.assertIsNone(r2)
        # The cache should have a (None, datetime) entry
        self.assertIn('888888888888888888', _CHANNEL_LAST_MESSAGE_CACHE)
        cached = _CHANNEL_LAST_MESSAGE_CACHE['888888888888888888']
        self.assertIsNone(cached[0])


if __name__ == '__main__':
    unittest.main()
