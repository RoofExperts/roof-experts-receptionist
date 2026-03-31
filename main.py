import os

import uvicorn
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv

from agent import run_bot

load_dotenv()

app = FastAPI()


@app.get("/health")
async def health():
    """Simple health check endpoint for Render."""
    return {"status": "ok", "service": "roof-experts-receptionist"}


@app.post("/incoming-call")
async def incoming_call(request: Request):
    """
    Twilio calls this webhook when a call comes in.
    We respond with TwiML that tells Twilio to stream audio to our WebSocket.
    """
    form_data = await request.form()
    caller_number = form_data.get("From", "Unknown")

    base_url = os.getenv("BASE_URL", "").replace("https://", "").replace("http://", "")

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"wss://{base_url}/ws")
    stream.parameter(name="callerNumber", value=caller_number)
    connect.append(stream)
    response.append(connect)

    return PlainTextResponse(str(response), media_type="application/xml")


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Twilio streams call audio here over WebSocket.
    Pipecat's parse_telephony_websocket handles the Twilio handshake,
    then we pipe audio to Gemini Live and stream responses back.
    """
    await websocket.accept()
    await run_bot(websocket)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

