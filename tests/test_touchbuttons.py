"""
Tests for TouchButtons coordinate-to-key mapping and button hit detection.

Tests the pure logic in seedsigner.hardware.touchbuttons without requiring
any hardware or the full SeedSigner stack.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# Mock hardware dependencies before importing TouchButtons
sys.modules['seedsigner.hardware.touch'] = MagicMock()

from seedsigner.hardware.touchbuttons import (
    TouchButtons,
    HardwareButtonsConstants,
    get_buttons,
)


def _bar_shown(tb, shown=True):
    """
    The control bar is now an OVERLAY: taps below TOUCH_BAR_TOP only mean
    KEY1/2/3 while a screen is actually showing a bar. Tests that exercise bar
    geometry must say so; without this the bottom strip is ordinary content.
    """
    tb._bar_visible = lambda: shown
    return tb

class TestCoordsToNavKey:
    """Test _coords_to_nav_key() mapping of screen coordinates to key codes."""

    def _make_buttons(self):
        """Create a TouchButtons instance with mocked touch device."""
        # Reset singleton
        TouchButtons._instance = None
        tb = TouchButtons.get_instance()
        return _bar_shown(tb)

    # --- Touch bar (y >= 480) maps to KEY1/KEY2/KEY3 ---

    def test_touch_bar_left(self):
        """Left third of touch bar returns KEY1."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key(80, 560) == tb.KEY1

    def test_touch_bar_center(self):
        """Center third of touch bar returns KEY2."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key(240, 560) == tb.KEY2

    def test_touch_bar_right(self):
        """Right third of touch bar returns KEY3."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key(400, 560) == tb.KEY3

    def test_touch_bar_boundary(self):
        """Exactly at touch bar top (y=480) maps to touch bar."""
        tb = self._make_buttons()
        key = tb._coords_to_nav_key(240, 480)
        assert key == tb.KEY2

    # --- UI area top 20% (y < 96 of 480) maps to KEY_UP ---

    def test_ui_top_center(self):
        """Top center of UI area returns KEY_UP."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key(240, 40) == tb.KEY_UP

    def test_ui_top_left(self):
        """Top left of UI area still returns KEY_UP."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key(20, 40) == tb.KEY_UP

    # --- UI area bottom 40% (y > 288 of 480) ---

    def test_ui_bottom_center(self):
        """Bottom center of UI area returns KEY_DOWN."""
        tb = self._make_buttons()
        # y > 0.60 * 480 = 288, center x (0.25 < px < 0.75)
        assert tb._coords_to_nav_key(240, 400) == tb.KEY_DOWN

    def test_ui_bottom_left(self):
        """Bottom left of UI area returns KEY_LEFT."""
        tb = self._make_buttons()
        # y > 288, px < 0.25 (x < 120)
        assert tb._coords_to_nav_key(50, 400) == tb.KEY_LEFT

    def test_ui_bottom_right(self):
        """Bottom right of UI area returns KEY_RIGHT."""
        tb = self._make_buttons()
        # y > 288, px > 0.75 (x > 360)
        assert tb._coords_to_nav_key(430, 400) == tb.KEY_RIGHT

    # --- UI area middle 40% (96 <= y <= 288) ---

    def test_ui_center(self):
        """Center of UI area returns KEY_PRESS (select)."""
        tb = self._make_buttons()
        # 0.20 < py < 0.60, 0.25 < px < 0.75
        assert tb._coords_to_nav_key(240, 200) == tb.KEY_PRESS

    def test_ui_middle_left(self):
        """Middle left of UI area returns KEY_LEFT."""
        tb = self._make_buttons()
        # 0.20 < py < 0.60, px < 0.25
        assert tb._coords_to_nav_key(50, 200) == tb.KEY_LEFT

    def test_ui_middle_right(self):
        """Middle right of UI area returns KEY_RIGHT."""
        tb = self._make_buttons()
        # 0.20 < py < 0.60, px > 0.75
        assert tb._coords_to_nav_key(430, 200) == tb.KEY_RIGHT


