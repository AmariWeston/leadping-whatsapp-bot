"""
LeadPing sales agent — static one-pager landing page (web) + legacy Twilio
WhatsApp webhook (unused).

V2 of this bot. The original version ran an interactive yes/no chat at
GET /chat -> POST /api/chat. Data showed 13 SMS link clicks, 0 chat
completions — people clicked through but weren't willing to type back and
forth with an unknown number/bot before seeing any payoff. This version
drops the chat entirely: GET /chat now renders a static landing page that
shows the trade-specific cost number immediately, explains the tool in
plain language, and has a single "book a call" button. No typing required.

Primary channel: GET /chat?trade=<keyword> — static landing page.
CTA: GET /go?trade=<keyword> — logs a click-through event, then redirects
to BOOKING_LINK. This is the new conversion metric (replaces "chat
completed").

Legacy (kept, unused unless wired back up): POST /whatsapp for Twilio's
WhatsApp webhook, and POST /api/chat / the old conversational flow, in case
you want to bring the chat back later. conversation_state.json /
handle_message() are untouched from v1.

Basic analytics (page views + CTA click-throughs) are logged to a local
JSON file and viewable at GET /stats. Note: on Railway the filesystem
typically resets on redeploy, so these numbers don't survive a `git push`
— fine for short-term testing, but swap in a real DB (e.g. Railway
Postgres) if you want numbers that persist across deploys.

SETUP REQUIRED BEFORE THIS WORKS:
1. pip install flask twilio
2. Run this app somewhere with a public HTTPS URL.
3. Point the SMS first-touch link at https://<your-public-url>/chat?trade=...
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, redirect, request
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
ANALYTICS_FILE = Path("analytics.json")
BOOKING_LINK = "https://cal.eu/wm-ai/30min"

# Representative UK hourly rates + a friendly plural label, keyed by trade
# keyword (sourced from Checkatrade/MyBuilder/Bark cost guides — see
# method1_sms_whatsapp_playbook.md for sources). One dict so the rate and
# label can never drift out of sync with each other.
TRADE_INFO = {
    "mechanic": (45, "mechanics"), "car repair": (45, "mechanics"),
    "plumber": (45, "plumbers"),
    "electrician": (50, "electricians"),
    "locksmith": (55, "locksmiths"),
    "gas": (55, "heating engineers"), "heating": (55, "heating engineers"),
    "drainage": (45, "drainage engineers"),
    "domestic cleaning": (19, "cleaners"), "cleaning": (19, "cleaners"),
    "gardening": (32, "gardeners"), "landscaping": (32, "gardeners"),
    "window cleaning": (30, "window cleaners"),
    "carpet": (22, "carpet cleaners"), "upholstery": (22, "carpet cleaners"),
    "end-of-tenancy": (22, "cleaners"), "eot": (22, "cleaners"),
    "oven cleaning": (50, "oven cleaners"),
    "gutter cleaning": (25, "gutter cleaners"),
    "man-with-van": (60, "clearance firms"), "clearance": (60, "clearance firms"),
}
DEFAULT_RATE = 40
DEFAULT_LABEL = "tradespeople"
HOURS_LOST_PER_WEEK = 8
WORKING_WEEKS = 48

YES_WORDS = {"yes", "y", "yeah", "yep", "yup", "correct", "true", "definitely", "sure"}
NO_WORDS = {"no", "n", "nah", "nope", "not really", "false"}


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def log_event(event_type, session_id=None, trade=None):
    """Append a simple analytics event (page view / CTA click-through) to
    ANALYTICS_FILE. Best-effort — never let analytics break the bot."""
    try:
        record = {
            "ts": datetime.utcnow().isoformat(),
            "event": event_type,
            "session_id": session_id,
            "trade": trade,
        }
        data = []
        if ANALYTICS_FILE.exists():
            try:
                data = json.loads(ANALYTICS_FILE.read_text())
            except Exception:
                data = []
        data.append(record)
        ANALYTICS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        log.exception("Failed to log analytics event %s", event_type)


def classify_yes_no(text):
    t = text.strip().lower()
    if any(t == w or t.startswith(w + " ") for w in YES_WORDS):
        return "yes"
    if any(t == w or t.startswith(w + " ") for w in NO_WORDS):
        return "no"
    return None


def info_for_business(business_hint):
    """Return (rate, label) for a trade hint, falling back to defaults.

    Checks longer/more specific keywords first (e.g. "window cleaning"
    before "cleaning") so a generic substring like "cleaning" can't shadow
    a more specific match — "window cleaning" used to silently match the
    generic "cleaning" entry (£19/hr) instead of its own £30/hr rate
    because dict order put "cleaning" earlier than "window cleaning".
    """
    if not business_hint:
        return DEFAULT_RATE, DEFAULT_LABEL
    hint = business_hint.lower()
    for keyword in sorted(TRADE_INFO, key=len, reverse=True):
        if keyword in hint:
            return TRADE_INFO[keyword]
    return DEFAULT_RATE, DEFAULT_LABEL


def rate_for_business(business_hint):
    return info_for_business(business_hint)[0]


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
    """Legacy interactive flow — kept for /whatsapp and /api/chat, not used
    by the default /chat landing page anymore. See module docstring."""
    state = load_state()
    convo = state.get(phone, {"stage": 0, "answers": {}, "trade_hint": trade_hint})
    stage = convo["stage"]
    reply = None

    if stage == 0:
        reply = (
           "Hey, it's Tom's assistant at West Midlands AI. We help trades spot "
            "exactly how much admin is costing them each year — most have "
            "never worked it out. Want to see your number? Just 3 quick "
            "questions, 30 seconds.\n\n"
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

    if BOOKING_LINK in (reply or "") and not convo.get("_completion_logged"):
        log_event("chat_completed", session_id=phone, trade=convo.get("trade_hint"))
        convo["_completion_logged"] = True
        state[phone] = convo
        save_state(state)

    log.info("phone=%s stage=%s->%s body=%r reply=%r", phone, stage, convo["stage"], body, reply)
    return reply


app = Flask(__name__)


def render_landing_page(trade_hint):
    rate, label = info_for_business(trade_hint)
    annual = HOURS_LOST_PER_WEEK * rate * WORKING_WEEKS
    cta_href = f"/go?trade={trade_hint}" if trade_hint else "/go"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>West Midlands AI — Stop losing money to paperwork</title>
<style>
  body {{ margin:0; font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#f4f5f7; color:#1a1a1a; }}
  .wrap {{ max-width: 560px; margin: 0 auto; background:#fff; min-height:100vh; }}
  header {{ background:#1F3864; color:#fff; padding:16px 20px; font-weight:600; }}
  .hero {{ padding:32px 24px 8px; }}
  .hero h1 {{ font-size:26px; line-height:1.3; margin:0 0 14px; }}
  .hero .cost {{ color:#c0392b; }}
  .hero p {{ font-size:16px; line-height:1.55; color:#444; margin:0 0 8px; }}
  .source {{ font-size:12px; color:#888; margin:14px 0 0; }}
  .steps {{ padding:8px 24px 4px; }}
  .steps h2 {{ font-size:18px; margin:24px 0 14px; }}
  .step {{ display:flex; gap:14px; margin-bottom:16px; align-items:flex-start; }}
  .step .num {{ flex:0 0 28px; height:28px; border-radius:50%; background:#1F3864; color:#fff; display:flex; align-items:center; justify-content:center; font-size:14px; font-weight:600; }}
  .step p {{ margin:2px 0 0; font-size:15px; line-height:1.5; color:#333; }}
  .cta-block {{ padding:20px 24px 36px; text-align:center; }}
  .cta-block a.button {{ display:inline-block; background:#1F3864; color:#fff; text-decoration:none; font-size:17px; font-weight:600; padding:16px 28px; border-radius:10px; width:100%; box-sizing:border-box; }}
  .cta-block .note {{ font-size:13px; color:#888; margin-top:10px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>West Midlands AI</header>
  <div class="hero">
    <h1>You could be losing <span class="cost">over £{annual:,} a year</span> to paperwork.</h1>
    <p>A 2026 survey of UK tradespeople found {label} lose an average of {HOURS_LOST_PER_WEEK} hours a week to quoting, invoicing and chasing payments — that's ~{HOURS_LOST_PER_WEEK * WORKING_WEEKS} hours a year most have never put a number on.</p>
    <p class="source">Source: UK Admin Drain Report 2026 (HeyBRB, reported by Electrical Times). Based on a typical rate of £{rate}/hr for your trade.</p>
  </div>
  <div class="steps">
    <h2>How it works</h2>
    <div class="step"><div class="num">1</div><p>A customer messages you on Checkatrade.</p></div>
    <div class="step"><div class="num">2</div><p>Software sends the quote, books the job in your diary, and sends the invoice once it's marked done — automatically.</p></div>
    <div class="step"><div class="num">3</div><p>You just turn up and do the work.</p></div>
  </div>
  <div class="cta-block">
    <a class="button" href="{cta_href}">Book a free 30-min call</a>
    <div class="note">No commitment — just a quick chat with Tom.</div>
  </div>
</div>
</body>
</html>
"""


