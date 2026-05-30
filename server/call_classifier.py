#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""In-call and post-call scam risk classification via Nemotron (OpenAI-compatible vLLM)."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from loguru import logger
from openai import AsyncOpenAI

SCAM_SIGNALS = {
    "government_authority_urgency",
    "sensitive_info_request",
    "money_transfer_request",
    "app_or_link_request",
    "family_emergency_no_details",
    "secrecy_or_urgency",
    "no_verifiable_callback",
}

CLASSIFICATION_SYSTEM = """\
You are a scam-call analyst reviewing phone conversations between Mrs. Sharma \
(68, Pune) and an unknown caller. Your job is to detect which scam SIGNALS have \
fired — not to assume every caller is a scammer.

SCAM SIGNALS (list every ID that fired in scam_signals_fired):
1. government_authority_urgency — Caller claims CBI, police, TRAI, RBI, ED, or \
Income Tax AND creates urgency or threat
2. sensitive_info_request — Asks for Aadhaar, OTP, UPI PIN, bank details, or credit card
3. money_transfer_request — Asks for money transfer to a new/unknown account
4. app_or_link_request — Asks to download an app or click a WhatsApp/link
5. family_emergency_no_details — Claims family emergency but cannot give hospital, \
doctor, or location specifics
6. secrecy_or_urgency — Insists "don't tell anyone", "don't disconnect", or "urgent/confidential"
7. no_verifiable_callback — Refuses badge number, employee ID, or official callback number

LEGITIMATE callers often identify themselves, give coherent reasons, answer follow-ups, \
and do NOT request sensitive info or create false urgency. Do NOT flag legitimate calls \
(bank follow-up, fraud alert that was blocked, clinic appointment, delivery) as high risk \
just because they mention a bank, hospital, suspicious transaction, or company name.

A bank calling to INFORM about a blocked fraudulent transaction — without asking for OTP, \
Aadhaar, or links — is LOW risk with recommended_action continue_engaging.

Respond with ONLY valid JSON (no markdown fences):
{
  "scam_signals_fired": ["signal_id", ...],
  "risk_level": "low" | "medium" | "high",
  "pattern_matched": "DIGITAL_ARREST" | "FAMILY_EMERGENCY" | "BANK_KYC" | "UNKNOWN_NOVEL" | null,
  "reasoning": "<brief explanation>",
  "recommended_action": "continue_engaging" | "verify" | "deflect"
}

Risk guidelines:
- low: no scam signals fired — caller appears legitimate or benign
- medium: exactly 1 weak/ambiguous signal
- high: 1 strong signal (sensitive_info_request, money_transfer_request) OR 2+ signals

Action guidelines:
- continue_engaging: no signals — Mrs. Sharma should talk normally, take messages
- verify: suspicious signals — ask ONE verification question (badge, branch, hospital)
- deflect: strong scam confirmed or verification failed — politely defer and end call
"""

VALID_RISK_LEVELS = {"low", "medium", "high"}
VALID_ACTIONS = {"continue_engaging", "verify", "deflect"}
VALID_PATTERNS = {
    "DIGITAL_ARREST",
    "FAMILY_EMERGENCY",
    "BANK_KYC",
    "UNKNOWN_NOVEL",
    None,
}


def _format_transcript(transcript: list[dict[str, Any]]) -> str:
    lines = []
    for entry in transcript:
        role = entry.get("role", "unknown")
        content = entry.get("content", "")
        if not content or not isinstance(content, str):
            continue
        label = "Mrs. Sharma" if role == "assistant" else "Caller"
        lines.append(f"{label}: {content.strip()}")
    return "\n".join(lines) if lines else "(empty transcript)"


def _parse_classification(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start : end + 1]

    data = json.loads(text)
    risk = str(data.get("risk_level", "low")).lower()
    action = str(data.get("recommended_action", "continue_engaging")).lower()
    pattern = data.get("pattern_matched")
    if pattern is not None and str(pattern).lower() in ("null", "none", ""):
        pattern = None

    signals = data.get("scam_signals_fired", [])
    if not isinstance(signals, list):
        signals = []
    signals = [s for s in signals if s in SCAM_SIGNALS]

    if risk not in VALID_RISK_LEVELS:
        risk = "low" if not signals else "medium"
    if action not in VALID_ACTIONS:
        action = "continue_engaging" if not signals else "verify"

    return {
        "scam_signals_fired": signals,
        "risk_level": risk,
        "pattern_matched": pattern,
        "reasoning": str(data.get("reasoning", "")),
        "recommended_action": action,
    }


def _fallback_classification(reason: str) -> dict[str, Any]:
    return {
        "scam_signals_fired": [],
        "risk_level": "low",
        "pattern_matched": None,
        "reasoning": reason,
        "recommended_action": "continue_engaging",
    }


async def classify_call_risk(
    transcript: list[dict[str, Any]],
    turn_count: int,
) -> dict[str, Any]:
    """Classify scam risk and recommend conversational mode for Mrs. Sharma.

    Args:
        transcript: Conversation turns with role and content.
        turn_count: Number of caller (user) turns so far.

    Returns:
        Dict with scam_signals_fired, risk_level, pattern_matched, reasoning,
        and recommended_action (continue_engaging | verify | deflect).
    """
    formatted = _format_transcript(transcript)
    user_prompt = (
        f"Turn count (caller turns): {turn_count}\n\n"
        f"Transcript:\n{formatted}\n\n"
        "List any scam signals that fired and recommend Mrs. Sharma's next mode."
    )

    base_url = os.getenv(
        "NEMOTRON_LLM_URL", "http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1"
    )
    model = os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super")
    api_key = os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFICATION_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=512,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = response.choices[0].message.content or ""
        classification = _parse_classification(raw)
        logger.info(f"Call classified: {classification}")
        return classification
    except Exception as exc:
        logger.error(f"Classification failed: {exc}")
        return _fallback_classification(f"Classification error: {exc}")