class TestBackAndPowerButtons:
    """Test corner tap detection for back and power buttons."""

    def _make_buttons(self):
        TouchButtons._instance = None
        return _bar_shown(TouchButtons.get_instance())

    # --- Back button (top-left, native < 48x48 = screen < 96x96) ---

    def test_back_button_top_left_corner(self):
        """Tap at (10, 10) hits back button."""
        tb = self._make_buttons()
        assert tb._check_back_button_tap(10, 10) is True

    def test_back_button_at_boundary(self):
        """Tap just inside the 96px top-left corner region still hits."""
        tb = self._make_buttons()
        assert tb._check_back_button_tap(95, 95) is True

    def test_back_button_miss_right(self):
        """Tap at screen (200, 10) misses back button."""
        tb = self._make_buttons()
        assert tb._check_back_button_tap(200, 10) is False

    def test_back_button_miss_below(self):
        """Tap at screen (10, 200) misses back button."""
        tb = self._make_buttons()
        assert tb._check_back_button_tap(10, 200) is False

    # --- Power button (top-right, native y < 48 and x > 192) ---

    def test_power_button_top_right(self):
        """Tap at screen (460, 10) hits power button."""
        tb = self._make_buttons()
        assert tb._check_power_button_tap(460, 10) is True

    def test_power_button_miss_left(self):
        """Tap left of the top-right corner region misses power button."""
        tb = self._make_buttons()
        assert tb._check_power_button_tap(200, 10) is False

    def test_power_button_miss_below(self):
        """Tap at screen (460, 200) misses power button."""
        tb = self._make_buttons()
        assert tb._check_power_button_tap(460, 200) is False


class TestButtonTapDetection:
    """Test _check_button_tap() with registered button rectangles."""

    def _make_buttons(self):
        TouchButtons._instance = None
        return _bar_shown(TouchButtons.get_instance())

    def _mock_button(self, screen_x, screen_y, width, height):
        """Create a mock button object with position attributes."""
        btn = MagicMock()
        btn.screen_x = screen_x
        btn.screen_y = screen_y
        btn.width = width
        btn.height = height
        btn.scroll_y = 0
        return btn

    def test_no_buttons_registered(self):
        """Returns -1 when no buttons are registered."""
        tb = self._make_buttons()
        assert tb._check_button_tap(240, 240) == -1

    def test_tap_hits_button(self):
        """Tap inside a registered button returns its index."""
        tb = self._make_buttons()
        # Register a button at native (60, 60) with size 120x40
        btn = self._mock_button(60, 60, 120, 40)
        tb.register_buttons([btn])
        # Canvas and touch share one space now: (120, 80) is inside the button
        assert tb._check_button_tap(120, 80) == 0

    def test_tap_misses_button(self):
        """Tap outside registered buttons returns -1."""
        tb = self._make_buttons()
        btn = self._mock_button(60, 60, 120, 40)
        tb.register_buttons([btn])
        # Tap at screen (20, 20) = native (10, 10), above the button
        assert tb._check_button_tap(20, 20) == -1

    def test_tap_in_touch_bar_ignored(self):
        """Taps in touch bar area (y >= 480) always return -1."""
        tb = self._make_buttons()
        btn = self._mock_button(0, 0, 240, 240)
        tb.register_buttons([btn])
        assert tb._check_button_tap(240, 500) == -1

    def test_multiple_buttons(self):
        """With multiple buttons, correct index is returned."""
        tb = self._make_buttons()
        btn0 = self._mock_button(0, 60, 240, 40)
        btn1 = self._mock_button(0, 110, 240, 40)
        btn2 = self._mock_button(0, 160, 240, 40)
        tb.register_buttons([btn0, btn1, btn2])
        # Tap on btn1 at (120, 130), canvas coords == touch coords
        assert tb._check_button_tap(120, 130) == 1

    def test_clear_buttons(self):
        """clear_buttons() removes all registered buttons."""
        tb = self._make_buttons()
        btn = self._mock_button(60, 60, 120, 40)
        tb.register_buttons([btn])
        tb.clear_buttons()
        assert tb._check_button_tap(240, 160) == -1
        assert tb.get_tapped_button_index() == -1


