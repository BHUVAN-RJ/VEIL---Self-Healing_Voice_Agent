#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Fix missing spaces in Nemotron streamed Hinglish tokens."""

from __future__ import annotations

import re

# Only used when an entire utterance arrives as one glued blob (no spaces at all).
HINGLISH_VOCAB = sorted(
    {
        "achha", "amount", "arre", "aur", "aap", "batane", "baje", "baare", "block", "branch",
        "dhanyavaad", "gaya", "hai", "haan", "hua", "hain", "hazaar", "kab", "kitna", "kya",
        "liye", "main", "matlab", "mein", "mujhe", "paise", "priya", "safe", "sahi", "samajh",
        "subah", "tha", "theek", "transaction", "wahi", "batao", "baat", "ji", "toh", "sab",
        "keliye", "karne", "kaha", "koi", "kiya", "kis", "ke", "ko", "na", "ab", "ho",
    },
    key=len,
    reverse=True,
)


def space_before_chunk(prev_char: str, chunk: str) -> str:
    """Insert a leading space on *chunk* only after punctuation.

    LLM token streams already carry their own word-boundary spaces: a leading
    space on a token marks a new word, while continuation tokens (e.g. "ha"
    completing "Achha") arrive with no space. Inserting a space between two
    adjacent alphanumeric tokens therefore shatters words into syllables
    ("Ach ha Pri ya"), so we never do that. We only normalize spacing right
    after sentence punctuation. Fully glued utterances are handled later by
    ``_split_glued_blob``.
    """
    if not chunk or not prev_char:
        return chunk
    if prev_char.isspace() or chunk[0].isspace():
        return chunk
    if prev_char in ",.!?;:" and chunk[0].isalnum():
        return " " + chunk
    return chunk


def append_stream_chunk(buffer: str, chunk: str) -> str:
    """Join streamed tokens into one string with correct spacing."""
    if not chunk:
        return buffer
    if not buffer:
        return chunk
    spaced = space_before_chunk(buffer[-1], chunk)
    return buffer + spaced


def _split_glued_blob(blob: str) -> str:
    """Last resort: split a completely space-free utterance."""
    if " " in blob or len(blob) < 10:
        return blob

    lower = blob.lower()
    parts: list[str] = []
    i = 0
    while i < len(lower):
        matched = False
        for word in HINGLISH_VOCAB:
            if lower.startswith(word, i):
                parts.append(blob[i : i + len(word)])
                i += len(word)
                matched = True
                break
        if not matched:
            return blob
    return " ".join(parts)


def fix_hinglish_spacing(text: str) -> str:
    """Light touch: fix glued tokens, never re-space normal text."""
    text = text.replace("✓", "").strip()
    text = re.sub(r"\([^)]*\)", "", text)

    # Collapse accidental double spaces first.
    text = re.sub(r" +", " ", text)

    # Only add missing space after punctuation (not if already present).
    text = re.sub(r",(?=\S)", ", ", text)
    text = re.sub(r"\.(?=\S)", ". ", text)
    text = re.sub(r"\?(?=\S)", "? ", text)

    # Entire utterance glued with zero spaces — split once.
    if " " not in text and len(text) >= 10:
        text = _split_glued_blob(text)

    return re.sub(r" +", " ", text).strip()
