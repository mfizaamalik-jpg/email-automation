# app.py
# FastAPI entrypoint for email-rag-simple.
# Exposes endpoints to trigger a Gmail inbox check, retrieve relevant context
# from the knowledge base via rag.py, draft a reply using Google Gemini, and
# send a Telegram notification for review/approval. No internal scheduler —
# an external cron pinger triggers /run-now. No web UI — API only.

import base64
import json
import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI
from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

import config
import rag
import html2text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("email-rag-simple")

# ---------------------------------------------------------------------------
# GMAIL
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]


def credentials_file_path() -> str:
    """Resolve the OAuth client credentials.json path.

    Prefers GOOGLE_CREDENTIALS_JSON (the full credentials.json contents as an
    env var) and materializes it to a temp file, otherwise falls back to a
    local credentials.json in the project root. Not needed for normal Gmail
    API calls (the cached token already carries client id/secret), but kept
    available for gen_token.py-style flows that need a real file path.
    """
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        path = os.path.join(tempfile.gettempdir(), "credentials.json")
        with open(path, "w") as f:
            f.write(creds_json)
        return path
    return "credentials.json"


def gmail_service():
    """Build an authorized Gmail API client from a pre-generated token.

    No local browser flow at runtime (this is expected to run headless, e.g.
    on Render). The token must be generated once locally via gen_token.py and
    supplied as the GOOGLE_TOKEN_JSON env var (a full token.json JSON string).
    Falls back to a local token.json file for local development.
    """
    token_json = os.getenv("GOOGLE_TOKEN_JSON")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    else:
        raise RuntimeError(
            "No Gmail token found. Set the GOOGLE_TOKEN_JSON environment variable "
            "with the contents of a token.json (run gen_token.py locally once to "
            "generate it), or place a token.json file next to app.py for local dev."
        )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Gmail token is invalid and cannot be refreshed. Re-run gen_token.py "
                "locally to generate a fresh token and update GOOGLE_TOKEN_JSON."
            )
        # Only persist the refreshed token back to disk when we're using a
        # local token.json file; env-var-based tokens are immutable at runtime.
        if not token_json:
            with open("token.json", "w") as f:
                f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _b64url_decode(data: str) -> str:
    """Decode Gmail's base64url message body data into text."""
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Walk a Gmail message payload, preferring text/plain over text/html."""

    def find(part: dict, mime_type: str):
        if part.get("mimeType") == mime_type and part.get("body", {}).get("data"):
            return part["body"]["data"]
        for sub_part in part.get("parts", []) or []:
            found = find(sub_part, mime_type)
            if found:
                return found
        return None

    data = find(payload, "text/plain") or find(payload, "text/html")
    if not data:
        # Single-part message with no explicit mimeType match.
        data = payload.get("body", {}).get("data")
    return _b64url_decode(data) if data else ""


def fetch_unread() -> list[dict]:
    """Fetch unread emails from whitelisted senders that aren't already processed."""
    service = gmail_service()
    sender_query = "(" + " OR ".join(f"from:{s}" for s in config.WHITELIST_SENDERS) + ")"
    query = f"is:unread {sender_query} -label:{config.PROCESSED_LABEL}"

    resp = service.users().messages().list(userId="me", q=query).execute()
    message_stubs = resp.get("messages", [])

    emails = []
    for stub in message_stubs:
        msg = service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        emails.append({
            "id": msg["id"],
            "threadId": msg["threadId"],
            "sender": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "body": _extract_body(msg["payload"]),
        })
    return emails


def save_draft(thread_id: str, to: str, subject: str, body: str) -> None:
    """Create a reply draft in the same Gmail thread."""
    service = gmail_service()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service.users().drafts().create(
        userId="me",
        body={"message": {"raw": raw, "threadId": thread_id}},
    ).execute()


