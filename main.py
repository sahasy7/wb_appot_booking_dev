from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
import pytz
import requests
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

SESSION_TTL = 1800  # 30 mins

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

# ---------------- SESSION HELPERS ----------------
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

# ---------------- DATE PARSER ----------------
def parse_user_date(text):
    ist = pytz.timezone(CAL_TIME_ZONE)
    today = datetime.now(ist).date()
    text = text.lower().strip()

    if text in ["today"]:
        return today
    if text in ["tomorrow"]:
        return today + timedelta(days=1)

    try:
        parsed = datetime.fromisoformat(text).date()
        return parsed
    except:
        pass

    return None

# ---------------- FETCH SLOTS FOR A DATE ----------------
def get_slots_for_date(target_date):
    ist = pytz.timezone(CAL_TIME_ZONE)
    utc = pytz.utc

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
        return {}

    slots = res.json().get("data", {}).get(date_str, [])
    slot_map = {}

    for slot in slots[:3]:
        iso_utc = slot["start"]
        dt_ist = datetime.fromisoformat(
            iso_utc.replace("Z", "+00:00")
        ).astimezone(utc).astimezone(ist)

        slot_map[str(len(slot_map) + 1)] = {
            "iso": iso_utc,
            "label": dt_ist.strftime("%d %b, %I:%M %p")
        }

    return slot_map

# ---------------- CAL BOOKING ----------------
def create_booking(name, email, start_iso_utc):
    payload = {
        "start": start_iso_utc,
        "eventTypeId": EVENT_TYPE_ID,
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
        "date": None,
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
        state["stage"] = "ASK_DATE"
        save_session(phone, state)

        return response(
            "SEND_MESSAGE",
            "📅 Which date would you prefer?\n\nYou can reply with:\n• *today*\n• *tomorrow*\n• *YYYY-MM-DD*"
        )

    # -------- STEP 3: DATE --------
    if state["stage"] == "ASK_DATE":
        selected_date = parse_user_date(text)

        if not selected_date:
            return response("SEND_MESSAGE", "❌ Please enter a valid date.")

        slots = get_slots_for_date(selected_date)

        if not slots:
            return response(
                "SEND_MESSAGE",
                "⚠️ No slots available on that date. Please try another date."
            )

        state["date"] = selected_date.isoformat()
        state["slots"] = slots
        state["stage"] = "ASK_SLOT"
        save_session(phone, state)

        msg = "🕒 *Available slots (IST):*\n\n"
        for k, v in slots.items():
            msg += f"{k}. {v['label']}\n"
        msg += "\nReply with *1, 2, or 3*."

        return response("SEND_MESSAGE", msg)

    # -------- STEP 4: SLOT --------
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

        ist = pytz.timezone(CAL_TIME_ZONE)
        dt_ist = datetime.fromisoformat(
            start_utc.replace("Z", "+00:00")
        ).astimezone(pytz.utc).astimezone(ist)

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