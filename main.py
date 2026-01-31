from fastapi import FastAPI
from pydantic import BaseModel
from datetime import datetime, timedelta
import pytz
import requests
import json
import os
from dotenv import load_dotenv

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

print("🔑 CAL_API_KEY loaded:", bool(CAL_API_KEY))
print("📅 EVENT_TYPE_ID:", EVENT_TYPE_ID)

# ---------------- STATE STORE ----------------
STATE = {}

# ---------------- REQUEST MODEL ----------------
class SignalRequest(BaseModel):
    phone: str
    message: str

# ---------------- RESPONSE FORMAT ----------------
def response(action, message, status="PENDING", meta=None):
    print(f"📤 RESPONSE | action={action} | status={status}")
    print(f"📤 MESSAGE:\n{message}\n")
    return {
        "action": action,
        "message": message,
        "status": status,
        "meta": meta or {}
    }

# ---------------- FETCH REAL CAL SLOTS (MULTI-DAY FALLBACK) ----------------
def get_available_slots_from_cal():
    print("🕒 Fetching slots with multi-day fallback logic...")

    collected_slots = {}
    required_slots = 3
    days_ahead_limit = 7

    utc = pytz.utc
    ist = pytz.timezone(CAL_TIME_ZONE)

    current_date = datetime.now().date()

    try:
        for day_offset in range(days_ahead_limit):
            if len(collected_slots) >= required_slots:
                break

            target_date = current_date + timedelta(days=day_offset)
            date_str = target_date.isoformat()

            print(f"📅 Checking slots for date: {date_str}")

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

            if not day_slots:
                continue

            for slot in day_slots:
                if len(collected_slots) >= required_slots:
                    break

                iso_start = slot["start"]

                dt_utc = datetime.fromisoformat(iso_start.replace("Z", "+00:00"))
                dt_utc = dt_utc.replace(tzinfo=utc) if dt_utc.tzinfo is None else dt_utc
                dt_ist = dt_utc.astimezone(ist)

                label = dt_ist.strftime("%d %b, %I:%M %p")

                slot_index = str(len(collected_slots) + 1)
                collected_slots[slot_index] = {
                    "iso": iso_start,
                    "label": label
                }

        print("🧠 FINAL COLLECTED SLOTS:", collected_slots)
        return collected_slots

    except Exception as e:
        print("❌ ERROR FETCHING SLOTS:", str(e))
        return {}

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

    print("📤 CAL BOOKING REQUEST PAYLOAD:")
    print(json.dumps(payload, indent=2))

    res = requests.post(
        CAL_BOOKING_URL,
        headers=CAL_HEADERS,
        params={"apiKey": CAL_API_KEY},
        json=payload
    )

    print("📥 CAL RESPONSE STATUS:", res.status_code)
    print("📥 CAL RESPONSE BODY:", res.text)

    if res.status_code not in (200, 201):
        return None

    return res.json()

# ---------------- SIGNAL API ----------------
@app.post("/signal")
def signal_handler(req: SignalRequest):
    phone = req.phone
    text = req.message.strip()

    state = STATE.get(phone, {
        "name": None,
        "email": None,
        "slots": None,
        "stage": "ASK_NAME"
    })

    # -------- STEP 1: NAME --------
    if state["stage"] == "ASK_NAME":
        state["name"] = text
        state["stage"] = "ASK_EMAIL"
        STATE[phone] = state
        return response("SEND_MESSAGE", "Thanks 😊 Please share your email ID.")

    # -------- STEP 2: EMAIL --------
    if state["stage"] == "ASK_EMAIL":
        state["email"] = text

        slot_map = get_available_slots_from_cal()
        if not slot_map:
            return response(
                "SEND_MESSAGE",
                "⚠️ No slots available right now. Please try again later."
            )

        state["slots"] = slot_map
        state["stage"] = "ASK_SLOT"
        STATE[phone] = state

        msg = "🕒 *Available slots:*\n\n"
        for k, v in slot_map.items():
            msg += f"{k}. {v['label']}\n"

        msg += "\nReply with *1, 2, or 3*."
        return response("SEND_MESSAGE", msg)

    # -------- STEP 3: SLOT --------
    if state["stage"] == "ASK_SLOT":
        if text not in state["slots"]:
            return response(
                "SEND_MESSAGE",
                "❌ Invalid choice. Please reply with 1, 2, or 3."
            )

        selected_slot = state["slots"][text]["iso"]
        booking = create_booking(state["name"], state["email"], selected_slot)

        if not booking:
            return response(
                "SEND_MESSAGE",
                "⚠️ Failed to book appointment. Please try again."
            )

        data = booking.get("data", {})
        meeting_url = data.get("meetingUrl") or data.get("location") or "Will be shared shortly"

        start_utc = data.get("start")
        meeting_time_ist = "N/A"

        if start_utc:
            utc = pytz.utc
            ist = pytz.timezone(CAL_TIME_ZONE)

            dt_utc = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
            dt_utc = dt_utc.replace(tzinfo=utc) if dt_utc.tzinfo is None else dt_utc
            meeting_time_ist = dt_utc.astimezone(ist).strftime("%d %b %Y, %I:%M %p IST")

        duration = data.get("duration", 30)
        host_name = data.get("hosts", [{}])[0].get("name", "Host")

        STATE.pop(phone, None)

        return response(
            "SEND_MESSAGE",
            f"""✅ *Appointment Confirmed!*

👤 *Attendee:* {state['name']}
📧 *Email:* {state['email']}
🧑‍💼 *Host:* {host_name}

📅 *Date & Time:* {meeting_time_ist}
⏱ *Duration:* {duration} minutes

🔗 *Meeting Link:*
{meeting_url}

Looking forward to the meeting 😊""",
            status="BOOKED"
        )

    return response(
        "SEND_MESSAGE",
        "Something went wrong. Please start again."
    )


