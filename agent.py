import os

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.transports.network.twilio_websocket import TwilioWebsocketTransport, TwilioParams
from pipecat.services.google import GeminiMultimodalLiveLLMService
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.frames.frames import EndFrame

from functions import get_function_definitions, handle_function_call
from config import SYSTEM_PROMPT, GEMINI_VOICE


async def run_bot(websocket, caller_number: str):
    """
    Main Pipecat pipeline.
    Twilio streams raw audio (8kHz mu-law) â this server â Gemini Live (native audio) â back to Twilio.
    Gemini handles STT + LLM + TTS natively â no separate speech services needed.
    """
    transport = TwilioWebsocketTransport(
        websocket=websocket,
        params=TwilioParams(
            audio_in_sample_rate=8000,   # Twilio sends 8kHz mu-law
            audio_out_sample_rate=8000,  # Twilio expects 8kHz mu-law back
            vad_analyzer=SileroVADAnalyzer(),
        ),
    )

    llm = GeminiMultimodalLiveLLMService(
        api_key=os.getenv("GOOGLE_API_KEY"),
        model="gemini-2.5-flash-native-audio-preview-12-2025",  # Latest stable Live model
        voice_id=GEMINI_VOICE,
        system_instruction=SYSTEM_PROMPT,
        tools=get_function_definitions(),
    )

    # Inject caller number into initial context so Gemini has it available
    context = OpenAILLMContext(
        messages=[{
            "role": "user",
            "content": f"[System: Inbound call starting. Caller phone number: {caller_number}. Begin the conversation now.]",
        }]
    )
    context_aggregator = llm.create_context_aggregator(context)

    # Wire all function calls to the single handler in functions.py
    llm.register_function(None, handle_function_call)

    pipeline = Pipeline([
        transport.input(),
        context_aggregator.user(),
        llm,
        transport.output(),
        context_aggregator.assistant(),
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(allow_interruptions=True),  # Enable barge-in
    )

    @transport.event_handler("on_client_disconnected")
    async def on_disconnect(transport, client):
        await task.queue_frame(EndFrame())

    runner = PipelineRunner()
    await runner.run(task)