def ensure_label(name: str) -> str:
    """Return the id of a Gmail label, creating it if it doesn't exist."""
    service = gmail_service()
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"] == name:
            return label["id"]

    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def mark_processed(msg_id: str) -> None:
    """Tag a message as handled and remove it from the unread inbox view."""
    service = gmail_service()
    label_id = ensure_label(config.PROCESSED_LABEL)
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]},
    ).execute()


# ---------------------------------------------------------------------------
# CLEANING
# ---------------------------------------------------------------------------

_QUOTE_HEADER_RE = re.compile(r"^On .+ wrote:\s*$", re.IGNORECASE)
_SIGNOFF_RE = re.compile(
    r"^(best( regards)?|kind regards|regards|thanks|thank you|cheers|sincerely)[,!]?\s*$",
    re.IGNORECASE,
)
_LOOKS_LIKE_HTML_RE = re.compile(r"<[a-z][\s\S]*>", re.IGNORECASE)


def clean(raw: str) -> str:
    """Turn a raw email body into plain, reply-relevant text.

    Converts HTML to text, then strips quoted replies, signature blocks,
    and sign-offs, and collapses extra whitespace.
    """
    if not raw:
        return ""

    text = raw
    if _LOOKS_LIKE_HTML_RE.search(text):
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = html2text.html2text(str(soup))

    kept_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        # These markers indicate everything below is quoted/signature noise.
        if (
            _QUOTE_HEADER_RE.match(stripped)
            or stripped == "-----Original Message-----"
            or stripped in ("--", "-- ")
            or _SIGNOFF_RE.match(stripped)
        ):
            break
        if stripped.startswith(">") or stripped.lower().startswith("sent from"):
            continue
        kept_lines.append(line)

    text = "\n".join(kept_lines)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# GEMINI
# ---------------------------------------------------------------------------

_genai_client = genai.Client(api_key=config.GEMINI_API_KEY)


def _strip_code_fences(text: str) -> str:
    """Remove ```...``` wrapping that Gemini sometimes adds around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```\s*$", "", text)
    return text.strip()


def _generate_with_retry(prompt: str, attempts: int = 3, backoff: float = 2.0):
    """Call Gemini, retrying on transient errors (e.g. 503 overload) before giving up."""
    last_error = None
    for attempt in range(attempts):
        try:
            return _genai_client.models.generate_content(model=config.MODEL, contents=prompt)
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(backoff)
    raise last_error


def classify(text: str) -> dict:
    """Classify an email into one of config.NICHES. Always returns valid JSON."""
    niches_desc = "\n".join(f"- {k}: {v}" for k, v in config.NICHES.items())
    prompt = f"""Classify the email below into exactly one niche.

Niches:
{niches_desc}

Email:
\"\"\"{text}\"\"\"

Respond with ONLY valid JSON (no code fences, no commentary) in this exact shape:
{{"niche": "<one niche key>", "confidence": <float 0..1>, "reason": "<short reason>"}}"""

    try:
        response = _generate_with_retry(prompt)
        data = json.loads(_strip_code_fences(response.text or ""))
        niche = data.get("niche", "unknown")
        if niche not in config.NICHES:
            niche = "unknown"
        return {
            "niche": niche,
            "confidence": float(data.get("confidence", 0.0)),
            "reason": data.get("reason", ""),
        }
    except Exception as e:
        logger.warning(f"classify() failed to parse a valid result: {e}")
        return {"niche": "unknown", "confidence": 0.0, "reason": "parse_error"}
    finally:
        time.sleep(1)  # stay under free-tier rate limits


def draft_reply(text: str, sender: str, subject: str, niche: str, context: list[str]) -> str:
    """Draft a reply grounded only in the given context. Never invents facts."""
    fallback = "Thanks for reaching out — I've received your email and will personally follow up soon."
    context_block = "\n\n".join(context) if context else ""

    prompt = f"""You are drafting a reply to an email on behalf of the recipient.

From: {sender}
Subject: {subject}
Category: {niche}

Email:
\"\"\"{text}\"\"\"

Reference context (may be empty or irrelevant):
\"\"\"{context_block}\"\"\"

