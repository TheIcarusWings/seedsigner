"""
Tests for TouchInput coordinate transformation and event parsing.

Tests the pure logic in seedsigner.hardware.touch without requiring
any hardware (no /dev/input, no evdev, no touchscreen).
"""

import struct
import time
import pytest
from unittest.mock import patch, MagicMock

from seedsigner.hardware.touch import (
    TouchInput,
    TOUCH_WIDTH, TOUCH_HEIGHT,
    EV_SYN, EV_ABS, ABS_MT_TRACKING_ID, ABS_MT_POSITION_X, ABS_MT_POSITION_Y,
)


class TestTouchInputTransform:
    """Test _transform() coordinate mapping from raw touch panel to screen coords."""

    def _make_touch(self, **kwargs):
        """Create a TouchInput with device init disabled."""
        with patch.object(TouchInput, '_init_device'):
            return TouchInput(**kwargs)

    def test_center_point(self):
        """Center of touch panel maps to center of screen."""
        t = self._make_touch(screen_width=480, screen_height=640)
        x, y = t._transform(TOUCH_WIDTH // 2, TOUCH_HEIGHT // 2)
        assert x == 240
        assert y == 320

    def test_origin(self):
        """Origin (0,0) maps to (0,0)."""
        t = self._make_touch(screen_width=480, screen_height=640)
        x, y = t._transform(0, 0)
        assert x == 0
        assert y == 0

    def test_max_point(self):
        """Maximum raw coords map to near screen max."""
        t = self._make_touch(screen_width=480, screen_height=640)
        x, y = t._transform(TOUCH_WIDTH - 1, TOUCH_HEIGHT - 1)
        # Should be close to but not exceeding screen bounds
        assert 0 <= x <= 479
        assert 0 <= y <= 639

    def test_scaling(self):
        """Raw coords scale proportionally to screen dimensions."""
        t = self._make_touch(screen_width=480, screen_height=640)
        # Quarter point on touch panel
        x, y = t._transform(TOUCH_WIDTH // 4, TOUCH_HEIGHT // 4)
        assert x == 120  # 480/4
        assert y == 160  # 640/4

    def test_swap_xy(self):
        """swap_xy swaps the output coordinates."""
        t = self._make_touch(screen_width=480, screen_height=640, swap_xy=True)
        x, y = t._transform(TOUCH_WIDTH // 2, 0)
        # After scaling: x=240, y=0, then swap: x=0, y=240
        assert x == 0
        assert y == 240

    def test_invert_x(self):
        """invert_x mirrors the X axis."""
        t = self._make_touch(screen_width=480, screen_height=640, invert_x=True)
        x, y = t._transform(0, 0)
        assert x == 479  # screen_width - 1 - 0
        assert y == 0

    def test_invert_y(self):
        """invert_y mirrors the Y axis."""
        t = self._make_touch(screen_width=480, screen_height=640, invert_y=True)
        x, y = t._transform(0, 0)
        assert x == 0
        assert y == 639  # screen_height - 1 - 0

    def test_invert_both(self):
        """Both axes inverted flips the whole screen."""
        t = self._make_touch(screen_width=480, screen_height=640,
                             invert_x=True, invert_y=True)
        x, y = t._transform(0, 0)
        assert x == 479
        assert y == 639

    def test_clamping_negative(self):
        """Negative raw values get clamped to 0."""
        t = self._make_touch(screen_width=480, screen_height=640)
        x, y = t._transform(-100, -100)
        assert x == 0
        assert y == 0

    def test_clamping_overflow(self):
        """Values beyond touch panel range get clamped to screen max."""
        t = self._make_touch(screen_width=480, screen_height=640)
        x, y = t._transform(TOUCH_WIDTH * 2, TOUCH_HEIGHT * 2)
        assert x == 479
        assert y == 639


class TestTouchInputPoll:
    """Test poll() event parsing from raw Linux input_event structs."""

    def _make_touch(self):
        """Create a TouchInput with a fake fd."""
        with patch.object(TouchInput, '_init_device'):
            t = TouchInput(screen_width=480, screen_height=640)
            t.fd = 999  # Fake file descriptor
            return t

    def _make_event(self, ev_type, ev_code, ev_value, ts_ms=None):
        """Build a raw 16-byte Linux input_event struct, stamped 'now' unless given."""
        # tv_sec(4) + tv_usec(4) + type(2) + code(2) + value(4) = 16 bytes
        # A realistic timestamp matters: poll() discards taps staler than
        # TouchInput.MAX_EVENT_AGE_MS by design
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)
        return struct.pack("IIHHi", ts_ms // 1000, (ts_ms % 1000) * 1000, ev_type, ev_code, ev_value)

    def test_poll_no_device(self):
        """poll() returns None when no device is open."""
        with patch.object(TouchInput, '_init_device'):
            t = TouchInput()
            t.fd = None
            assert t.poll() is None

    def test_poll_touch_down(self):
        """Touch down event produces ('down', x, y)."""
        t = self._make_touch()
        # os.read is called per EVENT_SIZE (16 bytes) in a loop
        events = [
            self._make_event(EV_ABS, ABS_MT_TRACKING_ID, 1),   # finger down
            self._make_event(EV_ABS, ABS_MT_POSITION_X, 320),  # raw x
            self._make_event(EV_ABS, ABS_MT_POSITION_Y, 240),  # raw y
            self._make_event(EV_SYN, 0, 0),                     # sync
        ]
        with patch('select.select', return_value=([t.fd], [], [])):
            with patch('os.read', side_effect=events + [BlockingIOError()]):
                result = t.poll()
        assert result is not None
        event_type, x, y = result
        assert event_type == "down"
        assert t.touching is True

    def test_poll_touch_up(self):
        """Touch up event produces ('up', x, y)."""
        t = self._make_touch()
        t.touching = True
        events = [
            self._make_event(EV_ABS, ABS_MT_TRACKING_ID, -1),  # finger up
            self._make_event(EV_SYN, 0, 0),                     # sync
        ]
        with patch('select.select', return_value=([t.fd], [], [])):
            with patch('os.read', side_effect=events + [BlockingIOError()]):
                result = t.poll()
        assert result is not None
        event_type, x, y = result
        assert event_type == "up"
        assert t.touching is False

    def test_poll_no_data(self):
        """poll() returns None when select says no data ready."""
        t = self._make_touch()
        with patch('select.select', return_value=([], [], [])):
            assert t.poll() is None


class TestTouchEventQueue:
    """
    Test the tap-event queue: taps that accumulate while the main loop is busy
    rendering must deliver in order, with bounce and staleness guards.
    Feeds _process_event() directly (the post-struct-parsing entry point).
    """

    def _make_touch(self):
        # No device on the test host: fd stays None, so poll() only serves the queue
        with patch.object(TouchInput, '_init_device'):
            return TouchInput(screen_width=480, screen_height=640)

    def _send_touch_edge(self, t, ts_ms, down, raw_x=100, raw_y=100):
        """Feed one complete down or up packet through the event state machine."""
        t._process_event(ts_ms, EV_ABS, ABS_MT_TRACKING_ID, 0 if down else -1)
        t._process_event(ts_ms, EV_ABS, ABS_MT_POSITION_X, raw_x)
        t._process_event(ts_ms, EV_ABS, ABS_MT_POSITION_Y, raw_y)
        t._process_event(ts_ms, EV_SYN, 0, 0)

    def _drain_types(self, t):
        events = []
        while True:
            event = t.poll()
            if event is None:
                return events
            events.append(event[0])

    def test_buffered_tap_is_not_swallowed(self):
        """A whole tap (down+up) buffered during a render delivers BOTH events in order."""
        t = self._make_touch()
        now = int(time.time() * 1000)
        self._send_touch_edge(t, now - 200, down=True)
        self._send_touch_edge(t, now - 150, down=False)
        assert self._drain_types(t) == ["down", "up"]

    def test_contact_bounce_fires_once(self):
        """Finger micro-lift mid-press: the re-down within DEBOUNCE_MS must not re-fire."""
        t = self._make_touch()
        now = int(time.time() * 1000)
        self._send_touch_edge(t, now - 500, down=True)
        self._send_touch_edge(t, now - 400, down=False)
        self._send_touch_edge(t, now - 380, down=True)   # bounce: 20ms after release
        self._send_touch_edge(t, now - 100, down=False)
        events = self._drain_types(t)
        assert events.count("down") == 1
        assert events[0] == "down"

    def test_deliberate_double_tap_fires_twice(self):
        """T9 multi-tap cycling: two taps ~200ms apart must both register."""
        t = self._make_touch()
        now = int(time.time() * 1000)
        self._send_touch_edge(t, now - 500, down=True)
        self._send_touch_edge(t, now - 420, down=False)
        self._send_touch_edge(t, now - 220, down=True)   # 200ms after release
        self._send_touch_edge(t, now - 140, down=False)
        assert self._drain_types(t) == ["down", "up", "down", "up"]

    def test_stale_tap_is_discarded(self):
        """A tap older than MAX_EVENT_AGE_MS must not fire on whatever screen shows now."""
        t = self._make_touch()
        now = int(time.time() * 1000)
        self._send_touch_edge(t, now - 5000, down=True)
        self._send_touch_edge(t, now - 4900, down=False)
        assert "down" not in self._drain_types(t)

    def test_render_delayed_tap_survives(self):
        """A few hundred ms of render blocking is the case the queue exists for."""
        t = self._make_touch()
        now = int(time.time() * 1000)
        self._send_touch_edge(t, now - 400, down=True)
        assert self._drain_types(t) == ["down"]

    def test_empty_queue_returns_none(self):
        t = self._make_touch()
        assert t.poll() is None

    def test_coordinates_are_transformed(self):
        """Queued events carry transformed screen coords, not raw panel coords."""
        t = self._make_touch()
        self._send_touch_edge(t, int(time.time() * 1000), down=True, raw_x=320, raw_y=240)
        event = t.poll()
        assert event is not None
        event_type, x, y = event
        assert event_type == "down"
        assert (x, y) == (240, 320)  # 320/640*480, 240/480*640
