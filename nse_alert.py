import os
import re
import json
import time
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

STATE_FILE = Path("seen.json")
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "15"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "6"))

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Official NSE RSS feed for corporate announcements.
# This avoids the NSE JSON API that can return HTTP 403 from GitHub Actions.
NSE_RSS_URL = os.getenv(
    "NSE_RSS_URL",
    "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
)

NSE_FILINGS_URL = (
    "https://www.nseindia.com/companies-listing/"
    "corporate-filings-announcements"
)


# ---------------------------------------------------------------------------
# High-impact keyword scoring
# ---------------------------------------------------------------------------

HIGH = {
    "acquisition": 10,
    "merger": 10,
    "scheme of arrangement": 10,
    "demerger": 10,
    "takeover": 10,
    "open offer": 10,
    "large order": 9,
    "order worth": 9,
    "work order": 8,
    "contract": 7,
    "letter of award": 9,
    "loa": 7,
    "fund raising": 9,
    "fundraise": 9,
    "qip": 9,
    "preferential": 8,
    "rights issue": 8,
    "qualified institutional": 9,
    "buyback": 8,
    "delisting": 10,
    "credit rating": 7,
    "rating upgrade": 9,
    "rating downgrade": 9,
    "default": 10,
    "insolvency": 10,
    "bankruptcy": 10,
    "regulatory approval": 8,
    "approval": 6,
    "fda": 8,
    "government order": 9,
    "government contract": 9,
    "termination": 8,
    "resignation": 7,
    "appointment": 5,
    "promoter": 7,
    "pledge": 8,
    "stake sale": 8,
    "stake acquisition": 8,
    "material issue": 7,
    "fire": 7,
    "accident": 7,
    "production halt": 9,
    "plant shutdown": 9,
    "fraud": 10,
    "investigation": 9,
    "sebi": 8,
    "litigation": 6,
    "penalty": 7,
    "order cancellation": 8,
    "joint venture": 7,
    "jv": 6,
}

LOW = {
    "trading window": -6,
    "newspaper publication": -5,
    "investor meet": -3,
    "analyst/institutional investor meet": -3,
    "press release": 0,
    "general updates": -2,
    "address change": -7,
    "record date": -6,
    "dividend": -5,
    "agm": -4,
    "shareholders meeting": -4,
    "secretarial": -5,
}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"seen": []}


