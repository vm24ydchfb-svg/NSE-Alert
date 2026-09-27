import os
import re
import json
import time
import html
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
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

NSE_BASE_URL = "https://www.nseindia.com"
NSE_API_URL = f"{NSE_BASE_URL}/api/corporate-announcements"
NSE_RSS_URL = os.getenv(
    "NSE_RSS_URL",
    "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
)
NSE_FILINGS_URL = (
    "https://www.nseindia.com/companies-listing/"
    "corporate-filings-announcements"
)

# Short network timeouts prevent GitHub Actions from hanging for minutes.
CONNECT_TIMEOUT = 6
READ_TIMEOUT = 12
RETRIES = 2


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
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"seen": []}


def save_state(state):
    state["seen"] = list(dict.fromkeys(state.get("seen", [])))[-1000:]
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
        "%d-%b-%Y %H:%M",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
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
    wanted = {name.lower() for name in names}

    for child in list(parent):
        tag = child.tag
        if "}" in tag:
            tag = tag.rsplit("}", 1)[-1]
        if tag.lower() in wanted:
            return xml_text(child)

    return ""


# ---------------------------------------------------------------------------
# NSE HTTP session
# ---------------------------------------------------------------------------

def nse_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": (
            "application/json, text/plain, */*"
        ),
        "Referer": NSE_BASE_URL + "/",
        "Origin": NSE_BASE_URL,
        "Connection": "keep-alive",
    })

    return session


def request_with_retry(session, method, url, **kwargs):
    last_error = None

    for attempt in range(RETRIES):
        try:
            response = session.request(
                method,
                url,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                **kwargs,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            print(
                f"HTTP attempt {attempt + 1}/{RETRIES} failed: "
                f"{type(exc).__name__}: {exc}"
            )
            if attempt + 1 < RETRIES:
                time.sleep(1.5)

    raise last_error


# ---------------------------------------------------------------------------
# NSE direct corporate-announcements API
# ---------------------------------------------------------------------------

def normalize_api_row(item):
    # NSE field names can vary slightly. Prefer the structured fields.
    symbol = text_of(
        item.get("symbol")
        or item.get("SYMBOL")
        or item.get("sm_symbol")
    )

    company = text_of(
        item.get("sm_name")
        or item.get("companyName")
        or item.get("COMPANY_NAME")
    )

    subject = text_of(
        item.get("subject")
        or item.get("SUBJECT")
        or item.get("desc")
        or item.get("DESCRIPTION")
    )

    details = text_of(
        item.get("desc")
        or item.get("description")
        or item.get("DETAILS")
        or item.get("attchmntText")
    )

    pub_date = text_of(
        item.get("an_dt")
        or item.get("broadcastDateTime")
        or item.get("broadcastDate")
        or item.get("date")
        or item.get("sort_date")
    )

    attachment = text_of(
        item.get("attchmntFile")
        or item.get("attachment")
        or item.get("ATTACHMENT")
    )

    guid = text_of(
        item.get("seq_id")
        or item.get("announcementId")
        or item.get("ANN_ID")
        or item.get("newsId")
        or item.get("NEWS_ID")
        or item.get("id")
    )

    link = attachment

    # NSE sometimes returns a relative attachment path.
    if link and link.startswith("/"):
        link = NSE_BASE_URL + link

    return {
        "symbol": symbol,
        "company": company,
        "title": subject,
        "description": details,
        "pubDate": pub_date,
        "link": link,
        "guid": guid,
    }


def fetch_announcements_api():
    session = nse_session()

    # Prime NSE cookies before calling the API. This is important on
    # environments such as GitHub Actions.
    request_with_retry(
        session,
        "GET",
        NSE_BASE_URL + "/",
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        },
    )

    now = local_now()
    # Fetch today's and yesterday's announcements. We filter locally to the
    # configured LOOKBACK_MINUTES, which avoids boundary issues around midnight.
    from_date = (now - timedelta(days=1)).strftime("%d-%m-%Y")
    to_date = now.strftime("%d-%m-%Y")

    params = {
        "index": "equities",
        "from_date": from_date,
        "to_date": to_date,
    }

    response = request_with_retry(
        session,
        "GET",
        NSE_API_URL,
        params=params,
    )

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"NSE API returned non-JSON data (HTTP {response.status_code})"
        ) from exc

    if not isinstance(payload, list):
        if isinstance(payload, dict):
            for key in ("data", "results", "records"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break

    if not isinstance(payload, list):
        raise RuntimeError("NSE API returned an unexpected response shape.")

    rows = []

    for item in payload:
        if isinstance(item, dict):
            row = normalize_api_row(item)

            if row["title"] or row["description"]:
                rows.append(row)

    if not rows:
        # Empty is allowed from NSE when there are genuinely no announcements,
        # but only after a valid API response was received.
        return []

    print(f"NSE API: received {len(rows)} announcements.")
    return rows


# ---------------------------------------------------------------------------
# NSE RSS fallback
# ---------------------------------------------------------------------------

def fetch_announcements_rss():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    session = requests.Session()
    session.headers.update(headers)

    response = request_with_retry(
        session,
        "GET",
        NSE_RSS_URL,
    )

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
            "symbol": "",
            "company": "",
            "title": clean_html(title),
            "description": clean_html(description),
            "pubDate": text_of(pub_date),
            "link": text_of(link),
            "guid": text_of(guid),
        })

    if not rows:
        raise RuntimeError("NSE RSS feed returned no announcements.")

    print(f"NSE RSS: received {len(rows)} announcements.")
    return rows


