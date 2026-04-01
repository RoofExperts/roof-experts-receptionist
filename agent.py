import os

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.websocket.fastapi import FastAPIWebsocketTransport, FastAPIWebsocketParams
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContext,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.frames.frames import EndFrame, LLMRunFrame

from functions import get_function_definitions, handle_function_call
from config import SYSTEM_PROMPT, GEMINI_VOICE


async def run_bot(websocket):
    """
    Main Pipecat pipeline.
    Twilio streams raw audio (8kHz mu-law) -> this server -> Gemini Live (native audio) -> back to Twilio.
    Gemini handles STT + LLM + TTS natively - no separate speech services needed.
    """
    # Parse the Twilio WebSocket handshake (connected + start events)
    transport_type, call_data = await parse_telephony_websocket(websocket)

    # Extract caller info from Twilio custom parameters
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
        system_instruction=SYSTEM_PROMPT,
        tools=get_function_definitions(),
        settings=GeminiLiveLLMService.Settings(
            model="gemini-2.5-flash-native-audio-preview-09-2025",
            voice=GEMINI_VOICE,
        ),
    )

    # Create context with an initial message that tells Gemini to greet the caller.
    # This message is sent to Gemini once the LLMRunFrame triggers context delivery.
    context = LLMContext(
        messages=[{
            "role": "user",
            "content": (
                f"[System: Inbound call starting now. "
                f"Caller phone number: {caller_number}. "
                f"Greet the caller according to your instructions.]"
            ),
        }]
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(),
    )

    # Wire all function calls to the single handler in functions.py
    llm.register_function(None, handle_function_call)

    pipeline = Pipeline([
        transport.input(),
        user_aggregator,
        llm,
        transport.output(),
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            audio_out_sample_rate=8000,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # Trigger initial greeting: LLMRunFrame flows downstream to the
        # assistant_aggregator, which pushes the context frame UPSTREAM
        # to the LLM, causing Gemini to process the initial context and speak.
        await task.queue_frame(LLMRunFrame())

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    await runner.run(task)
