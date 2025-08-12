#!/usr/bin/env python3
"""
scraper.py
- Aggregates listings from Craigslist, Apartments.com, Zillow, HotPads
- Filters for 1BR, $1000-$1150, Troy/Albany/Schenectady NY
- Sends a single email per run with new listings (first image embedded + thumbnails)
- Persists seen listing keys in seen.json
- Designed to run as a long-running web service (Flask + APScheduler) on Render or similar
"""

import os
import re
import json
import time
import logging
from datetime import datetime
from urllib.parse import urlencode, quote_plus
import requests
from bs4 import BeautifulSoup
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import smtplib
from flask import Flask, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# -------- CONFIG (set these as environment vars on Render) ----------
GMAIL_USER = os.getenv("GMAIL_USER")            # e.g. youngpupc@gmail.com
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")  # 16-char Google App Password
RECIPIENT_EMAIL = os.getenv("RECIPIENT_EMAIL")  # e.g. CollinMckennaa@icloud.com

# Filters
CITIES = ["Troy", "Albany", "Schenectady"]     # simple city name checks
MIN_PRICE = 1000
MAX_PRICE = 1150
BEDROOMS = 1

SEEN_FILE = "seen.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ApartmentScraper/1.0; +https://example.com/bot)",
    "Accept-Language": "en-US,en;q=0.9",
}
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# -------- Logging ----------
logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger("aptscraper")

# -------- Flask (keeps process alive on Render) ----------
app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

# -------- Persistence helpers ----------
def load_seen():
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
    except Exception:
        logger.exception("Failed to load seen file; starting fresh.")
    return set()

def save_seen(seen_set):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen_set), f)
    except Exception:
        logger.exception("Failed to save seen file.")

