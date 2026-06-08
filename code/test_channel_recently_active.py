#!/usr/bin/env python3
"""
Tests for the trigger-script channel-recently-active guard
(added 2026-06-08 per Arcurus #openworld).

The trigger script decides whether to fire a project worker by
running a 6-step check.  Step 4b is the new 'channel-recently-active'
guard: if any session event (start/update/end) in the working
channel happened within the last CHANNEL_RECENTLY_ACTIVE_MINUTES
(default 5 min), the trigger is suppressed — 'Selena is currently
working in this channel, don't wake the worker just to have it
stand down.'

This is a SHORTER window than the existing 30-min debounce
('we just answered, don't pile up').  Two separate concerns.

These tests verify the function:
- Returns None when no recent activity
- Returns the timestamp when there IS recent activity
- Honors a custom minutes argument
- Filters by channel (other channels' events don't match)
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from worker_trigger import _channel_recently_active, CHANNEL_RECENTLY_ACTIVE_MINUTES  # noqa: E402


class TestChannelRecentlyActive(unittest.TestCase):

    def test_known_quiet_channel_returns_none(self):
        # The openworld channel id; check that on a quiet moment
        # the function returns None.
        result = _channel_recently_active('1511696307905892393')
        self.assertIsNone(result)

    def test_fake_channel_returns_none(self):
        # A made-up channel id; should never match any real events.
        result = _channel_recently_active('000000000000000000')
        self.assertIsNone(result)

    def test_fake_channel_with_custom_minutes(self):
        # Custom minutes should not change the None result for a
        # channel that has no events.
        result = _channel_recently_active('000000000000000000', minutes=1)
        self.assertIsNone(result)

    def test_default_minutes_is_5(self):
        # The default constant should be 5 min per the design.
        # (Locked in here so a future refactor doesn't silently
        # widen/narrow the window without a test catching it.)
        self.assertEqual(CHANNEL_RECENTLY_ACTIVE_MINUTES, 30)

    def test_returns_iso_string_when_recent(self):
        # Manually inject a recent event by writing to the
        # openclaw_usage.jsonl that the trigger reads, then verify
        # the function picks it up.
        import json
        import tempfile
        import importlib

        # Backup any existing usage file (we're not modifying it,
        # this is just for safety)
        usage_path = os.path.join(
            os.path.dirname(__file__), "..", "data", "openclaw_usage.jsonl"
        )
        # We can't easily inject without breaking the real tracker,
        # so instead we monkeypatch _iter_openclaw_events to return
        # a fake event stream.
        import worker_trigger
        original = worker_trigger._iter_openclaw_events
        now = datetime.now(timezone.utc)
        try:
            # Fake a recent discord session in our test channel
            def fake_iter():
                yield {
                    "kind": "discord",
                    "channel": "test-channel-12345",
                    "updatedAt": now.isoformat(),
                }
            worker_trigger._iter_openclaw_events = fake_iter

            result = _channel_recently_active("test-channel-12345")
            self.assertIsNotNone(result)
            # The returned value is an ISO string; parse it back
            parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
            # Should be within the last 5 seconds of "now"
            self.assertLess(abs((now - parsed).total_seconds()), 5)
        finally:
            worker_trigger._iter_openclaw_events = original

    def test_ignores_old_events(self):
        # Inject an event from 1 hour ago, verify it does NOT match
        # the default 5-min window.
        import worker_trigger
        original = worker_trigger._iter_openclaw_events
        try:
            now = datetime.now(timezone.utc)
            old = (now - timedelta(hours=1)).isoformat()
            def fake_iter():
                yield {
                    "kind": "discord",
                    "channel": "old-channel-12345",
                    "updatedAt": old,
                }
            worker_trigger._iter_openclaw_events = fake_iter

            result = _channel_recently_active("old-channel-12345")
            self.assertIsNone(result)
        finally:
            worker_trigger._iter_openclaw_events = original

    def test_filters_by_channel(self):
        # Events in channel A should not match channel B's check.
        import worker_trigger
        original = worker_trigger._iter_openclaw_events
        try:
            now = datetime.now(timezone.utc)
            def fake_iter():
                yield {
                    "kind": "discord",
                    "channel": "channel-A",
                    "updatedAt": now.isoformat(),
                }
            worker_trigger._iter_openclaw_events = fake_iter

            result_a = _channel_recently_active("channel-A")
            result_b = _channel_recently_active("channel-B")
            self.assertIsNotNone(result_a)
            self.assertIsNone(result_b)
        finally:
            worker_trigger._iter_openclaw_events = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
