# Veil — a self-healing scam-screening voice agent

> Veil answers the calls your grandparents shouldn't have to. It picks up unknown numbers as a calm, slightly hard-of-hearing Pune aunty who speaks Hinglish, stays warm with real callers, and quietly stonewalls scammers — never handing over an OTP, Aadhaar, or rupee. After every call it classifies the risk and flags the family.

Built for the **YC Voice Agents Hackathon** (Cekura · Daily · NVIDIA · AWS · Twilio) on [Pipecat](https://pipecat.ai).

## 🎥 Demo

**[▶ Watch the demo video](https://drive.google.com/drive/folders/1GH1yjzbPZAl8yJdalQ9TLD1AR60dzs9Q?usp=sharing)**

Hackathon submission: `[SUBMISSION LINK: TO ADD]`

---

## The problem

Phone scams disproportionately target the elderly: fake "bank fraud" alerts, "digital arrest" threats from people posing as CBI/police, KYC-expiry traps, and family-emergency cons. The ask is always the same — an OTP, an Aadhaar number, a UPI transfer, or "install this app." A confused 68-year-old on the spot is the perfect victim.

**Veil is a screening layer that picks up first.** It behaves like a real, slightly slow Pune resident, so:

- **Legitimate callers** (a bank confirming a blocked transaction, a clinic, a delivery) have a normal short conversation and get politely wrapped up.
- **Scammers** get a warm, curious, time-wasting target who never reveals anything sensitive — and the moment intent is clear, Veil disengages and hangs up.
- **The family** gets a post-call summary whenever a call looks risky.

---

## What makes it "self-healing"

Veil heals itself at two levels — once per reply at runtime, and once per iteration during development.

### 1. Runtime self-healing — the Response Guard

A small LLM like Nemotron streams imperfect output: glued Hinglish tokens, over-long rambles, occasional unsafe or over-defensive lines. Rather than trust raw model text, every reply passes through a **response guard** (`server/response_guard.py` + `server/text_spacing.py`) that repairs it *before it reaches the caller*:

- **Fixes Hinglish spacing** so the TTS speaks `Achha Priya ji, batao kya baat hai?` instead of syllable-shattered `Ach ha Pri ya ji…`.
- **Caps length** to one short, human sentence (no monologues).
- **Blocks self-sabotage** — strips replies where Veil would leak suspicion, ask for badge/employee IDs on a normal call, or otherwise break character.
- **Rescues dead-ends** — rewrites passive "theek hai, dhanyavaad" replies into a curious follow-up when the caller just raised something real.
- **Decides when to end the call** (see below), deterministically, so a flaky model can't strand the line.

### 2. Eval-driven self-healing — the Cekura loop

We treated [Cekura](https://cekura.com) as the test harness: generate adversarial scenarios (digital-arrest, fake bank fraud, KYC expiry, family emergency, plus benign calls), run them against the live Pipecat agent, read the failures, patch the prompt/guard, and re-run. Each pass tightened behavior.

| Cekura run | Result | What we fixed |
|---|---|---|
| Baseline | **5 / 11** | Over-defensive on legit calls, leaked verification asks, rambled |
| Iteration 1 | **11 / 11** | Conditional engagement (curious on legit, defensive on scam), brevity cap |
| Expanded suite | **15 / 16** | New edge cases; one wrap-up/hang-up miss |
| Final | **16 / 16** | Confirm-once closing + reliable scam hang-up |

---

## How a call flows

```
 Caller ──▶ Twilio / WebRTC ──▶ STT ──▶ Nemotron LLM ──▶ Response Guard ──▶ Cartesia TTS ──▶ Caller
                                                              │
                                                  (repairs + safety + end-call)
                                                              │
                              ┌───────────────────────────────┘
                              ▼
                on disconnect: classify risk ─▶ notify family if medium/high ─▶ write JSON call log
```

- **Greeting:** Veil answers with a short, neutral *"Hello, kaun bol raha hai?"*
- **Conditional engagement:** warm and curious with legit callers; defensive (one verification question or a polite refusal, never sensitive data) the moment scam patterns appear.
- **Closing a legit call:** when the caller signals they're done, Veil confirms **once** — *"Theek hai, aur kuch batana tha?"* — and on a "no," says a brief goodbye and hangs up automatically.
- **Ending a scam call:** once there's strong proof (an unambiguous OTP/Aadhaar/arrest/link demand, or repeated pushing), Veil drops a firm sign-off and disconnects — no polite confirmation.
- **Post-call (always):** a Nemotron classifier (`server/call_classifier.py`) scores the transcript for scam signals → `risk_level` + `recommended_action`; medium/high risk triggers a family notification, and every call is written to a JSON log (`server/call_logging.py`) for Cekura review.

---

## Tech stack

| Layer | Choice |
|---|---|
| Orchestration | [Pipecat](https://pipecat.ai) |
| STT | [Deepgram](https://deepgram.com) `nova-3` (multilingual Hinglish), or NVIDIA Nemotron streaming ASR |
| LLM | [NVIDIA Nemotron-3-Super](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16) (vLLM, OpenAI-compatible) |
| TTS | [Cartesia](https://cartesia.ai) `sonic-3`, Hindi/Hinglish voice |
| Transports | SmallWebRTC (local browser) · [Twilio](https://twilio.com) (telephony) |
| Deploy | [Pipecat Cloud](https://pipecat.daily.co) |
| Eval | [Cekura](https://cekura.com) |

---

## Repository layout

```
.
├── README.md
└── server/
    ├── bot-sharma.py          # Veil — main agent (pipeline, tools, call lifecycle)
    ├── response_guard.py      # runtime self-healing: sanitize + safety + end-call
    ├── text_spacing.py        # Hinglish token-stream spacing repair
    ├── call_classifier.py     # post-call scam-risk classification (Nemotron)
    ├── call_logging.py        # JSON call logs + family notifications
    ├── nemotron_llm.py        # Nemotron LLM service wrapper (TTFB metrics)
    ├── nvidia_stt.py          # NVIDIA streaming ASR client
    ├── bot-nemotron.py        # alt bot (Nemotron starter)
    ├── bot-gpt.py             # alt bot (GPT-4.1 / Gradium starter)
    ├── live_transcript.py     # dev helper: clean live transcript from server logs
    ├── test_*.py              # unit + scenario tests
    ├── .env.example           # required env vars (placeholders only)
    └── pcc-deploy.toml        # Pipecat Cloud deploy config
```

> The Veil agent is implemented in `server/bot-sharma.py` (the filename is a leftover from the persona's surname; the product is **Veil**).

---

## Run it locally

Talk to Veil over WebRTC in your browser before wiring up the phone.

### Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
- API keys for Deepgram (STT) and Cartesia (TTS); access to a Nemotron LLM endpoint

### Setup

```bash
git clone https://github.com/BHUVAN-RJ/VEIL---Self-Healing_Voice_Agent.git
cd VEIL---Self-Healing_Voice_Agent/server

cp .env.example .env
# Fill in the values in .env (see "Environment variables" below).

uv sync
```

### Start the agent

```bash
uv run bot-sharma.py
```

Open [http://localhost:7860](http://localhost:7860) and click **Connect** to start talking. First launch takes ~20s while Pipecat downloads VAD and turn-detection models.

### Watch a live transcript (optional)

While the bot runs, stream a clean, de-noised transcript of the conversation:

```bash
uv run live_transcript.py <path-to-the-bot's-terminal-log>
```

### Run the tests

```bash
uv run python test_text_spacing.py
uv run python test_response_guard.py
uv run python test_sharma_scenarios.py   # live scenario tests (needs LLM endpoint)
```

---

## Take phone calls (Twilio)

Expose port 7860 with an ngrok tunnel and run in Twilio mode:

```bash
uv run bot-sharma.py -t twilio -x your-subdomain.ngrok-free.dev --port 7860
```

Point a Twilio number's voice webhook at your tunnel (TwiML `<Connect><Stream>`), then call the number. For a managed deployment, push to **Pipecat Cloud** using `server/pcc-deploy.toml`:

```bash
pc cloud secrets set <secret-set> --file .env
pc cloud deploy
```

---

## Environment variables

Copy `server/.env.example` to `server/.env` and fill in real values. **Never commit `.env`** — it's gitignored.

| Variable | Purpose |
|---|---|
| `NEMOTRON_LLM_URL` | Nemotron LLM endpoint (OpenAI-compatible base URL) |
| `NEMOTRON_LLM_MODEL` | Model name, e.g. `nvidia/nemotron-3-super` |
| `NEMOTRON_LLM_API_KEY` | API key for the endpoint (`EMPTY` for open vLLM) |
| `DEEPGRAM_API_KEY` | Deepgram STT |
| `DEEPGRAM_MODEL` / `DEEPGRAM_LANGUAGE` | STT model / language (`nova-3-general` / `multi`) |
| `CARTESIA_API_KEY` | Cartesia TTS |
| `CARTESIA_VOICE_ID` / `CARTESIA_MODEL` | Voice + model (`sonic-3`) |
| `STT_PROVIDER` | `deepgram` (default) or `nvidia` |
| `NVIDIA_ASR_URL` | NVIDIA streaming ASR (only if `STT_PROVIDER=nvidia`) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio (phone transport + call metadata) |
| `TWILIO_PROXY` | Public ngrok hostname for Twilio media streams |
| `ENV` | `local` to disable the Krisp noise filter during local dev |

---

## Acknowledgements

Built on the [Pipecat](https://pipecat.ai) framework and the YC Voice Agents Hackathon starter, with NVIDIA Nemotron models, Deepgram, Cartesia, Twilio, and evaluated with [Cekura](https://cekura.com). Thanks to the on-site teams from Daily, NVIDIA, AWS, Twilio, and Cekura.
