#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Call transcript and classification logging for Cekura review."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

LOGS_DIR = Path(__file__).resolve().parent / "logs" / "calls"
FAMILY_LOG = Path(__file__).resolve().parent / "logs" / "family_notifications.log"


def ensure_log_dirs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    FAMILY_LOG.parent.mkdir(parents=True, exist_ok=True)


def send_summary_to_family(
    transcript: list[dict[str, Any]],
    classification: dict[str, Any],
    *,
    call_id: str,
    from_number: str | None = None,
    action_taken: str = "summary_logged",
) -> dict[str, Any]:
    """Log a family notification (demo stand-in for SMS to Rohan).

    In production this would SMS Rohan with the call summary. For the demo we
    write to console and append to a family notifications log file.

    Returns:
        The notification record that was logged.
    """
    ensure_log_dirs()
    timestamp = datetime.now(UTC).isoformat()

    record = {
        "timestamp": timestamp,
        "call_id": call_id,
        "from_number": from_number,
        "classification": classification,
        "transcript": transcript,
        "action_taken": action_taken,
        "notify_target": "Rohan (son) — demo log only, no SMS sent",
    }

    logger.warning(
        f"[FAMILY NOTIFY] Call {call_id} — risk={classification.get('risk_level')} "
        f"pattern={classification.get('pattern_matched')} — logged for Rohan"
    )

    with FAMILY_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    return record


def write_call_log(
    *,
    call_id: str,
    transcript: list[dict[str, Any]],
    classification: dict[str, Any],
    turn_count: int,
    from_number: str | None = None,
    to_number: str | None = None,
    outcome: str = "disconnected",
    family_notification: dict[str, Any] | None = None,
) -> Path:
    """Write a per-call JSON log for Cekura review.

    File path: server/logs/calls/{timestamp}_{call_id}.json
    """
    ensure_log_dirs()
    timestamp = datetime.now(UTC)
    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    safe_call_id = call_id.replace("/", "_").replace(":", "_")
    path = LOGS_DIR / f"{stamp}_{safe_call_id}.json"

    payload = {
        "call_id": call_id,
        "timestamp": timestamp.isoformat(),
        "from_number": from_number,
        "to_number": to_number,
        "turn_count": turn_count,
        "transcript": transcript,
        "classification": classification,
        "outcome": outcome,
        "family_notification": family_notification,
    }

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Call log written: {path}")
    return path
