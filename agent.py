import os
import asyncio
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.frames.frames import EndFrame
from functions import get_function_definitions, handle_function_call
from config import SYSTEM_PROMPT, GEMINI_VOICE

# === LAYER 2: Silence Watchdog ===
# If no audio activity for this many seconds, hang up automatically.
# Catches robodialers and dead-air calls that get past Twilio screening.
SILENCE_TIMEOUT_SECS = 30


async def run_bot(websocket):
    transport_type, call_data = await parse_telephony_websocket(websocket)
    caller_number = call_data.get("body", {}).get("callerNumber", "Unknown")
    stream_sid = call_data.get("stream_id", "")
    call_sid = call_data.get("call_id", "")

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
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
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        voice_id=GEMINI_VOICE,
        system_instruction=SYSTEM_PROMPT,
        tools=get_function_definitions(),
    )

    context = OpenAILLMContext(
        messages=[
            {
                "role": "user",
                "content": f"[System: Inbound call starting. Caller phone number: {caller_number}. Begin the conversation now.]",
            }
        ]
    )
    context_aggregator = llm.create_context_aggregator(context)

    llm.register_function(None, handle_function_call)

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

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        await task.queue_frame(EndFrame())

    # Layer 2: Silence watchdog — auto-hangup after 30s of no activity
    async def silence_watchdog():
        await asyncio.sleep(SILENCE_TIMEOUT_SECS)
        print(f"[silence-watchdog] No activity for {SILENCE_TIMEOUT_SECS}s — ending call from {caller_number}")
        await task.queue_frame(EndFrame())

    watchdog = asyncio.create_task(silence_watchdog())

    try:
        runner = PipelineRunner()
        await runner.run(task)
    finally:
        watchdog.cancel()
