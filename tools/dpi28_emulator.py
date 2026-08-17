#!/usr/bin/env python
"""
dpi28_emulator.py - Headless desktop emulator for the SeedSigner DPI28 touch port.

Boots the *real* SeedSigner app (Controller -> Renderer -> Views/Screens) exactly as
it runs on a Waveshare 2.8" DPI panel, except:
  - the display driver is `seedsigner.hardware.DPI28.DPI28Emulator`, which composes the
    same 480x640 panel frame (2x-scaled 240x240 UI + touch bar) but saves it to a PNG
    file instead of writing to /dev/fb0;
  - touch input is injected synthetically (screen coords, 480x640) into
    `TouchButtons.get_instance().touch` instead of being read from a real touchscreen
    device;
  - hardware-only modules that don't exist off-device (RPi, RPi.GPIO, spidev,
    picamera, the PiCamera video stream, ...) are mocked out so the app can boot on a
    plain macOS/Linux dev machine. Camera-dependent screens (QR scanning, image
    entropy) will not work in the emulator -- that's expected.

No other file in the repo is touched or imported in a modified form; all hardware
workarounds live in this script (env vars, sys.modules mocks, and two narrowly scoped
monkeypatches: `DisplayDriverFactory.instantiate_display_driver` for the "dpi28"
branch, and `Settings.SETTINGS_FILENAME` so a real settings.json is never read/written).

Usage
-----
Interactive (type commands on stdin, one per line):

    DYLD_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python tools/dpi28_emulator.py \\
        --frame /tmp/seedsigner_panel.png

    (then type, e.g.)
    sleep 5
    tap 124 372
    sleep 1
    tap 124 372
    quit

Scripted, boots to Home and taps "Tools" (Home is MainMenuScreen, a 2x2
LargeButtonScreen; button order is Scan/Seeds/Tools/Settings, so "Tools" is the
bottom-left button. In native 240x240 coords that button is roughly x:[8,116]
y:[144,228], i.e. centered near (62, 186) native -> (124, 372) on the 480x640 panel
after the driver's 2x scale -- derived from LargeButtonScreen's layout math in
src/seedsigner/gui/screens/screen.py and confirmed against a saved frame).

Two taps on the same point are needed: LargeButtonScreen (single_tap_buttons=False
for the Home menu) treats the first tap on a not-yet-selected button as "move the
highlight there" and only a second tap on an already-selected button confirms/enters
it. The `sleep 5` before the first tap is also deliberate: boot shows a ~3-4s splash
animation, and a `RemoveSDCardToastManagerThread` starts listening for input the
moment Home first renders -- a tap that lands in its 3-second activation_delay window
is consumed silently (as "dismiss the toast") and never reaches the Home screen at
all, so the first real Home-screen tap should land comfortably after that window:

    printf 'sleep 5\\ntap 124 372\\nsleep 1\\ntap 124 372\\nsleep 1\\nquit\\n' | \\
        DYLD_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python tools/dpi28_emulator.py \\
        --frame /tmp/e2e_frame.png --frames-dir /tmp/e2e_frames

    # or via --script:
    DYLD_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python tools/dpi28_emulator.py \\
        --script my_script.txt --frame /tmp/seedsigner_panel.png

Script command grammar (one per line; blank lines and lines starting with '#' are
ignored):

    tap X Y      # synthetic "down" at (X, Y) then "up" 80ms later (screen coords, 480x640)
    down X Y     # synthetic touch-down only
    up X Y       # synthetic touch-up only
    move X Y     # synthetic touch-move (only meaningful while "down")
    sleep SECS   # pause the input loop (float seconds)
    quit         # stop reading input and exit the process
"""

import argparse
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock


