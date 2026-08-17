"""
Touch input handler for capacitive touchscreen.

On Raspberry Pi: Reads raw Linux input events from /dev/input/
On PC/Emulator: Uses injected mock that translates pygame mouse events
"""

import logging
import struct
import select
import os
import time
from collections import deque
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# Linux input event codes (from linux/input-event-codes.h)
EV_SYN = 0x00
EV_ABS = 0x03
ABS_MT_TRACKING_ID = 0x39
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36

# Touch panel native resolution (Waveshare 2.8" DPI LCD)
TOUCH_WIDTH = 640
TOUCH_HEIGHT = 480


class TouchInput:
    """
    Handles touch input from capacitive touchscreen.
    Reads raw Linux input events - no external dependencies required.

    Taps are queued and delivered strictly in order, one per poll() call, so a
    tap that completes while the main loop is busy rendering is delayed, never
    dropped. Two guards keep the queue honest:
    - a re-touch within DEBOUNCE_MS of a release is contact bounce, not a new
      tap, and is not queued
    - a queued tap older than MAX_EVENT_AGE_MS is discarded at delivery time:
      the screen it was aimed at may no longer be showing
    """

    # input_event struct on 32-bit ARM (Pi Zero):
    # tv_sec(4) + tv_usec(4) + type(2) + code(2) + value(4) = 16 bytes
    EVENT_SIZE = 16

    # A new touch this soon (ms) after a release is contact bounce / finger
    # micro-lift. Deliberate T9 multi-taps run >= ~120ms apart, so legitimate
    # rapid taps always clear this window.
    DEBOUNCE_MS = 50

    # Never deliver a tap older than this (ms). Kernel event timestamps and
    # time.time() both read CLOCK_REALTIME, so the comparison is valid even
    # with no RTC/NTP. Render-blocked delays are a few hundred ms, so real
    # delayed taps always survive this cutoff.
    MAX_EVENT_AGE_MS = 1000

    def __init__(self, device_path: str = None,
                 screen_width: int = 480, screen_height: int = 640,
                 swap_xy: bool = False, invert_x: bool = False, invert_y: bool = False):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.swap_xy = swap_xy
        self.invert_x = invert_x
        self.invert_y = invert_y

        self.x = 0
        self.y = 0
        self.touching = False
        self.fd = None

        # Queued ("down"|"up", x, y, ts_ms) tuples awaiting delivery
        self._pending_events = deque()
        self._last_up_ms = None  # timestamp of last release; None until first up
        # Packet state persists across reads in case a SYN-delimited packet is
        # split between two drains
        self._pending_down = False
        self._pending_up = False
        self._last_move = None

        self._init_device(device_path)

    def _find_touch_device(self) -> Optional[str]:
        """Find touch input device by scanning /dev/input/"""
        for i in range(10):
            name_path = f"/sys/class/input/event{i}/device/name"
            if os.path.exists(name_path):
                try:
                    with open(name_path, "r") as f:
                        name = f.read().strip()
                        if "Goodix" in name or "touch" in name.lower():
                            return f"/dev/input/event{i}"
                except Exception:
                    pass
        return None

    def _init_device(self, device_path: str = None):
        """Initialize raw input device access"""
        path = device_path or self._find_touch_device()

        if path and os.path.exists(path):
            try:
                self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                logger.info(f"Opened touch device: {path}")
            except Exception as e:
                logger.warning(f"Failed to open {path}: {e}")
                self.fd = None
        else:
            logger.info("No touch device found")

    def poll(self) -> Optional[Tuple[str, int, int]]:
        """
        Poll for touch events (non-blocking).

        Delivers queued down/up events strictly in order, ONE per call: if the
        caller was busy while a whole tap (down+up) accumulated in the kernel
        buffer, both events still come through on consecutive calls instead of
        the down being lost.
        """
        if self.fd is not None:
            try:
                self._drain_kernel_events()
            except Exception:
                pass

        now_ms = int(time.time() * 1000)
        while self._pending_events:
            event_type, x, y, ts_ms = self._pending_events.popleft()
            if event_type == "down" and now_ms - ts_ms > self.MAX_EVENT_AGE_MS:
                # Stale tap: the loop was blocked so long the screen may have
                # changed; firing it now would tap something the user never
                # aimed at. Its paired "up" still delivers (an unmatched
                # release is a no-op downstream and keeps touch state honest).
                continue
            return (event_type, x, y)

        if self._last_move is not None:
            move = self._last_move
            self._last_move = None
            if self.touching:
                return move
        return None

    def _drain_kernel_events(self):
        """Read all buffered kernel input events into the pending queue."""
        r, _, _ = select.select([self.fd], [], [], 0)
        if not r:
            return

        while True:
            try:
                data = os.read(self.fd, self.EVENT_SIZE)
            except Exception:
                break
            if len(data) < self.EVENT_SIZE:
                break

            (tv_sec, tv_usec) = struct.unpack("II", data[0:8])
            (ev_type, ev_code, ev_value) = struct.unpack("HHi", data[8:16])
            self._process_event(tv_sec * 1000 + tv_usec // 1000, ev_type, ev_code, ev_value)

    def _process_event(self, ts_ms: int, ev_type: int, ev_code: int, ev_value: int):
        """Feed one input event through the packet state machine."""
        if ev_type == EV_ABS:
            if ev_code == ABS_MT_POSITION_X:
                self.x = ev_value
            elif ev_code == ABS_MT_POSITION_Y:
                self.y = ev_value
            elif ev_code == ABS_MT_TRACKING_ID:
                if ev_value >= 0:
                    self.touching = True
                    self._pending_down = True
                else:
                    self.touching = False
                    self._pending_up = True

        elif ev_type == EV_SYN:
            # SYN marks end of event packet - coordinates now complete
            x, y = self._transform(self.x, self.y)
            if self._pending_down:
                self._pending_down = False
                # Debounce only against an actually-seen release, so behavior
                # doesn't depend on the kernel clock's timestamp base
                if self._last_up_ms is None or ts_ms - self._last_up_ms >= self.DEBOUNCE_MS:
                    self._pending_events.append(("down", x, y, ts_ms))
                # else: contact bounce - the same physical touch continuing;
                # queuing it would fire a second tap the user never made
            elif self._pending_up:
                self._pending_up = False
                self._last_up_ms = ts_ms
                self._pending_events.append(("up", x, y, ts_ms))
            elif self.touching:
                self._last_move = ("move", x, y)

    def _transform(self, raw_x: int, raw_y: int) -> Tuple[int, int]:
        """Transform raw touch coordinates to screen coordinates."""
        # Scale from touch panel resolution to screen resolution
        screen_x = raw_x * self.screen_width // TOUCH_WIDTH
        screen_y = raw_y * self.screen_height // TOUCH_HEIGHT

        if self.swap_xy:
            screen_x, screen_y = screen_y, screen_x

        if self.invert_x:
            screen_x = self.screen_width - 1 - screen_x

        if self.invert_y:
            screen_y = self.screen_height - 1 - screen_y

        # Clamp to screen bounds
        screen_x = max(0, min(self.screen_width - 1, screen_x))
        screen_y = max(0, min(self.screen_height - 1, screen_y))

        return screen_x, screen_y

    def inject_event(self, event_type: str, x: int, y: int):
        """
        Emulator/test hook: queue a synthetic touch event.

        Coordinates are SCREEN coordinates (480x640), i.e. already transformed —
        injection bypasses _transform() and the contact-bounce debounce, which
        only apply to raw kernel events.
        """
        ts_ms = int(time.time() * 1000)
        if event_type == "down":
            self.touching = True
        elif event_type == "up":
            self.touching = False
        self._pending_events.append((event_type, x, y, ts_ms))

    def close(self):
        """Close the device"""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
