"""
TouchButtons - Touch input adapter for SeedSigner.

Provides the same interface as HardwareButtons but uses touchscreen input.
Supports direct tap detection on UI buttons.
"""

import logging
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

from seedsigner.models.singleton import Singleton


class HardwareButtonsConstants:
    """Constants matching the original HardwareButtons interface"""
    # Key codes - using same values as GPIO pins for compatibility
    KEY_UP = 31
    KEY_DOWN = 35
    KEY_LEFT = 29
    KEY_RIGHT = 37
    KEY_PRESS = 33
    KEY1 = 40
    KEY2 = 38
    KEY3 = 36

    OVERRIDE = 1000

    # Key groups
    KEYS__LEFT_RIGHT_UP_DOWN = [KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT]
    KEYS__ANYCLICK = [KEY_PRESS, KEY1, KEY2, KEY3]
    ALL_KEYS = KEYS__LEFT_RIGHT_UP_DOWN + KEYS__ANYCLICK

    # Release lock for button handling
    release_lock = True


class TouchButtons(Singleton):
    """
    Touch-based input that provides HardwareButtons-compatible interface.

    Key features:
    - Direct tap: Screens register button positions, taps return button index
    - Fallback navigation: Touch regions map to UP/DOWN/LEFT/RIGHT for screens
      that don't support direct tap
    """

    # Screen layout for 480x640 display
    # Touch bar: 480x160 (bottom) - KEY1, KEY2, KEY3
    SCREEN_WIDTH = 480
    SCREEN_HEIGHT = 640
    # The UI fills the whole panel and is now drawn at the panel's own
    # resolution, so the canvas and the touch surface share one coordinate
    # space. The control bar is an OVERLAY that only exists while a screen asks
    # for one, so the bottom strip is ordinary content the rest of the time.
    UI_HEIGHT = 640
    TOUCH_BAR_TOP = 480

    # Panel pixels -> canvas pixels.
    #
    # This was 2 while the UI drew a 240x320 canvas that DPI28.compose()
    # upscaled to the panel. Native rendering draws at 480x640 directly, so
    # touch input and the canvas are 1:1 and no conversion is needed.
    #
    # Kept as a named constant rather than deleting the arithmetic: it keeps
    # the 240x320 path a one-line revert, and it makes the remaining literal
    # `/ 2` expressions in this file unambiguously centre calculations rather
    # than forgotten scale conversions.
    PANEL_TO_CANVAS = 1

    # Top-nav corner hit regions, in canvas pixels. Mirrors
    # GUIConstants.TOP_NAV_HEIGHT (48 * SCALE); kept local so the input layer
    # does not have to import the whole GUI stack.
    CORNER_HIT_SIZE = 96

    # Height of the scroll-indicator chevrons drawn at the top and bottom of
    # the centre column. They are decoration, so taps there must not activate
    # whatever button sits underneath.
    #
    # This was previously hardcoded as `native_y < 48 or native_y > 226`, where
    # 226 assumed a 240px-tall UI. That was already stale on the 240x320
    # canvas; deriving it from UI_HEIGHT keeps it correct at any canvas size.
    SCROLL_INDICATOR_BAND = 28

    # Re-export constants
    KEY_UP = HardwareButtonsConstants.KEY_UP
    KEY_DOWN = HardwareButtonsConstants.KEY_DOWN
    KEY_LEFT = HardwareButtonsConstants.KEY_LEFT
    KEY_RIGHT = HardwareButtonsConstants.KEY_RIGHT
    KEY_PRESS = HardwareButtonsConstants.KEY_PRESS
    KEY1 = HardwareButtonsConstants.KEY1
    KEY2 = HardwareButtonsConstants.KEY2
    KEY3 = HardwareButtonsConstants.KEY3

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls.__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Initialize touch input"""
        from seedsigner.hardware.touch import TouchInput
        self.touch = TouchInput()

        self.override_ind = False

        # Timing for input handling
        self.cur_input = None
        self.cur_input_started = None
        self.last_input_time = int(time.time() * 1000)
        self.first_repeat_threshold = 225
        self.next_repeat_threshold = 250

        # Touch state
        self.last_touch_x = 0
        self.last_touch_y = 0
        self.touch_down = False
        self._held_key = None      # Which key the current touch maps to (check_for_low)
        self._held_claimed = False  # A check_for_low query matched the held key
        self._tap_latch = None     # Completed-but-unclaimed tap awaiting its matching query

        # Press feedback + drag-to-scroll
        self._press_handler = None
        self._scroll_handler = None
        self._drag_active = False
        self._drag_last_y = 0
        self._drag_total = 0

        # Direct tap support
        # List of (x, y, width, height, index) in NATIVE coords (240x240)
        self.button_rects: List[Tuple[int, int, int, int, int]] = []
        # On-canvas controls: same rects, but each carries the key code it
        # stands in for, so polling screens (check_for_low) can recognise them
        # without knowing their index.
        self.control_rects: List[Tuple[int, int, int, int, int]] = []
        self._tapped_button_index = -1
        self._back_button_tapped = False
        self._power_button_tapped = False
        self._touch_bar_back_tapped = False  # Touch bar left button (BACK in keyboard mode)

        # Last tap coordinates in native space (240x240) for keyboard detection
        self._last_tap_native_x = -1
        self._last_tap_native_y = -1

    # A touch that moves more than this (screen px) before release is a DRAG,
    # not a tap: it scrolls and must never activate whatever was under the
    # finger. Small enough that a deliberate swipe registers immediately,
    # large enough that finger roll during a tap does not.
    DRAG_THRESHOLD_PX = 10

    def _bar_visible(self) -> bool:
        """
        Is a control bar currently overlaid on the bottom of the panel?

        When it is, taps below TOUCH_BAR_TOP belong to the bar. When it is
        not, that strip is ordinary content and must be hit-tested like the
        rest of the screen - otherwise the bottom fifth of every full-bleed
        screen would silently swallow taps.
        """
        try:
            from seedsigner.gui.renderer import Renderer
            disp = Renderer.get_instance().disp
            return bool(getattr(disp, "is_touch_bar_visible", False))
        except Exception:
            return False

    def register_press_handler(self, handler):
        """
        Register handler(button_index_or_None) called the moment a finger goes
        DOWN on a registered button, and again with None when that press is
        cancelled (drag-off or drag-to-scroll).

        The skill guidance for touch UIs is a tactile press state (e.g. a
        scale(0.96) on press). Animation is not an option at ~7fps on this
        hardware, so the equivalent here is an immediate static state change:
        the row under the finger paints as pressed before the action runs.
        Without it, a tap has no acknowledgement until the whole next screen
        renders, which reads as a missed tap.
        """
        self._press_handler = handler

    def clear_press_handler(self):
        self._press_handler = None

    def register_scroll_handler(self, handler):
        """
        Register a callable invoked as handler(dy_native) while the user drags
        vertically. dy_native is the movement since the last callback in NATIVE
        (240x240) pixels: positive = finger moved down (content follows finger).
        A screen registers this to become drag-scrollable.
        """
        self._scroll_handler = handler

    def clear_scroll_handler(self):
        self._scroll_handler = None

    def register_buttons(self, buttons: list, pad_y: int = 0):
        """
        Register button positions for direct tap detection.

        Args:
            buttons: List of Button objects with screen_x, screen_y, width, height.
                     Coordinates are in NATIVE canvas space.
            pad_y:   Grow each rect by this many native px above and below, so
                     the gaps between rows stay tappable. The visible button is
                     unchanged; only the hit area grows (the touch-target
                     equivalent of extending a small control with a pseudo
                     element).
        """
        self.button_rects = []
        for i, btn in enumerate(buttons):
            if hasattr(btn, 'screen_y') and hasattr(btn, 'height'):
                x = getattr(btn, 'screen_x', 0)
                y = btn.screen_y - getattr(btn, 'scroll_y', 0)  # Account for scroll
                w = getattr(btn, 'width', 240)
                h = btn.height
                self.button_rects.append((x, y - pad_y, w, h + 2 * pad_y, i))

    def register_control_keys(self, controls: list):
        """
        Register on-canvas controls that stand in for hardware keys.

        Each control needs screen_x/screen_y/width/height in NATIVE canvas
        space plus a `touch_key`. Unlike register_buttons, which reports an
        index, these map straight to a key code, so screens that poll with
        check_for_low() (live camera loops) see them as that key.
        """
        self.control_rects = [
            (c.screen_x, c.screen_y, c.width, c.height, c.touch_key)
            for c in controls if hasattr(c, 'touch_key')
        ]

    def _control_key_at(self, touch_x: int, touch_y: int):
        """Key of the on-canvas control under this touch, or None."""
        native_x, native_y = touch_x // self.PANEL_TO_CANVAS, touch_y // self.PANEL_TO_CANVAS
        for x, y, w, h, key in self.control_rects:
            if x <= native_x <= x + w and y <= native_y <= y + h:
                return key
        return None

    def clear_buttons(self):
        """Clear registered button positions"""
        self.button_rects = []
        self.control_rects = []
        self._tapped_button_index = -1

    def get_tapped_button_index(self) -> int:
        """
        Get index of directly tapped button, or -1 if none.
        Resets after reading.
        """
        idx = self._tapped_button_index
        self._tapped_button_index = -1
        return idx

    def was_back_button_tapped(self) -> bool:
        """
        Check if back button was tapped.
        Resets after reading.
        """
        result = self._back_button_tapped
        self._back_button_tapped = False
        return result

    def was_power_button_tapped(self) -> bool:
        """
        Check if power button was tapped.
        Resets after reading.
        """
        result = self._power_button_tapped
        self._power_button_tapped = False
        return result

    def was_touch_bar_back_tapped(self) -> bool:
        """
        Check if touch bar left button (BACK) was tapped.
        Resets after reading.
        """
        result = self._touch_bar_back_tapped
        self._touch_bar_back_tapped = False
        return result

    def get_last_tap_native_coords(self) -> Tuple[int, int]:
        """
        Get the last tap coordinates in native space (240x240).
        Resets after reading.

        Returns:
            (x, y) tuple in native coords, or (-1, -1) if no tap
        """
        x, y = self._last_tap_native_x, self._last_tap_native_y
        self._last_tap_native_x = -1
        self._last_tap_native_y = -1
        return (x, y)

    def clear_pending_input(self):
        """
        Clear all pending input state.
        Call this when entering a new screen to avoid stale tap data.
        """
        self._last_tap_native_x = -1
        self._last_tap_native_y = -1
        self._tapped_button_index = -1
        self._back_button_tapped = False
        self._power_button_tapped = False
        self._touch_bar_back_tapped = False
        self._tap_latch = None
        self.touch_down = False
        # Drain any pending touch events
        while self.touch.poll():
            pass

    def _check_back_button_tap(self, touch_x: int, touch_y: int) -> bool:
        """
        Check if touch hit the back button area (top-left corner).

        Args:
            touch_x, touch_y: Touch coordinates in screen space (480x640)

        Returns:
            True if back button was tapped
        """
        native_x = touch_x // self.PANEL_TO_CANVAS
        native_y = touch_y // self.PANEL_TO_CANVAS

        # Top-left corner of the top nav.
        if native_y < self.CORNER_HIT_SIZE and native_x < self.CORNER_HIT_SIZE:
            return True
        return False

    def _check_power_button_tap(self, touch_x: int, touch_y: int) -> bool:
        """
        Check if touch hit the power button area (top-right corner).

        Args:
            touch_x, touch_y: Touch coordinates in screen space (480x640)

        Returns:
            True if power button was tapped
        """
        native_x = touch_x // self.PANEL_TO_CANVAS
        native_y = touch_y // self.PANEL_TO_CANVAS

        # Top-right corner of the top nav.
        if (native_y < self.CORNER_HIT_SIZE
                and native_x > self.SCREEN_WIDTH - self.CORNER_HIT_SIZE):
            return True
        return False

    def _check_button_tap(self, touch_x: int, touch_y: int) -> int:
        """
        Check if touch hit a registered button.

        Args:
            touch_x, touch_y: Touch coordinates in screen space (480x640)

        Returns:
            Button index or -1 if no hit
        """
        if not self.button_rects:
            return -1

        # The bar (when shown) is not part of the list
        if touch_y >= self.TOUCH_BAR_TOP and self._bar_visible():
            return -1

        # Convert screen coords (480x480) to native coords (240x240)
        native_x = touch_x // self.PANEL_TO_CANVAS
        native_y = touch_y // self.PANEL_TO_CANVAS

        # Exclude the scroll-indicator zones (centre column, top and bottom):
        # visual only, not tappable.
        if self.SCREEN_WIDTH // 3 <= native_x <= 2 * self.SCREEN_WIDTH // 3:
            if (native_y < self.CORNER_HIT_SIZE
                    or native_y > self.UI_HEIGHT - self.SCROLL_INDICATOR_BAND):
                return -1

        for x, y, w, h, index in self.button_rects:
            if x <= native_x <= x + w and y <= native_y <= y + h:
                return index

        return -1

    def _coords_to_nav_key(self, x: int, y: int) -> int:
        """
        Map touch coordinates to navigation key (fallback for non-button areas).

        Args:
            x, y: Touch coordinates in screen space (480x640)

        Returns:
            Key code (KEY_UP, KEY_DOWN, etc.)
        """
        # Touch bar - KEY1, KEY2, KEY3 (only while a bar is actually shown)
        if y >= self.TOUCH_BAR_TOP and self._bar_visible():
            third = self.SCREEN_WIDTH // 3
            if x < third:
                return self.KEY1
            elif x < 2 * third:
                return self.KEY2
            else:
                return self.KEY3

        # UI area - map to D-pad style navigation
        px = x / self.SCREEN_WIDTH  # 0-1 horizontal
        py = y / self.UI_HEIGHT      # 0-1 vertical within UI area

        # Top 20% = UP
        if py < 0.20:
            return self.KEY_UP

        # Bottom 40% = DOWN in center, LEFT/RIGHT on edges
        if py > 0.60:
            if 0.25 < px < 0.75:
                return self.KEY_DOWN
            elif px < 0.25:
                return self.KEY_LEFT
            else:
                return self.KEY_RIGHT

        # Middle 40% - LEFT/RIGHT on edges, CENTER for select
        if px < 0.25:
            return self.KEY_LEFT
        elif px > 0.75:
            return self.KEY_RIGHT
        else:
            return self.KEY_PRESS

    def _coords_to_nav_key_center_relative(self, x: int, y: int) -> int:
        """
        Map a tap to a pan direction relative to the screen centre, for full-screen
        2D-pan screens (e.g. the SeedQR zoomed transcription view) where the focused
        cell is always dead-centre.

        The default d-pad mapping puts an exit (KEY_PRESS) block through the middle
        and exit buttons along the touch bar, which pincer the DOWN target so stray
        taps exit instead of pan. Here the whole UI area pans: direction is just the
        dominant axis of the tap from centre, so a pan tap can never exit. Exit stays
        on the touch bar (and the top-left back corner, handled in wait_for).

        Args:
            x, y: Touch coordinates in screen space (480x640)

        Returns:
            A KEY_* code, or -1 for a near-centre no-op (ignored by wait_for).
        """
        # Touch bar -> exit buttons, identical to the default mapping.
        if y >= self.TOUCH_BAR_TOP and self._bar_visible():
            third = self.SCREEN_WIDTH // 3
            if x < third:
                return self.KEY1
            elif x < 2 * third:
                return self.KEY2
            else:
                return self.KEY3

        # UI area: pan toward the tap. A small central dead zone is a no-op so a tap
        # on the centred cell itself doesn't jitter the view (0.15 is tunable).
        dx = x - self.SCREEN_WIDTH / 2
        dy = y - self.UI_HEIGHT / 2
        dead_zone = self.UI_HEIGHT * 0.15
        if abs(dx) < dead_zone and abs(dy) < dead_zone:
            return -1

        if abs(dx) > abs(dy):
            return self.KEY_LEFT if dx < 0 else self.KEY_RIGHT
        return self.KEY_UP if dy < 0 else self.KEY_DOWN

    def _tap_zone(self, x: int, y: int, keys, nav_relative_center: bool = False) -> tuple:
        """
        Classify a touch coordinate into the control "zone" it targets, mirroring
        the dispatch order in wait_for(): back corner, power corner, registered
        button rect (only when direct taps are live, i.e. KEY_PRESS requested),
        then the nav-key region. Used to compare touch-down vs release positions:
        a release in a DIFFERENT zone cancels the tap (standard drag-off-to-cancel
        touch hygiene) so a finger sliding off a control can never activate it.
        """
        if self._check_back_button_tap(x, y):
            return ('back',)
        if self._check_power_button_tap(x, y):
            return ('power',)
        if self.KEY_PRESS in keys:
            btn_idx = self._check_button_tap(x, y)
            if btn_idx >= 0:
                return ('button', btn_idx)
        if nav_relative_center:
            return ('nav', self._coords_to_nav_key_center_relative(x, y))
        return ('nav', self._coords_to_nav_key(x, y))

    def wait_for(self, keys=[], check_release=True, release_keys=[], timeout_ms=0, nav_relative_center=False) -> int:
        """
        Wait for touch input matching requested keys.

        This is the main input method called by SeedSigner screens.

        Args:
            keys: List of key codes to listen for
            check_release: Whether to wait for touch release
            release_keys: Keys that require release detection
            timeout_ms: If > 0, return None after this many milliseconds with no input

        Returns:
            Key code that was activated, or None on timeout
        """
        from seedsigner.controller import Controller
        controller = Controller.get_instance()

        if not release_keys:
            release_keys = keys

        self.override_ind = False
        pending_key = None  # Key waiting for release
        pending_zone = None  # Zone the touch-down landed in (drag-off-cancel check)
        wait_start = int(time.time() * 1000) if timeout_ms > 0 else 0

        while True:
            cur_time = int(time.time() * 1000)

            if timeout_ms > 0 and cur_time - wait_start >= timeout_ms:
                return None

            # Screensaver check (a View may opt out, e.g. SeedQR transcription)
            if (cur_time - self.last_input_time > controller.screensaver_activation_ms
                    and controller.is_screensaver_start_allowed):
                controller.start_screensaver()
                self.update_last_input_time()
                time.sleep(self.next_repeat_threshold / 1000.0)
                continue

            # Override handling (for toast/notifications)
            if self.override_ind:
                self.override_ind = False
                return HardwareButtonsConstants.OVERRIDE

            # Poll for touch events
            event = self.touch.poll()
            if event:
                event_type, x, y = event
                logger.debug(f"Touch {event_type} at ({x}, {y})")

                if event_type == 'down':
                    self.touch_down = True
                    # wait_for owns this touch; don't let check_for_low ghost-match it
                    self._held_key = None
                    self._tap_latch = None
                    self.last_touch_x = x
                    self.last_touch_y = y
                    pending_zone = self._tap_zone(x, y, keys, nav_relative_center)
                    # Begin (potential) drag tracking. Nothing scrolls until the
                    # finger passes DRAG_THRESHOLD_PX, so a plain tap is unaffected.
                    self._drag_active = False
                    self._drag_last_y = y
                    self._drag_total = 0

                    # Store native coords for keyboard/grid tap detection. Skip
                    # only the bar region, and only while a bar is showing.
                    if y < self.TOUCH_BAR_TOP or not self._bar_visible():
                        self._last_tap_native_x = x // self.PANEL_TO_CANVAS
                        self._last_tap_native_y = y // self.PANEL_TO_CANVAS

                    # Check for back button tap (top-left corner)
                    if self._check_back_button_tap(x, y):
                        logger.debug("Back button tapped")
                        self._back_button_tapped = True
                        pending_key = self.KEY_PRESS
                        self.cur_input = self.KEY_PRESS
                        self.cur_input_started = cur_time
                        self.last_input_time = cur_time
                        if not check_release or self.KEY_PRESS not in release_keys:
                            return self.KEY_PRESS
                        continue

                    # Check for power button tap (top-right corner)
                    if self._check_power_button_tap(x, y):
                        logger.debug("Power button tapped")
                        self._power_button_tapped = True
                        pending_key = self.KEY_PRESS
                        self.cur_input = self.KEY_PRESS
                        self.cur_input_started = cur_time
                        self.last_input_time = cur_time
                        if not check_release or self.KEY_PRESS not in release_keys:
                            return self.KEY_PRESS
                        continue

                    # Press feedback: acknowledge the touch immediately.
                    if self._press_handler is not None:
                        pressed_idx = self._check_button_tap(x, y)
                        if pressed_idx >= 0:
                            self._press_handler(pressed_idx)

                    # Check for direct button tap
                    if self.KEY_PRESS in keys:
                        btn_idx = self._check_button_tap(x, y)
                        logger.debug(f"Button check: {btn_idx}, registered: {len(self.button_rects)}")
                        if btn_idx >= 0:
                            self._tapped_button_index = btn_idx
                            pending_key = self.KEY_PRESS
                            self.cur_input = self.KEY_PRESS
                            self.cur_input_started = cur_time
                            self.last_input_time = cur_time
                            logger.debug(f"Direct tap on button {btn_idx}, waiting for release")
                            # If not checking release, return immediately
                            if not check_release or self.KEY_PRESS not in release_keys:
                                return self.KEY_PRESS
                            continue

                    # Fall back to navigation key mapping
                    if nav_relative_center:
                        key = self._coords_to_nav_key_center_relative(x, y)
                    else:
                        key = self._coords_to_nav_key(x, y)
                    logger.debug(f"Nav key: {key}, in keys: {key in keys}")

                    # Track if touch bar BACK (left button) was tapped
                    if key == self.KEY1 and y >= self.TOUCH_BAR_TOP:
                        logger.debug("Touch bar BACK tapped")
                        self._touch_bar_back_tapped = True

                    if key in keys:
                        pending_key = key
                        self.cur_input = key
                        self.cur_input_started = cur_time
                        self.last_input_time = cur_time
                        # If not checking release, return immediately
                        if not check_release or key not in release_keys:
                            return key
                        # Otherwise wait for release
                        continue

                elif event_type == 'move':
                    # Only the UI area scrolls; the touch bar is a button strip.
                    if self._scroll_handler is not None and (
                            self.last_touch_y < self.TOUCH_BAR_TOP or not self._bar_visible()):
                        self._drag_total += abs(y - self._drag_last_y)
                        if not self._drag_active and self._drag_total >= self.DRAG_THRESHOLD_PX:
                            # Promote to a drag: this touch can no longer activate
                            # anything, and any tap flags latched at touch-down
                            # must be dropped.
                            self._drag_active = True
                            if self._press_handler is not None:
                                self._press_handler(None)   # press became a scroll
                            pending_key = None
                            pending_zone = None
                            self._tapped_button_index = -1
                            self._back_button_tapped = False
                            self._power_button_tapped = False
                            self._touch_bar_back_tapped = False
                            self._last_tap_native_x = -1
                            self._last_tap_native_y = -1
                        if self._drag_active:
                            dy_screen = y - self._drag_last_y
                            if dy_screen:
                                self.last_input_time = cur_time
                                self._scroll_handler(dy_screen / float(self.PANEL_TO_CANVAS))
                    self._drag_last_y = y

                elif event_type == 'up':
                    self.touch_down = False
                    self._held_key = None
                    self._tap_latch = None
                    logger.debug(f"Release, pending_key: {pending_key}")
                    if self._drag_active:
                        # Finished a scroll gesture: consume it, activate nothing.
                        self._drag_active = False
                        pending_key = None
                        pending_zone = None
                        continue
                    if pending_key is not None:
                        # Drag-off-to-cancel: only activate if the release landed in
                        # the same zone as the touch-down. A finger that slid onto a
                        # different control (or off all controls) aborts the tap.
                        release_zone = self._tap_zone(x, y, keys, nav_relative_center)
                        if release_zone != pending_zone:
                            logger.debug(f"Tap cancelled: down zone {pending_zone} != release zone {release_zone}")
                            if self._press_handler is not None:
                                self._press_handler(None)
                            pending_key = None
                            pending_zone = None
                            self.cur_input = None
                            # Clear any tap flags latched at touch-down
                            self._tapped_button_index = -1
                            self._back_button_tapped = False
                            self._power_button_tapped = False
                            self._touch_bar_back_tapped = False
                            continue

                        key = pending_key
                        pending_key = None
                        pending_zone = None
                        self.cur_input = None
                        HardwareButtonsConstants.release_lock = True
                        logger.debug(f"Returning key: {key}")
                        return key

            time.sleep(0.01)  # Small delay to avoid busy loop

    def update_last_input_time(self):
        """Update timestamp of last input"""
        self.last_input_time = int(time.time() * 1000)

    def add_events(self, keys):
        """Compatibility method - no-op for touch"""
        pass

    def trigger_override(self):
        """Trigger override input (for notifications)"""
        self.override_ind = True

    def has_any_input(self) -> bool:
        """Check if there's any touch input (polls for events)"""
        # Poll for touch events to update state
        event = self.touch.poll()
        if event:
            event_type, x, y = event
            if event_type == 'down':
                self.touch_down = True
                self._held_key = self._classify_tap(x, y)
                return True
            elif event_type == 'up':
                self.touch_down = False
                self._held_key = None
                return True  # Return True on release too to wake from screensaver
        return self.touch_down

    def _classify_tap(self, x: int, y: int) -> int:
        """
        Map a touch to the key it should count as for check_for_low():
        back corner -> KEY1, touch bar thirds -> KEY1/KEY2/KEY3,
        anywhere else in the UI area -> KEY_PRESS.
        """
        control_key = self._control_key_at(x, y)
        if control_key is not None:
            return control_key
        if self._check_back_button_tap(x, y):
            return self.KEY1
        if y >= self.TOUCH_BAR_TOP and self._bar_visible():
            third = self.SCREEN_WIDTH // 3
            if x < third:
                return self.KEY1
            elif x < 2 * third:
                return self.KEY2
            else:
                return self.KEY3
        return self.KEY_PRESS

    def check_for_low(self, key: int = None, keys: list = None) -> bool:
        """
        Check if specified key(s) are pressed.

        For touch, the current touch position maps to a key via _classify_tap()
        and only a match on the requested key(s) returns True. (Previously ANY
        touch matched ANY query, so screens polling back-then-snap could never
        reach the snap check: every tap read as "back".)
        """
        requested = set()
        if key is not None:
            requested.add(key)
        if keys:
            requested.update(keys)

        event = self.touch.poll()
        if event:
            event_type, x, y = event
            if event_type == "down":
                self.touch_down = True
                self._held_key = self._classify_tap(x, y)
                self._held_claimed = False
                self._tap_latch = None
                self.update_last_input_time()
            elif event_type == "up":
                # A quick tap's down/up events may each be consumed by DIFFERENT
                # check_for_low calls (screens poll several key sets per loop).
                # If no query matched while the touch was down, latch the tap so
                # the query that DOES ask for its key still sees it.
                if self.touch_down and not self._held_claimed:
                    self._tap_latch = self._held_key
                self.touch_down = False
                self._held_key = None

        if self._tap_latch is not None and (not requested or self._tap_latch in requested):
            self._tap_latch = None
            return True

        if not self.touch_down:
            return False
        if not requested:
            return True
        if self._held_key in requested:
            self._held_claimed = True
            return True
        return False



# Factory function to get appropriate input handler
def get_buttons():
    """
    Get the appropriate button handler based on environment.

    Returns TouchButtons for touchscreen, HardwareButtons for GPIO.
    """
    import os
    if os.environ.get('SEEDSIGNER_TOUCH') == '1':
        return TouchButtons.get_instance()
    else:
        from seedsigner.hardware.buttons import HardwareButtons
        return HardwareButtons.get_instance()
