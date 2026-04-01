import os
from dotenv import load_dotenv

load_dotenv()

# ── Routing Rules ─────────────────────────────────────────────────────────────
TX_MARKETS = [
    "garland", "dallas", "fort worth", "dfw", "austin", "san antonio",
    "houston", "frisco", "plano", "mckinney", "allen", "rockwall",
    "greenville", "texas", "tx"
]

TX_ROUND_ROBIN = [
    os.getenv("RC_TIMOTHY_NUMBER"),  # Slot 0 — Timothy
    os.getenv("RC_JOSH_NUMBER"),     # Slot 1 — Josh
    os.getenv("RC_JIM_NUMBER"),      # Slot 2 — Jim
]

NON_TX_NUMBER = os.getenv("RC_MIKE_NUMBER")
EMERGENCY_NUMBER = os.getenv("RC_EMERGENCY_NUMBER")

BUSINESS_HOURS = {
    "start": 8,          # 8 AM CST
    "end": 17,           # 5 PM CST
    "days": [0, 1, 2, 3, 4]  # Monday–Friday (0=Mon)
}

# ── Round Robin State ─────────────────────────────────────────────────────────
# In-memory — resets on server restart. Acceptable for current call volume.
_rr_index = {"tx": 0}

def get_next_tx_estimator():
    """Returns (phone_number, name) for the next TX estimator in round-robin."""
    names = ["Timothy", "Josh", "Jim"]
    idx = _rr_index["tx"] % len(TX_ROUND_ROBIN)
    number = TX_ROUND_ROBIN[idx]
    name = names[idx]
    _rr_index["tx"] += 1
    return number, name

# ── Gemini Voice ──────────────────────────────────────────────────────────────
# Aoede = warm female | Puck = energetic male | Charon = calm male
# Kore = professional female | Fenrir = confident male | Leda = bright female
GEMINI_VOICE = "Sulafat"

# ── System Prompt ─────────────────────────────────────────────────────────────
import pathlib

_prompt_path = pathlib.Path(__file__).parent / "prompts" / "system_prompt.txt"
with open(_prompt_path, "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()