@app.route("/chat", methods=["GET"])
def chat_page():
    trade_hint = request.args.get("trade")
    log_event("page_view", trade=trade_hint)
    return render_landing_page(trade_hint)


@app.route("/go", methods=["GET"])
def go():
    trade_hint = request.args.get("trade")
    log_event("cta_click", trade=trade_hint)
    return redirect(BOOKING_LINK, code=302)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Legacy — kept in case you want to bring the interactive chat back.
    Not linked to from the current landing page."""
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get("session_id") or str(uuid.uuid4())
    message = data.get("message", "")
    trade_hint = data.get("trade")

    if message == "__start__":
        state = load_state()
        if session_id in state:
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


@app.route("/stats", methods=["GET"])
def stats():
    if not ANALYTICS_FILE.exists():
        return jsonify({"page_views": 0, "cta_clicks": 0, "by_trade": {}})
    try:
        data = json.loads(ANALYTICS_FILE.read_text())
    except Exception:
        data = []

    by_trade = {}
    for d in data:
        t = d.get("trade") or "unknown"
        by_trade.setdefault(t, {"page_views": 0, "cta_clicks": 0})
        event = d.get("event")
        # Back-compat with v1 event names (link_click / chat_completed) in
        # case analytics.json already has rows from before this rewrite.
        if event in ("page_view", "link_click"):
            by_trade[t]["page_views"] += 1
        elif event in ("cta_click", "chat_completed"):
            by_trade[t]["cta_clicks"] += 1

    return jsonify({
        "page_views": sum(v["page_views"] for v in by_trade.values()),
        "cta_clicks": sum(v["cta_clicks"] for v in by_trade.values()),
        "by_trade": by_trade,
    })


@app.route("/health", methods=["GET"])
def health():
    return "ok", 200


if __name__ == "__main__":
    # Local dev only. For real use, run behind gunicorn/waitress on a real
    # host (see deployment notes in chat) — Flask's built-in server isn't
    # production-grade.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))