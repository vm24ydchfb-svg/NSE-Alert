import os
import re
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

STATE_FILE = Path("seen.json")
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "15"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

NSE_HOME = "https://www.nseindia.com/"
NSE_API = "https://www.nseindia.com/api/corporate-announcements"

# Keywords are deliberately conservative: the aim is to surface potentially
# market-moving filings, not every routine exchange announcement.
HIGH = {
    "acquisition": 10, "merger": 10, "scheme of arrangement": 10,
    "demerger": 10, "takeover": 10, "open offer": 10,
    "large order": 9, "order worth": 9, "work order": 8,
    "contract": 7, "letter of award": 9, "loa": 7,
    "fund raising": 9, "fundraise": 9, "qip": 9, "preferential": 8,
    "rights issue": 8, "qualified institutional": 9,
    "buyback": 8, "delisting": 10,
    "credit rating": 7, "rating upgrade": 9, "rating downgrade": 9,
    "default": 10, "insolvency": 10, "bankruptcy": 10,
    "regulatory approval": 8, "approval": 6, "fda": 8,
    "government order": 9, "government contract": 9,
    "termination": 8, "resignation": 7, "appointment": 5,
    "promoter": 7, "pledge": 8, "stake sale": 8, "stake acquisition": 8,
    "material issue": 7, "fire": 7, "accident": 7,
    "production halt": 9, "plant shutdown": 9,
    "fraud": 10, "investigation": 9, "sebi": 8,
    "litigation": 6, "penalty": 7, "order cancellation": 8,
    "joint venture": 7, "jv": 6,
}
LOW = {
    "trading window": -6, "newspaper publication": -5,
    "investor meet": -3, "analyst/institutional investor meet": -3,
    "press release": 0, "general updates": -2, "address change": -7,
    "record date": -6, "dividend": -5, "agm": -4,
    "shareholders meeting": -4, "secretarial": -5,
}

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen": []}

def save_state(state):
    state["seen"] = state["seen"][-1000:]
    STATE_FILE.write_text(json.dumps(state))

def nse_session():
    s = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/json,text/plain,*/*",
        "Referer": NSE_HOME,
        "Connection": "keep-alive",
    }
    s.headers.update(headers)
    r = s.get(NSE_HOME, timeout=20)
    r.raise_for_status()
    return s

def fetch_announcements():
    now = datetime.now(timezone.utc).astimezone()
    day = now.strftime("%d-%m-%Y")
    params = {
        "index": "equities",
        "from_date": day,
        "to_date": day,
    }
    s = nse_session()
    r = s.get(NSE_API, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        for key in ("data", "records", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []

def text_of(x):
    if x is None:
        return ""
    return re.sub(r"\s+", " ", str(x)).strip()

def get_field(row, names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n]
    return ""

def parse_dt(value):
    value = text_of(value)
    if not value:
        return None
    formats = [
        "%d-%b-%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S",
    ]
    for f in formats:
        try:
            return datetime.strptime(value, f).astimezone()
        except ValueError:
            pass
    return None

def score_row(row):
    subject = text_of(get_field(row, ["subject", "desc", "sm_subject", "sub"]))
    details = text_of(get_field(row, ["details", "attchmntText", "description"]))
    blob = f"{subject} {details}".lower()
    score = 0
    hits = []
    for k, v in HIGH.items():
        if k in blob:
            score += v
            hits.append(k)
    for k, v in LOW.items():
        if k in blob:
            score += v
    return score, hits, subject, details

def announcement_time(row):
    return parse_dt(get_field(row, [
        "broadcastDateTime", "an_dt", "broadcast_date_time",
        "bcastDateTime", "dateTime", "dt"
    ]))

def announcement_id(row):
    return text_of(get_field(row, [
        "id", "seq_id", "newsId", "ANN_ID", "announcementId"
    ])) or json.dumps(row, sort_keys=True)[:500]

def filing_url(row):
    symbol = text_of(get_field(row, ["symbol", "sym", "nseSymbol"]))
    if symbol:
        return f"https://www.nseindia.com/companies-listing/corporate-filings-announcements?symbol={quote(symbol)}&tabIndex=equity"
    return "https://www.nseindia.com/companies-listing/corporate-filings-announcements"

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }, timeout=20)
    r.raise_for_status()

def main():
    state = load_state()
    seen = set(state.get("seen", []))
    now = datetime.now().astimezone()
    cutoff = now - timedelta(minutes=LOOKBACK_MINUTES)

    rows = fetch_announcements()
    candidates = []

    for row in rows:
        rid = announcement_id(row)
        if rid in seen:
            continue

        dt = announcement_time(row)
        if dt is None or dt < cutoff:
            continue

        score, hits, subject, details = score_row(row)
        if score < MIN_SCORE:
            continue

        symbol = text_of(get_field(row, ["symbol", "sym", "nseSymbol"])) or "UNKNOWN"
        company = text_of(get_field(row, ["companyName", "sm_name", "company"])) or symbol
        candidates.append((dt, score, symbol, company, subject, details, hits, filing_url(row), rid))

    # Oldest first so Telegram notifications follow exchange time.
    candidates.sort(key=lambda x: x[0])

    for dt, score, symbol, company, subject, details, hits, url, rid in candidates:
        hit_text = ", ".join(hits[:6]) if hits else "material filing"
        msg = (
            f"🚨 NSE HIGH-IMPACT ALERT\n\n"
            f"📌 {symbol} — {company}\n"
            f"🕒 {dt.strftime('%d-%b-%Y %H:%M:%S %Z')}\n"
            f"📈 Score: {score}\n"
            f"🔎 Trigger: {hit_text}\n\n"
            f"📰 {subject[:500]}\n"
            f"{details[:900]}\n\n"
            f"🔗 Verify filing:\n{url}\n\n"
            f"⚠️ Public filing alert — verify the original exchange document before trading."
        )
        send_telegram(msg)
        seen.add(rid)

    # Mark all processed candidates as seen.
    state["seen"] = list(seen)
    save_state(state)

if __name__ == "__main__":
    main()
