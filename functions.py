import os
import json
from datetime import datetime

import pytz
from twilio.rest import Client as TwilioClient

from zoho_client import ZohoClient
from config import (
    TX_MARKETS, get_next_tx_estimator,
    NON_TX_NUMBER, EMERGENCY_NUMBER, BUSINESS_HOURS,
)

zoho = ZohoClient()
twilio_client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))


# ââ Tool Definitions (passed to Gemini Live) ââââââââââââââââââââââââââââââââââ

def get_function_definitions() -> list:
    return [
        {
            "name": "capture_lead",
            "description": (
                "Save caller contact information and job details to Zoho CRM. "
                "Call this as soon as you have the caller's name, phone number, and market/city. "
                "Call it again if you later collect email or more details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caller_name":      {"type": "string", "description": "Caller's full name"},
                    "caller_phone":     {"type": "string", "description": "Caller's phone number"},
                    "caller_email":     {"type": "string", "description": "Caller's email address (if provided)"},
                    "property_address": {"type": "string", "description": "Property address or city/zip"},
                    "market":           {"type": "string", "description": "City or state where the property is located"},
                    "job_type": {
                        "type": "string",
                        "enum": ["repair", "replacement", "inspection", "emergency", "commercial", "new_construction", "unknown"],
                    },
                    "property_type": {
                        "type": "string",
                        "enum": ["residential", "commercial", "unknown"],
                    },
                    "notes": {"type": "string", "description": "Any additional details about what the caller needs"},
                },
                "required": ["caller_name", "caller_phone", "market"],
            },
        },
        {
            "name": "transfer_to_estimator",
            "description": (
                "Transfer the live call to the right estimator based on market. "
                "Only use during business hours (MonâFri 7amâ6pm CST) when the caller "
                "wants to speak with someone now. Call check_business_hours first if unsure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "market":   {"type": "string", "description": "The caller's city or state â used to pick TX vs non-TX routing"},
                    "lead_id":  {"type": "string", "description": "Zoho CRM lead ID if already captured"},
                },
                "required": ["market"],
            },
        },
        {
            "name": "escalate_emergency",
            "description": (
                "Immediately page on-call staff for emergencies: active leak, water coming in, "
                "storm damage, hail damage, tree on roof, structural damage, or any urgent situation. "
                "Use this BEFORE collecting full contact info if the situation is critical."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "caller_phone": {"type": "string", "description": "Caller's phone number"},
                    "caller_name":  {"type": "string", "description": "Caller's name if known"},
                    "situation":    {"type": "string", "description": "Brief description of the emergency"},
                    "address":      {"type": "string", "description": "Property address if provided"},
                },
            },
        },
        {
            "name": "check_business_hours",
            "description": (
                "Check whether it is currently within Roof Experts business hours "
                "(MonâFri 7amâ6pm CST). Use this to decide whether to transfer live "
                "or take a message."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "search_knowledge_base",
            "description": (
                "Search the Roof Experts knowledge base for detailed answers about "
                "roofing materials, processes, warranties, financing, insurance claims, "
                "or company policies. Use this when a caller asks something specific "
                "that is NOT already covered in your basic instructions â for example: "
                "'How long does a TPO roof last?', 'Do you offer financing?', "
                "'What does the GAF warranty cover?', 'How does the insurance claim process work?'. "
                "Do NOT use this for questions you can already answer: service areas, "
                "basic warranty tiers (1/5/2 year), general pricing policy, or GAF certification."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "The caller's question rephrased as 2â5 search keywords. "
                            "Examples: 'TPO roof lifespan', 'hail damage insurance claim process', "
                            "'GAF warranty coverage details', 'financing options payment plans', "
                            "'EPDM vs TPO commercial comparison'."
                        ),
                    }
                },
                "required": ["query"],
            },
        },
    ]


# ââ Function Call Router ââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def handle_function_call(function_name: str, arguments: dict) -> str:
    """Routes Gemini function calls to the right handler. Always returns a JSON string."""
    try:
        if function_name == "capture_lead":
            return await _capture_lead(arguments)
        elif function_name == "transfer_to_estimator":
            return await _transfer_to_estimator(arguments)
        elif function_name == "escalate_emergency":
            return await _escalate_emergency(arguments)
        elif function_name == "check_business_hours":
            return _check_business_hours()
        elif function_name == "search_knowledge_base":
            return await _search_knowledge_base(arguments)
        else:
            return json.dumps({"error": f"Unknown function: {function_name}"})
    except Exception as e:
        print(f"[functions] handle_function_call error in {function_name}: {e}")
        return json.dumps({"error": str(e)})