def fetch_announcements():
    # API is the primary source because it exposes structured symbol/date data.
    try:
        return fetch_announcements_api()
    except Exception as api_error:
        print(f"NSE API unavailable: {api_error}")
        print("Trying official NSE RSS fallback...")

    try:
        return fetch_announcements_rss()
    except Exception as rss_error:
        print(f"NSE RSS unavailable: {rss_error}")

    # Critical: do NOT return [] here. Returning [] would falsely mean
    # "there were no announcements" and could hide a data outage.
    raise RuntimeError(
        "Both NSE corporate-announcement sources are unavailable. "
        "No alert decision was made."
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
    match = re.search(
        r"\b(?:NSE\s*:\s*)?([A-Z][A-Z0-9&.-]{1,19})\b",
        text,
    )

    if match:
        candidate = match.group(1)

        stop = {
            "THE", "AND", "FOR", "NSE", "BSE", "LTD", "LIMITED",
            "COMPANY", "WITH", "FROM", "ABOUT", "HAS", "HAVE",
            "UNDER", "REGULATION", "DISCLOSURE", "EXCHANGE",
            "BOARD", "APPROVAL", "NOTICE", "UPDATE", "ORDER",
            "DATE", "TIME", "INDIA", "INDIAN", "PUBLIC", "GENERAL",
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

    symbol = text_of(row.get("symbol"))

    if not symbol:
        symbol = extract_symbol(
            f"{row.get('title', '')} {row.get('description', '')}"
        )

    if symbol and symbol != "UNKNOWN":
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

    if len(message) > 4000:
        message = message[:3980] + "\n\n[Message truncated]"

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=(5, 15),
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

    print(
        f"Starting NSE Alert | now={now.isoformat()} | "
        f"lookback={LOOKBACK_MINUTES}m | min_score={MIN_SCORE}"
    )

    rows = fetch_announcements()
    candidates = []

    for row in rows:
        rid = announcement_id(row)

        if rid in seen:
            continue

        dt = announcement_time(row)

        # Never alert on an item whose timestamp cannot be parsed.
        if dt is None or dt < cutoff or dt > now + timedelta(minutes=2):
            continue

        score, hits, subject, details = score_row(row)

        if score < MIN_SCORE:
            continue

        symbol = text_of(row.get("symbol"))

        # The API gives us the exact NSE symbol. Only use regex extraction
        # when the source does not provide one.
        if not symbol:
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

    candidates.sort(key=lambda x: x[0])

    print(
        f"Fetched {len(rows)} announcements; "
        f"{len(candidates)} high-impact candidates."
    )

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
        print(f"Telegram alert sent: {symbol} | {subject[:100]}")
        seen.add(rid)

    state["seen"] = list(seen)
    save_state(state)

    print("NSE Alert completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FATAL: {type(exc).__name__}: {exc}")
        raise