class TestTapStateReadAndReset:
    """Test that tap state flags reset after being read."""

    def _make_buttons(self):
        TouchButtons._instance = None
        return _bar_shown(TouchButtons.get_instance())

    def test_tapped_button_index_resets(self):
        """get_tapped_button_index() returns value then resets to -1."""
        tb = self._make_buttons()
        tb._tapped_button_index = 2
        assert tb.get_tapped_button_index() == 2
        assert tb.get_tapped_button_index() == -1

    def test_back_button_tapped_resets(self):
        """was_back_button_tapped() returns True then resets."""
        tb = self._make_buttons()
        tb._back_button_tapped = True
        assert tb.was_back_button_tapped() is True
        assert tb.was_back_button_tapped() is False

    def test_power_button_tapped_resets(self):
        """was_power_button_tapped() returns True then resets."""
        tb = self._make_buttons()
        tb._power_button_tapped = True
        assert tb.was_power_button_tapped() is True
        assert tb.was_power_button_tapped() is False

    def test_touch_bar_back_tapped_resets(self):
        """was_touch_bar_back_tapped() returns True then resets."""
        tb = self._make_buttons()
        tb._touch_bar_back_tapped = True
        assert tb.was_touch_bar_back_tapped() is True
        assert tb.was_touch_bar_back_tapped() is False

    def test_last_tap_native_coords_resets(self):
        """get_last_tap_native_coords() returns coords then resets to (-1, -1)."""
        tb = self._make_buttons()
        tb._last_tap_native_x = 100
        tb._last_tap_native_y = 50
        assert tb.get_last_tap_native_coords() == (100, 50)
        assert tb.get_last_tap_native_coords() == (-1, -1)

    def test_clear_pending_input(self):
        """clear_pending_input() resets all state."""
        tb = self._make_buttons()
        tb._tapped_button_index = 3
        tb._back_button_tapped = True
        tb._power_button_tapped = True
        tb._touch_bar_back_tapped = True
        tb._last_tap_native_x = 100
        tb._last_tap_native_y = 50
        tb.touch_down = True

        # touch.poll() must return None to break the drain loop
        tb.touch.poll.return_value = None

        tb.clear_pending_input()

        assert tb._tapped_button_index == -1
        assert tb._back_button_tapped is False
        assert tb._power_button_tapped is False
        assert tb._touch_bar_back_tapped is False
        assert tb._last_tap_native_x == -1
        assert tb._last_tap_native_y == -1
        assert tb.touch_down is False


class TestGetButtonsFactory:
    """Test the get_buttons() factory function."""

    def test_returns_touchbuttons_when_env_set(self):
        """With SEEDSIGNER_TOUCH=1, returns TouchButtons."""
        TouchButtons._instance = None
        with patch.dict(os.environ, {'SEEDSIGNER_TOUCH': '1'}):
            buttons = get_buttons()
            assert isinstance(buttons, TouchButtons)

    def test_returns_hardwarebuttons_when_env_unset(self):
        """Without SEEDSIGNER_TOUCH, returns HardwareButtons (mocked)."""
        # Mock the HardwareButtons import since RPi.GPIO isn't available
        mock_hw_buttons = MagicMock()
        mock_hw_instance = MagicMock()
        mock_hw_buttons.HardwareButtons.get_instance.return_value = mock_hw_instance
        with patch.dict(os.environ, {}, clear=True):
            with patch.dict(sys.modules, {'seedsigner.hardware.buttons': mock_hw_buttons}):
                buttons = get_buttons()
                assert not isinstance(buttons, TouchButtons)


