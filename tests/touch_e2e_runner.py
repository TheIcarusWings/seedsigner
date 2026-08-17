"""
End-to-end synthetic-touch navigation checks.

Runs REAL Screens with the REAL TouchButtons + TouchInput input stack (events
injected via TouchInput.inject_event) rendering through the REAL DPI28Emulator
composition — no mocks anywhere in the touch path.

This file is deliberately NOT named test_*.py: the main pytest suite installs
MagicMock stand-ins for gui.renderer / hardware.buttons at import time
(tests/base.py), which would corrupt these real-module tests. Instead,
tests/test_touch_e2e.py launches this script in a clean subprocess.

Exit code 0 = all scenarios passed.
"""
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

os.environ["SEEDSIGNER_TOUCH"] = "1"

# Hardware-only modules that don't exist on a desktop dev machine
for mod in ["RPi", "RPi.GPIO", "spidev", "picamera", "picamera.array",
            "seedsigner.hardware.camera", "seedsigner.hardware.pivideostream",
            "seedsigner.hardware.microsd"]:
    sys.modules[mod] = MagicMock()

# CRITICAL: hardware/buttons.py derives its key-code constants from the GPIO
# pin numbers, branching on RPI_INFO['P1_REVISION']. TouchButtons hardcodes the
# P1_REVISION == 3 (40-pin) values, so the mock must report the same revision or
# screens will wait on key codes TouchButtons never emits.
sys.modules["RPi.GPIO"].RPI_INFO = {"P1_REVISION": 3}
sys.modules["RPi"].GPIO = sys.modules["RPi.GPIO"]

from PIL import Image, ImageDraw

from seedsigner.gui.renderer import Renderer
from seedsigner.hardware.DPI28 import DPI28Emulator
from seedsigner.hardware.touchbuttons import TouchButtons
from seedsigner.hardware.buttons import HardwareButtonsConstants


class E2ERenderer(Renderer):
    """In-memory renderer: 240x240 canvas pushed through DPI28Emulator."""

    @classmethod
    def configure_instance(cls):
        renderer = cls.__new__(cls)
        cls._instance = renderer
        # Screens resolve the singleton via the base class
        Renderer._instance = renderer
        renderer.canvas_width = 240
        renderer.canvas_height = 320          # full-panel native size
        renderer.canvas = Image.new("RGB", (240, 320))
        renderer.draw = ImageDraw.Draw(renderer.canvas)
        renderer.disp = DPI28Emulator(_width=240, _height=320)
        renderer.display_type = "dpi28"
        renderer.frames = 0

    def show_image(self, image=None, alpha_overlay=None, show_direct=False, is_background_thread=False):
        if alpha_overlay:
            if image is None:
                image = self.canvas
            image = Image.alpha_composite(image, alpha_overlay)
        if image:
            self.canvas.paste(image)
        self.disp.show_image(self.canvas)
        self.frames += 1


