# config.py
# Centralized configuration for email-rag-simple.
# Loads environment variables (Gemini API key, Gmail OAuth paths, Telegram
# bot token/chat id, polling interval, ChromaDB storage path) via python-dotenv
# and exposes them as constants for use across app.py and rag.py.

import os

from dotenv import load_dotenv

load_dotenv()

# --- Secrets & per-deployment settings (all from environment variables) ---
MY_EMAIL = os.getenv("MY_EMAIL")
WHITELIST_SENDERS = [s.strip() for s in os.getenv("WHITELIST_SENDERS", "").split(",") if s.strip()]

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Constants ---
POLL_INTERVAL_SECONDS = 180
CONFIDENCE_THRESHOLD = 0.6
PROCESSED_LABEL = "AutoReplied"
CHROMA_DIR = "./chroma_db"
COLLECTION_NAME = "knowledge_base"
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
EMBED_MODEL = "gemini-embedding-001"
REPLY_SIGNOFF = os.getenv("REPLY_SIGNOFF", "The Team")


# --- Email niches the classifier can assign ---
NICHES = {
    "sales_inquiry": "Interested in buying, pricing, demos, or your offering.",
    "support_request": "Existing user reporting a problem or asking how to do something.",
    "partnership_collab": "Business proposals, collaborations, sponsorships.",
    "meeting_scheduling": "Requests to book, reschedule, or confirm a meeting.",
    "general_query": "Legit questions that don't fit other niches but need a reply.",
    "unknown": "Doesn't fit any niche or low confidence — manual review.",
}


def validate_config():
    if not MY_EMAIL:
        print("[config] WARNING: MY_EMAIL is not set in the environment.")

    if not WHITELIST_SENDERS:
        print("[config] WARNING: WHITELIST_SENDERS is not set in the environment.")

    if not GEMINI_API_KEY:
        print("[config] WARNING: GEMINI_API_KEY is not set in the environment.")

    if not TELEGRAM_BOT_TOKEN:
        print("[config] WARNING: TELEGRAM_BOT_TOKEN is not set in the environment.")

    if not TELEGRAM_CHAT_ID:
        print("[config] WARNING: TELEGRAM_CHAT_ID is not set in the environment.")

    if not os.getenv("GOOGLE_TOKEN_JSON"):
        print("[config] WARNING: GOOGLE_TOKEN_JSON is not set in the environment "
              "(required unless a local token.json file is present).")