class TestCoordsToNavKeyCenterRelative:
    """Test _coords_to_nav_key_center_relative(): the 2D-pan mapping used by the
    SeedQR zoomed transcription screen. The whole UI area pans toward the tap, so
    a pan tap can never land on an exit -- exit stays on the touch bar only. This
    fixes the DOWN-taps-exit bug caused by the default mapping's central KEY_PRESS
    exit block pincered against the touch bar."""

    def _make_buttons(self):
        TouchButtons._instance = None
        return _bar_shown(TouchButtons.get_instance())

    # --- UI area pans by dominant axis from centre (240, 240) ---

    def test_bottom_center_is_down(self):
        """Bottom-centre tap pans DOWN (the reported failure case)."""
        tb = self._make_buttons()
        # Centre is now (240, 320) and the dead zone is 0.15 * 640 = 96px, so
        # "below centre but above the bar" is 416 < y < 480.
        assert tb._coords_to_nav_key_center_relative(240, 460) == tb.KEY_DOWN

    def test_just_below_centre_is_down_not_exit(self):
        """A tap just below the centred cell pans DOWN. In the old mapping this
        band was a KEY_PRESS exit; it must no longer exit."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key_center_relative(240, 430) == tb.KEY_DOWN

    def test_top_center_is_up(self):
        tb = self._make_buttons()
        assert tb._coords_to_nav_key_center_relative(240, 80) == tb.KEY_UP

    def test_left_is_left(self):
        tb = self._make_buttons()
        assert tb._coords_to_nav_key_center_relative(60, 240) == tb.KEY_LEFT

    def test_right_is_right(self):
        tb = self._make_buttons()
        assert tb._coords_to_nav_key_center_relative(420, 240) == tb.KEY_RIGHT

    def test_diagonal_resolves_to_dominant_axis(self):
        """More vertical than horizontal -> vertical wins."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key_center_relative(300, 420) == tb.KEY_DOWN

    # --- The centre no longer exits (anti-regression for the pincer bug) ---

    def test_dead_centre_is_noop_not_exit(self):
        """Dead-centre tap is a no-op (-1), NOT an exit. -1 is not in any screen's
        requested keys, so wait_for ignores it."""
        tb = self._make_buttons()
        key = tb._coords_to_nav_key_center_relative(240, 240)
        assert key == -1
        assert key not in HardwareButtonsConstants.KEYS__ANYCLICK

    def test_old_center_press_point_is_now_noop(self):
        """(240, 200) returned KEY_PRESS (exit) under the default mapping; under the
        pan mapping it is inside the dead zone -> no-op, never an exit."""
        tb = self._make_buttons()
        # Centre of the panel is now (240, 320): the UI fills all 640px rather
        # than the old 480px above a permanent bar.
        assert tb._coords_to_nav_key(240, 300) == tb.KEY_PRESS  # old behaviour
        assert tb._coords_to_nav_key_center_relative(240, 300) == -1  # new behaviour

    # --- Exit still works via the touch bar ---

    def test_touch_bar_still_exits(self):
        """Touch bar taps still return KEY1/KEY2/KEY3 (all ANYCLICK = exit)."""
        tb = self._make_buttons()
        assert tb._coords_to_nav_key_center_relative(80, 560) == tb.KEY1
        assert tb._coords_to_nav_key_center_relative(240, 560) == tb.KEY2
        assert tb._coords_to_nav_key_center_relative(400, 560) == tb.KEY3
        for k in (tb.KEY1, tb.KEY2, tb.KEY3):
            assert k in HardwareButtonsConstants.KEYS__ANYCLICK


class TestCheckForLowKeyAware:
    """check_for_low() must honor the requested key(s), mapping the touch
    position via _classify_tap: back corner / bar-left -> KEY1, bar-center ->
    KEY2, bar-right -> KEY3, UI area -> KEY_PRESS. The old any-touch-matches-
    any-query behavior made the camera preview unusable: its back check
    consumed every tap before the snap check could run."""

    def _make_buttons(self):
        TouchButtons._instance = None
        tb = TouchButtons.get_instance()
        tb.touch = MagicMock()
        return _bar_shown(tb)

    def test_ui_tap_is_press_not_back(self):
        """The camera-preview bug: a tap on the image must NOT read as back
        (KEY_LEFT/KEY1) but MUST read as a snap (ANYCLICK)."""
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 300), None, None]
        assert tb.check_for_low(HardwareButtonsConstants.KEY_LEFT) is False
        assert tb.check_for_low(HardwareButtonsConstants.KEY1) is False
        assert tb.check_for_low(keys=HardwareButtonsConstants.KEYS__ANYCLICK) is True

    def test_back_corner_is_key1(self):
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 30, 30), None]
        assert tb.check_for_low(HardwareButtonsConstants.KEY1) is True

    def test_touch_bar_left_is_key1(self):
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 80, 560), None]
        assert tb.check_for_low(HardwareButtonsConstants.KEY1) is True

    def test_touch_bar_center_is_key2_snap(self):
        """Bar-center (the camera glyph) is KEY2: not back, but in ANYCLICK."""
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 560), None, None]
        assert tb.check_for_low(HardwareButtonsConstants.KEY1) is False
        assert tb.check_for_low(keys=HardwareButtonsConstants.KEYS__ANYCLICK) is True

    def test_release_clears_state(self):
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 300), ("up", 240, 300), None]
        assert tb.check_for_low(keys=HardwareButtonsConstants.KEYS__ANYCLICK) is True
        assert tb.check_for_low(keys=HardwareButtonsConstants.KEYS__ANYCLICK) is False
        assert tb.check_for_low(HardwareButtonsConstants.KEY1) is False

    def test_unkeyed_query_matches_any_touch(self):
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 300)]
        assert tb.check_for_low() is True

    def test_held_key_persists_across_polls(self):
        """State must survive the event being consumed by an earlier query in
        the same loop iteration (poll returns None on later calls)."""
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 560), None, None, None]
        assert tb.check_for_low(HardwareButtonsConstants.KEY_RIGHT) is False
        assert tb.check_for_low(HardwareButtonsConstants.KEY_LEFT) is False
        assert tb.check_for_low(HardwareButtonsConstants.KEY2) is True


