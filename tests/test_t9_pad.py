from base import BaseTest  # noqa: F401 - must import first; mocks out hardware deps

from embit import bip39
from PIL import Image, ImageDraw

from seedsigner.gui.keyboard import T9Pad


def valid_next_letters(prefix: str) -> set:
    """Valid next letters for a BIP-39 prefix (mirrors calc_possible_alphabet)."""
    return {w[len(prefix)] for w in bip39.WORDLIST if w.startswith(prefix) and len(w) > len(prefix)}


def make_pad(prefix: str) -> T9Pad:
    img = Image.new("RGB", (240, 240))
    pad = T9Pad(draw=ImageDraw.Draw(img), rect=(0, 60, 160, 240))
    pad.update_active_letters(valid_next_letters(prefix))
    return pad


class TestT9PredictiveSkip(BaseTest):

    def test_cycle_skips_invalid_letters(self):
        # Prefix "ac": valid continuations are a subset; every key must cycle
        # only its valid letters and wrap back to the first
        pad = make_pad("ac")
        valid = valid_next_letters("ac")

        for key_num in range(2, 10):
            key_valid = [ch for ch in T9Pad.T9_GROUPS[key_num] if ch in valid]
            if not key_valid:
                assert not pad.is_key_active(key_num)
                assert pad.start_cycling(key_num) is None
                continue
            seen = [pad.start_cycling(key_num)]
            for _ in range(len(key_valid)):
                seen.append(pad.start_cycling(key_num))
            assert seen[-1] == seen[0], f"key {key_num} did not wrap: {seen}"
            assert set(seen) == set(key_valid), f"key {key_num} cycled {seen}, expected {key_valid}"
            pad.cancel_cycling()

    def test_first_tap_lands_on_first_valid_letter(self):
        # After "ac", key 7 (pqrs) has only q,r valid (acquire, across):
        # first tap must be "q", not "p"
        pad = make_pad("ac")
        assert pad.get_valid_letters(7) == ["q", "r"]
        assert pad.start_cycling(7) == "q"
        assert pad.start_cycling(7) == "r"
        assert pad.start_cycling(7) == "q"  # wraps, never offers p or s

    def test_commit_returns_valid_letter(self):
        pad = make_pad("ac")
        letter = pad.start_cycling(8)
        assert letter in valid_next_letters("ac")
        assert pad.commit_cycling() == letter
        assert pad.cycling_key is None and pad.cycling_letter is None

    def test_dead_keys_after_narrow_prefix(self):
        # Prefix "zo": only zone/zoo remain, so only key 6 (mno) stays active
        pad = make_pad("zo")
        assert pad.get_valid_letters(6) == ["n", "o"]
        for key_num in [2, 3, 4, 5, 7, 8, 9]:
            assert not pad.is_key_active(key_num), f"key {key_num} should be dead after 'zo'"

    def test_dead_key_taps_return_none(self):
        pad = make_pad("zo")
        # Center of key 2's rect (top row, middle column in the DEL/2/3 layout)
        x1, y1, x2, y2 = pad._get_key_rect(1, 0)
        assert pad.get_key_at_screen_coords((x1 + x2) // 2, (y1 + y2) // 2) is None

    def test_empty_prefix_offers_all_first_letters(self):
        first_letters = {w[0] for w in bip39.WORDLIST}
        pad = make_pad("")
        pad.update_active_letters(first_letters)
        for key_num in range(2, 10):
            expected = [ch for ch in T9Pad.T9_GROUPS[key_num] if ch in first_letters]
            assert pad.get_valid_letters(key_num) == expected
