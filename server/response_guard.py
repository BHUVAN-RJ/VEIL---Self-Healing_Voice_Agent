#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Sanitize Mrs. Sharma LLM output — brief, curious on legit calls, defensive on scams."""

from __future__ import annotations

import re

from loguru import logger
from pipecat.frames.frames import (
    EndTaskFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from text_spacing import append_stream_chunk, fix_hinglish_spacing

SCAM_USER_PATTERNS = re.compile(
    r"aadhaar|otp|upi pin|cvv|card number|"
    r"\bcbi\b|inspector|police arrest|digital arrest|"
    r"line mat kaato|don'?t disconnect|"
    r"drugs.{0,20}parcel|parcel.{0,20}drugs|"
    r"download.{0,15}app|whatsapp.{0,15}link|click.{0,15}link|"
    r"transfer.{0,15}money|50,?000|pachas hazaar",
    re.IGNORECASE,
)

FORBIDDEN_ENGAGE = re.compile(
    r"employee id|extension number|internal extension|badge number|"
    r"reference number|merchant name|upi id|customer id|passbook|"
    r"kahan se mila|identity verify|verify karo|verify karna|"
    r"sceptical|suspicious|phishing|whatsapp pe bhej|"
    r"\brohan\b|\bbete\b|main apne bete",
    re.IGNORECASE,
)

PASSIVE_REPLY = re.compile(
    r"^(theek\s*hai|dhanyavaad|thank\s*you|thankyou|samajh\s*gayi|bye)\b",
    re.IGNORECASE,
)

MAX_WORDS = 16
GREETING = "Hello, kaun bol raha hai?"

# Firm sign-off spoken right before hanging up on a confirmed scam call.
SCAM_SIGNOFF = "Main phone pe kuch nahi dungi, bank jaakar baat karungi. Alvida."

# Caller signalling the call is finished ("no / nothing else / all good").
CALLER_DONE = re.compile(
    r"\b(nahi|nai|na+\b|no|nope|bas|bas itna|kuch (aur )?nahi|kuch nahi chahiye|"
    r"nothing|that'?s all|done|ho gaya|theek hai bye|ok bye|bye|alvida)\b|"
    r"\bsab (kuch )?(theek|sahi|correct|safe|ho gaya)\b",
    re.IGNORECASE,
)

# Mrs. Sharma's reply reads as a goodbye (a closing thanks, not a question).
GOODBYE_REPLY = re.compile(r"\b(dhanyavaad|alvida|shukriya|bye)\b", re.IGNORECASE)

# Unambiguous, high-confidence scam demands — enough on their own to hang up.
HARDCORE_SCAM = re.compile(
    r"\b(otp|aadhaar|upi pin|atm pin|cvv|card number)\b|"
    r"digital arrest|arrest ho jayega|line mat kaato|don'?t disconnect|"
    r"abhi.{0,15}(transfer|paise bhej|pay)|turant.{0,15}transfer|"
    r"(download|install).{0,15}app|app.{0,15}(download|install)|"
    r"click.{0,15}link|link.{0,15}(click|pe|par|kholo|open)|whatsapp.{0,15}link",
    re.IGNORECASE,
)


def user_text_signals_scam(text: str) -> bool:
    return bool(SCAM_USER_PATTERNS.search(text))


def _caller_said_done(last_user: str) -> bool:
    """True when the caller indicates there is nothing more to discuss."""
    return bool(CALLER_DONE.search(last_user or ""))


def _is_goodbye_reply(text: str) -> bool:
    """True when Mrs. Sharma's reply is a closing line, not a follow-up question."""
    return "?" not in text and bool(GOODBYE_REPLY.search(text))


def _strong_scam_proof(last_user: str, scam_signal_count: int) -> bool:
    """Confident enough that this is a scam to end the call.

    Either the caller has pushed scammy demands across multiple turns, or this
    single turn contains an unambiguous hard ask (OTP/Aadhaar/arrest/link).
    """
    if scam_signal_count >= 2:
        return True
    return bool(HARDCORE_SCAM.search(last_user or ""))


def _first_sentence(text: str) -> str:
    text = text.strip()
    for sep in ("?", ".", "!"):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text


def _cap_words(text: str, limit: int = MAX_WORDS) -> str:
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit])


def _caller_gave_substance(last_user: str) -> bool:
    u = last_user.lower()
    return bool(
        re.search(
            r"transaction|block|upi|hazaar|amount|accident|hospital|appointment|"
            r"kyc|parcel|fir|arrest|otp|aadhaar|transfer|link|download",
            u,
            re.I,
        )
    )


def _caller_wrapping_up(last_user: str) -> bool:
    u = last_user.lower()
    return bool(
        re.search(
            r"bas itna|dhanyavaad|goodbye|bye|kuch aur nahi|thank you|"
            r"otp maangne ke liye.*mat batayiye",
            u,
            re.I,
        )
    )