class TestCheckForLowTapLatch:
    """A quick tap's down/up events can each be consumed by DIFFERENT
    check_for_low calls (the camera preview polls back keys, then pastes a
    frame, then polls snap keys). An unclaimed tap must latch so the query
    that asks for its key still fires - otherwise back taps landing mid-paste
    were lost or, worse, matched by the overlapping snap set."""

    KEYS_SNAP = [
        HardwareButtonsConstants.KEY_PRESS,
        HardwareButtonsConstants.KEY2,
        HardwareButtonsConstants.KEY3,
    ]

    def _make_buttons(self):
        TouchButtons._instance = None
        tb = TouchButtons.get_instance()
        tb.touch = MagicMock()
        return _bar_shown(tb)

    def _preview_iteration(self, tb):
        """One iteration of the camera preview loop's polling order."""
        back = (tb.check_for_low(HardwareButtonsConstants.KEY_LEFT)
                or tb.check_for_low(HardwareButtonsConstants.KEY1))
        snap = tb.check_for_low(keys=self.KEYS_SNAP)
        return back, snap

    def test_quick_back_tap_consumed_by_snap_check_still_exits(self):
        """Down lands in the snap check's poll, up in the next back check:
        the back tap must still register (this was 'there is no exit')."""
        tb = self._make_buttons()
        # iter1: back checks see nothing; snap check consumes the bar-left DOWN
        tb.touch.poll.side_effect = [None, None, ("down", 80, 560),
                                     ("up", 80, 560), None, None]
        back, snap = self._preview_iteration(tb)
        assert back is False
        assert snap is False  # KEY1 not in the snap set: no phantom capture
        # iter2: back check consumes the UP -> latched tap must fire as back
        back, snap = self._preview_iteration(tb)
        assert back is True
        assert snap is False

    def test_quick_snap_tap_fires_once_only(self):
        """An image tap that matches while held must NOT re-fire via the latch."""
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 300), None, None,
                                     ("up", 240, 300), None, None]
        back, snap = self._preview_iteration(tb)
        assert back is False
        assert snap is True
        back, snap = self._preview_iteration(tb)
        assert back is False
        assert snap is False

    def test_bar_shutter_tap_snaps(self):
        """Bar-center (camera glyph) quick tap: down consumed by back checks,
        up by snap check - must still snap exactly once."""
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 560), ("up", 240, 560),
                                     None, None, None, None]
        back, snap = self._preview_iteration(tb)
        assert back is False
        assert snap is True
        back, snap = self._preview_iteration(tb)
        assert (back, snap) == (False, False)

    def test_latched_tap_expires_on_new_touch(self):
        """A stale unclaimed latch is replaced by the next touch."""
        tb = self._make_buttons()
        tb.touch.poll.side_effect = [("down", 240, 560), ("up", 240, 560),
                                     ("down", 30, 30), None]
        # KEY2 tap latches (query never asks for it)
        assert tb.check_for_low(HardwareButtonsConstants.KEY_LEFT) is False
        assert tb.check_for_low(HardwareButtonsConstants.KEY_LEFT) is False
        # New corner touch replaces the stale KEY2 latch; KEY1 matches held
        assert tb.check_for_low(HardwareButtonsConstants.KEY1) is True
        assert tb.check_for_low(HardwareButtonsConstants.KEY2) is False
