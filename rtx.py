#!/usr/bin/env python3
"""
rtx_job_watcher.py - get a push notification when a new RTX (Raytheon / Collins /
Pratt & Whitney) job posting shows up on careers.rtx.com.

Zero dependencies (stdlib only). Notifications go through ntfy.sh (free):
  1. Install the "ntfy" app on your phone (iOS / Android).
  2. Subscribe to a long, random topic name, e.g. "rtx-jobs-8f3k2x".
  3. export NTFY_TOPIC="rtx-jobs-8f3k2x"

Usage:
  python rtx_job_watcher.py              # check once (use with cron / Task Scheduler)
  python rtx_job_watcher.py --loop 900   # check every 15 minutes, forever
  python rtx_job_watcher.py --test       # send a test notification
  python rtx_job_watcher.py --list       # show what each query returns (date/title/location)
  python rtx_job_watcher.py --debug      # dump raw API response

The first run records what's currently posted without notifying (no spam).
After editing SEARCH_QUERIES, delete rtx_seen_jobs.json to re-seed cleanly
(or rely on NOTIFY_MAX_AGE_DAYS, which skips old postings anyway).
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# CONFIG - edit these
# --------------------------------------------------------------------------- #

# Text typed into the site's keyword box. [""] = no keyword, rely on the filters below.
# You can add terms too, e.g. ["", "mechatronics"] (results are merged).
SEARCH_QUERIES = [""]

# Filters applied by RTX's server (same as the left-hand facets on the search page),
# so we only page through relevant jobs instead of all ~1,900. Values must match the
# site's facet labels exactly. Empty list = don't filter on that facet.
# If the server ignores a key the script still works - the client-side filters below
# are the safety net (use --list to compare totals).
USE_SERVER_FILTERS = True
SERVER_FILTERS = {
    "experienceLevel": ["Entry Level", "Intern"],
    "country": ["United States of America"],
    "category": [],                      # e.g. ["Engineering"]
}

# A title must contain at least one of these. Empty = accept anything the search returns.
TITLE_MUST_INCLUDE = []

# Skip titles containing any of these.
TITLE_EXCLUDE = ["senior", "sr ", "principal", "fellow", "director"]

# Substring filters (case-insensitive). Empty list = no filter.
COUNTRY_MUST_INCLUDE = ["United States"]   # e.g. [] to include every country
LOCATION_MUST_INCLUDE = []                 # e.g. ["Texas", "Remote"]
EXPERIENCE_MUST_INCLUDE = ["Entry Level", "Intern"]   # others: Professional, Manager, Tech/Bus Supp, Fellow
CATEGORY_MUST_INCLUDE = []                 # e.g. ["Engineering"]; others: Quality, Operations, Digital Technology...

# Paging: each page is PAGE_SIZE jobs; fetch up to MAX_PAGES pages per query.
PAGE_SIZE = 50
MAX_PAGES = 8

# Safety nets
NOTIFY_MAX_AGE_DAYS = 14     # don't notify for postings older than this (if date is known)
MAX_NOTIFS_PER_RUN = 8       # extra matches are rolled into one summary notification

STATE_FILE = Path(__file__).with_name("rtx_seen_jobs.json")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")   # your numeric Discord user ID, to get @-pinged

BASE = "https://careers.rtx.com"
API = f"{BASE}/widgets"
JOB_URL = BASE + "/global/en/job/{job_id}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": BASE,
    "Referer": f"{BASE}/global/en/search-results",
}

# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #

def selected_fields(filters: dict | None = None) -> dict:
    """Server-side facet filters. filters=None -> SERVER_FILTERS; filters={} -> none."""
    if not USE_SERVER_FILTERS:
        return {}
    src = SERVER_FILTERS if filters is None else filters
    return {k: v for k, v in src.items() if v}


def build_payload(query: str, offset: int, filters: dict | None = None) -> dict:
    return {
        "lang": "en_global", "deviceType": "desktop", "country": "global",
        "pageName": "search-results", "ddoKey": "refineSearch",
        "sortBy": "Most recent", "subsearch": "", "from": offset,
        "jobs": True, "counts": True,
        "all_fields": ["category", "country", "state", "city", "type", "experienceLevel"],
        "size": PAGE_SIZE, "clearAll": False, "jdsource": "facets",
        "isSliderEnable": False, "pageId": "page11", "siteType": "external",
        "keywords": query, "global": True, "selected_fields": selected_fields(filters),
        "sort": {"order": "desc", "field": "postedDate"}, "locationData": {},
    }


def http_post_json(url: str, body: dict, headers: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def parse_posted(j: dict) -> dt.date | None:
    """Posted date from postedDate, falling back to 'Date Posted. YYYY-MM-DD' in the teaser."""
    for text, pat in ((j.get("postedDate") or "", r"(\d{4}-\d{2}-\d{2})"),
                      (j.get("descriptionTeaser") or "", r"Date Posted[.:\s]*(\d{4}-\d{2}-\d{2})")):
        m = re.search(pat, str(text))
        if m:
            try:
                return dt.date.fromisoformat(m.group(1))
            except ValueError:
                pass
    return None


def normalize(j: dict) -> dict | None:
    job_id = str(j.get("jobId") or j.get("reqId") or "")
    if not job_id:
        return None
    loc = j.get("location") or ", ".join(
        x.title() if x.islower() else x
        for x in (j.get("city"), j.get("state"), j.get("country")) if x)
    loc = ", ".join(p.strip().title() if p.strip().islower() else p.strip() for p in loc.split(","))
    return {
        "id": job_id,
        "title": (j.get("title") or "").strip(),
        "location": loc,
        "country": j.get("country") or "",
        "experience": j.get("experienceLevel") or "",
        "category": ", ".join(j.get("multi_category") or [j.get("category") or ""]).strip(", "),
        "posted": parse_posted(j),
        "url": JOB_URL.format(job_id=job_id),
    }


def fetch_jobs(query: str, debug: bool = False, max_pages: int = MAX_PAGES,
               filters: dict | None = None) -> tuple[list[dict], int]:
    jobs: list[dict] = []
    total = 0
    for page in range(max_pages):
        offset = page * PAGE_SIZE
        data = http_post_json(API, build_payload(query, offset, filters), HEADERS)
        if debug and page == 0:
            print(f"[debug] top-level keys: {list(data.keys())}")
            print(json.dumps(data, indent=2)[:3000])
        try:
            block = data["refineSearch"]
            raw = block["data"]["jobs"]
            total = int(block.get("totalHits") or 0)
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("Unexpected response shape from careers.rtx.com "
                               "(see troubleshooting notes at the bottom of this file).")
        if page == 0 and not raw and selected_fields(filters):
            print("[warn] server-side filters returned nothing; retrying without them "
                  "(client-side filters still apply)", file=sys.stderr)
            return fetch_jobs(query, debug, max_pages, filters={})
        jobs += [n for n in map(normalize, raw) if n]
        if len(raw) < PAGE_SIZE or offset + PAGE_SIZE >= total:
            break
        time.sleep(1)  # be polite
    return jobs, total


def passes_filters(job: dict) -> bool:
    title, loc = job["title"].lower(), job["location"].lower()
    if TITLE_MUST_INCLUDE and not any(k.lower() in title for k in TITLE_MUST_INCLUDE):
        return False
    if any(k.lower() in title for k in TITLE_EXCLUDE):
        return False
    if COUNTRY_MUST_INCLUDE and not any(k.lower() in job["country"].lower() for k in COUNTRY_MUST_INCLUDE):
        return False
    if LOCATION_MUST_INCLUDE and not any(k.lower() in loc for k in LOCATION_MUST_INCLUDE):
        return False
    if EXPERIENCE_MUST_INCLUDE and not any(k.lower() in job["experience"].lower() for k in EXPERIENCE_MUST_INCLUDE):
        return False
    if CATEGORY_MUST_INCLUDE and not any(k.lower() in job["category"].lower() for k in CATEGORY_MUST_INCLUDE):
        return False
    return True


def too_old(job: dict) -> bool:
    return bool(job["posted"]) and (dt.date.today() - job["posted"]).days > NOTIFY_MAX_AGE_DAYS

# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def _post(url: str, data: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20):
        pass


def notify(title: str, message: str, url: str = "") -> None:
    sent = False
    if NTFY_TOPIC:
        headers = {"Title": title.encode("ascii", "ignore").decode()[:250],
                   "Priority": "high", "Tags": "briefcase"}
        if url:
            headers["Click"] = url
        try:
            _post(f"https://ntfy.sh/{NTFY_TOPIC}", message.encode("utf-8"), headers)
            sent = True
        except Exception as e:
            print(f"[warn] ntfy failed: {e}", file=sys.stderr)
    if DISCORD_WEBHOOK:
        embed = {"title": title[:256], "description": message[:4000], "color": 0xC8102E}
        if url:
            embed["url"] = url
        payload = {"embeds": [embed]}
        if DISCORD_USER_ID:
            payload["content"] = f"<@{DISCORD_USER_ID}>"
            payload["allowed_mentions"] = {"users": [DISCORD_USER_ID]}
        try:
            _post(DISCORD_WEBHOOK, json.dumps(payload).encode(),
                  {"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]})
            sent = True
        except Exception as e:
            print(f"[warn] discord failed: {e}", file=sys.stderr)
    if not sent:
        print(f"\n*** {title}\n{message}\n{url}\n")

# --------------------------------------------------------------------------- #
# State + main
# --------------------------------------------------------------------------- #

def load_seen() -> tuple[set[str], bool]:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text())), False
        except Exception:
            pass
    return set(), True


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen)))


def find_new(debug: bool = False):
    """Fetch, diff against saved state, save state. Returns None if nothing could be
    fetched, else (matching_new_jobs, first_run, fetched_count, new_count)."""
    seen, first_run = load_seen()
    all_jobs: dict[str, dict] = {}

    for q in SEARCH_QUERIES or [""]:
        try:
            jobs, _ = fetch_jobs(q, debug=debug)
            all_jobs.update({j["id"]: j for j in jobs})
        except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as e:
            print(f"[error] query {q!r}: {e}", file=sys.stderr)

    if not all_jobs:
        print("[warn] no jobs returned; leaving state untouched")
        return None

    new = [j for jid, j in all_jobs.items() if jid not in seen]
    matching = [j for j in new if passes_filters(j) and not too_old(j)]
    matching.sort(key=lambda j: j["posted"] or dt.date.min, reverse=True)

    # Record everything seen (even non-matching) so changing filters later
    # doesn't dump old postings on you.
    seen.update(all_jobs)
    save_seen(seen)
    return matching, first_run, len(all_jobs), len(new)


def check_once(debug: bool = False) -> int:
    res = find_new(debug)
    if res is None:
        return 0
    matching, first_run, fetched, new = res

    if first_run:
        print(f"First run: recorded {fetched} current postings. "
              f"You'll be notified about anything new from now on.")
        return 0

    for j in matching[:MAX_NOTIFS_PER_RUN]:
        notify(f"New RTX job: {j['title']}",
               f"{j['location']}\nPosted: {j['posted'] or 'unknown'}\nReq ID: {j['id']}",
               j["url"])
    extra = matching[MAX_NOTIFS_PER_RUN:]
    if extra:
        notify(f"+{len(extra)} more new RTX jobs",
               "\n".join(f"{j['title']} - {j['location']}" for j in extra[:15]),
               f"{BASE}/global/en/search-results")

    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {fetched} fetched, "
          f"{new} new, {len(matching)} matched")
    return len(matching)


def list_results() -> None:
    """Print what each query returns so you can check sort order and filters."""
    for q in SEARCH_QUERIES or [""]:
        try:
            jobs, total = fetch_jobs(q, max_pages=1)
        except Exception as e:
            print(f"[error] query {q!r}: {e}", file=sys.stderr)
            continue
        print(f"\n=== {q!r}: {total} total hits (server filters: "
              f"{selected_fields() or 'off'}), showing first {len(jobs)} ===")
        for j in jobs[:20]:
            mark = "*" if passes_filters(j) else " "
            print(f"{mark} {str(j['posted'] or '----------'):10}  {j['experience'][:11]:11}  "
                  f"{j['title'][:50]:50}  {j['location'][:30]}")
    print("\n(* = passes your filters)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Notify on new RTX job postings.")
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="check every N seconds (min 120)")
    ap.add_argument("--test", action="store_true", help="send a test notification and exit")
    ap.add_argument("--list", action="store_true", help="show first results per query and exit")
    ap.add_argument("--debug", action="store_true", help="dump raw API response")
    args = ap.parse_args()

    if args.test:
        notify("RTX watcher test", "If you can read this, notifications work.", BASE)
    elif args.list:
        list_results()
    elif args.loop:
        interval = max(args.loop, 120)
        while True:
            try:
                check_once(args.debug)
            except Exception as e:
                print(f"[error] {e}", file=sys.stderr)
            time.sleep(interval)
    else:
        check_once(args.debug)


if __name__ == "__main__":
    main()

# --------------------------------------------------------------------------- #
# TROUBLESHOOTING
# --------------------------------------------------------------------------- #
# "Unexpected response shape" / nothing returned: RTX may have changed the endpoint.
#   1. Open https://careers.rtx.com/global/en/search-results in Chrome.
#   2. DevTools (F12) -> Network -> Fetch/XHR, then use the search box.
#   3. Find the POST (usually /widgets) that returns jobs as JSON.
#   4. Copy as cURL and adjust API / build_payload() / normalize().
#
# Schedule every 15 min (Linux/macOS):
#   */15 * * * * NTFY_TOPIC=your-topic /usr/bin/python3 /path/to/rtx_job_watcher.py >> /tmp/rtx.log 2>&1