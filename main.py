import os
import uvicorn
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import PlainTextResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from dotenv import load_dotenv
from agent import run_bot

load_dotenv()
app = FastAPI()

# === LAYER 1: Twilio-Level Spam Blocking ===
# Calls from these numbers get an instant busy signal — never reach the AI.
BLOCKED_NUMBERS = set()  # Add specific numbers like "+18005551234"

# Known spam/sales caller names (from CNAM lookup).
# Twilio provides CallerName on inbound calls — if it matches, reject instantly.
SPAM_CALLER_NAMES = [
    "credit one bank", "fundwell", "caine weiner", "revvi card",
    "pch", "phalanx media", "qxo", "toll free call",
    "net pay advance", "lending club", "netcredit", "v shred",
    "brinks home", "adt", "sunrun", "core insurance",
]


def is_spam_caller(from_number: str, caller_name: str) -> bool:
    """Check if caller should be blocked before reaching the AI."""
    if from_number in BLOCKED_NUMBERS:
        return True
    name_lower = (caller_name or "").lower().strip()
    return any(spam in name_lower for spam in SPAM_CALLER_NAMES)


@app.get("/health")
async def health():
    """Simple health check endpoint for Render."""
    return {"status": "ok", "service": "roof-experts-receptionist"}


@app.post("/incoming-call")
async def incoming_call(request: Request):
    """Handle incoming Twilio calls — screen for spam, then connect to AI."""
    form_data = await request.form()
    caller_number = form_data.get("From", "Unknown")
    caller_name = form_data.get("CallerName", "")

    # Layer 1: Block known spam callers at the Twilio level
    if is_spam_caller(caller_number, caller_name):
        response = VoiceResponse()
        response.reject(reason="busy")
        print(f"[spam-block] Rejected call from {caller_number} ({caller_name})")
        return PlainTextResponse(str(response), media_type="application/xml")

    # Legitimate call — connect to Pipecat via WebSocket
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
    await websocket.accept()
    await run_bot(websocket)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