def run_screen_async(screen):
    """Run screen.display() in a thread; returns (thread, result-holder)."""
    holder = {}

    def target():
        try:
            holder["result"] = screen.display()
        except Exception as e:  # pragma: no cover - failure reporting only
            holder["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, holder


def tap(x, y, hold_ms=40):
    """Inject a synthetic tap at SCREEN coords (480x640 panel space)."""
    touch = TouchButtons.get_instance().touch
    touch.inject_event("down", x, y)
    time.sleep(hold_ms / 1000.0)
    touch.inject_event("up", x, y)


def button_center_screen_coords(screen, index):
    """Panel (480x640) coords of the center of a rendered Button."""
    btn = screen.buttons[index]
    native_x = getattr(btn, "screen_x", 0) + btn.width // 2
    native_y = btn.screen_y - getattr(btn, "scroll_y", 0) + btn.height // 2
    return native_x * 2, native_y * 2


def wait_for_render(screen, holder, timeout=5.0):
    """Block until the screen thread has rendered and is in its input loop."""
    deadline = time.time() + timeout
    renderer = Renderer.get_instance()
    while time.time() < deadline:
        if "error" in holder:
            raise holder["error"]
        if renderer.frames > 0 and getattr(screen, "buttons", None):
            # One extra beat so _run()'s wait_for loop is actually polling
            time.sleep(0.15)
            return
        time.sleep(0.02)
    raise AssertionError("screen never rendered")


def finish(thread, holder, timeout=5.0):
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError("screen did not return after input")
    if "error" in holder:
        raise holder["error"]
    return holder.get("result")


PASSED = []
FAILED = []


def scenario(name):
    def deco(fn):
        def wrapper():
            # Fresh input state per scenario
            tb = TouchButtons.get_instance()
            tb.clear_pending_input()
            Renderer.get_instance().frames = 0
            try:
                fn()
                PASSED.append(name)
                print(f"PASS: {name}")
            except Exception as e:
                FAILED.append((name, e))
                print(f"FAIL: {name}: {e!r}")
        wrapper.__name__ = name
        return wrapper
    return deco


@scenario("button_list_single_tap_activates")
def s1():
    # Tap-driven UX: a tap on any list item activates it directly. Scrolling
    # is a drag gesture now, so a tap is unambiguous.
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    screen = ButtonListScreen(title="Test", button_data=[
        ButtonOption("Alpha"), ButtonOption("Bravo"), ButtonOption("Charlie")])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    x, y = button_center_screen_coords(screen, 1)
    tap(x, y)
    assert finish(t, holder) == 1


@scenario("button_list_top_left_corner_goes_back")
def s2():
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption, RET_CODE__BACK_BUTTON
    screen = ButtonListScreen(title="Test", show_back_button=True, button_data=[
        ButtonOption("Alpha"), ButtonOption("Bravo")])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    tap(20, 20)  # top-left corner (back), screen coords
    assert finish(t, holder) == RET_CODE__BACK_BUTTON


@scenario("overlay_bar_keys_work_when_shown")
def s3():
    # Lists are full-bleed now (drag + tap), so the bar only exists where a
    # screen asks for one. When it IS shown, its thirds must still map to
    # KEY1/KEY2/KEY3 - that is how keyboards and the camera get DEL/OK/shutter.
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    screen = ButtonListScreen(title="Test", button_data=[
        ButtonOption("Alpha"), ButtonOption("Bravo"), ButtonOption("Charlie")])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    screen._set_touch_bar('TOUCH_BAR_DEFAULT')   # opt this screen into a bar
    time.sleep(0.1)
    tap(400, 560)   # bar right third = KEY3 (down)
    time.sleep(0.3)
    tap(240, 560)   # bar middle = KEY2 (select)
    assert finish(t, holder) == 1


@scenario("large_button_grid_tap")
def s4():
    from seedsigner.gui.screens.screen import LargeButtonScreen, ButtonOption
    screen = LargeButtonScreen(title="Menu", show_back_button=False, button_data=[
        ButtonOption("One"), ButtonOption("Two"), ButtonOption("Three"), ButtonOption("Four")])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    x, y = button_center_screen_coords(screen, 2)
    tap(x, y)          # single tap activates a grid tile
    assert finish(t, holder) == 2


@scenario("tap_on_empty_area_does_not_select")
def s5():
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    screen = ButtonListScreen(title="Test", show_back_button=True, button_data=[
        ButtonOption("Alpha")])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    # Tap dead space between title and button in the middle band that maps to
    # KEY_PRESS nav fallback... on a list screen a bare KEY_PRESS activates the
    # CURRENT selection, so instead probe the top band (KEY_UP): must not return.
    tap(240, 130)  # upper UI area, no button there -> KEY_UP, list stays put
    time.sleep(0.5)
    assert "result" not in holder, f"stray tap activated: {holder.get('result')!r}"
    # Now select explicitly to unblock the thread
    x, y = button_center_screen_coords(screen, 0)
    tap(x, y)
    assert finish(t, holder) == 0


@scenario("drag_off_button_cancels_tap")
def s6():
    # Touch hygiene: down on a control, slide off, release -> must NOT activate.
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    screen = ButtonListScreen(title="Test", button_data=[
        ButtonOption("Alpha"), ButtonOption("Bravo")])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    touch = TouchButtons.get_instance().touch
    x, y = button_center_screen_coords(screen, 0)  # the SELECTED button (would fire on tap)
    touch.inject_event("down", x, y)
    time.sleep(0.05)
    # Release far from where the touch started. With a full-panel canvas the
    # old "empty" point sat inside the same button, so this now targets the
    # bottom strip, which is empty content while no bar is shown.
    touch.inject_event("up", 240, 620)
    time.sleep(0.4)
    assert "result" not in holder, f"drag-off activated: {holder.get('result')!r}"
    tap(x, y)  # clean tap on selected button activates
    assert finish(t, holder) == 0


@scenario("drag_scrolls_list_without_activating")
def s7():
    # A vertical drag must scroll the list and activate NOTHING, even though
    # it starts on top of a button.
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    screen = ButtonListScreen(title="Long list", button_data=[
        ButtonOption(f"Item {i}") for i in range(15)])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    assert screen.has_scroll_arrows, "test needs a scrollable list"

    before = screen.buttons[0].scroll_y
    touch = TouchButtons.get_instance().touch
    x, y = button_center_screen_coords(screen, 0)
    touch.inject_event("down", x, y)
    # drag upward in steps (finger moves up => content scrolls down the list)
    for step in range(1, 7):
        touch.inject_event("move", x, y - step * 20)
        time.sleep(0.05)
    touch.inject_event("up", x, y - 120)
    time.sleep(0.5)

    after = screen.buttons[0].scroll_y
    assert "result" not in holder, f"drag activated item {holder.get('result')!r}"
    assert after > before, f"list did not scroll (scroll_y {before} -> {after})"

    # and the list is still usable afterwards: a clean tap activates
    idx = next(i for i, b in enumerate(screen.buttons)
               if b.screen_y - b.scroll_y >= screen.top_nav.height
               and b.screen_y - b.scroll_y + b.height <= screen.down_arrow_img_y)
    x, y = button_center_screen_coords(screen, idx)
    tap(x, y)
    assert finish(t, holder) == idx


@scenario("drag_stops_at_list_bounds")
def s8():
    # Over-dragging must clamp, not run the content off into space.
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    screen = ButtonListScreen(title="Long list", button_data=[
        ButtonOption(f"Item {i}") for i in range(15)])
    t, holder = run_screen_async(screen)
    wait_for_render(screen, holder)
    touch = TouchButtons.get_instance().touch

    # yank far past the end
    touch.inject_event("down", 240, 400)
    for step in range(1, 20):
        touch.inject_event("move", 240, 400 - step * 40)
        time.sleep(0.02)
    touch.inject_event("up", 240, 20)
    time.sleep(0.5)
    assert screen.buttons[0].scroll_y <= screen._max_scroll_y(), "scrolled past the end"

    # and back past the top
    touch.inject_event("down", 240, 100)
    for step in range(1, 20):
        touch.inject_event("move", 240, 100 + step * 40)
        time.sleep(0.02)
    touch.inject_event("up", 240, 460)
    time.sleep(0.5)
    assert screen.buttons[0].scroll_y == 0, f"scroll_y {screen.buttons[0].scroll_y} != 0 at top"

    idx = 0
    x, y = button_center_screen_coords(screen, idx)
    tap(x, y)
    assert finish(t, holder) == idx



@scenario("qwerty_key_tap_types_a_letter")
def s9():
    """Tapping a QWERTY key appends that letter to the entry field."""
    from seedsigner.gui.screens import seed_screens
    from seedsigner.models.seed import Seed
    from seedsigner.models.settings_definition import SettingsConstants

    wordlist = Seed.get_wordlist(SettingsConstants.WORDLIST_LANGUAGE__ENGLISH)
    screen = seed_screens.SeedMnemonicEntryQwertyScreen(
        title="Seed Word #1", initial_letters=[" "], wordlist=wordlist)
    thread, holder = run_screen_async(screen)
    wait_for_render_keyboard(screen, holder)

    key = find_key(screen, "f")
    assert key is not None, "no 'f' key in the QWERTY layout"
    tap_key(screen, key)
    time.sleep(0.3)

    typed = "".join(screen.letters).strip()
    assert typed == "f", f"expected 'f' typed, got {typed!r}"

    # Leave via the back corner so the thread ends
    tap(20, 20)
    finish(thread, holder)


@scenario("qwerty_suggestion_tap_selects_word")
def s10():
    """Typing narrows the suggestions; tapping one returns that word."""
    from seedsigner.gui.screens import seed_screens
    from seedsigner.models.seed import Seed
    from seedsigner.models.settings_definition import SettingsConstants

    wordlist = Seed.get_wordlist(SettingsConstants.WORDLIST_LANGUAGE__ENGLISH)
    screen = seed_screens.SeedMnemonicEntryQwertyScreen(
        title="Seed Word #1", initial_letters=list("mush"), wordlist=wordlist)
    thread, holder = run_screen_async(screen)
    wait_for_render_keyboard(screen, holder)

    assert screen.possible_words, "no suggestions for the prefix 'mush'"
    expected = screen.possible_words[0]

    btn = screen.matches_list_highlight_button
    tap((btn.screen_x + btn.width // 2) * 2, (btn.screen_y + btn.height // 2) * 2)

    result = finish(thread, holder)
    assert result == expected, f"expected {expected!r} selected, got {result!r}"


@scenario("qwerty_keys_fit_the_panel")
def s11():
    """Every key sits inside the canvas and is at least 40 physical px wide."""
    from seedsigner.gui.screens import seed_screens
    from seedsigner.models.seed import Seed
    from seedsigner.models.settings_definition import SettingsConstants

    wordlist = Seed.get_wordlist(SettingsConstants.WORDLIST_LANGUAGE__ENGLISH)
    screen = seed_screens.SeedMnemonicEntryQwertyScreen(
        title="Seed Word #1", initial_letters=[" "], wordlist=wordlist)
    kb = screen.keyboard

    assert kb.rect[3] <= screen.canvas_height, "keyboard runs off the bottom"
    for row in kb.keys:
        for key in row:
            right = key.screen_x + kb.key_width * key.size
            assert 0 <= key.screen_x and right <= screen.canvas_width, (
                f"key {key.letter!r} spans {key.screen_x}..{right}, canvas is "
                f"0..{screen.canvas_width}")
    # Physical px = native * 2; ~4mm is the Ledger Flex key width.
    assert kb.key_width * 2 >= 40, f"keys only {kb.key_width * 2}px wide"
    assert kb.key_height * 2 >= 72, f"keys only {kb.key_height * 2}px tall"


def wait_for_render_keyboard(screen, holder, timeout=5.0):
    """wait_for_render() wants screen.buttons; keyboard screens have none."""
    deadline = time.time() + timeout
    renderer = Renderer.get_instance()
    while time.time() < deadline:
        if "error" in holder:
            raise holder["error"]
        if renderer.frames > 0:
            time.sleep(0.2)
            return
        time.sleep(0.02)
    raise AssertionError("screen never rendered")


def find_key(screen, letter):
    for row in screen.keyboard.keys:
        for key in row:
            if key.letter == letter:
                return key
    return None


def tap_key(screen, key):
    kb = screen.keyboard
    x = key.screen_x + (kb.key_width * key.size) // 2
    y = key.screen_y + kb.key_height // 2
    tap(x * 2, y * 2)


def main():
    # TouchButtons.wait_for consults the Controller singleton for screensaver
    # timing; keep it inert without booting the full app.
    controller = MagicMock()
    controller.screensaver_activation_ms = 10**9
    controller.is_screensaver_start_allowed = False

    E2ERenderer.configure_instance()

    with patch("seedsigner.controller.Controller.get_instance", return_value=controller):
        for fn in (s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11):
            fn()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
