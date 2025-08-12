# ApartmentBot — Troy/Albany/Schenectady Scraper

This repo contains a ready-to-deploy apartment scraping + email alert service.

**What it does**
- Scrapes Zillow, Apartments.com, HotPads, and Craigslist (best-effort)
- Filters for 1-bedroom apartments in Troy, Albany, Schenectady, NY priced $1000–$1150
- Deduplicates listings using `seen.json`
- Sends a **single combined email** per run (first image embedded + thumbnails)
- Designed to run hourly (uses APScheduler inside a Flask app so it persists on Render)

---

## Files
- `scraper.py` — main app
- `requirements.txt` — Python dependencies

---

## Environment variables (set these in Render or locally)
- `GMAIL_USER` — Gmail address used to send emails (e.g. `youngpupc@gmail.com`)
- `GMAIL_APP_PASSWORD` — Google **App Password** (16 chars). Do NOT use your normal Gmail password.
- `RECIPIENT_EMAIL` — where alerts are sent (e.g. `CollinMckennaa@icloud.com`)
- `PORT` — optional (Render sets this automatically)

---

## Deployment (Render.com) — quick steps

1. Create a GitHub repo and push these files.
2. On Render:
   - Click **New → Web Service**.
   - Connect your GitHub repo and select it.
   - Environment: `Python 3`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn scraper:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`
   - Attach a **Persistent Disk** (so `seen.json` sticks between restarts). Choose the smallest available persistent disk.
3. In Render service settings → Environment:
   - Add `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`.
4. Deploy. The service will run immediately and then hourly.

---

## Testing locally

1. Create virtualenv & install deps:
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

2. Export environment vars locally:
- macOS/Linux:
  ```
  export GMAIL_USER=youngpupc@gmail.com
  export GMAIL_APP_PASSWORD=your_app_password_here
  export RECIPIENT_EMAIL=CollinMckennaa@icloud.com
  ```
3. Run:
python scraper.py