def save_state(state):
    state["seen"] = state.get("seen", [])[-1000:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def text_of(value):
    if value is None:
        return ""
    value = html.unescape(str(value))
    return re.sub(r"\s+", " ", value).strip()


def local_now():
    return datetime.now().astimezone()


def parse_dt(value):
    value = text_of(value)
    if not value:
        return None

    # RSS normally uses RFC-2822 dates, e.g.:
    # Wed, 28 Sep 2026 10:30:00 +0530
    try:
        dt = parsedate_to_datetime(value)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_now().tzinfo)
            return dt.astimezone()
    except (TypeError, ValueError, OverflowError):
        pass

    formats = [
        "%d-%b-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=local_now().tzinfo)
            return dt.astimezone()
        except ValueError:
            continue

    return None


def clean_html(value):
    value = html.unescape(text_of(value))
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def xml_text(element):
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def child_text(parent, names):
    """Find an XML child by local tag name."""
    wanted = {name.lower() for name in names}

    for child in list(parent):
        tag = child.tag
        if "}" in tag:
            tag = tag.rsplit("}", 1)[-1]
        if tag.lower() in wanted:
            return xml_text(child)

    return ""


# ---------------------------------------------------------------------------
# NSE RSS
# ---------------------------------------------------------------------------

def fetch_announcements():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; NSE-Alert-Bot/1.0; "
            "+https://github.com/)"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    last_error = None

    for attempt in range(3):
        try:
            response = requests.get(
                NSE_RSS_URL,
                headers=headers,
                timeout=30,
            )
            response.raise_for_status()

            root = ET.fromstring(response.content)

            rows = []
            for item in root.iter():
                tag = item.tag
                if "}" in tag:
                    tag = tag.rsplit("}", 1)[-1]

                if tag.lower() not in {"item", "entry"}:
                    continue

                title = child_text(item, ["title"])
                description = child_text(
                    item,
                    ["description", "summary", "content", "encoded"],
                )
                pub_date = child_text(
                    item,
                    ["pubDate", "published", "updated", "date"],
                )
                link = child_text(item, ["link"])
                guid = child_text(item, ["guid", "id"])

                # Atom feeds can store the URL in a link href attribute.
                if not link:
                    for child in list(item):
                        ctag = child.tag
                        if "}" in ctag:
                            ctag = ctag.rsplit("}", 1)[-1]
                        if ctag.lower() == "link":
                            link = child.attrib.get("href", "")
                            if link:
                                break

                rows.append({
                    "title": clean_html(title),
                    "description": clean_html(description),
                    "pubDate": text_of(pub_date),
                    "link": text_of(link),
                    "guid": text_of(guid),
                })

            if not rows:
                raise RuntimeError("NSE RSS feed returned no announcements.")

            return rows

        except (requests.RequestException, ET.ParseError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Could not read NSE RSS feed after 3 attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Announcement parsing / scoring
# ---------------------------------------------------------------------------

def announcement_id(row):
    return (
        text_of(row.get("guid"))
        or text_of(row.get("link"))
        or json.dumps(row, sort_keys=True, ensure_ascii=False)[:500]
    )


def announcement_time(row):
    return parse_dt(row.get("pubDate"))


def extract_symbol(text):
    # Common NSE symbol pattern. This is only a best-effort extraction because
    # the RSS feed does not guarantee a dedicated symbol field.
    match = re.search(r"\b(?:NSE\s*:\s*)?([A-Z][A-Z0-9&.-]{1,19})\b", text)
    if match:
        candidate = match.group(1)
        # Avoid treating ordinary uppercase words as symbols.
        stop = {
            "THE", "AND", "FOR", "NSE", "BSE", "LTD", "LIMITED",
            "COMPANY", "WITH", "FROM", "ABOUT", "HAS", "HAVE",
            "UNDER", "REGULATION", "DISCLOSURE", "EXCHANGE",
        }
        if candidate not in stop:
            return candidate

    return "UNKNOWN"


def score_row(row):
    subject = text_of(row.get("title"))
    details = text_of(row.get("description"))
    blob = f"{subject} {details}".lower()

    score = 0
    hits = []

    for keyword, value in HIGH.items():
        if keyword in blob:
            score += value
            hits.append(keyword)

    for keyword, value in LOW.items():
        if keyword in blob:
            score += value

    return score, hits, subject, details


def filing_url(row):
    link = text_of(row.get("link"))
    if link:
        return link

    symbol = extract_symbol(
        f"{row.get('title', '')} {row.get('description', '')}"
    )

    if symbol != "UNKNOWN":
        return (
            f"{NSE_FILINGS_URL}?symbol={quote(symbol)}"
            f"&tabIndex=equity"
        )

    return NSE_FILINGS_URL


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    # Telegram sendMessage has a 4096-character limit.
    if len(message) > 4000:
        message = message[:3980] + "\n\n[Message truncated]"

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    response.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    state = load_state()
    seen = set(state.get("seen", []))

    now = local_now()
    cutoff = now - timedelta(minutes=LOOKBACK_MINUTES)

    rows = fetch_announcements()
    candidates = []

    for row in rows:
        rid = announcement_id(row)

        if rid in seen:
            continue

        dt = announcement_time(row)

        # If an RSS item has no readable timestamp, skip it rather than
        # accidentally sending an old item.
        if dt is None or dt < cutoff:
            continue

        score, hits, subject, details = score_row(row)

        if score < MIN_SCORE:
            continue

        symbol = extract_symbol(f"{subject} {details}")
        candidates.append(
            (
                dt,
                score,
                symbol,
                subject,
                details,
                hits,
                filing_url(row),
                rid,
            )
        )

    # Oldest first so Telegram notifications follow exchange time.
    candidates.sort(key=lambda x: x[0])

    for dt, score, symbol, subject, details, hits, url, rid in candidates:
        hit_text = ", ".join(hits[:6]) if hits else "material filing"

        message = (
            "ð¨ NSE HIGH-IMPACT ALERT\n\n"
            f"ð {symbol}\n"
            f"ð {dt.strftime('%d-%b-%Y %H:%M:%S %Z')}\n"
            f"ð Score: {score}\n"
            f"ð Trigger: {hit_text}\n\n"
            f"ð° {subject[:700]}\n"
            f"{details[:1400]}\n\n"
            f"ð Verify filing:\n{url}\n\n"
            "â ï¸ Public filing alert â verify the original exchange "
            "document before making any decision."
        )

        send_telegram(message)
        seen.add(rid)

    state["seen"] = list(seen)
    save_state(state)


if __name__ == "__main__":
    main()
