#!/usr/bin/env python3
"""
Rasterize Feather icons (https://feathericons.com, MIT) into tintable alpha
masks that the app can paste at runtime.

WHY PRE-RASTERIZE: the signer runs on an ARMv6 Pi Zero with PIL only. There is
no SVG rasterizer on the device (cairo/librsvg are far too heavy for that
target), so SVGs are converted here, on a workstation, and the repo ships
deterministic PNG assets. Runtime cost on the Pi is then a paste through an
alpha mask - the same mechanism used for anti-aliased button corners.

WHY ALPHA MASKS (not coloured PNGs): the interface derives icon state from
colour (accent / body / disabled). One mask per icon tinted at runtime is the
Feather guidance ("use currentColor, never separate assets") expressed in PIL.

Assets are written at ICON_RASTER_SIZE and downscaled with LANCZOS at runtime,
so one file serves every on-screen size.

Usage:
    DYLD_LIBRARY_PATH=/opt/homebrew/lib .venv/bin/python tools/build_feather_icons.py
    (needs `brew install cairo` + `pip install cairosvg`; build-time only,
     never installed on the device)
"""
import hashlib
import io
import json
import pathlib
import sys
import urllib.request

import cairosvg
from PIL import Image

# Pinned release. Bump deliberately, never float: these are third-party assets
# that end up inside a signer image, so the version must be auditable.
FEATHER_VERSION = "4.29.2"
BASE_URL = f"https://unpkg.com/feather-icons@{FEATHER_VERSION}/dist/icons"

# Rendered at 4x the largest on-screen icon, then LANCZOS-downscaled at runtime.
ICON_RASTER_SIZE = 128

# Feather's default stroke (2 at 24px) reads thin once scaled down on a
# 286 DPI panel; 2.25 keeps it matched to the semibold UI text weight.
STROKE_WIDTH = 2.25

OUT_DIR = pathlib.Path(__file__).parent.parent / "src/seedsigner/resources/icons/feather"

# SeedSigner icon constant -> Feather icon name.
ICON_MAP = {
    "SEEDS": "key",
    "SETTINGS": "settings",
    "TOOLS": "tool",
    "BACK": "arrow-left",
    "CHECK": "check",
    "CHECKBOX": "square",
    "CHECKBOX_SELECTED": "check-square",
    "CHEVRON_DOWN": "chevron-down",
    "CHEVRON_LEFT": "chevron-left",
    "CHEVRON_RIGHT": "chevron-right",
    "CHEVRON_UP": "chevron-up",
    "PLUS": "plus",
    "POWER": "power",
    "RESTART": "refresh-cw",
    "INFO": "info",
    "SUCCESS": "check-circle",
    "WARNING": "alert-triangle",
    "ERROR": "x-circle",
    "CHANGE": "shuffle",
    "DERIVATION": "git-branch",
    "PASSPHRASE": "lock",
    "SIGN": "edit-3",
    "DELETE": "delete",
    # A fingerprint IS a hash of the key, so Feather's hash mark carries the
    # same meaning without pretending to be a fingerprint drawing.
    "FINGERPRINT": "hash",
    "QRCODE": "grid",
    "MICROSD": "hard-drive",
    "SPACE": "minus",          # space bar
}

# Composites: a base icon with a second icon nested inside it. "Scan" needs to
# read as *scanning a QR code*, not just a viewfinder, which is what the
# original SeedSigner glyph conveyed - the corner brackets alone lose that.
COMPOSITE_MAP = {
    "SCAN": {"base": "maximize", "inner": "grid", "inner_scale": 0.44},
}

# FontAwesome constant -> Feather icon name. Assets are written with an "FA_"
# prefix so the two icon namespaces cannot collide.
FONTAWESOME_MAP = {
    "CAMERA": "camera",
    "KEYBOARD": "type",        # Feather has no keyboard; "type" = text entry
    "X": "x",
    "CIRCLE": "circle",
    "ANGLE_DOWN": "chevron-down",
    "ANGLE_UP": "chevron-up",
    "MAP": "map",
}

