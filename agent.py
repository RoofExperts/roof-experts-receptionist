import os
import asyncio
import time

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.processors.aggregators.llm_response_universal import LLMContext, LLMContextAggregatorPair
from pipecat.frames.frames import EndFrame

from functions import get_function_definitions, handle_function_call
from config import SYSTEM_PROMPT, GEMINI_VOICE
from database import (
    get_call_by_sid, update_call_end, update_call_lead, log_event,
)

# === LAYER 2: Silence Watchdog ===
SILENCE_TIMEOUT_SECS = 30


async def run_bot(websocket):
    transport_type, call_data = await parse_telephony_websocket(websocket)
    caller_number = call_data.get("body", {}).get("callerNumber", "Unknown")
    call_sid = call_data.get("body", {}).get("callSid", "")
    stream_sid = call_data.get("stream_id", "")
    raw_call_sid = call_data.get("call_id", "") or call_sid

    start_time = time.time()
    call_outcome = "info_only"  # default â overridden by function calls
    call_db = get_call_by_sid(raw_call_sid)
    call_id = call_db["id"] if call_db else None

    if call_id:
        log_event(call_id, "call_connected", {"caller": caller_number})

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=raw_call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        auth_token=os.getenv("TWILIO_AUTH_TOKEN", ""),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            vad_enabled=False,
            serializer=serializer,
        ),
    )

    llm = GeminiLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        voice_id=GEMINI_VOICE,
        system_instruction=SYSTEM_PROMPT,
        tools=get_function_definitions(),
        settings=GeminiLiveLLMService.Settings(
            model="gemini-2.5-flash-native-audio-preview-12-2025",
        ),
    )

    context = LLMContext(
        messages=[
            {
                "role": "user",
                "content": f"[System: Inbound call starting. Caller phone number: {caller_number}. Begin the conversation now.]",
            }
        ]
    )
    context_aggregator = LLMContextAggregatorPair(context)

    # Wrap handle_function_call to log events
    async def _logged_function_call(function_name: str, arguments: dict) -> str:
        nonlocal call_outcome

        result = await handle_function_call(function_name, arguments)

        # Log the event and track outcome
        if call_id:
            log_event(call_id, function_name, arguments)

            if function_name == "capture_lead":
                import json
                data = json.loads(result)
                if data.get("success"):
                    update_call_lead(
                        raw_call_sid,
                        data.get("lead_id", ""),
                        data.get("assigned_to", ""),
                    )
                    call_outcome = "lead_captured"

            elif function_name == "transfer_to_estimator":
                call_outcome = "transferred"

            elif function_name == "escalate_emergency":
                call_outcome = "emergency"

        return result

    llm.register_function(None, _logged_function_call)

    pipeline = Pipeline(
        [
            transport.input(),
            context_aggregator.user(),
            llm,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=True))

    silence_ended = False

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        await task.queue_frame(EndFrame())

    # Layer 2: Silence watchdog
    async def silence_watchdog():
        nonlocal silence_ended
        await asyncio.sleep(SILENCE_TIMEOUT_SECS)
        silence_ended = True
        print(f"[silence-watchdog] No activity for {SILENCE_TIMEOUT_SECS}s â ending call from {caller_number}")
        if call_id:
            log_event(call_id, "silence_timeout", {"seconds": SILENCE_TIMEOUT_SECS})
        await task.queue_frame(EndFrame())

    watchdog = asyncio.create_task(silence_watchdog())

    try:
        runner = PipelineRunner()
        await runner.run(task)
    finally:
        watchdog.cancel()
        duration = int(time.time() - start_time)

        # Log call end
        if raw_call_sid:
            final_status = "silence_timeout" if silence_ended else "completed"
            update_call_end(raw_call_sid, status=final_status,
                            outcome=call_outcome, duration=duration)

        if call_id:
            log_event(call_id, "call_ended", {
                "duration": duration,
                "outcome": call_outcome,
                "status": "silence_timeout" if silence_ended else "completed",
            })

        print(f"[agent] Call ended: {caller_number} | {duration}s | {call_outcome}")
