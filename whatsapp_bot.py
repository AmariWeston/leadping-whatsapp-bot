"""
LeadPing sales agent — web chat widget + Twilio WhatsApp webhook (legacy).

Implements the qualifying-question -> cost-reveal -> book-a-call flow from
method1_sms_whatsapp_playbook.md.

Primary channel (current): a web chat widget at GET /chat, backed by
POST /api/chat. The SMS first-touch links here instead of to a wa.me
WhatsApp link, so there's no Meta Business verification / WhatsApp opt-in
policy involved at all — it's just a webpage.

Legacy channel (kept, unused unless you wire it back up): POST /whatsapp
for Twilio's WhatsApp webhook, in case you go back to the WhatsApp Business
Platform route later.

Conversation state is kept in a local JSON file, keyed by a session id (web
chat) or phone number (WhatsApp), so the bot remembers where each prospect
is in the flow between messages (Flask requests are stateless otherwise).

SETUP REQUIRED BEFORE THIS WORKS:
1. pip install flask twilio
2. Run this app somewhere with a public HTTPS URL.
3. Point the SMS first-touch link at https://<your-public-url>/chat?trade=...
   instead of the wa.me link.
"""

import json
import logging
import os
import re
import uuid
from pathlib import Path

from flask import Flask, jsonify, request
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
        f"Let's put a number on it: a 2026 survey of UK tradespeople found they "
        f"lose an average of {HOURS_LOST_PER_WEEK} hours a week to admin — "
        f"quoting, invoicing, chasing payments — that's "
        f"~{HOURS_LOST_PER_WEEK * WORKING_WEEKS} hours a year. At a typical "
        f"rate of £{rate}/hr for your trade, that's over £{annual:,} of work "
        f"you could be taking on instead — bigger jobs, more customers, same hours."
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


CHAT_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>West Midlands AI — Quick chat</title>
<style>
  body { margin:0; font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f4f5f7; }
  .wrap { max-width: 480px; margin: 0 auto; min-height: 100vh; display:flex; flex-direction:column; background:#fff; }
  header { background:#1F3864; color:#fff; padding:16px 18px; font-weight:600; }
  header span { display:block; font-weight:400; font-size:13px; opacity:.85; margin-top:2px; }
  #messages { flex:1; padding:16px; overflow-y:auto; display:flex; flex-direction:column; gap:10px; }
  .bubble { max-width:80%; padding:10px 14px; border-radius:14px; line-height:1.4; font-size:15px; white-space:pre-wrap; }
  .bot { background:#eef0f4; color:#222; align-self:flex-start; border-bottom-left-radius:4px; }
  .me { background:#1F3864; color:#fff; align-self:flex-end; border-bottom-right-radius:4px; }
  form { display:flex; border-top:1px solid #e3e4e8; padding:10px; gap:8px; }
  input { flex:1; border:1px solid #d8dadf; border-radius:20px; padding:10px 16px; font-size:15px; outline:none; }
  button { background:#1F3864; color:#fff; border:none; border-radius:20px; padding:10px 18px; font-size:15px; cursor:pointer; }
  button:disabled { opacity:.5; }
  a { color:#1F3864; }
</style>
</head>
<body>
<div class="wrap">
  <header>West Midlands AI<span>Quick 30-second chat</span></header>
  <div id="messages"></div>
  <form id="form">
    <input id="input" autocomplete="off" placeholder="Type a message…" />
    <button type="submit">Send</button>
  </form>
</div>
<script>
  const params = new URLSearchParams(window.location.search);
  const trade = params.get('trade') || '';
  let sessionId = localStorage.getItem('wmai_session_id');
  if (!sessionId) {
    sessionId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
    localStorage.setItem('wmai_session_id', sessionId);
  }

  const messagesEl = document.getElementById('messages');
  const formEl = document.getElementById('form');
  const inputEl = document.getElementById('input');

  function addBubble(text, who) {
    const div = document.createElement('div');
    div.className = 'bubble ' + who;
    div.innerHTML = text.replace(/\\n/g, '<br>').replace(
      /(https?:\\/\\/[^\\s]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>'
    );
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  async function send(message) {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, message: message, trade: trade })
    });
    const data = await res.json();
    addBubble(data.reply, 'bot');
  }

  formEl.addEventListener('submit', (e) => {
    e.preventDefault();
    const val = inputEl.value.trim();
    if (!val) return;
    addBubble(val, 'me');
    inputEl.value = '';
    send(val);
  });

  // Kick off the conversation automatically on load.
  send('__start__');
</script>
</body>
</html>
"""


@app.route("/chat", methods=["GET"])
def chat_page():
    return CHAT_PAGE_HTML


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id") or str(uuid.uuid4())
    message = data.get("message", "")
    trade_hint = data.get("trade")

    if message == "__start__":
        # First load of the page — don't run it through classify_yes_no,
        # just trigger the opening message for a brand-new session.
        state = load_state()
        if session_id in state:
            # Returning visitor mid-flow — re-send their last bot reply
            # is not tracked, so just nudge them to continue.
            return jsonify({"reply": "Pick up where we left off — go ahead and reply to the last question."})
        reply_text = handle_message(session_id, "", trade_hint=trade_hint)
        return jsonify({"reply": reply_text})

    reply_text = handle_message(session_id, message, trade_hint=trade_hint)
    return jsonify({"reply": reply_text})


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
