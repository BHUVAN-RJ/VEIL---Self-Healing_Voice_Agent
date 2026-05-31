#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Mrs. Sharma — elderly scam-screening voice agent for the YC Voice Agents Hackathon.

Screens incoming phone calls for Mrs. Sharma (68, Pune, Hinglish speaker). The agent
plays Mrs. Sharma when answering unknown numbers, engages callers in Hinglish, detects
scams, and logs classifications for family review.

Pipeline: Deepgram STT → Nemotron-3-Super-120B LLM → Cartesia TTS.

Post-call (always): classify_call_risk → send_summary_to_family (if medium/high) → JSON log.

Run locally::

    uv run bot-sharma.py

Twilio phone calls (ngrok tunnel to port 7860)::

    uv run bot-sharma.py -t twilio -x your-subdomain.ngrok-free.dev --port 7860
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timezone

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.runner.types import (
    RunnerArguments,
    SmallWebRTCRunnerArguments,
    WebSocketRunnerArguments,
)
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.turns.user_turn_strategies import FilterIncompleteUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from call_classifier import classify_call_risk
from call_logging import send_summary_to_family, write_call_log
from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService
from response_guard import GREETING, ResponseGuardProcessor, user_text_signals_scam

load_dotenv(override=True)

# Slightly longer pauses for elderly-paced conversation (emulated).
ELDERLY_VAD_PARAMS = VADParams(
    confidence=0.65,
    start_secs=0.25,
    stop_secs=0.55,
    min_volume=0.55,
)


INTERNAL_USER_PREFIXES = (
    "Unknown number se call aaya hai",
    "Customer ne abhi call kiya",
)


def build_system_instruction() -> str:
    """Build Mrs. Sharma's system prompt — brief, human, trust-first."""
    return (
        "You are Mrs. Sharma, a middle-aged Indian woman (40s-50s) in Pune, answering an unknown call. "
        "You speak natural Hinglish (Hindi + English mix), Latin script. "
        "You are NOT elderly — you are a normal urban Indian professional woman, warm and engaged on the phone.\n\n"
        "FORMAT (critical for speech):\n"
        "- Put a SPACE between every word.\n"
        "- ONE short sentence. Max 15 words. ONE question max.\n"
        "- Use 'dhanyavaad' not 'Thank you'. Natural fillers ok: 'Arre', 'Achha', 'Haan ji'.\n\n"
        "PERSONALITY — sound alive:\n"
        "- Show mild reaction to news: 'Arre, pachees hazaar?' or 'Achha, subah 11 baje?'.\n"
        "- Be curious about THEIR story, offering friendly follow-ups.\n"
        "- Warm but not performative — like a real person listening and maintaining a cautious curiosity.\n\n"
        "DEFAULT — curious and engaged:\n"
        "- They introduce themselves → 'Achha Priya ji, batao kya baat hai?'.\n"
        "- Bank alert / bad news → react mildly + ONE friendly follow-up (amount, time, safe or not).\n"
        "- They confirm details → 'Haan ji, sahi hai, wahi branch hai.'.\n\n"
        "CLOSING the call:\n"
        "- When the caller seems done → ask ONCE: 'Theek hai, aur kuch batana tha?'\n"
        "- If they say no → short goodbye only: 'Theek hai, dhanyavaad batane ke liye.'.\n\n"
        "NEVER in default mode:\n"
        "- Multiple questions in one reply.\n"
        "- Ask badge, employee ID, extension, reference, UPI ID, merchant name.\n"
        "- Say sceptical, phishing, suspicious, or that you don't trust them.\n"
        "- Mention son, Rohan, bete, WhatsApp verification UNLESS call clearly escalates into scam.\n"
        "- Ask them to prove themselves when they're just giving you information.\n"
        "- Excessive 'beta', 'bolo ji', or other elder-mannered fillers.\n\n"
        "ONLY go defensive when caller:\n"
        "- Demands Aadhaar, OTP, PIN, card number, or money NOW.\n"
        "- Claims CBI/police with arrest threat.\n"
        "- Sends links or asks to download apps.\n"
        "- Fake emergency with no hospital/doctor name.\n\n"
        "Then: ONE verification question OR polite refusal. Keep it short, while showing concern.\n\n"
        "SCAM DEFENSE examples:\n"
        "CBI scam: 'CBI se hoon, Aadhaar bataiye abhi.' → 'Pehle badge number bataiye, but kaisa kaam hai yeh?'\n"
        "OTP scam: 'OTP share karo verification ke liye.' → 'Bank kabhi OTP nahi maangta. Main nahi bataungi, kaisa verification hai yeh?'\n\n"
        "LEGITIMATE call examples:\n"
        "Doctor clinic: Engage normally, confirm appointment details with curiosity.\n"
        "Delivery: Help with directions, confirm order positively.\n"
        "Family friend: Chat normally, show warmth without suspicion.\n\n"
        f"Today: {date.today().strftime('%A, %B %d, %Y')}."
    )