# ââ Handlers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

async def _capture_lead(args: dict) -> str:
    market = args.get("market", "").lower()
    is_tx = any(m in market for m in TX_MARKETS)

    if is_tx:
        _, estimator_name = get_next_tx_estimator()
    else:
        estimator_name = "Mike Nasiadka"

    lead_data = {
        "Last_Name":    args.get("caller_name", "Unknown"),
        "Phone":        args.get("caller_phone", ""),
        "Email":        args.get("caller_email", ""),
        "Lead_Source":  "AI Receptionist",
        "Description":  args.get("notes", ""),
        "City":         args.get("market", ""),
        "Street":       args.get("property_address", ""),
        "Lead_Status":  "New",
    }

    result = zoho.create_lead(lead_data)
    lead_id = result.get("id", "")

    return json.dumps({
        "success": True,
        "lead_id": lead_id,
        "assigned_to": estimator_name,
        "message": f"Lead created and assigned to {estimator_name}.",
    })


async def _transfer_to_estimator(args: dict) -> str:
    market = args.get("market", "").lower()
    is_tx = any(m in market for m in TX_MARKETS)

    if is_tx:
        transfer_number, estimator_name = get_next_tx_estimator()
    else:
        transfer_number = NON_TX_NUMBER
        estimator_name = "Mike"

    # Returning the number tells Gemini who to say it's transferring to.
    # The actual Twilio transfer is handled in main.py when Gemini signals transfer intent.
    return json.dumps({
        "action": "transfer",
        "transfer_to": transfer_number,
        "estimator_name": estimator_name,
        "message": f"Transferring to {estimator_name} now.",
    })


async def _escalate_emergency(args: dict) -> str:
    caller_phone = args.get("caller_phone", "Unknown")
    caller_name  = args.get("caller_name", "Unknown caller")
    situation    = args.get("situation", "Emergency reported")
    address      = args.get("address", "Address not provided")

    # Create urgent CRM lead
    zoho.create_lead({
        "Last_Name":   caller_name,
        "Phone":       caller_phone,
        "Lead_Source": "AI Receptionist - EMERGENCY",
        "Description": f"EMERGENCY: {situation} | Address: {address}",
        "Lead_Status": "Emergency",
    })

    # Send SMS to on-call number
    try:
        twilio_client.messages.create(
            body=(
                f"ð¨ ROOF EXPERTS EMERGENCY\n"
                f"Caller: {caller_name} | {caller_phone}\n"
                f"Situation: {situation}\n"
                f"Address: {address}"
            ),
            from_=os.getenv("TWILIO_PHONE_NUMBER"),
            to=EMERGENCY_NUMBER,
        )
    except Exception as e:
        print(f"[functions] Emergency SMS failed: {e}")

    return json.dumps({
        "success": True,
        "action": "emergency_escalated",
        "message": "On-call staff has been notified immediately.",
    })


def _check_business_hours() -> str:
    cst = pytz.timezone("America/Chicago")
    now = datetime.now(cst)
    is_open = (
        now.weekday() in BUSINESS_HOURS["days"]
        and BUSINESS_HOURS["start"] <= now.hour < BUSINESS_HOURS["end"]
    )
    return json.dumps({
        "is_business_hours": is_open,
        "current_time_cst": now.strftime("%I:%M %p CST"),
        "day": now.strftime("%A"),
        "message": "We are currently open." if is_open else "We are currently closed.",
    })


async def _search_knowledge_base(args: dict) -> str:
    """
    Searches Zoho Desk published KB articles and returns a voice-friendly answer.
    Answer is capped at 50 words â long answers sound terrible on phone calls.
    """
    query = args.get("query", "").strip()
    if not query:
        return json.dumps({
            "found": False,
            "answer": "I don't have specific details on that right now, but one of our estimators can answer when I connect you.",
        })

    articles = zoho.search_kb_articles(query, limit=3)

    if not articles:
        return json.dumps({
            "found": False,
            "answer": "I don't have a specific article on that right now. Our estimators will have the exact details for you.",
        })

    top = articles[0]
    title   = top.get("title", "")
    summary = top.get("summary", "").strip()

    # Hard cap at 50 words â voice AI should be concise
    words = summary.split()
    if len(words) > 50:
        summary = " ".join(words[:50]) + "..."

    if not summary:
        summary = f"We do have information on that. Let me connect you with an estimator who can walk you through the details."

    return json.dumps({
        "found": True,
        "article_title": title,
        "answer": summary,
    })
