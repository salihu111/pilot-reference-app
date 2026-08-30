# Pilot Reference Mini App

A Telegram Mini App that turns your uploaded manuals (FOM, FCOM, QRH, DDG, FRM…)
into a searchable, cited reference — plus duty/FTL cross-checks, airport
weather/NOTAM sync, crew bulletins, and a pay calculator.

Builds on the same stack as your existing FOMbot (python-telegram-bot +
PyMuPDF + fastembed + Groq) so both projects share an embedding approach.

## Architecture

```
backend/            FastAPI app (RAG, PDF ingestion, screenshots, airports, bulletins, salary)
  app/main.py        all HTTP endpoints
  app/ingest.py       PDF -> pages -> chunks -> embeddings (fastembed, BAAI/bge-small-en-v1.5)
  app/rag.py          cross-document semantic search + cited answer generation (Groq)
  app/legal_check.py  FTL/duty advisory cross-check against YOUR uploaded FRM/FOM
  app/airports.py     airport CRUD + METAR/TAF (aviationweather.gov) + NOTAM (AVWX)
  app/bulletins.py    crew bulletins + push via Telegram Bot API
  app/salary.py       hourly/annual pay estimate
  static/index.html   single-file React dashboard (no build step) — served
                       directly by FastAPI at `/`, and also what opens as
                       the Telegram Mini App
frontend/index.html  standalone copy of the same dashboard, for hosting it
                      separately (e.g. GitHub Pages) instead of via FastAPI
```

**Single Railway service, one process.** `app/main.py` starts the Telegram
bot (via polling) in the same process as the API on startup, whenever
`TELEGRAM_BOT_TOKEN` is set — there's no separate `bot.py` process to run.
`/start` replies with a button that opens `WEBAPP_URL` as a Telegram Mini
App; that URL is this same FastAPI service's public URL, since it also
serves the dashboard at `/`.

Storage: SQLite (`storage/app.db` inside the container by default, or
`DB_PATH` if you set it to a mounted volume). Without a Railway Volume,
uploaded PDFs/bulletins/subscribers/weather cache reset on redeploy — the
airport briefings baked into the repo re-seed automatically either way.

## Airport briefings, SIDs & escape routes (from your export)

Your zipped notes export is already parsed and included at
`backend/data/airport_briefings/` — 51 airport briefing files plus the two
FIR-wide reference docs ("Addis FIR Escape Routes from exit points" and
"Addis SID from the exit point"), attachments included.

`app/seed_airports.py` parses that folder into the database:
- Each airport's full briefing (SIDs, exit points, ATC phraseology, frequencies)
  is stored and shown in the **Airports** tab — tap an airport → "Briefing".
- Its own ICAO is auto-detected from the note header (validated against your
  real data: DIAP for Abidjan, OTHH for Doha, VABB for Mumbai, etc.) — kept
  separate from the "ALT" field, which your notes use for *alternate*
  diversion airports, not the airport's own code.
- Charts/PDFs/images from each airport's `Attachments/` folder are copied in
  and linked from the briefing popup.
- The two FIR-wide exit-point documents get their own **Escape Routes** tab,
  filterable by exit point name (KONET, ALEMU, RUDOL, etc.).
- Everything above is also embedded into the same vector index as your
  PDFs, so the **Ask** tab can pull an answer from an airport briefing
  right alongside the FOM/FCOM/QRH.

Run it once after the first deploy (and again any time you update the notes):

```bash
cd backend
python -m app.seed_airports --source data/airport_briefings
```

Add `--skip-embed-index` for a fast dry run that only populates the
Airports/Escape Routes tabs without touching the embedding model (useful to
sanity-check parsing locally before you have `GROQ_API_KEY` etc. set up).

A handful of entries (e.g. `SHJ (NOT FULLY DONE)`) parsed fine but are
flagged incomplete in your own notes — worth finishing those in the source
before relying on them operationally.

## What still needs your input

- **NOTAM source**: wired for AVWX (avwx.rest) free tier — sign up, get a
  token, set `AVWX_TOKEN`. Swap in CheckWX or the FAA NOTAM API if you prefer.
- **Legality checker** is an advisory cross-check against your own FRM/FOM
  excerpts, not a legal ruling — it's built to point you to the source page
  and to crew scheduling/OCC, not to help you dodge a legal duty.

## Environment variables (backend)

| Var | Where to get it |
|---|---|
| `GROQ_API_KEY` | console.groq.com — same key style as your FOMbot |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `WEBAPP_URL` | the frontend's public URL once deployed |
| `AVWX_TOKEN` | avwx.rest (free tier) — optional, enables live NOTAMs |
| `DB_PATH` | defaults to `/data/app.db` |
| `UPLOAD_DIR` / `SCREENSHOT_DIR` | default under `/data` |

## Deploy: step-by-step (GitHub + Railway free tier)

