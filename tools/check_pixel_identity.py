#!/usr/bin/env python3
"""
Guard the "non-touch builds render pixel-identically to upstream" invariant.

The touchscreen fork's central promise is that every UI change is gated behind
`SEEDSIGNER_TOUCH=1`, so an ordinary ST7789/GPIO build renders exactly as it
did before. Nothing in CI currently checks that: `tests.yml` generates the
screenshot set and archives it, but never compares it to anything.

This script closes that gap. It renders the non-touch screenshot set twice,
once at a baseline ref and once at the working tree, and fails if any image
differs by a single byte.

    tools/check_pixel_identity.py                     # vs merge-base with dev
    tools/check_pixel_identity.py --baseline dev
    tools/check_pixel_identity.py --locale en --keep  # keep renders for diffing

Both renders run with SEEDSIGNER_TOUCH unset, so this says nothing about the
touch UI (which is *supposed* to differ); it only proves the non-touch path was
left alone.

Two screenshots legitimately vary between refs and are skipped by default; the
script always prints what it skipped rather than hiding it.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# Screenshots whose name ends in this render the live git branch/tag/hash into
# the image, so they differ between any two refs (and even between a branch and
# a detached checkout of the same commit) by definition. Matching on the suffix
# rather than listing names keeps this correct as views are added; there are
# currently two, in different sections.
GIT_STATE_SUFFIX = "_current_git_state"

# These render a version string fetched from the GitHub releases API at import
# time. Two runs minutes apart normally agree, but a failed fetch on one side
# swaps in a placeholder and produces a spurious diff. Reported specially so a
# network blip is never mistaken for a real regression.
NETWORK_DEPENDENT = {
    "OpeningSplashView",
    "OpeningSplashView_no_partner_logos",
    "VersionView",
}


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout[-3000:]}\n"
            f"--- stderr ---\n{proc.stderr[-3000:]}"
        )
    return proc.stdout


def repo_root() -> Path:
    return Path(run(["git", "rev-parse", "--show-toplevel"]).strip())


def render(tree: Path, out_dir: Path, locale: str, python: Path, root: Path) -> Path:
    """
    Render the screenshot set for `tree` and move the result to `out_dir`.

    Must run with cwd == tree: the generator writes to ./seedsigner-screenshots
    and reads tests/screenshot_generator/template.md via a relative path.
    """
    env = dict(os.environ)
    # The whole point: prove the *non-touch* path is untouched.
    env.pop("SEEDSIGNER_TOUCH", None)
    env.pop("SEEDSIGNER_DISPLAY", None)
    env.pop("SEEDSIGNER_SCREENSHOT_DPI28", None)
    env["PYTHONPATH"] = str(tree / "src")
    # pyzbar needs brew's libzbar on macOS; harmless elsewhere.
    if sys.platform == "darwin" and "DYLD_LIBRARY_PATH" not in env:
        env["DYLD_LIBRARY_PATH"] = "/opt/homebrew/lib"

    shots = tree / "seedsigner-screenshots"
    if shots.exists():
        shutil.rmtree(shots)

    run([str(python), "-m", "pytest",
         str(tree / "tests" / "screenshot_generator" / "generator.py"),
         "--locale", locale, "-q"],
        cwd=tree, env=env)

    produced = shots / locale
    if not produced.is_dir():
        raise RuntimeError(f"no screenshots produced at {produced}")
    shutil.move(str(produced), str(out_dir))
    return out_dir


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def index(d: Path) -> dict[str, str]:
    """
    Map "<section>/<name>.png" -> sha256. The generator nests screenshots one
    level deep, in a directory per view section, so this must recurse; a
    non-recursive glob silently finds nothing and every comparison "passes".
    """
    return {str(p.relative_to(d)): digest(p) for p in sorted(d.rglob("*.png"))}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", default=None,
                    help="git ref to compare against (default: merge-base with "
                         "the tracked upstream branch, else 'dev')")
    ap.add_argument("--locale", default="en")
    ap.add_argument("--keep", action="store_true",
                    help="keep both render dirs for manual diffing")
    ap.add_argument("--python", default=None,
                    help="interpreter to render with (default: this one)")
    args = ap.parse_args()

    root = repo_root()
    python = Path(args.python) if args.python else Path(sys.executable)

    baseline = args.baseline
    if baseline is None:
        for candidate in ("fork/dev", "origin/dev", "dev"):
            try:
                run(["git", "rev-parse", "--verify", candidate], cwd=root)
                baseline = candidate
                break
            except RuntimeError:
                continue
        if baseline is None:
            print("could not resolve a baseline ref; pass --baseline", file=sys.stderr)
            return 2

    base_sha = run(["git", "rev-parse", "--short", baseline], cwd=root).strip()
    head_sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=root).strip()

    print("=" * 70)
    print("Non-touch pixel-identity check")
    print("=" * 70)
    print(f"  baseline : {baseline} ({base_sha})")
    print(f"  working  : HEAD ({head_sha})")
    print(f"  locale   : {args.locale}")
    print(f"  python   : {python}")

    work = Path(tempfile.mkdtemp(prefix="pixel-identity-"))
    tree = work / "baseline-tree"
    try:
        print(f"\n[1/3] checking out {baseline} into a temp worktree")
        run(["git", "worktree", "add", "--detach", str(tree), baseline], cwd=root)

        # The translations submodule is not populated in a fresh worktree, and
        # the generator needs its compiled catalogs. Reuse the ones we already
        # have rather than re-cloning and re-compiling.
        src_tx = root / "src" / "seedsigner" / "resources" / "seedsigner-translations"
        dst_tx = tree / "src" / "seedsigner" / "resources" / "seedsigner-translations"
        if src_tx.is_dir():
            if dst_tx.exists() and not dst_tx.is_symlink():
                shutil.rmtree(dst_tx, ignore_errors=True)
            if not dst_tx.exists():
                dst_tx.symlink_to(src_tx)

        print(f"[2/3] rendering baseline")
        base_dir = render(tree, work / "baseline", args.locale, python, root)

        print(f"[3/3] rendering working tree")
        head_dir = render(root, work / "head", args.locale, python, root)

        base_idx, head_idx = index(base_dir), index(head_dir)

        only_base = sorted(set(base_idx) - set(head_idx))
        only_head = sorted(set(head_idx) - set(base_idx))
        common = sorted(set(base_idx) & set(head_idx))

        # A gate that passes because it found nothing is worse than no gate.
        if not common:
            print(f"\n  FAIL - no screenshots were compared "
                  f"({len(base_idx)} baseline, {len(head_idx)} working).\n"
                  f"  The render or the output layout changed; fix this script "
                  f"rather than trusting the result.")
            return 2

        differing, skipped, network_flagged = [], [], []
        for name in common:
            if base_idx[name] == head_idx[name]:
                continue
            stem = Path(name).stem
            if stem.endswith(GIT_STATE_SUFFIX):
                skipped.append(name)
            elif stem in NETWORK_DEPENDENT:
                network_flagged.append(name)
            else:
                differing.append(name)

        print(f"\n  compared {len(common)} screenshots "
              f"({len(base_idx)} baseline, {len(head_idx)} working)")
        # Never hide what was excluded from the verdict.
        for name in skipped:
            print(f"    skipped (renders live git state): {name}")
        for name in network_flagged:
            print(f"    DIFFERS, but depends on the GitHub releases fetch: {name}")
            print(f"      -> re-run before treating this as a regression")

        ok = True
        if only_base or only_head:
            ok = False
            print(f"\n  SCREENSHOT SET CHANGED")
            for n in only_base:
                print(f"    removed: {n}")
            for n in only_head:
                print(f"    added:   {n}")

        if differing:
            ok = False
            print(f"\n  {len(differing)} SCREENSHOT(S) DIFFER:")
            for n in differing:
                print(f"    {n}")
                print(f"      baseline {base_idx[n][:16]}  working {head_idx[n][:16]}")

        print("\n" + "=" * 70)
        if ok:
            print("PASS - non-touch rendering is unchanged")
        else:
            print("FAIL - non-touch rendering changed; gate the change on "
                  "SEEDSIGNER_TOUCH")
            if not args.keep:
                print("      re-run with --keep to diff the images")
        print("=" * 70)

        if args.keep:
            print(f"\nrenders kept at:\n  {base_dir}\n  {head_dir}")
        return 0 if ok else 1

    finally:
        try:
            run(["git", "worktree", "remove", "--force", str(tree)], cwd=root)
        except RuntimeError:
            pass
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
