import os
from contextlib import contextmanager
from dataclasses import dataclass
from PIL import Image, ImageDraw

from seedsigner.gui.renderer import Renderer
from seedsigner.gui.toast import BaseToastOverlayManagerThread
from seedsigner.views.view import View



class ScreenshotComplete(Exception):
    """
        Slightly hacky way for the ScreenshotRenderer to intentionally break out of the
        normal Controller flow in order to return control to the screenshot generator.
    """
    pass



class ScreenshotRenderer(Renderer):
    screenshot_path: str = None
    screenshot_filename: str = None

    @property
    def is_screenshot_generator(self) -> bool:
        return True

    @classmethod
    def configure_instance(cls):
        # Instantiate the one and only Renderer instance
        renderer = cls.__new__(cls)
        cls._instance = renderer

        # DPI28 touch mode renders at the panel's own 480x640; the ST7789
        # baseline stays 240x240.
        _dpi28 = os.environ.get('SEEDSIGNER_SCREENSHOT_DPI28') == '1'
        renderer.canvas_width = 480 if _dpi28 else 240
        renderer.canvas_height = 640 if _dpi28 else 240

        renderer.canvas = Image.new('RGB', (renderer.canvas_width, renderer.canvas_height))
        renderer.draw = ImageDraw.Draw(renderer.canvas)

        renderer.render_count = 0

        # DPI28 touchscreen mode: also emit the composed 480x640 physical-panel
        # frame (native-resolution UI + touch bar) alongside each screenshot.
        # Screens set their touch-bar preset via BaseScreen._set_touch_bar(),
        # which reaches this driver through renderer.disp; SEEDSIGNER_TOUCH=1
        # must also be set for those calls to fire.
        renderer.dpi28_disp = None
        if os.environ.get('SEEDSIGNER_SCREENSHOT_DPI28') == '1':
            from seedsigner.hardware.DPI28 import DPI28Emulator
            renderer.disp = DPI28Emulator(_width=480, _height=640)
            renderer.dpi28_disp = renderer.disp
            renderer.display_type = "dpi28"


    def set_screenshot_filename(self, filename:str):
        self.screenshot_filename = filename


    def set_screenshot_path(self, path):
        if not os.path.exists(path):
            os.makedirs(path)
        self.screenshot_path = path


    def show_image(self, image=None, alpha_overlay=None, is_background_thread: bool = False):            
        if is_background_thread:
            return

        if alpha_overlay:
            if image == None:
                image = self.canvas
            image = Image.alpha_composite(image, alpha_overlay)

        if image:
            # Always write to the current canvas, rather than trying to replace it
            self.canvas.paste(image)

        self.canvas.save(os.path.join(self.screenshot_path, self.screenshot_filename))

        if self.dpi28_disp is not None:
            dpi28_path = os.path.join(self.screenshot_path, "dpi28")
            os.makedirs(dpi28_path, exist_ok=True)
            self.dpi28_disp.compose(self.canvas).save(os.path.join(dpi28_path, self.screenshot_filename))

        self.render_count += 1

        # Break out of the normal Controller flow and return to the screenshot generator
        raise ScreenshotComplete()



@contextmanager
def default_mock_context_manager():
    # Just a no-op context manager
    yield



@dataclass
class ScreenshotConfig:
    """
    - mock_context_manager: Option to provide mocks to set up custom data or state that
      the screenshot might need. The mocks will only be active during this one
      screenshot's generation. Ensures that there are no persistent state changes left
      over that might affect other screenshots.
    """
    View_cls: View
    view_kwargs: dict = None
    screenshot_name: str = None
    toast_thread: BaseToastOverlayManagerThread = None
    mock_context_manager: callable = default_mock_context_manager


    def __post_init__(self):
        if not self.view_kwargs:
            self.view_kwargs = {}
        if not self.screenshot_name:
            self.screenshot_name = self.View_cls.__name__