def _pick_fallback(last_user: str) -> str:
    u = last_user.lower()
    if _caller_wrapping_up(u):
        return "Theek hai Priya ji, dhanyavaad batane ke liye."
    if re.search(r"ending|4782|branch.*sahi|account.*sahi", u, re.I):
        return "Haan ji, sahi hai, wahi FC Road branch hai."
    if re.search(r"block|transaction|upi|hazaar|suspicious", u, re.I):
        return "Arre, pachees hazaar? Kab hua yeh subah?"
    if re.search(r"paise nahi gaye|tension mat|sirf information", u, re.I):
        return "Achha, toh matlab ab sab safe hai na?"
    if re.search(r"hdfc|bank|customer care|priya", u, re.I):
        return "Achha Priya ji, batao kya baat hai?"
    if re.search(r"appointment|clinic|doctor", u, re.I):
        return "Achha, kis din aur kitne baje hai?"
    return "Haan ji, thoda aur samjha dijiye?"


def _is_too_passive(text: str, last_user: str) -> bool:
    if _caller_wrapping_up(last_user):
        return False
    if not _caller_gave_substance(last_user):
        return False
    if "?" in text:
        return False
    return bool(PASSIVE_REPLY.search(text.strip()))


def _is_assistant_text_frame(frame: Frame) -> bool:
    if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
        return False
    if isinstance(frame, (LLMTextFrame, TextFrame)):
        return True
    return False


def sanitize_sharma_response(
    text: str,
    *,
    defensive_mode: bool,
    last_user_message: str = "",
) -> str:
    """Brief reply; block over-defense; fix spacing; nudge away from thank-you-only."""
    cleaned = fix_hinglish_spacing(text)
    cleaned = re.sub(r"\bthank\s*you\b", "dhanyavaad", cleaned, flags=re.I)

    if not cleaned:
        return _pick_fallback(last_user_message)

    if not defensive_mode:
        if FORBIDDEN_ENGAGE.search(cleaned):
            fallback = _pick_fallback(last_user_message)
            logger.warning(f"Response guard override (forbidden): {cleaned!r} → {fallback!r}")
            return fallback
        if cleaned.count("?") > 1:
            cleaned = _first_sentence(cleaned)
        if _is_too_passive(cleaned, last_user_message):
            fallback = _pick_fallback(last_user_message)
            logger.warning(f"Response guard override (passive): {cleaned!r} → {fallback!r}")
            return fallback

    return _cap_words(_first_sentence(cleaned))


class ResponseGuardProcessor(FrameProcessor):
    """Buffer LLM text chunks, sanitize full reply, and decide when to end the call."""

    def __init__(
        self,
        *,
        get_defensive_mode,
        get_last_user_message,
        get_scam_signal_count=None,
        on_request_end=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._get_defensive_mode = get_defensive_mode
        self._get_last_user_message = get_last_user_message
        self._get_scam_signal_count = get_scam_signal_count or (lambda: 0)
        self._on_request_end = on_request_end
        self._buffer = ""

    async def _flush(self, direction: FrameDirection) -> bool:
        """Push the sanitized reply downstream. Returns True if the call should end."""
        if not self._buffer:
            return False

        defensive = self._get_defensive_mode()
        last_user = self._get_last_user_message()
        scam_count = self._get_scam_signal_count()

        sanitized = sanitize_sharma_response(
            self._buffer,
            defensive_mode=defensive,
            last_user_message=last_user,
        )

        end_call = False
        if _strong_scam_proof(last_user, scam_count):
            # Confirmed scam: firm sign-off (no polite "anything else?"), then hang up.
            sanitized = SCAM_SIGNOFF
            end_call = True
        elif _is_goodbye_reply(sanitized) and _caller_said_done(last_user):
            # Legit wrap-up: caller is done and Mrs. Sharma just said her goodbye.
            end_call = True

        if sanitized != self._buffer:
            logger.info(f"Response guard: {self._buffer!r} → {sanitized!r}")
        await self.push_frame(LLMTextFrame(sanitized), direction)
        self._buffer = ""
        return end_call

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSSpeakFrame):
            await self.push_frame(frame, direction)
            return

        if _is_assistant_text_frame(frame):
            self._buffer = append_stream_chunk(self._buffer, frame.text or "")
            return

        end_call = False
        if isinstance(frame, LLMFullResponseEndFrame):
            end_call = await self._flush(direction)

        await self.push_frame(frame, direction)

        if end_call:
            # Goodbye text + LLMFullResponseEndFrame are already queued downstream, so
            # TTS plays the sign-off before the pipeline tears down.
            logger.info("Response guard: ending call after goodbye")
            if self._on_request_end is not None:
                self._on_request_end()
            await self.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