# -------- Utilities ----------
def extract_int_from_string(s):
    if not s:
        return None
    m = re.search(r"(\d[\d,]*)", s.replace(",", ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except:
        return None

def price_in_range(price_str):
    v = extract_int_from_string(price_str)
    if v is None:
        return False
    return MIN_PRICE <= v <= MAX_PRICE

def normalize_text(t):
    return re.sub(r"\s+", " ", (t or "").strip())

def likely_one_bed(text):
    t = (text or "").lower()
    return any(x in t for x in ["1 br", "1 br.", "1 bd", "1bd", "1 bedroom", "one bedroom"])

def city_in_text(text):
    t = (text or "").lower()
    return any(city.lower() in t for city in CITIES)

# -------- scrapers (best-effort) ----------
def scrape_craigslist():
    results = []
    regions = ["albany", "troy", "schenectady"]
    for region in regions:
        try:
            base = f"https://{region}.craigslist.org/search/apa"
            params = {
                "min_price": MIN_PRICE,
                "max_price": MAX_PRICE,
                "bedrooms": BEDROOMS,
                "format": "rss",
            }
            url = base + "?" + urlencode(params)
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                logger.debug("Craigslist returned %s for %s", r.status_code, region)
                continue
            soup = BeautifulSoup(r.content, "xml")
            for item in soup.find_all("item"):
                title = normalize_text(item.title.string if item.title else "")
                link = item.link.string if item.link else None
                desc = item.description.string if item.description else ""
                price = None
                if item.find("price"):
                    price = item.find("price").string
                else:
                    p = re.search(r"\$(\d[\d,]*)", desc)
                    if p:
                        price = "$" + p.group(1)
                results.append({
                    "source": "Craigslist",
                    "title": title,
                    "link": link,
                    "price": price,
                    "description": normalize_text(desc)[:350],
                    "image": None
                })
        except Exception:
            logger.exception("Craigslist scrape failed for %s", region)
    return results

def scrape_apartments_com():
    results = []
    for city in ["Troy, NY", "Albany, NY", "Schenectady, NY"]:
        try:
            q = quote_plus(city)
            url = f"https://www.apartments.com/{q}/"
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                logger.debug("Apartments.com %s returned %s", city, r.status_code)
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("article.placard, .placard")[:50]
            for c in cards:
                title_el = c.select_one(".property-title a") or c.select_one(".placardTitle a")
                title = normalize_text(title_el.get_text()) if title_el else normalize_text(c.get_text()[:80])
                link = title_el["href"] if title_el and title_el.has_attr("href") else None
                price_el = c.select_one(".price-range, .property-pricing, .rent")
                price = price_el.get_text(strip=True) if price_el else None
                desc_el = c.select_one(".description, .property-text")
                desc = normalize_text(desc_el.get_text()) if desc_el else ""
                img = None
                img_el = c.select_one("img")
                if img_el and (img_el.get("data-src") or img_el.get("src")):
                    img = img_el.get("data-src") or img_el.get("src")
                if price and not price_in_range(price):
                    continue
                results.append({
                    "source": "Apartments.com",
                    "title": title,
                    "link": link,
                    "price": price,
                    "description": desc[:350],
                    "image": img
                })
        except Exception:
            logger.exception("Apartments.com scrape failed for %s", city)
    return results

def scrape_hotpads():
    results = []
    # HotPads is Zillow-owned; try search result pages
    for city in ["Troy, NY", "Albany, NY", "Schenectady, NY"]:
        try:
            q = quote_plus(city)
            url = f"https://hotpads.com/{q}/apartments-for-rent"
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                logger.debug("HotPads %s returned %s", city, r.status_code)
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("li.SearchResult, .listing, .HomeCard")[:50]
            for c in cards:
                title_el = c.select_one("a")  # best-effort
                title = normalize_text(title_el.get_text()) if title_el else normalize_text(c.get_text()[:80])
                link = title_el["href"] if title_el and title_el.has_attr("href") else None
                if link and link.startswith("/"):
                    link = "https://hotpads.com" + link
                price_el = c.select_one(".price, .displayPrice")
                price = price_el.get_text(strip=True) if price_el else None
                desc = ""
                desc_el = c.select_one(".propertyDescription")
                if desc_el:
                    desc = normalize_text(desc_el.get_text())
                img = None
                img_el = c.select_one("img")
                if img_el and (img_el.get("data-src") or img_el.get("src")):
                    img = img_el.get("data-src") or img_el.get("src")
                if price and not price_in_range(price):
                    continue
                results.append({
                    "source": "HotPads",
                    "title": title,
                    "link": link,
                    "price": price,
                    "description": desc[:350],
                    "image": img
                })
        except Exception:
            logger.exception("HotPads scrape failed for %s", city)
    return results

def scrape_zillow():
    results = []
    # Zillow is JS-heavy; attempt a best-effort fetch (may be incomplete)
    for city in ["Troy, NY", "Albany, NY", "Schenectady, NY"]:
        try:
            q = quote_plus(city)
            url = f"https://www.zillow.com/homes/for_rent/{q}/"
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                logger.debug("Zillow %s returned %s", city, r.status_code)
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(".list-card, article, .photo-card")[:40]
            for c in cards:
                title = normalize_text(c.get_text()[:100])
                link_el = c.select_one("a.list-card-link, a")
                link = link_el.get("href") if link_el and link_el.has_attr("href") else None
                if link and link.startswith("/"):
                    link = "https://www.zillow.com" + link
                price_el = c.select_one(".list-card-price, .zsg-photo-card-price")
                price = price_el.get_text(strip=True) if price_el else None
                if price and not price_in_range(price):
                    continue
                img = None
                img_el = c.select_one("img")
                if img_el and img_el.get("src"):
                    img = img_el.get("src")
                results.append({
                    "source": "Zillow",
                    "title": title,
                    "link": link,
                    "price": price,
                    "description": "",
                    "image": img
                })
        except Exception:
            logger.exception("Zillow scrape failed for %s", city)
    return results

# -------- aggregator & email ----------
def run_scrapers_once():
    logger.info("Scrape run starting at %s", datetime.utcnow().isoformat())
    seen = load_seen()
    new_keys = set()
    found = []

    scrapers = [scrape_craigslist, scrape_apartments_com, scrape_hotpads, scrape_zillow]
    for fn in scrapers:
        try:
            items = fn()
            logger.info("%s returned %d items", fn.__name__, len(items))
            for it in items:
                key = (it.get("link") or it.get("title") or "")[:240]
                if not key:
                    continue
                if key in seen:
                    continue
                # basic heuristics
                combined_text = " ".join([it.get("title",""), it.get("description",""), it.get("link","")])
                if not city_in_text(combined_text):
                    # allow through (some sources omit city) but could filter more strongly later
                    pass
                if it.get("price") and not price_in_range(it.get("price")):
                    continue
                # bedroom heuristic (best-effort)
                if not likely_one_bed(it.get("title","") + " " + it.get("description","")):
                    # don't strictly block — price range is primary filter
                    pass
                found.append(it)
                new_keys.add(key)
        except Exception:
            logger.exception("Scraper %s failed", fn.__name__)

    if new_keys:
        try:
            send_email(found)
        except Exception:
            logger.exception("Failed to send email")
    else:
        logger.info("No new listings found this run.")

    seen.update(new_keys)
    save_seen(seen)
    logger.info("Seen store updated (%d total).", len(seen))

def send_email(items):
    if not GMAIL_USER or not GMAIL_APP_PASSWORD or not RECIPIENT_EMAIL:
        logger.error("Email env vars missing. Set GMAIL_USER, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL")
        return

    # Build multipart email
    msg = MIMEMultipart("related")
    subject = f"[ApartmentBot] {len(items)} new listing(s) — {', '.join(CITIES)}"
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL

    html_blocks = []
    cid_count = 0

    for i, it in enumerate(items[:12]):
        title = it.get("title","No title")
        price = it.get("price","Price N/A")
        link = it.get("link","")
        desc = it.get("description","")
        src = it.get("source","")
        img_url = it.get("image")

        img_html = ""
        if i == 0 and img_url:
            try:
                r = requests.get(img_url, headers=HEADERS, timeout=12)
                if r.status_code == 200 and r.content:
                    cid = f"img{cid_count}"
                    mime_img = MIMEImage(r.content)
                    mime_img.add_header("Content-ID", f"<{cid}>")
                    mime_img.add_header("Content-Disposition", "inline", filename=f"img0.jpg")
                    msg.attach(mime_img)
                    img_html = f'<div><img src="cid:{cid}" style="max-width:420px;height:auto;margin-bottom:8px;"></div>'
                    cid_count += 1
            except Exception:
                logger.debug("Failed to fetch embed image %s", img_url)

        thumbs_html = ""
        if img_url:
            thumbs_html = f'<a href="{link}" target="_blank"><img src="{img_url}" style="width:120px;height:auto;margin-right:6px;border-radius:3px;"/></a>'

        html_blocks.append(f"""
            <div style="padding:10px;border:1px solid #eee;border-radius:6px;margin-bottom:10px;">
                <h3 style="margin:0 0 6px 0;"><a href="{link}" target="_blank">{title}</a></h3>
                <div style="font-weight:600;margin-bottom:8px;">{price} — {src}</div>
                {img_html}
                <div style="margin-top:6px;">{desc}</div>
                <div style="margin-top:8px;">{thumbs_html}</div>
                <div style="margin-top:6px;font-size:12px;color:#666;">{link}</div>
            </div>
        """)

    html_body = "<html><body>"
    html_body += f"<p>Found {len(items)} new listing(s) at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
    html_body += "".join(html_blocks)
    html_body += "<hr><p>ApartmentBot</p></body></html>"

    mime = MIMEText(html_body, "html")
    msg.attach(mime)

    # send
    smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
    smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    smtp.sendmail(GMAIL_USER, [RECIPIENT_EMAIL], msg.as_string())
    smtp.quit()
    logger.info("Email sent to %s with %d items", RECIPIENT_EMAIL, len(items))

# -------- Scheduler setup ----------
scheduler = BackgroundScheduler()
# run immediately, then every hour
scheduler.add_job(run_scrapers_once, "interval", hours=1, next_run_time=datetime.utcnow())
scheduler.start()
logger.info("Scheduler started; first run queued.")

if __name__ == "__main__":
    # Initial run
    time.sleep(2)
    try:
        run_scrapers_once()
    except Exception:
        logger.exception("Initial run failed.")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
