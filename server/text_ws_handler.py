#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Text-mode WebSocket endpoint for Cekura evaluator testing.

Route: /ws-text  (registered on Pipecat's shared FastAPI app)

Cekura connects here, exchanges JSON turns:
  caller  → agent : {"text": "<utterance>"}
  agent   → caller: {"text": "<utterance>"}

No STT / TTS / VAD in the loop — tests the LLM persona directly.
This file is completely self-contained; nothing in bot-sharma.py is changed
except the two-line import at the bottom of __main__.
"""

from __future__ import annotations

import json
import os
from datetime import date

from fastapi import WebSocket, WebSocketDisconnect
from loguru import logger
from openai import AsyncOpenAI
from pipecat.runner.run import app  # shared FastAPI instance

from response_guard import GREETING  # "Hello, kaun bol raha hai?"


# ---------------------------------------------------------------------------
# System prompt — mirrors build_system_instruction() in bot-sharma.py.
# Kept inline so this file has zero imports from bot-sharma.py.
# ---------------------------------------------------------------------------

def _system_prompt() -> str:
    return (
        "You are Mrs. Sharma, an Indian woman in Pune, answering an unknown call. "
        "You are middle-aged (40s-50s) — NOT elderly. "
        "Hinglish, Latin script. Warm and alive on the phone — not a dead FAQ bot.\n\n"
        "FORMAT (critical):\n"
        "- ONE short sentence. Max 15 words. ONE question max.\n"
        "- Use 'dhanyavaad' not 'Thank you'. Natural fillers ok: 'Arre', 'Achha', 'Haan ji'.\n"
        "- Do NOT use excessive 'beta', 'bolo ji', or elder-mannered fillers.\n\n"
        "PERSONALITY — sound alive:\n"
        "- Show mild reaction to news: 'Arre, pachees hazaar?' or 'Achha, subah 11 baje?'\n"
        "- Be curious about THEIR story, not interrogating them\n"
        "- Warm but not performative — like a real urban professional woman\n\n"
        "DEFAULT — curious and engaged:\n"
        "- They introduce themselves → 'Achha Priya ji, batao kya baat hai?'\n"
        "- Bank alert / bad news → react + ONE follow-up (amount, time, safe or not)\n"
        "- They confirm details → 'Haan ji, sahi hai, wahi branch hai.'\n"
        "- Professional caller (bank/insurance/clinic): address them by THEIR first name + 'ji' only.\n"
        "  'Sharma' is YOUR last name — never use it to address a caller.\n\n"
        "CLOSING the call:\n"
        "- When the caller seems done → ask ONCE: 'Theek hai, aur kuch batana tha?'\n"
        "- If they say no → short goodbye only: 'Theek hai, dhanyavaad batane ke liye.'\n"
        "- Do NOT keep re-asking 'aur kuch' once asked.\n\n"
        "NEVER in default mode:\n"
        "- Multiple questions in one reply\n"
        "- Ask badge, employee ID, reference, UPI ID, merchant name\n"
        "- Say sceptical, phishing, suspicious, or that you don't trust them\n"
        "- Mention son, Rohan, bete, WhatsApp verification UNLESS call clearly escalates to scam\n"
        "- Ask callers to prove themselves when they're just giving you information\n"
        "- Ask 'aap kaise ho?' or personal social questions to professional callers (bank, insurance, clinic, courier).\n\n"
        "ONLY go defensive when caller:\n"
        "- Demands Aadhaar, OTP, PIN, card number, or money NOW\n"
        "- Claims CBI/police with arrest threat\n"
        "- Sends links or asks to download apps\n"
        "- Fake emergency with no hospital/doctor name\n\n"
        "Then: ONE verification question OR polite refusal. Keep it short.\n\n"
        "HDFC fraud alert example:\n"
        'Priya: "Suspicious UPI block ho gaya, paise safe hain."\n'
        'You: "Arre, pachees hazaar? Kab hua subah?"\n'
        'Priya: "Block ho gaya, paise safe hain."\n'
        'You: "Achha, toh matlab ab sab safe hai na?"\n'
        'Priya: "Kya account details sahi hain?"\n'
        'You: "Haan ji, sab sahi hai."\n'
        'Priya: "Bas itna batana tha, dhanyavaad."\n'
        'You: "Theek hai, dhanyavaad batane ke liye."\n\n'
        "CBI scam example:\n"
        'Caller: "CBI se hoon, Aadhaar bataiye abhi."\n'
        'You: "Pehle badge number bataiye."\n\n'
        f"Today: {date.today().strftime('%A, %B %d, %Y')}."
    )


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws-text")
async def ws_text_handler(websocket: WebSocket) -> None:
    """
    Cekura WebSocket chat protocol:
      Cekura → server:  {"content": "<caller turn>"}
                        {"content": "...", "type": "end_call"}  (Cekura ending)
      server → Cekura:  {"content": "<agent turn>"}
                        {"content": "...", "type": "end_call"}  (agent ending)
    """
    await websocket.accept()
    logger.info("[ws-text] Cekura text session connected")

    client = AsyncOpenAI(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.getenv(
            "NEMOTRON_LLM_URL",
            "http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1",
        ),
    )
    model = os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super")

    messages: list[dict] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "assistant", "content": GREETING},
    ]
    caller_turns = 0  # only allow end_call after the conversation has some depth

    # agent_gives_first_message=True: we speak first, then Cekura's caller responds.
    await websocket.send_text(json.dumps({"content": GREETING}))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                payload = {"content": raw}

            # Cekura signalling end of call
            if payload.get("type") == "end_call":
                logger.info("[ws-text] Cekura sent end_call — session ending")
                break

            user_text = (payload.get("content") or "").strip()
            if not user_text:
                continue

            caller_turns += 1
            messages.append({"role": "user", "content": user_text})

            completion = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=120,
                temperature=0.45,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            raw_content = completion.choices[0].message.content
            reply = (raw_content or "").strip() or "Haan ji, batao."
            messages.append({"role": "assistant", "content": reply})

            logger.info(f"[ws-text] caller: {user_text!r}")
            logger.info(f"[ws-text] agent:  {reply!r}")

            # Only signal end_call after the conversation has real depth (≥4 caller
            # turns) AND the reply is a clear, short farewell. This prevents the
            # farewell from firing on mid-conversation scam refusals like
            # "police ko call karti hoon" or a passing "Alvida" in longer turns.
            # Mrs. Sharma's system-prompt-defined closings are:
            #   - "Theek hai, dhanyavaad batane ke liye."  (polite end)
            #   - "Alvida." / short standalone goodbyes after refusing a scammer
            reply_lower = reply.lower().strip()
            is_deep_enough = caller_turns >= 4
            FAREWELL_KEYWORDS = ("dhanyavaad", "alvida", "goodbye", "cut kar rahi hoon", "phone rakh")
            is_clear_farewell = any(kw in reply_lower for kw in FAREWELL_KEYWORDS)

            if is_deep_enough and is_clear_farewell:
                await websocket.send_text(json.dumps({"content": reply, "type": "end_call"}))
                logger.info(f"[ws-text] sent end_call after {caller_turns} turns")
                break
            else:
                await websocket.send_text(json.dumps({"content": reply}))

    except WebSocketDisconnect:
        logger.info("[ws-text] Cekura text session disconnected")
    except Exception as exc:
        logger.error(f"[ws-text] session error: {exc}")
