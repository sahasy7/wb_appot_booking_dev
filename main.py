from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
import pytz
import requests
import os
from dotenv import load_dotenv
from azure.cosmos import CosmosClient
import dateparser

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

BOOKING_WINDOW_DAYS = 30

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

# ---------------- NATURAL LANGUAGE DATE PARSER ----------------
def parse_user_date(text):
    ist = pytz.timezone(CAL_TIME_ZONE)
    now = datetime.now(ist)

    parsed = dateparser.parse(
        text,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "TIMEZONE": CAL_TIME_ZONE,
            "RETURN_AS_TIMEZONE_AWARE": True
        }
    )

    if not parsed:
        return None

    parsed_date = parsed.astimezone(ist).date()

    if parsed_date < now.date():
        return None
    if parsed_date > now.date() + timedelta(days=BOOKING_WINDOW_DAYS):
        return None

    return parsed_date

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

# ---------------- AUTO-SUGGEST NEXT AVAILABLE DATE ----------------
def find_next_available_date(start_date):
    for i in range(1, BOOKING_WINDOW_DAYS + 1):
        next_date = start_date + timedelta(days=i)
        slots = get_slots_for_date(next_date)
        if slots:
            return next_date, slots
    return None, {}

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
    text = req.message.strip().lower()

    state = get_session(phone) or {
        "name": None,
        "email": None,
        "date": None,
        "slots": None,
        "selected_slot": None,
        "stage": "ASK_NAME"
    }

    # -------- STEP 1: NAME --------
    if state["stage"] == "ASK_NAME":
        state["name"] = req.message.strip()
        state["stage"] = "ASK_EMAIL"
        save_session(phone, state)
        return response("SEND_MESSAGE", "Thanks 😊 Please share your email ID.")

    # -------- STEP 2: EMAIL --------
    if state["stage"] == "ASK_EMAIL":
        state["email"] = req.message.strip()
        state["stage"] = "ASK_DATE"
        save_session(phone, state)

        return response(
            "SEND_MESSAGE",
            "📅 Which date would you prefer?\n\n"
            "You can say:\n"
            "• today / tomorrow\n"
            "• 23rd / 23 Feb\n"
            "• this Friday / next Monday"
        )

    # -------- STEP 3: DATE --------
    if state["stage"] == "ASK_DATE":
        selected_date = parse_user_date(req.message)

        if not selected_date:
            return response(
                "SEND_MESSAGE",
                "❌ I couldn’t understand the date. Please try something like *23rd*, *tomorrow*, or *next Monday*."
            )

        slots = get_slots_for_date(selected_date)

        if not slots:
            next_date, next_slots = find_next_available_date(selected_date)

            if not next_date:
                return response(
                    "SEND_MESSAGE",
                    "⚠️ No availability in the next 30 days. Please try later."
                )

            state["date"] = next_date.isoformat()
            state["slots"] = next_slots
            state["stage"] = "ASK_SLOT"
            save_session(phone, state)

            msg = (
                f"⚠️ No slots on *{selected_date.strftime('%d %b')}*.\n\n"
                f"✅ Next available date is *{next_date.strftime('%d %b')}*:\n\n"
            )
        else:
            state["date"] = selected_date.isoformat()
            state["slots"] = slots
            state["stage"] = "ASK_SLOT"
            save_session(phone, state)
            msg = "🕒 *Available slots (IST):*\n\n"

        for k, v in state["slots"].items():
            msg += f"{k}. {v['label']}\n"

        msg += "\nReply with *1, 2, or 3*."
        return response("SEND_MESSAGE", msg)

    # -------- STEP 4: SLOT --------
    if state["stage"] == "ASK_SLOT":
        if text not in state["slots"]:
            return response("SEND_MESSAGE", "❌ Invalid choice.")

        state["selected_slot"] = state["slots"][text]
        state["stage"] = "CONFIRM_BOOKING"
        save_session(phone, state)

        return response(
            "SEND_MESSAGE",
            f"""✅ *Please confirm your booking*

👤 {state['name']}
📧 {state['email']}
📅 {state['selected_slot']['label']} IST

Reply with *YES* to confirm or *NO* to change the date."""
        )

    # -------- STEP 5: CONFIRMATION --------
    if state["stage"] == "CONFIRM_BOOKING":
        if text == "no":
            state["stage"] = "ASK_DATE"
            save_session(phone, state)
            return response("SEND_MESSAGE", "Okay 👍 Please share a new preferred date.")

        if text != "yes":
            return response("SEND_MESSAGE", "Please reply with *YES* or *NO*.")

        booking = create_booking(
            state["name"],
            state["email"],
            state["selected_slot"]["iso"]
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
            f"""🎉 *Appointment Confirmed!*

👤 *Name:* {state['name']}
📧 *Email:* {state['email']}
📅 *Time:* {dt_ist.strftime('%d %b %Y, %I:%M %p IST')}

🔗 *Meeting Link:*
{meeting_url}
""",
            status="BOOKED"
        )

    return response("SEND_MESSAGE", "Something went wrong. Please start again.")