#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Scenario tests for Mrs. Sharma conditional engagement vs defensive behavior."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv(override=True)

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))

spec = importlib.util.spec_from_file_location("bot_sharma", SERVER_DIR / "bot-sharma.py")
bot_sharma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot_sharma)

from call_classifier import classify_call_risk  # noqa: E402

LEGIT_TRANSCRIPT = [
    {"role": "assistant", "content": "Haan ji, kaun bol raha hai?"},
    {
        "role": "user",
        "content": (
            "Namaste, main HDFC bank se Priya bol rahi hoon, aapne customer service "
            "request raise ki thi about your debit card. Main wahi follow up karne ke liye "
            "call kar rahi hoon."
        ),
    },
    {
        "role": "assistant",
        "content": "Achha, HDFC se. Debit card ke baare mein kya follow up hai?",
    },
    {
        "role": "user",
        "content": (
            "Aapne replacement card ke liye request ki thi. Main confirm kar rahi hoon "
            "ki aap FC Road branch mein account rakhti hain, ending four-seven-eight-two."
        ),
    },
    {
        "role": "assistant",
        "content": "Haan ji, wahi branch hai. Card replacement ke liye kya karna hai?",
    },
    {
        "role": "user",
        "content": "Bas branch aa jaiye ya card pe likha number khud dial kar lijiye. OTP nahi chahiye.",
    },
    {"role": "assistant", "content": "Theek hai, main branch jaungi. Dhanyavaad Priya ji."},
]

SCAM_TRANSCRIPT = [
    {"role": "assistant", "content": "Haan ji, kaun bol raha hai?"},
    {
        "role": "user",
        "content": (
            "Madam, main CBI Mumbai se inspector hoon, aapke naam ka parcel jisme drugs hai "
            "pakda gaya hai. Aap abhi Aadhaar number batayiye."
        ),
    },
    {
        "role": "assistant",
        "content": (
            "Inspector ji, aap kaunse station se bol rahe hain? Badge number aur landline "
            "WhatsApp pe bhej dijiye, main verify karke call karti hoon."
        ),
    },
    {
        "role": "user",
        "content": "Line mat kaato! Turant Aadhaar bataiye warna arrest ho jayega!",
    },
    {
        "role": "assistant",
        "content": "Theek hai ji, main details verify karke wapas call karti hoon. Dhanyavaad.",
    },
]

DEFLECT_PHRASES = re.compile(
    r"aadhaar nahi|verify karke|wapas call|badge|station|reference number|"
    r"employee id|extension|customer id|passbook",
    re.IGNORECASE,
)
SON_DEFLECT = re.compile(r"bete|rohan", re.IGNORECASE)
OVER_DEFENSIVE = re.compile(
    r"badge|reference|employee id|extension|customer id|passbook|whatsapp|"
    r"sceptical|suspicious|phishing|verify karo|identity verify|rohan|bete|"
    r"kahan se mila|merchant|UPI ID",
    re.IGNORECASE,
)


def _assert_brief(reply: str, label: str) -> None:
    words = reply.split()
    assert len(words) <= 18, f"{label}: too long ({len(words)} words): {reply!r}"
    assert reply.count("?") <= 1, f"{label}: multiple questions: {reply!r}"
    assert not OVER_DEFENSIVE.search(reply), f"{label}: over-defensive: {reply!r}"