def extract_transcript(messages: list[dict]) -> list[dict]:
    """Build a clean transcript from LLM context messages."""
    transcript: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role not in ("user", "assistant"):
            continue
        if not content or not isinstance(content, str):
            continue
        if role == "user" and any(content.startswith(p) for p in INTERNAL_USER_PREFIXES):
            continue

        transcript.append(
            {
                "role": role,
                "content": content.strip(),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
    return transcript


def count_caller_turns(transcript: list[dict]) -> int:
    return sum(1 for entry in transcript if entry.get("role") == "user")


async def get_call_info(call_sid: str) -> dict:
    """Fetch call information from Twilio REST API."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"

    try:
        auth = aiohttp.BasicAuth(account_sid, auth_token)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Twilio API error ({response.status}): {error_text}")
                    return {}
                data = await response.json()
                return {
                    "from_number": data.get("from"),
                    "to_number": data.get("to"),
                }
    except Exception as exc:
        logger.error(f"Error fetching call info from Twilio: {exc}")
        return {}


async def run_bot(
    transport: BaseTransport,
    *,
    call_id: str,
    from_number: str | None = None,
    to_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
    is_telephony: bool = False,
):
    """Main Mrs. Sharma bot logic."""
    logger.info(f"Starting Mrs. Sharma bot — call_id={call_id}")

    call_state = {
        "ended_by_agent": False,
        "post_call_done": False,
    }

    async def end_call(params: FunctionCallParams) -> None:
        """End the call after saying goodbye. Final classification runs after disconnect."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        call_state["ended_by_agent"] = True
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [end_call]
    tools = ToolsSchema(standard_tools=tool_functions)

    system_instruction = build_system_instruction()

    stt_provider = os.getenv("STT_PROVIDER", "deepgram").lower()
    if stt_provider == "nvidia":
        stt = NVidiaWebSocketSTTService(
            url=os.getenv("NVIDIA_ASR_URL", "ws://192.168.7.228:8081"),
            strip_interim_prefix=True,
        )
    else:
        stt = DeepgramSTTService(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            settings=DeepgramSTTService.Settings(
                model=os.getenv("DEEPGRAM_MODEL", "nova-3-general"),
                language=os.getenv("DEEPGRAM_LANGUAGE", "multi"),
                smart_format=True,
                punctuate=True,
            ),
        )

    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),
        base_url=os.getenv(
            "NEMOTRON_LLM_URL",
            "http://nemotron-fleet-alb-1322439314.us-west-2.elb.amazonaws.com/v1",
        ),
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            max_tokens=65,
            temperature=0.45,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        voice_id=os.getenv("CARTESIA_VOICE_ID", "95d51f79-c397-46f9-b49a-23763d3eaa2d"),
        model=os.getenv("CARTESIA_MODEL", "sonic-3"),
        sample_rate=audio_out_sample_rate if is_telephony else None,
        settings=CartesiaTTSService.Settings(language=Language.HI),
    )

    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)

    def _last_user_message() -> str:
        for msg in reversed(context.get_messages()):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str) and not any(
                    content.startswith(p) for p in INTERNAL_USER_PREFIXES
                ):
                    return content
        return ""

    def _defensive_mode() -> bool:
        for msg in context.get_messages():
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and user_text_signals_scam(content):
                return True
        return False

    def _scam_signal_count() -> int:
        count = 0
        for msg in context.get_messages():
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if isinstance(content, str) and user_text_signals_scam(content):
                count += 1
        return count

    def _mark_agent_ended() -> None:
        call_state["ended_by_agent"] = True

    response_guard = ResponseGuardProcessor(
        get_defensive_mode=_defensive_mode,
        get_last_user_message=_last_user_message,
        get_scam_signal_count=_scam_signal_count,
        on_request_end=_mark_agent_ended,
    )

    if is_telephony:
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)
    else:
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=ELDERLY_VAD_PARAMS),
                user_turn_strategies=FilterIncompleteUserTurnStrategies(),
            ),
        )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            response_guard,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=audio_in_sample_rate,
            audio_out_sample_rate=audio_out_sample_rate,
        ),
    )

    async def run_post_call_processing() -> None:
        """Always run after disconnect: classify, notify family if needed, write log."""
        if call_state["post_call_done"]:
            return
        call_state["post_call_done"] = True

        messages = context.get_messages()
        transcript = extract_transcript(messages)
        turn_count = count_caller_turns(transcript)

        if not transcript:
            logger.warning(f"No transcript for call {call_id} — skipping classification")
            return

        logger.info(f"Post-call processing for {call_id}: {turn_count} caller turns")

        classification = await classify_call_risk(transcript, turn_count)

        family_notification = None
        risk = classification.get("risk_level", "low")
        if risk in ("medium", "high"):
            family_notification = send_summary_to_family(
                transcript,
                classification,
                call_id=call_id,
                from_number=from_number,
                action_taken="summary_logged_for_rohan",
            )

        outcome = "ended_by_agent" if call_state["ended_by_agent"] else "ended_by_caller"
        write_call_log(
            call_id=call_id,
            transcript=transcript,
            classification=classification,
            turn_count=turn_count,
            from_number=from_number,
            to_number=to_number,
            outcome=outcome,
            family_notification=family_notification,
        )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        context.add_message({"role": "assistant", "content": GREETING})
        await worker.queue_frames([TTSSpeakFrame(GREETING)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await run_post_call_processing()
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""
    from_number: str | None = None
    to_number: str | None = None
    call_id = f"webrtc-{uuid.uuid4().hex[:12]}"
    transport_overrides: dict = {}

    if os.environ.get("ENV") != "local":
        from pipecat.audio.filters.krisp_viva_filter import KrispVivaFilter

        krisp_filter = KrispVivaFilter()
    else:
        krisp_filter = None

    match runner_args:
        case SmallWebRTCRunnerArguments():
            webrtc_connection: SmallWebRTCConnection = runner_args.webrtc_connection
            transport = SmallWebRTCTransport(
                webrtc_connection=webrtc_connection,
                params=TransportParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                ),
            )
        case WebSocketRunnerArguments():
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            _, call_data = await parse_telephony_websocket(runner_args.websocket)
            call_id = call_data["call_id"]

            call_info = await get_call_info(call_id)
            if call_info:
                from_number = call_info.get("from_number")
                to_number = call_info.get("to_number")
                logger.info(f"Call from: {from_number} to: {to_number}")

            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_id,
                account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
                auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
            )

            transport = FastAPIWebsocketTransport(
                websocket=runner_args.websocket,
                params=FastAPIWebsocketParams(
                    audio_in_enabled=True,
                    audio_in_filter=krisp_filter,
                    audio_out_enabled=True,
                    add_wav_header=False,
                    vad_analyzer=SileroVADAnalyzer(params=ELDERLY_VAD_PARAMS),
                    serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(
        transport,
        call_id=call_id,
        from_number=from_number,
        to_number=to_number,
        is_telephony=isinstance(runner_args, WebSocketRunnerArguments),
        **transport_overrides,
    )


if __name__ == "__main__":
    import text_ws_handler  # noqa: F401 — registers /ws-text on the shared app

    from pipecat.runner.run import main

    main()
