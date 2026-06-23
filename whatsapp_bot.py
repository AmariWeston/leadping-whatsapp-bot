"""
LeadPing WhatsApp sales agent — Twilio webhook bot.

Implements the qualifying-question -> cost-reveal -> book-a-call flow from
method1_sms_whatsapp_playbook.md. Runs as a Flask app; Twilio POSTs each
inbound WhatsApp message to /whatsapp, and this returns TwiML with the
agent's reply (this is the standard Twilio "session reply" pattern — no
separate outbound API call needed for replies within an active thread).

Conversation state is kept in a local JSON file, keyed by phone number, so
the bot remembers where each prospect is in the flow between messages
(Flask requests are stateless otherwise).

SETUP REQUIRED BEFORE THIS WORKS:
1. pip install flask twilio
2. Run this app somewhere with a public HTTPS URL (see notes at bottom).
3. In Twilio Console -> Messaging -> Senders -> WhatsApp senders -> your
   +447378639124 sender -> set "When a message comes in" webhook to:
       https://<your-public-url>/whatsapp   (method: HTTP POST)
4. Fill in BOOKING_LINK and PHONE_TO_TRADE lookup below if you want the
   cost-reveal to use the correct rate per business (falls back to a
   generic rate if the phone number isn't matched).
"""

import json
import logging
import os
import re
from pathlib import Path

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("whatsapp_bot_log.txt"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("whatsapp_bot")

STATE_FILE = Path("conversation_state.json")
BOOKING_LINK = "https://cal.eu/wm-ai/30min"

# Representative UK hourly rates by trade keyword (sourced from Checkatrade/
# MyBuilder/Bark cost guides — see method1_sms_whatsapp_playbook.md for
# sources). Used to personalise the cost-reveal line.
TRADE_RATES = {
    "mechanic": 45, "car repair": 45,
    "plumber": 45,
    "electrician": 50,
    "locksmith": 55,
    "gas": 55, "heating": 55,
    "drainage": 45,
    "domestic cleaning": 19, "cleaning": 19,
    "gardening": 32, "landscaping": 32,
    "window cleaning": 30,
    "carpet": 22, "upholstery": 22, "end-of-tenancy": 22, "eot": 22,
    "oven cleaning": 50,  # per-job trade; treated as flat-rate equivalent
    "gutter cleaning": 25,
    "man-with-van": 60, "clearance": 60,
}
DEFAULT_RATE = 40
HOURS_LOST_PER_WEEK = 3
WORKING_WEEKS = 46

YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "correct", "true", "definitely", "sure"}
NO_WORDS = {"no", "n", "nah", "nope", "not really", "false"}


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def classify_yes_no(text):
    t = text.strip().lower()
    if any(t == w or t.startswith(w + " ") for w in YES_WORDS):
        return "yes"
    if any(t == w or t.startswith(w + " ") for w in NO_WORDS):
        return "no"
    return None


def rate_for_business(business_hint):
    if not business_hint:
        return DEFAULT_RATE
    hint = business_hint.lower()
    for keyword, rate in TRADE_RATES.items():
        if keyword in hint:
            return rate
    return DEFAULT_RATE


def cost_reveal_line(rate):
    annual = HOURS_LOST_PER_WEEK * rate * WORKING_WEEKS
    return (
        f"Let's put a number on it: {HOURS_LOST_PER_WEEK} hours a week on admin "
        f"is ~{HOURS_LOST_PER_WEEK * WORKING_WEEKS} hours a year. At a typical "
        f"rate of £{rate}/hr for your trade, that's roughly £{annual:,} of "
        f"billable time spent on paperwork instead of work every year."
    )


def handle_message(phone, body, trade_hint=None):
    state = load_state()
    convo = state.get(phone, {"stage": 0, "answers": {}, "trade_hint": trade_hint})
    stage = convo["stage"]
    reply = None

    if stage == 0:
        reply = (
            "Hey, thanks for reaching out! I'm Tom's assistant at West "
            "Midlands AI. Mind if I ask 3 quick yes/no questions to see if "
            "this is even relevant to you? Takes 30 seconds.\n\n"
            "Do you currently quote and invoice jobs manually — by hand, "
            "spreadsheet, call or texting back and forth — rather than with "
            "automatic software?"
        )
        convo["stage"] = 1

    elif stage == 1:
        answer = classify_yes_no(body)
        if answer == "no":
            reply = "Got it — sounds like you're already sorted on that front. No worries, thanks for the chat!"
            convo["stage"] = 99
        elif answer == "yes":
            convo["answers"]["q1"] = "yes"
            reply = "Would you say that admin eats up more than 2-3 hours of your week?"
            convo["stage"] = 2
        else:
            reply = "Just need a quick yes or no there 🙂 Do you currently quote/invoice jobs manually?"

    elif stage == 2:
        answer = classify_yes_no(body)
        if answer is None:
            reply = "Yes or no works fine — does admin eat up more than 2-3 hours of your week?"
        else:
            convo["answers"]["q2"] = answer
            reply = (
                "If you got that time back, would you put it toward more or "
                "bigger jobs rather than catching up on admin?"
            )
            convo["stage"] = 3

    elif stage == 3:
        answer = classify_yes_no(body)
        if answer is None:
            reply = "Yes or no is fine — would you put that time toward more/bigger jobs?"
        else:
            convo["answers"]["q3"] = answer
            rate = rate_for_business(convo.get("trade_hint"))
            reply = (
                cost_reveal_line(rate) + "\n\n"
                "Our tool plugs into your Checkatrade leads and handles the "
                "quoting, booking and invoicing automatically — so that time "
                "goes back into doing jobs, not admin.\n\n"
                f"Worth a 30-min call to see if it fits how you work? Here's my calendar: {BOOKING_LINK}"
            )
            convo["stage"] = 4

    elif stage == 4:
        reply = (
            "Sure — in short: lead comes in on Checkatrade → tool auto-quotes "
            "based on your pricing → customer books a slot → invoice goes out "
            "automatically once the job's marked done. You just show up and "
            f"do the work. Happy to walk through it on a quick call: {BOOKING_LINK}"
        )
        convo["stage"] = 99

    else:
        reply = f"Thanks again! Whenever's good for you: {BOOKING_LINK}"

    state[phone] = convo
    save_state(state)
    log.info("phone=%s stage=%s->%s body=%r reply=%r", phone, stage, convo["stage"], body, reply)
    return reply


app = Flask(__name__)


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from_number = request.values.get("From", "")  # e.g. "whatsapp:+447123456789"
    body = request.values.get("Body", "")
    phone = re.sub(r"^whatsapp:", "", from_number)

    reply_text = handle_message(phone, body)

    resp = MessagingResponse()
    resp.message(reply_text)
    return str(resp)


@app.route("/health", methods=["GET"])
def health():
    return "ok", 200


if __name__ == "__main__":
    # Local dev only. For real use, run behind gunicorn/waitress on a real
    # host (see deployment notes in chat) — Flask's built-in server isn't
    # production-grade.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
