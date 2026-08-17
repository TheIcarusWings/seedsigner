from base import BaseTest  # noqa: F401 - must import first; mocks out hardware deps

from embit import bip39

from seedsigner.gui.keyboard import T9Pad


class TestT9PredictiveMode(BaseTest):
    """
    Pure-logic tests for predictive ("best guess") T9: one tap per key, the
    BIP-39 wordlist disambiguates. A dead-end or unreachable word here would
    make a seed word untypeable, so the invariants run over the full wordlist.
    """

    def test_word_to_key_seq(self):
        assert T9Pad.word_to_key_seq("zoo") == [9, 6, 6]
        assert T9Pad.word_to_key_seq("act") == [2, 2, 8]
        assert T9Pad.word_to_key_seq("cat") == [2, 2, 8]  # same keys as "act"
        assert T9Pad.word_to_key_seq("") == []

    def test_word_to_key_seq_rejects_non_t9_chars(self):
        try:
            T9Pad.word_to_key_seq("é")
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_ambiguous_sequence_ranks_short_then_alpha(self):
        # 2-2-8 matches both "act" and "cat" plus their longer continuations.
        # Exact-length matches must surface first, alphabetical within a length.
        words = T9Pad.filter_words_by_key_seq(bip39.WORDLIST, [2, 2, 8])
        assert words[0] == "act"
        assert words[1] == "cat"
        assert "action" in words and "actual" in words
        for shorter, longer in zip(words, words[1:]):
            assert (len(shorter), shorter) < (len(longer), longer)

    def test_zoo_key_family(self):
        # 9-6-6 (wxyz, mno, mno) matches the z-o-* and w-o-* families.
        # Shortest first, alphabetical within a length.
        assert T9Pad.filter_words_by_key_seq(bip39.WORDLIST, [9, 6, 6]) == [
            "zoo", "wood", "wool", "zone", "woman", "wonder"
        ]

    def test_next_letters(self):
        assert T9Pad.next_letters(["zoo", "zone"], 2) == "no"
        # Position beyond every word: nothing left to type
        assert T9Pad.next_letters(["zoo"], 3) == ""

    def test_empty_seq_matches_everything(self):
        assert len(T9Pad.filter_words_by_key_seq(bip39.WORDLIST, [])) == len(bip39.WORDLIST)

    def test_every_word_reachable_with_no_dead_ends(self):
        # For every BIP-39 word: at each prefix depth, the word's next letter
        # must be offered by the pad (in next_letters), and the word must
        # survive in the candidate list to the end. Incremental filtering
        # mirrors filter_words_by_key_seq for speed.
        for w in bip39.WORDLIST:
            seq = T9Pad.word_to_key_seq(w)
            cands = list(bip39.WORDLIST)
            for i, key in enumerate(seq):
                assert w[i] in T9Pad.next_letters(cands, i), \
                    f"dead end typing '{w}' at position {i}"
                group = T9Pad.T9_GROUPS[key]
                cands = [c for c in cands if len(c) > i and c[i] in group]
            assert w in cands, f"'{w}' lost from its own candidate list"

    def test_incremental_filter_matches_direct_filter(self):
        # Cross-check the incremental filtering used above against the real
        # filter_words_by_key_seq on a deterministic sample
        for w in bip39.WORDLIST[::100]:
            seq = T9Pad.word_to_key_seq(w)
            direct = T9Pad.filter_words_by_key_seq(bip39.WORDLIST, seq)
            cands = list(bip39.WORDLIST)
            for i, key in enumerate(seq):
                group = T9Pad.T9_GROUPS[key]
                cands = [c for c in cands if len(c) > i and c[i] in group]
            assert sorted(direct) == sorted(cands)
            assert w in direct

    def test_full_word_candidates_share_key_signature(self):
        # After typing a full word's keys, every candidate must map to the
        # same key sequence on its shared prefix (sanity on the group math)
        for w in ["zoo", "act", "quiz", "abandon"]:
            seq = T9Pad.word_to_key_seq(w)
            for cand in T9Pad.filter_words_by_key_seq(bip39.WORDLIST, seq):
                assert T9Pad.word_to_key_seq(cand[:len(seq)]) == seq
