# email-rag-simple

A simple FastAPI app that auto-drafts email replies using retrieval-augmented
generation (RAG), built entirely on free services:

- **Gmail API** — reads incoming emails and creates draft replies
- **Google Gemini** — classifies emails and generates the reply text
- **ChromaDB** — local vector store for RAG context, built from `knowledge/`
- **Telegram** — sends a free notification when a draft is ready

A small built-in dashboard at `/dashboard` shows status and lets you trigger
runs by hand; the core app is still API-first, driven by an external cron
pinger hitting `/run-now`.

## Project layout

```
email-rag-simple/
├── app.py                 # FastAPI app, Gmail/Gemini/Telegram logic
├── rag.py                 # RAG indexing/retrieval over the knowledge/ folder
├── config.py               # Environment-based configuration
├── gen_token.py            # Run locally once to generate a Gmail OAuth token for deployment
├── knowledge/               # Reference docs used to ground replies
│   └── nexacloud_kb.txt      # demo combined knowledge base
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── render.yaml              # Render free-tier web service definition
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

This whole cycle is `process_inbox()`, triggered via `GET`/`POST /run-now`.
There's no internal scheduler — locally you can hit it manually, and in
production an external cron pinger (e.g. cron-job.org) calls it on a
schedule instead. This keeps the app stateless and works within Render's
free tier, which doesn't run background workers.

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

4. **Copy `.env.example` to `.env`** and fill in the values:

   ```
   GEMINI_API_KEY=your-gemini-key
   TELEGRAM_BOT_TOKEN=your-bot-token
   TELEGRAM_CHAT_ID=your-chat-id
   MY_EMAIL=your-gmail-address
   WHITELIST_SENDERS=sender1@example.com,sender2@example.com
   ```

   `.env` is gitignored, so these never get committed. `WHITELIST_SENDERS` is
   a comma-separated list of sender addresses this app is allowed to
   auto-reply to. `GOOGLE_TOKEN_JSON` and `GOOGLE_CREDENTIALS_JSON` can stay
   empty for local development — see [Deploying to Render](#deploying-to-render-free-tier)
   for when those are needed.

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
   - `GET` or `POST /run-now` to process the inbox immediately. There's no
     internal scheduler, so call this whenever you want a check to run (see
     [Deploying to Render](#deploying-to-render-free-tier) for automating it
     with an external cron pinger).

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

   The API is now available at `http://localhost:8000`. There's no internal
   scheduler, so trigger checks with `GET`/`POST /run-now` (manually, or via
   your own cron). `chroma_db/` persists in a named Docker volume
   (`chroma_data`) across restarts, so the knowledge base only gets
   re-ingested when that volume is empty.

3. To run it on a different machine, copy the whole project folder
   (including your `credentials.json`, `token.json`, and `.env`) over and
   repeat step 2 there — no Python install needed, just Docker.

4. Stop it with `docker compose down` (add `-v` to also wipe the persisted
   `chroma_db` volume).

**Gotcha:** if `token.json` doesn't already exist as a file before your first
`docker compose up`, Docker's bind mount will create it as an empty
*directory* instead, which breaks Gmail auth inside the container. Confirm
`token.json` is a real file on the host first.

## Deploying to Render (free tier)

This runs the app on Render's free web service plan — nothing runs on your
own machine, and anyone (including you, from another computer) can hit the
deployed URL. Render's free tier has no persistent background worker, so
instead of an internal scheduler, an external free cron service pings
`/run-now` on a schedule.

1. **Generate a deployable Gmail token, locally, once.** Render can't open a
   browser for OAuth consent, so do that step on your own machine first, with
   `credentials.json` (from Setup step 1) in the project root:

   ```
   python gen_token.py
   ```

   This opens a browser for the usual Gmail consent flow, saves `token.json`
   locally as a backup, and prints a single-line JSON string. Copy that
   string — you'll paste it into Render as `GOOGLE_TOKEN_JSON` in step 4.

2. **Push this repo to GitHub.** `credentials.json`, `token.json`, and `.env`
   stay out of git (already gitignored) — none of your secrets need to be
   committed, since Render gets them via environment variables instead.

