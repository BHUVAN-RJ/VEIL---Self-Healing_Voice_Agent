#
# Copyright (c) 2024–2026, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""Field & Flower — flower shop voice ordering bot (hackathon starter).

A customer calls in and the bot helps them pick a bouquet and arrange delivery.
All backend calls (catalog, customer lookup, order placement) are mocked so the
starter runs with no external dependencies beyond the AI services.

Pipeline: Deepgram STT → Nemotron-3-Super-120B LLM → Cartesia TTS, with direct
function tools registered on the LLM context.

Run the bot using::

    uv run bot-nemotron.py

For Twilio phone calls (with ngrok tunnel to port 7860)::

    uv run bot-nemotron.py -t twilio -x your-subdomain.ngrok-free.dev --port 7860
"""

import os
import random
from datetime import date

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndTaskFrame, FunctionCallResultProperties, LLMRunFrame
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

from mock_backend import BOUQUETS, KNOWN_CUSTOMERS
from nemotron_llm import VLLMOpenAILLMService
from nvidia_stt import NVidiaWebSocketSTTService

load_dotenv(override=True)


async def get_call_info(call_sid: str) -> dict:
    """Fetch call information from Twilio REST API using aiohttp.

    Args:
        call_sid: The Twilio call SID

    Returns:
        Dictionary containing call information including from_number, to_number, status, etc.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")

    if not account_sid or not auth_token:
        logger.warning("Missing Twilio credentials, cannot fetch call info")
        return {}

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"

    try:
        # Use HTTP Basic Auth with aiohttp
        auth = aiohttp.BasicAuth(account_sid, auth_token)

        async with aiohttp.ClientSession() as session:
            async with session.get(url, auth=auth) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Twilio API error ({response.status}): {error_text}")
                    return {}

                data = await response.json()

                call_info = {
                    "from_number": data.get("from"),
                    "to_number": data.get("to"),
                }

                return call_info

    except Exception as e:
        logger.error(f"Error fetching call info from Twilio: {e}")
        return {}