# NOT mapped, deliberately - Feather cannot express these without losing
# meaning, so they keep their original glyph via the runtime fallback:
#   BITCOIN_ALT  - a currency mark; no generic icon substitutes for it
#   DICE_ONE..SIX - the pip count IS the information (dice entropy screen)

def fetch(name: str) -> bytes:
    url = f"{BASE_URL}/{name}.svg"
    with urllib.request.urlopen(url, timeout=30) as r:
        if r.status != 200:
            raise SystemExit(f"{url} -> HTTP {r.status}")
        return r.read()


def rasterize(svg: bytes) -> Image.Image:
    # Feather strokes use currentColor; force white so the result is a clean
    # luminance ramp we can use directly as an alpha mask.
    svg_text = svg.decode()
    svg_text = svg_text.replace('stroke-width="2"', f'stroke-width="{STROKE_WIDTH}"')
    svg_text = svg_text.replace('stroke="currentColor"', 'stroke="#ffffff"')
    png = cairosvg.svg2png(
        bytestring=svg_text.encode(),
        output_width=ICON_RASTER_SIZE,
        output_height=ICON_RASTER_SIZE,
        background_color="transparent",
    )
    rgba = Image.open(io.BytesIO(png)).convert("RGBA")
    # The alpha channel IS the mask.
    return rgba.getchannel("A")


def rasterize_composite(base_svg: bytes, inner_svg: bytes, inner_scale: float) -> Image.Image:
    """Nest `inner` centred inside `base`, combining their alpha masks."""
    base = rasterize(base_svg)
    inner_px = int(ICON_RASTER_SIZE * inner_scale)
    inner = rasterize(inner_svg).resize((inner_px, inner_px), Image.LANCZOS)
    offset = (ICON_RASTER_SIZE - inner_px) // 2
    combined = base.copy()
    # Lighter = per-pixel max, so overlapping strokes stay opaque rather than
    # summing into artefacts.
    region = combined.crop((offset, offset, offset + inner_px, offset + inner_px))
    from PIL import ImageChops
    combined.paste(ImageChops.lighter(region, inner), (offset, offset))
    return combined


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": "https://feathericons.com",
        "license": "MIT",
        "version": FEATHER_VERSION,
        "raster_size": ICON_RASTER_SIZE,
        "stroke_width": STROKE_WIDTH,
        "icons": {},
    }
    for const_name, spec in sorted(COMPOSITE_MAP.items()):
        base_svg = fetch(spec["base"])
        inner_svg = fetch(spec["inner"])
        mask = rasterize_composite(base_svg, inner_svg, spec["inner_scale"])
        out = OUT_DIR / f"{const_name}.png"
        mask.save(out, optimize=True)
        manifest["icons"][const_name] = {
            "feather": f'{spec["base"]}+{spec["inner"]}',
            "composite": spec,
            "svg_sha256": hashlib.sha256(base_svg + inner_svg).hexdigest(),
            "png_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        }
        print(f"  {const_name:20s} <- feather/{spec['base']} + {spec['inner']}")

    targets = [(n, f, n) for n, f in ICON_MAP.items()]
    targets += [(n, f, f"FA_{n}") for n, f in FONTAWESOME_MAP.items()]
    for const_name, feather_name, out_name in sorted(targets, key=lambda t: t[2]):
        svg = fetch(feather_name)
        mask = rasterize(svg)
        out = OUT_DIR / f"{out_name}.png"
        mask.save(out, optimize=True)
        manifest["icons"][out_name] = {
            "feather": feather_name,
            "svg_sha256": hashlib.sha256(svg).hexdigest(),
            "png_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        }
        print(f"  {out_name:20s} <- feather/{feather_name}")

    (OUT_DIR / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (OUT_DIR / "LICENSE").write_text(
        "Feather icons\n"
        "https://github.com/feathericons/feather\n"
        f"Version {FEATHER_VERSION}\n"
        "Licensed under the MIT License. Copyright (c) 2013-2023 Cole Bemis.\n"
    )
    print(f"\n{len(targets) + len(COMPOSITE_MAP)} icons -> {OUT_DIR}")
    print("Provenance (source URL, version, per-file sha256) in MANIFEST.json")


if __name__ == "__main__":
    main()