def _install_hardware_mocks():
    """
    Stand in for hardware-only modules that don't exist off-device, the same way
    tests/screenshot_generator/generator.py does for the pytest screenshot suite. Must
    run before ANY `seedsigner.*` module is imported.

    Without this, `seedsigner/gui/screens/screen.py` (imported transitively by
    `seedsigner.controller`) does a module-level
    `from seedsigner.hardware.buttons import HardwareButtonsConstants, HardwareButtons`,
    and `seedsigner/hardware/buttons.py` does `import RPi.GPIO as GPIO` plus reads
    `GPIO.RPI_INFO['P1_REVISION']` in its class body at import time -- both fatal on a
    non-Pi machine. Similarly, `Controller.configure_instance()` spins up a
    `BackgroundImportThread` that unconditionally imports
    `seedsigner.hardware.pivideostream`, which does `from picamera import PiCamera` at
    module level.

    Gotcha: `seedsigner/hardware/buttons.py` defines its OWN `HardwareButtonsConstants`
    class (screen.py imports THIS one, not the one in touchbuttons.py) with key-code
    values that branch on `GPIO.RPI_INFO['P1_REVISION'] == 3` -- 40-pin numbering
    (KEY_PRESS=33, KEY1=40, ...) if true, 26-pin numbering (KEY_PRESS=7, KEY1=16, ...)
    otherwise. `seedsigner/hardware/touchbuttons.py`'s TouchButtons class hardcodes the
    40-pin numbering unconditionally. If GPIO is a bare MagicMock,
    `GPIO.RPI_INFO['P1_REVISION'] == 3` is False (a MagicMock attribute never equals
    the int 3), so screen.py ends up comparing touch-derived key codes (always 40-pin,
    from touchbuttons.py) against a 26-pin ALL_KEYS/KEY_PRESS/KEY2 list -- every
    wait_for() key-membership check silently fails and every injected tap is a no-op,
    with no error raised anywhere. So RPI_INFO must be forced to report revision 3.
    """
    gpio_mock = MagicMock()
    gpio_mock.RPI_INFO = {"P1_REVISION": 3}
    sys.modules["RPi.GPIO"] = gpio_mock
    sys.modules["RPi"] = MagicMock()
    sys.modules["RPi"].GPIO = gpio_mock

    for name in (
        "spidev",
        "picamera",
        "picamera.array",
        "seedsigner.hardware.pivideostream",
        "seedsigner.hardware.microsd",
        "seedsigner.hardware.displays.st7789_mpy",
        "seedsigner.hardware.displays.ili9341",
    ):
        sys.modules[name] = MagicMock()


def _build_command_line_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Headless desktop emulator for the SeedSigner DPI28 touch port."
    )
    parser.add_argument(
        "--frame",
        default="/tmp/seedsigner_panel.png",
        help="PNG path that is overwritten with the latest 480x640 panel frame on every render (default: %(default)s)",
    )
    parser.add_argument(
        "--frames-dir",
        default=None,
        help="Optional directory to also dump every frame as a numbered PNG (frame_00001.png, frame_00002.png, ...)",
    )
    parser.add_argument(
        "--script",
        default=None,
        help="Path to a command script (see module docstring for grammar). If omitted, commands are read from stdin.",
    )
    parser.add_argument(
        "--settings",
        default=None,
        help="Path to use as the persistent settings.json. Defaults to a fresh temp file so the real on-disk settings.json is never touched.",
    )
    parser.add_argument(
        "--grace-period",
        type=float,
        default=1.0,
        help="Seconds to wait after the script ends (or stdin closes) before exiting, so a final in-flight frame can finish rendering (default: %(default)s)",
    )
    return parser