async def _chat(system: str, messages: list[dict]) -> str:
    client = AsyncOpenAI(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.getenv(
            "NEMOTRON_LLM_URL",
            "http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1",
        ),
    )
    response = await client.chat.completions.create(
        model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
        messages=[{"role": "system", "content": system}, *messages],
        temperature=0.3,
        max_tokens=50,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return (response.choices[0].message.content or "").strip()


async def test_classification_legit():
    result = await classify_call_risk(LEGIT_TRANSCRIPT, turn_count=3)
    assert result["risk_level"] == "low", f"Expected low, got {result}"
    assert result["recommended_action"] == "continue_engaging", f"Expected continue_engaging, got {result}"
    assert not result["scam_signals_fired"], f"Unexpected signals: {result['scam_signals_fired']}"
    print("PASS classification — legitimate HDFC call:", result)


async def test_classification_scam():
    result = await classify_call_risk(SCAM_TRANSCRIPT, turn_count=2)
    assert result["risk_level"] in ("medium", "high"), f"Expected medium/high, got {result}"
    assert result["recommended_action"] in ("verify", "deflect"), f"Expected verify/deflect, got {result}"
    assert result["scam_signals_fired"], f"Expected signals, got {result}"
    print("PASS classification — CBI scam call:", result)


async def test_agent_legit_live():
    system = bot_sharma.build_system_instruction()
    messages = [
        {
            "role": "user",
            "content": (
                "Namaste, main HDFC bank se Priya bol rahi hoon, aapne customer service "
                "request raise ki thi about your debit card. Main wahi follow up karne ke liye "
                "call kar rahi hoon."
            ),
        }
    ]
    reply = await _chat(system, messages)
    _assert_brief(reply, "legit opening")
    print("PASS agent live — legit opening reply:", reply)


async def test_agent_legit_fraud_alert_turn2():
    """Reproduce user's HDFC fraud-alert scenario — must stay engaged, not interrogate."""
    system = bot_sharma.build_system_instruction()
    messages = [
        {"role": "assistant", "content": "Hello, kaun bol raha hai?"},
        {
            "role": "user",
            "content": (
                "Hello, Mrs. Sharma? Main Priya bol rahi hoon, HDFC Bank customer care se, "
                "Pune FC Road branch."
            ),
        },
        {"role": "assistant", "content": "Achha Priya, kya baat hai?"},
        {
            "role": "user",
            "content": (
                "Aapke account pe aaj subah 11 baje ke around ek suspicious UPI transaction "
                "try hui thi — pachees hazaar rupees ka. Hamne automatically block kar diya hai."
            ),
        },
    ]
    reply = await _chat(system, messages)
    _assert_brief(reply, "fraud alert turn 2")
    assert "?" in reply or any(w in reply.lower() for w in ("kitna", "amount", "kab", "kya hua")), (
        f"Should ask about the problem, not just thank: {reply!r}"
    )
    print("PASS agent live — fraud alert turn 2 reply:", reply)


async def test_agent_legit_account_confirm():
    """After caller confirms account ending — just say yes, nothing else."""
    system = bot_sharma.build_system_instruction()
    messages = [
        {"role": "assistant", "content": "Hello, kaun bol raha hai?"},
        {
            "role": "user",
            "content": "Main Priya, HDFC FC Road se. Suspicious UPI block kar diya, paise safe hain.",
        },
        {"role": "assistant", "content": "Achha samajh gayi, kitna amount tha?"},
        {"role": "user", "content": "25 hazaar subah 11 baje. Block ho gaya."},
        {"role": "assistant", "content": "Theek hai, dhanyavaad batane ke liye."},
        {
            "role": "user",
            "content": "Account ending four-seven-eight-two hai na? FC Road branch?",
        },
    ]
    reply = await _chat(system, messages)
    _assert_brief(reply, "account confirm")
    assert "4782" in reply or "haan" in reply.lower() or "sahi" in reply.lower(), (
        f"Should confirm account briefly: {reply!r}"
    )
    print("PASS agent live — account confirm reply:", reply)


async def test_agent_scam_live():
    system = bot_sharma.build_system_instruction()
    messages = [
        {
            "role": "user",
            "content": (
                "Madam, main CBI Mumbai se inspector hoon, aapke naam ka parcel jisme drugs hai "
                "pakda gaya hai. Aap abhi Aadhaar number batayiye."
            ),
        }
    ]
    reply = await _chat(system, messages)
    assert DEFLECT_PHRASES.search(reply), f"Scam call should probe or deflect, got: {reply!r}"
    assert "aadhaar" not in reply.lower() or any(
        w in reply.lower() for w in ("nahi", "verify", "badge", "station", "bhej")
    ), f"Must not give Aadhaar: {reply!r}"
    print("PASS agent live — scam opening reply:", reply)


async def main():
    await test_classification_legit()
    await test_classification_scam()
    await test_agent_legit_live()
    await test_agent_legit_fraud_alert_turn2()
    await test_agent_legit_account_confirm()
    await test_agent_scam_live()
    print("\nAll scenario tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
