from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
import pytz
import requests
import json
import os
from dotenv import load_dotenv
from azure.cosmos import CosmosClient

load_dotenv()
app = FastAPI()

# ---------------- CONFIG ----------------
CAL_API_KEY = os.getenv("CAL_API_KEY")
CAL_BASE_URL = "https://api.cal.com/v2"
CAL_BOOKING_URL = f"{CAL_BASE_URL}/bookings"
CAL_SLOTS_URL = f"{CAL_BASE_URL}/slots"

EVENT_TYPE_ID = 3232034
CAL_TIME_ZONE = "Asia/Kolkata"

CAL_HEADERS = {
    "Content-Type": "application/json",
    "cal-api-version": "2024-08-13"
}

# ---------------- COSMOS DB ----------------
COSMOS_URI = os.getenv("COSMOS_URI")
COSMOS_KEY = os.getenv("COSMOS_KEY")
COSMOS_DATABASE = os.getenv("COSMOS_DATABASE")
COSMOS_SESSION_CONTAINER = os.getenv("COSMOS_SESSION_CONTAINER")

cosmos_client = CosmosClient(COSMOS_URI, COSMOS_KEY)
database = cosmos_client.get_database_client(COSMOS_DATABASE)
session_container = database.get_container_client(COSMOS_SESSION_CONTAINER)

SESSION_TTL = 1800  # 30 minutes

# ---------------- REQUEST MODEL ----------------
class SignalRequest(BaseModel):
    phone: str
    message: str

# ---------------- RESPONSE FORMAT ----------------
def response(action, message, status="PENDING", meta=None):
    return {
        "action": action,
        "message": message,
        "status": status,
        "meta": meta or {}
    }

# ---------------- COSMOS SESSION HELPERS ----------------
def get_session(phone):
    try:
        doc = session_container.read_item(
            item=f"session:{phone}",
            partition_key=phone
        )
        return doc["state"]
    except:
        return None

def save_session(phone, state):
    session_container.upsert_item({
        "id": f"session:{phone}",
        "phone": phone,
        "type": "appointment_session",
        "state": state,
        "updated_at": datetime.utcnow().isoformat(),
        "ttl": SESSION_TTL
    })

def delete_session(phone):
    try:
        session_container.delete_item(
            item=f"session:{phone}",
            partition_key=phone
        )
    except:
        pass

# ---------------- FETCH REAL CAL SLOTS (MULTI-DAY FALLBACK) ----------------
def get_available_slots_from_cal():
    collected_slots = {}
    required_slots = 3
    days_ahead_limit = 7

    utc = pytz.utc
    ist = pytz.timezone(CAL_TIME_ZONE)
    current_date = datetime.now().date()

    for day_offset in range(days_ahead_limit):
        if len(collected_slots) >= required_slots:
            break

        target_date = current_date + timedelta(days=day_offset)
        date_str = target_date.isoformat()

        res = requests.get(
            CAL_SLOTS_URL,
            headers={
                "Authorization": f"Bearer {CAL_API_KEY}",
                "cal-api-version": "2024-09-04"
            },
            params={
                "eventTypeId": EVENT_TYPE_ID,
                "start": date_str,
                "end": date_str,
                "timeZone": CAL_TIME_ZONE,
                "format": "time"
            }
        )

        if res.status_code != 200:
            continue

        data = res.json().get("data", {})
        day_slots = data.get(date_str, [])

        for slot in day_slots:
            if len(collected_slots) >= required_slots:
                break

            iso_start = slot["start"]
            dt_utc = datetime.fromisoformat(iso_start.replace("Z", "+00:00"))
            dt_utc = dt_utc.replace(tzinfo=utc)
            dt_ist = dt_utc.astimezone(ist)

            collected_slots[str(len(collected_slots) + 1)] = {
                "iso": iso_start,
                "label": dt_ist.strftime("%d %b, %I:%M %p")
            }

    return collected_slots

# ---------------- CAL BOOKING ----------------
def create_booking(name, email, start_iso):
    payload = {
        "start": start_iso,
        "eventTypeId": EVENT_TYPE_ID,
        "metadata": {},
        "attendee": {
            "name": name,
            "email": email,
            "timeZone": CAL_TIME_ZONE
        }
    }

    res = requests.post(
        CAL_BOOKING_URL,
        headers=CAL_HEADERS,
        params={"apiKey": CAL_API_KEY},
        json=payload
    )

    if res.status_code not in (200, 201):
        return None

    return res.json()

# ---------------- SIGNAL API ----------------
@app.post("/signal")
def signal_handler(req: SignalRequest):
    phone = req.phone
    text = req.message.strip()

    state = get_session(phone) or {
        "name": None,
        "email": None,
        "slots": None,
        "stage": "ASK_NAME"
    }

    # -------- STEP 1: NAME --------
    if state["stage"] == "ASK_NAME":
        state["name"] = text
        state["stage"] = "ASK_EMAIL"
        save_session(phone, state)
        return response("SEND_MESSAGE", "Thanks 😊 Please share your email ID.")

    # -------- STEP 2: EMAIL --------
    if state["stage"] == "ASK_EMAIL":
        state["email"] = text

        slot_map = get_available_slots_from_cal()
        if not slot_map:
            return response("SEND_MESSAGE", "⚠️ No slots available right now.")

        state["slots"] = slot_map
        state["stage"] = "ASK_SLOT"
        save_session(phone, state)

        msg = "🕒 *Available slots:*\n\n"
        for k, v in slot_map.items():
            msg += f"{k}. {v['label']}\n"

        msg += "\nReply with *1, 2, or 3*."
        return response("SEND_MESSAGE", msg)

    # -------- STEP 3: SLOT --------
    if state["stage"] == "ASK_SLOT":
        if text not in state["slots"]:
            return response("SEND_MESSAGE", "❌ Invalid choice.")

        booking = create_booking(
            state["name"],
            state["email"],
            state["slots"][text]["iso"]
        )

        if not booking:
            return response("SEND_MESSAGE", "⚠️ Booking failed.")

        data = booking["data"]
        meeting_url = data.get("meetingUrl") or data.get("location")
        start_utc = data.get("start")

        utc = pytz.utc
        ist = pytz.timezone(CAL_TIME_ZONE)
        dt_ist = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).replace(
            tzinfo=utc
        ).astimezone(ist)

        delete_session(phone)

        return response(
            "SEND_MESSAGE",
            f"""✅ *Appointment Confirmed!*

👤 *Name:* {state['name']}
📧 *Email:* {state['email']}
📅 *Time:* {dt_ist.strftime('%d %b %Y, %I:%M %p IST')}

🔗 *Meeting Link:*
{meeting_url}
""",
            status="BOOKED"
        )

    return response("SEND_MESSAGE", "Something went wrong. Please start again.")