async def run_bot(
    transport: BaseTransport,
    from_number: str | None = None,
    audio_in_sample_rate: int = 16000,
    audio_out_sample_rate: int = 24000,
    is_telephony: bool = False,
):
    """Main bot logic.

    Args:
        transport: The transport to use.
        from_number: Caller's phone number (Twilio path only) for known-customer lookup.
        audio_in_sample_rate: Input audio sample rate in Hz. Defaults to 16000 (WebRTC).
        audio_out_sample_rate: Output audio sample rate in Hz. Defaults to 24000 (WebRTC).
        is_telephony: Twilio/phone path — simpler turn detection, 8 kHz TTS, no LLM turn gates.
    """
    logger.info("Starting bot")

    # Per-call order state. Closed over by the tool functions below so each
    # call gets its own isolated order.
    order: dict = {"items": [], "delivery": None}

    # --- Tools the LLM can call ---------------------------------------------

    async def list_bouquets(
        params: FunctionCallParams,
        occasion: str | None = None,
        specials_only: bool = False,
    ) -> None:
        """List bouquets available today. Optionally filter by occasion or by
        what's currently on special.

        Use this when the caller asks what's available, mentions a specific
        occasion ("it's for my mom's birthday", "for Valentine's Day", "for a
        funeral"), or asks about specials/deals. Sold-out bouquets are
        automatically excluded from results.

        Args:
            occasion: Lowercase occasion to filter by. Common values:
                "birthday", "anniversary", "valentine's day", "mother's day",
                "sympathy", "wedding", "graduation", "thank you", "get well",
                "new baby", "housewarming", "christmas", "easter", "just
                because". Pass the canonical short form ("birthday", not "mom's
                birthday"). Omit to return the full catalog.
            specials_only: If True, only return bouquets currently on special.
        """
        results = []
        for name, info in BOUQUETS.items():
            if not info["in_stock"]:
                continue
            if specials_only and not info.get("on_special", False):
                continue
            if occasion is not None:
                occ = occasion.strip().lower()
                tags = [o.lower() for o in info.get("occasions", [])]
                if not any(occ in tag or tag in occ for tag in tags):
                    continue
            results.append({"name": name, **info})

        if not results and (occasion is not None or specials_only):
            await params.result_callback(
                {
                    "bouquets": [],
                    "note": (
                        "No bouquets match those filters. Tell the caller you don't have "
                        "anything specifically for that, and offer to browse the full "
                        "catalog or try a different angle."
                    ),
                }
            )
            return

        await params.result_callback({"bouquets": results})

    async def check_availability(params: FunctionCallParams, bouquet_name: str) -> None:
        """Check whether a specific bouquet is in stock today.

        Args:
            bouquet_name: The name of the bouquet to check, lowercase.
        """
        item = BOUQUETS.get(bouquet_name.lower())
        if not item:
            await params.result_callback(
                {"available": False, "reason": f"We don't carry a bouquet called '{bouquet_name}'."}
            )
            return
        if not item["in_stock"]:
            await params.result_callback(
                {"available": False, "reason": f"{bouquet_name} is sold out today."}
            )
            return
        await params.result_callback({"available": True, "price": item["price"]})

    async def add_to_order(
        params: FunctionCallParams, bouquet_name: str, quantity: int = 1
    ) -> None:
        """Add a bouquet to the customer's order. Only call this after the
        customer has confirmed they want this bouquet.

        Args:
            bouquet_name: The name of the bouquet to add, lowercase.
            quantity: How many of this bouquet to add. Defaults to 1.
        """
        item = BOUQUETS.get(bouquet_name.lower())
        if not item:
            await params.result_callback(
                {"ok": False, "reason": f"We don't carry a bouquet called '{bouquet_name}'."}
            )
            return
        if not item["in_stock"]:
            await params.result_callback(
                {"ok": False, "reason": f"{bouquet_name} is sold out today."}
            )
            return
        order["items"].append(
            {"bouquet": bouquet_name.lower(), "quantity": quantity, "price": item["price"]}
        )
        await params.result_callback({"ok": True, "items": order["items"]})

    async def get_order_summary(params: FunctionCallParams) -> None:
        """Read back the current order: items, quantities, and running total."""
        total = sum(line["price"] * line["quantity"] for line in order["items"])
        await params.result_callback(
            {"items": order["items"], "total": round(total, 2), "delivery": order["delivery"]}
        )

    async def set_delivery_details(
        params: FunctionCallParams,
        recipient_name: str,
        address: str,
        delivery_date: str,
    ) -> None:
        """Capture delivery details for the order.

        Args:
            recipient_name: Name of the person receiving the flowers.
            address: Delivery street address.
            delivery_date: Requested delivery date, in the customer's own words
                (e.g. "Friday", "May 20th"). No parsing required.
        """
        order["delivery"] = {
            "recipient_name": recipient_name,
            "address": address,
            "delivery_date": delivery_date,
        }
        await params.result_callback({"ok": True, "delivery": order["delivery"]})

    async def place_order(params: FunctionCallParams) -> None:
        """Finalize the order. Only call this after the customer has confirmed
        the items AND delivery details."""
        if not order["items"]:
            await params.result_callback({"ok": False, "reason": "No items in the order yet."})
            return
        if not order["delivery"]:
            await params.result_callback({"ok": False, "reason": "Missing delivery details."})
            return
        total = sum(line["price"] * line["quantity"] for line in order["items"])
        confirmation = f"FLW-{random.randint(100000, 999999)}"
        logger.info(f"Order placed: {confirmation} total=${total:.2f} order={order}")
        await params.result_callback(
            {
                "ok": True,
                "confirmation_number": confirmation,
                "total": round(total, 2),
                "eta": "within 2 business days",
            }
        )

    async def end_call(params: FunctionCallParams) -> None:
        """End the call. Only call this AFTER you have said goodbye to the
        customer in the same turn. The pipeline will flush any queued speech
        and then hang up."""
        logger.info("end_call invoked — pushing EndTaskFrame upstream")
        await params.llm.push_frame(EndTaskFrame(), FrameDirection.UPSTREAM)
        # run_llm=False prevents the LLM from generating a follow-up response
        # after this function returns — the goodbye should already be in flight.
        await params.result_callback(
            {"ok": True}, properties=FunctionCallResultProperties(run_llm=False)
        )

    tool_functions = [
        list_bouquets,
        check_availability,
        add_to_order,
        get_order_summary,
        set_delivery_details,
        place_order,
        end_call,
    ]
    tools = ToolsSchema(standard_tools=tool_functions)

    # --- System instruction (varies based on caller ID) ---------------------

    customer = KNOWN_CUSTOMERS.get(from_number or "")
    if customer:
        caller_context = (
            f"Yeh returning customer hai (caller ID match hua). Record mein: "
            f"name {customer['name']}, last order {customer['last_order']} bouquet. "
            'Generic greet karo: "Field & Flower mein wapas swagat hai! Aaj kaise help kar sakta hoon?" '
            "Greeting mein naam ya last order mat bolo — surveillance jaisa lagta hai. "
            "Jab woh flowers chahein, last order shortcut offer kar sakte ho: "
            f'"Record mein {customer["last_order"]} tha last time — wahi chahiye ya kuch aur?" '
            "Hamesha alternative bhi do."
        )
    else:
        caller_context = (
            "Naya customer hai. Shop briefly introduce karo aur puchho kaise help kar sakte ho."
        )

    system_instruction = (
        "You are a friendly order-taker for Field & Flower, ek neighborhood flower shop. "
        "Callers ko bouquet choose karne aur delivery arrange karne mein help karo. Tools use karo: "
        "bouquets lookup, stock check, order add, delivery details, place_order. "
        "place_order se pehle poora order confirm karo.\n\n"
        "LANGUAGE — mandatory:\n"
        "- You are a helpful voice assistant for Indian users speaking Hinglish (Hindi + English mixed).\n"
        "- ALWAYS write responses in Romanized Hinglish using Latin script only — never use Devanagari.\n"
        "- Example: 'Mera naam Bhuvan hai, Karnataka se hoon. Sab theek hai, aap batao kya help chahiye?'\n"
        "- Match the user's mix of Hindi and English words.\n"
        "- Keep replies short (1-2 sentences) for phone/voice audio.\n"
        "- No emojis, bullets, or formatting that cannot be spoken.\n\n"
        "Talk like a real shop clerk on the phone — chatbot nahi:\n"
        "- Har turn mein 1–2 short sentences. Lambi sirf options list karte waqt ya final order read-back.\n"
        "- EK cheez ek baar mein pucho. Naam, address, date ek saath mat maango.\n"
        '- Filler mat use karo jaise "Absolutely!", "Perfect!", "I\'d be happy to" — seedha point pe aao.\n'
        "- Bouquets plainly describe karo. Example: 'Ek dozen red roses baby\'s breath ke saath, "
        'sadsath dollar." Flowery marketing language mat use karo.\n'
        "- Bouquets list karte waqt hamesha naam pehle. Format: '<Name> — <description>, <price>.' "
        'Example: "Spring Sunshine — yellow tulips aur daffodils, forty-five dollars."\n'
        "- Occasion (birthday, Mother's Day, anniversary, sympathy) ya specials/deals pe "
        'list_bouquets filters use karo (occasion="..." ya specials_only=True). Poora catalog mat padho.\n'
        "- Ek baar mein max 4–5 bouquets bolo. Agar interest na ho, aur options offer karo.\n"
        "- Customer ne jo bola woh repeat mat karo, sirf final confirmation mein.\n"
        "- Contractions aur natural fragments theek hain.\n\n"
        "Responses spoken aloud hain. Prices words mein bolo ('forty-five dollars', '$45.00' nahi).\n\n"
        "Order place ho jaye aur customer ko aur kuch na ho, ya goodbye bole: "
        'short closing bolo (e.g. "Dhanyavaad, accha din!") aur same turn mein end_call call karo. '
        "Goodbye ke bina end_call mat karo.\n\n"
        f"Aaj {date.today().strftime('%A, %B %d, %Y')} hai. Relative dates jaise 'is Friday' ya "
        "'agle Tuesday' ke liye yeh use karo.\n\n"
        f"Caller context: {caller_context}"
    )

    # Speech-to-Text service
    #
    # Default: Deepgram with language=multi for code-switched speech.
    # Set STT_PROVIDER=nvidia to use the hackathon Nemotron ASR WebSocket instead.
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

    # LLM service — Nemotron-3-Super-120B served by vLLM (OpenAI-compatible chat
    # completions at /v1). vLLM exposes the Chat Completions API, not the Responses
    # API, so we use OpenAILLMService (not OpenAIResponsesLLMService). The live
    # endpoint serves the model as "nemotron-3-super" (per its /v1/models).
    #
    # Reasoning ("thinking") toggle — Nemotron is controlled per-request via
    # chat_template_kwargs.enable_thinking, forwarded through the OpenAI client's
    # extra_body (the request-body convention confirmed against this endpoint in
    # ../aiewf-eval traces). Default OFF for low-latency voice. To ENABLE, set
    # NEMOTRON_ENABLE_THINKING=true; to DISABLE, leave unset/false.
    #
    # CAUTION for voice: reasoning is only kept out of the spoken `content` if the
    # vLLM server runs a reasoning parser (e.g. --reasoning-parser nemotron_v3, which
    # routes it to a separate `reasoning_content` field). This live endpoint did NOT
    # surface reasoning_content in testing, so if thinking is enabled and the server
    # lacks a parser, chain-of-thought would appear inline in `content` and get
    # spoken. Keep thinking OFF for voice unless the parser is confirmed active.
    # VLLMOpenAILLMService is a thin OpenAILLMService subclass that reports TTFB to
    # the first NON-THINKING token (so the metric reflects time-to-first-spoken-word
    # when reasoning is enabled, not time-to-first-reasoning-token). No-op when
    # thinking is off. See server/nemotron_llm.py.
    enable_thinking = os.getenv("NEMOTRON_ENABLE_THINKING", "false").lower() == "true"
    llm = VLLMOpenAILLMService(
        api_key=os.getenv("NEMOTRON_LLM_API_KEY", "EMPTY"),  # vLLM ignores unless --api-key set
        base_url=os.getenv("NEMOTRON_LLM_URL", "http://192.168.7.228:8000/v1"),
        settings=VLLMOpenAILLMService.Settings(
            model=os.getenv("NEMOTRON_LLM_MODEL", "nvidia/nemotron-3-super"),
            system_instruction=system_instruction,
            extra={"extra_body": {"chat_template_kwargs": {"enable_thinking": enable_thinking}}},
        ),
    )

    # Text-to-Speech service — Cartesia sonic-3, Arushi voice, Language.HI.
    # Spoken Hinglish = this voice + Romanized LLM text (no Devanagari).
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        voice_id=os.getenv("CARTESIA_VOICE_ID", "95d51f79-c397-46f9-b49a-23763d3eaa2d"),
        model=os.getenv("CARTESIA_MODEL", "sonic-3"),
        sample_rate=audio_out_sample_rate if is_telephony else None,
        settings=CartesiaTTSService.Settings(language=Language.HI),
    )

    # ToolsSchema describes the tools to the LLM; register_direct_function
    # wires the actual handlers the LLM will invoke. Both are required.
    for fn in tool_functions:
        llm.register_direct_function(fn)

    context = LLMContext(tools=tools)
    # Phone: match pipecat-quickstart-phone-bot — VAD on transport only, default
    # turn strategies (no FilterIncompleteUserTurnStrategies / ✓○◐ gate).
    # WebRTC: keep LLM turn-completion filtering for browser sessions.
    if is_telephony:
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(context)
    else:
        user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(),
                user_turn_strategies=FilterIncompleteUserTurnStrategies(),
            ),
        )

    # Pipeline - assembled from reusable components
    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
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

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        # Kick off the conversation
        context.add_message(
            {
                "role": "user",
                "content": (
                    "Customer ne abhi call kiya. Unhe greet karo: "
                    "'Field & Flower bol raha hoon, aapka local flower shop. "
                    "Aaj kaise help kar sakta hoon?'"
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)

    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point."""

    from_number: str | None = None
    transport_overrides: dict = {}

    # Krisp is available when deployed to Pipecat Cloud
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
            # Twilio media streams are 8 kHz μ-law in both directions.
            # This overrides the default sample rates: 16 kHz in / 24 kHz out.
            transport_overrides["audio_in_sample_rate"] = 8000
            transport_overrides["audio_out_sample_rate"] = 8000

            # Parse Twilio websocket and fetch call information
            _, call_data = await parse_telephony_websocket(runner_args.websocket)

            # Fetch call information from Twilio REST API so we can personalize
            # the bot for known customers (see KNOWN_CUSTOMERS).
            call_info = await get_call_info(call_data["call_id"])
            if call_info:
                from_number = call_info.get("from_number")
                logger.info(f"Call from: {from_number} to: {call_info.get('to_number')}")

            serializer = TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_data["call_id"],
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
                    vad_analyzer=SileroVADAnalyzer(),
                    serializer=serializer,
                ),
            )
        case _:
            logger.error(f"Unsupported runner arguments type: {type(runner_args)}")
            return

    await run_bot(
        transport,
        from_number=from_number,
        is_telephony=isinstance(runner_args, WebSocketRunnerArguments),
        **transport_overrides,
    )


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