Instructions:
- Write a concise, professional reply grounded ONLY in the reference context above.
- Do NOT invent facts, prices, dates, or commitments that aren't in the context.
- If the context is empty or not relevant, write a brief, polite acknowledgment
  that a human will personally follow up soon — do not make anything up.
- Output ONLY the reply body text. No subject line, no commentary, no markdown."""

    try:
        response = _generate_with_retry(prompt)
        draft = (response.text or "").strip()
        return draft or fallback
    except Exception as e:
        logger.warning(f"draft_reply() failed: {e}")
        return fallback
    finally:
        time.sleep(1)  # stay under free-tier rate limits


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def send_telegram(text: str) -> bool:
    """Send a notification via Telegram bot. Never raises."""
    try:
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# PIPELINE
# ---------------------------------------------------------------------------

LAST_RESULTS: list[dict] = []
LAST_CHECK: str | None = None


def process_inbox() -> list[dict]:
    """Fetch unread whitelisted emails and classify/draft/notify for each."""
    global LAST_RESULTS, LAST_CHECK
    results = []

    for email in fetch_unread():
        sender, subject = email["sender"], email["subject"]
        try:
            clean_text = clean(email["body"])
            classification = classify(clean_text)
            niche, confidence = classification["niche"], classification["confidence"]

            if niche == "unknown" or confidence < config.CONFIDENCE_THRESHOLD:
                tg_sent = send_telegram(
                    f"Review needed\nFrom: {sender}\nSubject: {subject}\n"
                    f"Niche: {niche} ({confidence:.2f})\n"
                    f"Reason: {classification.get('reason', '')}"
                )
                mark_processed(email["id"])
                results.append({
                    "sender": sender, "subject": subject, "niche": niche,
                    "confidence": confidence, "draft": None, "tg_sent": tg_sent,
                    "status": "review",
                })
            else:
                # Search the WHOLE knowledge base — it's one combined file,
                # so there's no per-niche filter to apply here.
                context = rag.retrieve(clean_text, k=4)
                draft = draft_reply(clean_text, sender, subject, niche, context)
                save_draft(email["threadId"], sender, subject, draft)
                tg_sent = send_telegram(
                    f"Draft ready\nFrom: {sender}\nSubject: {subject}\n"
                    f"Niche: {niche} ({confidence:.2f})\n\n{draft}"
                )
                mark_processed(email["id"])
                results.append({
                    "sender": sender, "subject": subject, "niche": niche,
                    "confidence": confidence, "draft": draft, "tg_sent": tg_sent,
                    "status": "drafted",
                })
        except Exception as e:
            logger.exception(f"process_inbox: failed on email from {sender}")
            send_telegram(f"Error processing email from {sender}: {e}")
            results.append({
                "sender": sender, "subject": subject, "niche": None,
                "confidence": None, "draft": None, "tg_sent": False,
                "status": "error",
            })

    LAST_RESULTS = results
    LAST_CHECK = datetime.now(timezone.utc).isoformat()
    return results


# ---------------------------------------------------------------------------
# FASTAPI
# ---------------------------------------------------------------------------
# No internal scheduler here — Render's free tier has no background worker,
# so an external cron pinger (e.g. cron-job.org) hits GET /run-now on a
# schedule instead of this process polling on its own.


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate_config()
    if rag.count() == 0:
        rag.ingest()
    yield


app = FastAPI(title="email-rag-simple", lifespan=lifespan)


@app.get("/")
def status():
    return {
        "running": True,
        "last_check": LAST_CHECK,
        "poll_interval": config.POLL_INTERVAL_SECONDS,
        "kb_count": rag.count(),
        "last_results": LAST_RESULTS,
    }


@app.post("/run-now")
@app.get("/run-now")
def run_now():
    results = process_inbox()
    return {"processed": len(results), "results": results}


@app.get("/test-telegram")
def test_telegram():
    sent = send_telegram("Test message from email-rag-simple")
    return {"sent": sent}


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