def _process_one_command(line: str, touch_input) -> bool:
    """
    Parse and execute one input-loop command line.

    Returns False if the command was "quit" (caller should stop reading), True
    otherwise.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return True

    parts = line.split()
    cmd = parts[0].lower()

    if cmd == "quit":
        return False

    if cmd == "sleep":
        time.sleep(float(parts[1]))
        return True

    if cmd in ("down", "up", "move"):
        x, y = int(parts[1]), int(parts[2])
        touch_input.inject_event(cmd, x, y)
        return True

    if cmd == "tap":
        x, y = int(parts[1]), int(parts[2])
        touch_input.inject_event("down", x, y)
        time.sleep(0.08)
        touch_input.inject_event("up", x, y)
        return True

    logging.getLogger(__name__).warning(f"Ignoring unrecognized command: {line!r}")
    return True


def main():
    args = _build_command_line_argument_parser().parse_args()

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)8s [%(name)s]: %(message)s",
    )
    logger = logging.getLogger("dpi28_emulator")

    # --- Env vars that select the touch input path and the DPI28 display config,
    # exactly as they would be set on real DPI28 hardware. Must be set before any
    # seedsigner module is imported (seedsigner.gui.renderer reads SEEDSIGNER_DISPLAY
    # at import time; seedsigner.gui.screens.screen reads SEEDSIGNER_TOUCH lazily but
    # there's no reason to special-case it).
    os.environ["SEEDSIGNER_TOUCH"] = "1"
    os.environ["SEEDSIGNER_DISPLAY"] = "dpi28"

    _install_hardware_mocks()

    # --- Isolate the persistent settings file so this tool never reads/writes the
    # real on-disk settings.json.
    settings_path = args.settings
    settings_tempdir = None
    if not settings_path:
        settings_tempdir = tempfile.mkdtemp(prefix="seedsigner_emulator_settings_")
        settings_path = os.path.join(settings_tempdir, "settings.json")

    from seedsigner.models.settings import Settings
    Settings.SETTINGS_FILENAME = settings_path
    logger.info(f"Using settings file: {settings_path}")

    # --- Monkeypatch the display driver factory's "dpi28" branch so it returns a
    # headless DPI28Emulator (PNG output) instead of the real DPI28 driver (which
    # would try to mmap /dev/fb0). This is the one workaround explicitly sanctioned by
    # the task brief in lieu of touching display_driver.py itself.
    from seedsigner.hardware.displays import display_driver as display_driver_module
    from seedsigner.hardware.DPI28 import DPI28Emulator

    _original_instantiate_display_driver = display_driver_module.DisplayDriverFactory.instantiate_display_driver.__func__

    def _patched_instantiate_display_driver(cls, display_type=display_driver_module.DISPLAY_TYPE__ST7789, width=None, height=None):
        if display_type == display_driver_module.DISPLAY_TYPE__DPI28:
            logger.info(f"Instantiating DPI28Emulator (frame={args.frame}, frames_dir={args.frames_dir})")
            return DPI28Emulator(
                _width=DPI28Emulator.NATIVE_WIDTH,
                _height=DPI28Emulator.NATIVE_HEIGHT,
                frame_path=args.frame,
                frames_dir=args.frames_dir,
            )
        return _original_instantiate_display_driver(cls, display_type=display_type, width=width, height=height)

    display_driver_module.DisplayDriverFactory.instantiate_display_driver = classmethod(_patched_instantiate_display_driver)

    # Fresh frames-dir dump, if requested, so frame counts from a previous run don't
    # get confused with this run's.
    if args.frames_dir and os.path.isdir(args.frames_dir):
        shutil.rmtree(args.frames_dir)

    # --- Boot the app. Controller.get_instance() implicitly calls
    # Controller.configure_instance() on first access (verified in
    # src/seedsigner/controller.py), which wires up Settings, MicroSD (mocked
    # above), and Renderer (which in turn calls our patched display driver factory).
    # Controller.start() is the blocking main loop (src/main.py calls it the same
    # way), so it runs in a daemon thread here; os._exit() below ends the process
    # regardless of what that thread is doing.
    from seedsigner.controller import Controller

    def _run_app():
        try:
            Controller.get_instance().start()
        except Exception:
            logger.exception("Controller.start() raised")

    app_thread = threading.Thread(target=_run_app, name="seedsigner-controller", daemon=True)
    app_thread.start()

    # --- Touch input loop: read commands from the script file if given, else stdin.
    from seedsigner.hardware.touchbuttons import TouchButtons

    if args.script:
        command_lines = open(args.script)
    else:
        logger.info("Reading touch commands from stdin (tap X Y | down X Y | up X Y | move X Y | sleep SECS | quit)")
        command_lines = sys.stdin

    try:
        for line in command_lines:
            touch_input = TouchButtons.get_instance().touch
            if not _process_one_command(line, touch_input):
                break
    finally:
        if command_lines is not sys.stdin:
            command_lines.close()
        if settings_tempdir:
            shutil.rmtree(settings_tempdir, ignore_errors=True)

    # Give any in-flight render (e.g. a screen transition kicked off by the last
    # injected tap) a moment to finish and write its frame before we tear down.
    time.sleep(args.grace_period)

    logger.info("Exiting")
    os._exit(0)


if __name__ == "__main__":
    main()