3. **Create a Render web service.**
   - Go to [Render](https://render.com/) and create a new **Blueprint** (or
     **Web Service**) from your GitHub repo. Render auto-detects
     [render.yaml](render.yaml), which sets the build command
     (`pip install -r requirements.txt`), the start command
     (`uvicorn app:app --host 0.0.0.0 --port $PORT`), and the free plan.

4. **Set the environment variables** in the Render dashboard (Environment
   tab) — `render.yaml` declares these as required but leaves the values to
   you:
   - `GEMINI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `MY_EMAIL`
   - `WHITELIST_SENDERS` — comma-separated, e.g. `a@example.com,b@example.com`
   - `GOOGLE_TOKEN_JSON` — the string printed by `gen_token.py` in step 1
   - `GOOGLE_CREDENTIALS_JSON` — optional; contents of `credentials.json`,
     only needed if some code path requires the file itself rather than the
     cached token

   Save changes and let Render redeploy. The knowledge base is ingested into
   Chroma automatically on startup (Render's disk is ephemeral on the free
   tier, so this re-ingestion happens on every deploy/restart — that's
   expected and cheap for a small knowledge base).

5. **Verify it's live:** open `https://<your-service>.onrender.com/` and
   confirm you get back the status JSON, then hit `/test-telegram` to check
   notifications work end to end.

6. **Set up an external cron to replace the old internal scheduler.** Create
   a free account at [cron-job.org](https://cron-job.org/), and add a job
   that sends a `GET` request to `https://<your-service>.onrender.com/run-now`
   every few minutes. That's what actually drives inbox checks in
   production — the app itself no longer polls on its own.

   **Note:** Render's free web services spin down after periods of
   inactivity and take a few seconds to wake on the next request, so the
   first cron hit after idle time may be slower — this is expected and free.

7. **Confirm the cron job is actually firing.** Two sources of truth:
   - **cron-job.org's own dashboard** — open the job and check its
     "History"/execution log. This is authoritative for whether the job is
     configured and whether each ping succeeded (HTTP 200) or failed.
   - **This app's `/dashboard` page** — shows a `last_check` timestamp and a
     cron-health badge (active / stale / inactive) derived from it. If it
     says "no checks recorded" even though cron-job.org shows successful
     pings, the Render instance likely restarted since — `last_check` is
     in-memory only and resets on every redeploy/restart, it isn't persisted
     to disk.

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Status: `running`, `last_check`, `poll_interval`, `kb_count`, `last_results` |
| `/run-now` | GET or POST | Runs `process_inbox()` immediately; returns `{processed, results}`. GET exists so a simple cron pinger can trigger it with a plain URL hit. |
| `/test-telegram` | GET | Sends a test Telegram message; returns `{sent: bool}` |
| `/dashboard` | GET | Small HTML dashboard: status, a cron-health indicator (based on `last_check`), buttons to trigger `/run-now` and `/test-telegram`, and a table of the last run's results |

## Troubleshooting

- **Gemini rate limit errors.** The free tier has fairly low requests-per-minute
  limits. `app.py` already sleeps 1 second after each Gemini call; if you still
  hit limits, space out your cron pinger's calls to `/run-now` (`POLL_INTERVAL_SECONDS`
  in `config.py` documents the recommended spacing) or process fewer emails per run.

- **Gmail auth fails on Render with "No Gmail token found."** `GOOGLE_TOKEN_JSON`
  is missing or empty in the Render environment variables. Run `python gen_token.py`
  locally and paste its output into `GOOGLE_TOKEN_JSON`.

- **Gmail OAuth is stuck or using the wrong account.** Delete `token.json` and
  restart the app — this forces a fresh browser OAuth flow.

- **`/run-now` returns results but retrieval looks empty / drafts are generic
  acknowledgments.** Confirm `knowledge/nexacloud_kb.txt` (or your own file)
  exists and isn't empty, then re-run ingestion by deleting the `chroma_db/`
  folder and restarting the app (it re-ingests automatically when the
  collection is empty).
