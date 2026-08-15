# email-rag-simple

A simple FastAPI app that auto-drafts email replies using retrieval-augmented
generation (RAG), built entirely on free services:

- **Gmail API** — reads incoming emails and creates draft replies
- **Google Gemini** — classifies emails and generates the reply text
- **ChromaDB** — local vector store for RAG context, built from `knowledge/`
- **Telegram** — sends a free notification when a draft is ready

No web UI — this is an API-only backend, polled on a schedule and/or driven
via a couple of HTTP endpoints.

## Project layout

```
email-rag-simple/
├── app.py                 # FastAPI app, Gmail/Gemini/Telegram logic, scheduler
├── rag.py                 # RAG indexing/retrieval over the knowledge/ folder
├── config.py               # Environment-based configuration and placeholders
├── knowledge/               # Reference docs used to ground replies
│   └── nexacloud_kb.txt      # demo combined knowledge base
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── .gitignore
```

## How it works

For each unread, whitelisted email that hasn't already been processed:

1. **Fetch** — `fetch_unread()` pulls the message from Gmail and extracts its body.
2. **Clean** — `clean()` strips HTML, quoted replies, and signatures down to plain text.
3. **Classify** — Gemini assigns one of the niches in `config.NICHES` plus a confidence score.
4. **Route on confidence:**
   - **Low confidence / unknown niche** → no draft is written; a "needs review"
     Telegram alert is sent instead.
   - **Confident classification** → `rag.retrieve()` searches the whole
     knowledge base (it's one combined file, so there's no per-niche filter)
     for relevant context, Gemini drafts a reply grounded in that context, and
     the draft is saved into the Gmail thread.
5. **Notify & mark done** — a Telegram message is sent either way, and the
   email is labeled `AutoReplied` and marked read so it isn't reprocessed.

This whole cycle is `process_inbox()`, run automatically every
`POLL_INTERVAL_SECONDS` and also triggerable on demand via `POST /run-now`.

## Setup

1. **Enable the Gmail API and get OAuth credentials.** In
   [Google Cloud Console](https://console.cloud.google.com/), enable the
   Gmail API, then create an **OAuth client ID** of type "Desktop app".
   Download it and save it as `credentials.json` in the project root.

2. **Get a free Gemini API key.** Create one at
   [Google AI Studio](https://aistudio.google.com/).

3. **Set up a Telegram bot.** Message [@BotFather](https://t.me/BotFather) on
   Telegram, run `/newbot`, and follow the prompts to get a bot token. Then
   message your new bot (or add it to a chat) and hit
   `https://api.telegram.org/bot<token>/getUpdates` in a browser to find your
   `chat_id` in the response.

4. **Copy `.env.example` to `.env`** and fill in all three secrets:

   ```
   GEMINI_API_KEY=your-gemini-key
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_ID=your-chat-id
   ```

   `.env` is gitignored, so these never get committed. Then fill in the
   remaining, non-secret placeholders in [config.py](config.py):
   - `MY_EMAIL` — your Gmail address
   - `WHITELIST_SENDERS` — list of sender addresses this app is allowed to auto-reply to

5. **Knowledge base.** A demo knowledge base is already included at
   [knowledge/nexacloud_kb.txt](knowledge/nexacloud_kb.txt) — a single combined
   file covering the fictional company NexaCloud. Replace it with your own
   content, or add more `.txt`/`.md` files to `knowledge/`.

6. **Install dependencies** (Python 3.10+ required):

   ```
   pip install -r requirements.txt
   ```

7. **Run the app:**

   ```
   uvicorn app:app --reload
   ```

   On first run this opens a browser window for Gmail OAuth consent; the
   resulting token is cached in `token.json` so you won't be prompted again.
   On startup the app also checks the knowledge base and runs ingestion
   automatically if it's empty.

8. **Test it, in this order:**
   - `GET /test-telegram` to confirm Telegram notifications are working.
   - `POST /run-now` to process the inbox immediately, or just wait — the
     scheduler polls Gmail automatically every `POLL_INTERVAL_SECONDS`.

## Running with Docker

This lets you run the exact same app on any machine that has Docker
installed, without setting up Python locally.

**Gmail OAuth needs a real browser, which a container doesn't have** — so
`token.json` must be generated once *outside* Docker before you containerize.
If you've already completed Setup steps 1–8 above on this machine, you
already have it; otherwise run step 7 locally first (`uvicorn app:app
--reload`, hit `/run-now` once, sign in) to produce `token.json`, then stop
that local server.

1. Make sure `credentials.json`, `token.json`, and `.env` (from Setup steps
   1, 4, and 7) exist in the project root — Docker mounts these in rather
   than baking them into the image, so the image itself stays credential-free
   and portable.

2. Build and start the container:

   ```
   docker compose up --build
   ```

   The API is now available at `http://localhost:8000`, and the scheduler
   runs inside the container on the same `POLL_INTERVAL_SECONDS` cadence.
   `chroma_db/` persists in a named Docker volume (`chroma_data`) across
   restarts, so the knowledge base only gets re-ingested when that volume is
   empty.

3. To run it on a different machine, copy the whole project folder
   (including your `credentials.json`, `token.json`, and `.env`) over and
   repeat step 2 there — no Python install needed, just Docker.

4. Stop it with `docker compose down` (add `-v` to also wipe the persisted
   `chroma_db` volume).

**Gotcha:** if `token.json` doesn't already exist as a file before your first
`docker compose up`, Docker's bind mount will create it as an empty
*directory* instead, which breaks Gmail auth inside the container. Confirm
`token.json` is a real file on the host first.

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Status: `running`, `last_check`, `poll_interval`, `kb_count`, `last_results` |
| `/run-now` | POST | Runs `process_inbox()` immediately; returns `{processed, results}` |
| `/test-telegram` | GET | Sends a test Telegram message; returns `{sent: bool}` |

## Troubleshooting

- **Gemini rate limit errors.** The free tier has fairly low requests-per-minute
  limits. `app.py` already sleeps 1 second after each Gemini call; if you still
  hit limits, raise `POLL_INTERVAL_SECONDS` in `config.py` or process fewer
  emails per run.

- **Gmail OAuth is stuck or using the wrong account.** Delete `token.json` and
  restart the app — this forces a fresh browser OAuth flow.

- **`/run-now` returns results but retrieval looks empty / drafts are generic
  acknowledgments.** Confirm `knowledge/nexacloud_kb.txt` (or your own file)
  exists and isn't empty, then re-run ingestion by deleting the `chroma_db/`
  folder and restarting the app (it re-ingests automatically when the
  collection is empty).