### 0. Get your API keys first
- **Groq**: console.groq.com → API Keys → create one (same as your FOMbot).
- **Telegram bot token**: message @BotFather → `/newbot` (or reuse your
  FOMbot's token if you want it to answer to the same bot).
- **AVWX token** (optional, for live NOTAMs): avwx.rest → sign up → free tier
  key. Skip this for now if you just want METAR/TAF, which needs no key.

### 1. Test it locally first (catches config mistakes before you deploy)
```bash
cd fom-miniapp/backend
pip install -r requirements.txt
export GROQ_API_KEY=sk-...           # from step 0
export TELEGRAM_BOT_TOKEN=123:abc    # from step 0
export WEBAPP_URL=http://localhost:5500
export DB_PATH=./app.db              # local file instead of /data
export UPLOAD_DIR=./uploads SCREENSHOT_DIR=./screenshots AIRPORT_ATTACHMENTS_DIR=./airport_attachments
mkdir -p uploads screenshots airport_attachments

python -m app.seed_airports --source data/airport_briefings   # loads your 51 airport briefings
uvicorn app.main:app --reload --port 8000
```
In a second terminal:
```bash
cd fom-miniapp/frontend
python -m http.server 5500
```
Edit `index.html`'s `const API = ...` line to `http://localhost:8000/api`
temporarily, open `http://localhost:5500`, and click through the tabs —
Airports should already show your 51 briefings, Escape Routes should show
the two Addis reference docs. Upload one PDF in Documents and try Ask.

### 2. Push the repo to GitHub
```bash
cd fom-miniapp
git add -A && git commit -m "Pilot reference mini app"
gh repo create pilot-reference-app --private --source=. --push
# or manually: create the repo on github.com, then
# git remote add origin https://github.com/<you>/pilot-reference-app.git
# git branch -M main && git push -u origin main
```

### 3. Deploy on Railway — one service, no separate bot process
1. railway.app → New Project → "Deploy from GitHub repo" → pick the repo.
2. In the service's Settings → set **Root Directory** to `backend`.
3. Railway reads `Procfile` and auto-detects Python; the `web` process runs
   `uvicorn app.main:app`, which serves the API **and** the dashboard **and**
   starts the Telegram bot (polling, in-process) if `TELEGRAM_BOT_TOKEN` is
   set. That's the whole deployment — no second service to create.
4. Settings → **Volumes** → add a volume, e.g. mounted at `/data`, and set
   `DB_PATH=/data/app.db` (plus `UPLOAD_DIR`/`SCREENSHOT_DIR`/
   `AIRPORT_ATTACHMENTS_DIR` under `/data` too if you want uploads to
   survive redeploys). Without a volume, everything under `storage/`
   resets on every redeploy — the baked-in airport briefings re-seed
   automatically regardless, so that part's fine either way.
5. Settings → **Variables**: add `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`
   (from @BotFather), `AVWX_TOKEN` (from avwx.rest, enables live NOTAMs).
6. Deploy. Copy the service's public URL, e.g.
   `https://pilot-reference-backend.up.railway.app`.
7. Add one more variable: `WEBAPP_URL` = that same public URL. Redeploy
   (or just wait — Railway restarts the service when a variable changes).
   This is what the bot's `/start` button opens.
8. Open a Railway shell for the service (or a one-off deploy command) and
   run once:
   ```bash
   python -m app.seed_airports --source data/airport_briefings
   ```
   (Only needed if you're not relying on the automatic background
   auto-seed on first boot, or want to re-run it after updating the notes.)

### 4. Register the Mini App with @BotFather (nicer UX, optional)
`/newapp` → select your bot → paste your Railway URL. This adds it to the
bot's menu button (the small icon next to the message box in your chat
with the bot), in addition to the `/start` inline "Open Pilot Reference"
button that's already wired up.

### 5. Try it
Open your bot in Telegram → `/start` → tap "Open Pilot Reference" → the
dashboard opens as a Telegram Mini App, with your airports and reference
docs already loaded. Tap the weather icon on any airport card to pull
live METAR/TAF/NOTAMs — it auto-refreshes every 10 minutes while that
card stays open, and a manual "Refresh now" is always there too.

> The standalone `frontend/index.html` copy still exists if you'd rather
> host the dashboard separately (e.g. GitHub Pages) instead of through
> FastAPI — if so, edit its `const API = ...` line to point at your
> Railway URL + `/api` and set `WEBAPP_URL` to the GitHub Pages URL
> instead. Not needed for the single-service setup above.

### Free-tier notes
- Railway's free tier has a monthly usage credit, not unlimited — fine for
  personal/small-crew use, keep an eye on the dashboard.
- Alternatives if you outgrow it: **Render** (free web service, spins down
  on idle) or **Fly.io** (free allowance, persistent volumes supported) —
  same repo structure, minor Procfile-equivalent tweaks.

## Known limitations to plan around

- Vector search is brute-force cosine similarity in SQLite — fine up to a
  few thousand pages (your current manual set), but reindex/migrate to
  FAISS or pgvector if the corpus grows a lot.
- Page-highlight boxes in screenshots use a simple text search on the page;
  it works well for exact phrases, less well for paraphrased answers.
- NOTAM/weather refresh is on-demand, driven by the UI: opening an
  airport's weather widget triggers one live pull (cached in
  `airport_weather_cache` so reopening it is instant), then it
  auto-refreshes every 10 minutes for as long as that card stays open,
  plus a manual "Refresh now". There's deliberately no server-side cron
  polling all airports on a schedule — with 51 airports, that would burn
  through AVWX's free-tier daily quota fast for data nobody's looking at.
  If you want true background polling for all airports (e.g. to push a
  bulletin when a NOTAM changes), add a scheduler around
  `app.airports.sync_airport()` — the caching layer it needs already
  exists.
