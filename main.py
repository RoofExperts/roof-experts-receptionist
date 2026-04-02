import os
import asyncio
import uvicorn
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, WebSocket, Request, Response
from fastapi.responses import PlainTextResponse, HTMLResponse, JSONResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

from agent import run_bot
from database import (
    init_db, log_call_start, update_call_end, update_call_recording,
    get_calls, get_call_by_sid, get_call_events, get_stats,
    get_blocked_numbers, block_number, unblock_number, is_number_blocked,
)

load_dotenv()
app = FastAPI()

DASHBOARD_KEY = os.getenv("DASHBOARD_KEY", "roofexperts2026")

twilio_client = TwilioClient(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN"),
)

# ââ Initialize DB on startup âââââââââââââââââââââââââââââââââââââââââââââââââ

@app.on_event("startup")
async def startup():
    init_db()


# === LAYER 1: Twilio-Level Spam Blocking ===
BLOCKED_NUMBERS = set()

SPAM_CALLER_NAMES = [
    "credit one bank", "fundwell", "caine weiner", "revvi card",
    "pch", "phalanx media", "qxo", "toll free call",
    "net pay advance", "lending club", "netcredit", "v shred",
    "brinks home", "adt", "sunrun", "core insurance",
]


def is_spam_caller(from_number: str, caller_name: str) -> bool:
    if from_number in BLOCKED_NUMBERS:
        return True
    if is_number_blocked(from_number):
        return True
    name_lower = (caller_name or "").lower().strip()
    return any(spam in name_lower for spam in SPAM_CALLER_NAMES)


# ââ Health Check âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.get("/health")
async def health():
    return {"status": "ok", "service": "roof-experts-receptionist"}


# ââ Incoming Call Handler ââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.post("/incoming-call")
async def incoming_call(request: Request):
    form_data = await request.form()
    caller_number = form_data.get("From", "Unknown")
    caller_name = form_data.get("CallerName", "")
    call_sid = form_data.get("CallSid", "")

    # Layer 1: Block known spam
    if is_spam_caller(caller_number, caller_name):
        response = VoiceResponse()
        response.reject(reason="busy")
        log_call_start(call_sid, caller_number, caller_name, is_spam=True)
        print(f"[spam-block] Rejected {caller_number} ({caller_name})")
        return PlainTextResponse(str(response), media_type="application/xml")

    # Log the call
    log_call_start(call_sid, caller_number, caller_name, is_spam=False)

    # Start Twilio recording in background (after TwiML is returned)
    base_url = os.getenv("BASE_URL", "").replace("https://", "").replace("http://", "")
    asyncio.ensure_future(_start_recording_async(call_sid, base_url))

    # Connect to Pipecat
    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=f"wss://{base_url}/ws")
    stream.parameter(name="callerNumber", value=caller_number)
    stream.parameter(name="callSid", value=call_sid)
    connect.append(stream)
    response.append(connect)
    return PlainTextResponse(str(response), media_type="application/xml")


async def _start_recording_async(call_sid: str, base_url: str):
    """Start recording the call via Twilio REST API with delay."""
    await asyncio.sleep(2.0)  # Wait for call to connect
    try:
        twilio_client.calls(call_sid).recordings.create(
            recording_status_callback=f"https://{base_url}/recording-callback",
            recording_channels="dual",
        )
        print(f"[recording] Started for {call_sid}")
    except Exception as e:
        print(f"[recording] Failed to start for {call_sid}: {e}")


# ââ Recording Callback âââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.post("/recording-callback")
async def recording_callback(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    recording_url = form_data.get("RecordingUrl", "")
    recording_duration = int(form_data.get("RecordingDuration", 0))
    status = form_data.get("RecordingStatus", "")

    if status == "completed" and recording_url:
        mp3_url = f"{recording_url}.mp3"
        update_call_recording(call_sid, mp3_url, recording_duration)
        print(f"[recording] Saved for {call_sid}: {mp3_url} ({recording_duration}s)")

    return PlainTextResponse("OK")


# ââ Call Status Callback âââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.post("/call-status")
async def call_status(request: Request):
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    call_status_val = form_data.get("CallStatus", "")
    duration = int(form_data.get("CallDuration", 0))

    if call_status_val in ("completed", "no-answer", "busy", "failed"):
        call = get_call_by_sid(call_sid)
        if call and call["status"] == "active":
            update_call_end(call_sid, status="completed", duration=duration)

    return PlainTextResponse("OK")


# ââ WebSocket (Pipecat) âââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    await run_bot(websocket)


# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# DASHBOARD
# ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _check_key(request: Request) -> bool:
    key = request.query_params.get("key", "")
    return key == DASHBOARD_KEY


@app.get("/dashboard")
async def dashboard(request: Request):
    if not _check_key(request):
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;display:flex;justify-content:center;"
            "align-items:center;height:100vh;background:#f5f5f5'>"
            "<div style='text-align:center'><h2>Roof Experts Dashboard</h2>"
            "<p>Access key required. Add <code>?key=YOUR_KEY</code> to the URL.</p>"
            "</div></body></html>",
            status_code=401,
        )
    html_path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    with open(html_path, "r") as f:
        html = f.read()
    html = html.replace("{{DASHBOARD_KEY}}", DASHBOARD_KEY)
    return HTMLResponse(html)


# ââ Dashboard API ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.get("/api/calls")
async def api_calls(request: Request, limit: int = 50, offset: int = 0,
                    date_from: str = None, date_to: str = None,
                    status: str = None):
    if not _check_key(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    return JSONResponse(get_calls(limit, offset, date_from, date_to, status))


@app.get("/api/calls/{call_sid}")
async def api_call_detail(call_sid: str, request: Request):
    if not _check_key(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    call = get_call_by_sid(call_sid)
    if not call:
        return JSONResponse({"error": "not found"}, 404)
    call["events"] = get_call_events(call["id"])
    return JSONResponse(call)


@app.get("/api/stats")
async def api_stats(request: Request, date_from: str = None,
                    date_to: str = None):
    if not _check_key(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    return JSONResponse(get_stats(date_from, date_to))


@app.get("/api/recording/{call_sid}")
async def api_recording(call_sid: str, request: Request):
    if not _check_key(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    call = get_call_by_sid(call_sid)
    if not call or not call.get("recording_url"):
        return JSONResponse({"error": "no recording"}, 404)
    return JSONResponse({"url": call["recording_url"]})


# ââ Block / Unblock API ââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.get("/api/blocked")
async def api_blocked(request: Request):
    if not _check_key(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    return JSONResponse(get_blocked_numbers())


@app.post("/api/block")
async def api_block(request: Request):
    if not _check_key(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    body = await request.json()
    phone = body.get("phone_number", "").strip()
    reason = body.get("reason", "")
    if not phone:
        return JSONResponse({"error": "phone_number required"}, 400)
    created = block_number(phone, reason)
    # Also add to in-memory set for instant blocking
    BLOCKED_NUMBERS.add(phone)
    return JSONResponse({"blocked": True, "new": created})


@app.post("/api/unblock")
async def api_unblock(request: Request):
    if not _check_key(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    body = await request.json()
    phone = body.get("phone_number", "").strip()
    if not phone:
        return JSONResponse({"error": "phone_number required"}, 400)
    removed = unblock_number(phone)
    BLOCKED_NUMBERS.discard(phone)
    return JSONResponse({"unblocked": True, "was_blocked": removed})


# ââ Main âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
