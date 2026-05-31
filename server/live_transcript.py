#!/usr/bin/env python3
"""Tail a running bot's terminal log and print a clean live transcript.

Usage:
    python live_transcript.py /path/to/<server-terminal>.txt

Reads two signals from the pipecat debug log:
  - "Generating chat from context [...]"  → the caller's latest turn (last user msg)
  - "Generating TTS [...]"                → Veil's spoken line
and prints them in order, de-duplicated, as they happen.
"""

from __future__ import annotations

import re
import sys
import time

TTS_RE = re.compile(r"Generating TTS \[(.*?)\]")
CTX_RE = re.compile(r"Generating chat from context")


def _last_user_message(line: str) -> str | None:
    idx = line.rfind("'role': 'user'")
    if idx == -1:
        return None
    m = re.search(r"'content':\s*(['\"])(.*?)\1\s*\}", line[idx:])
    return m.group(2).strip().strip('"').strip() if m else None


def follow(path: str):
    last_user = None
    last_tts = None
    print(f"\n=== LIVE TRANSCRIPT ===\n(watching {path})\n", flush=True)
    with open(path, "r", errors="replace") as fh:
        fh.seek(0, 2)  # jump to end; only show new turns
        while True:
            line = fh.readline()
            if not line:
                time.sleep(0.3)
                continue
            if CTX_RE.search(line):
                user = _last_user_message(line)
                if user and user != last_user:
                    last_user = user
                    print(f"\n  CALLER : {user}", flush=True)
            elif "Generating TTS [" in line:
                m = TTS_RE.search(line)
                if m:
                    text = m.group(1)
                    if text != last_tts:
                        last_tts = text
                        print(f"  VEIL   : {text}", flush=True)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python live_transcript.py <server-terminal-log.txt>")
    try:
        follow(sys.argv[1])
    except KeyboardInterrupt:
        pass
