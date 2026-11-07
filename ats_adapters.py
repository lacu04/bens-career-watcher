#!/usr/bin/env python3
"""
ats_adapters.py - job-feed adapters shared by job_alert_bot.py and test_adapters.py.

Providers:  workday | oracle (Oracle HCM candidate experience) | phenom (RTX, via your watcher script)

Add a Workday company by pasting any of its job URLs into:
    py test_adapters.py --workday-url "<url>"
It tests the feed and prints the config line to paste into COMPANIES below.

Every job is a dict with: company, id, title, location, posted (date|None), url, level
('intern'|'early'|'experienced'|'unknown', from the title / site fields), engineering (bool),
min_years / entry_words (filled in by enrich_jobs from the job description).
"""
import argparse
import datetime as dt
import html
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# --------------------------------------------------------------------------- #
# Classification (shared by every provider)
# --------------------------------------------------------------------------- #
INTERN = re.compile(r"\b(intern(ship)?s?|co-?ops?|apprentice(ship)?)\b", re.I)
EXPERIENCED = re.compile(
    r"\b(experienced|mid[- ]level|intermediate|senior|sr\.?|principal|staff|lead|manager|director|fellow|architect|chief|vp|head of|ii|iii|iv)\b"
    r"|\b(engineer|analyst|technician|developer|specialist|scientist)\s*(2|3|4|5|6|v|vi)\b"
    r"|\blevel\s*([3-9])\b", re.I)
EARLY = re.compile(
    r"\b(entry[- ]level|new[- ]?grad(uate)?|recent grad(uate)?|graduate|early[- ]career|university|"
    r"junior|jr\.?|associate|rotational|leadership development|development program|career accelerator)\b"
    r"|(?<!data )\bentry\b"
    r"|\b(engineer|analyst|technician|developer|specialist)\s*(i|1)\b", re.I)
MULTILEVEL = re.compile(
    r"\b(?:associate|entry(?:[- ]level)?|junior)\b[\s,/&-]*(?:\b(?:or|and)\b[\s,]*)?"
    r"(?:mid[- ]level|intermediate|experienced|senior)|\ball[- ]levels?\b", re.I)
ENGINEERING = re.compile(
    r"engineer|engineering|mechatronic|controls?|automation|embedded|firmware|robot|hardware|"
    r"electrical|mechanical|systems?|design|test|manufacturing|process|technician|r&d", re.I)


def classify_level(title: str) -> str:
    """'intern' | 'early' | 'experienced' | 'unknown' - from the title only."""
    if INTERN.search(title):
        return "intern"
    if MULTILEVEL.search(title):  # e.g. "(Associate or Experienced)": open to associate level
        return "early"
    if EXPERIENCED.search(title):
        return "experienced"
    if EARLY.search(title):
        return "early"
    return "unknown"


def is_engineering(title: str) -> bool:
    return bool(ENGINEERING.search(title))


class AdapterError(Exception):
    pass


def http_json(method: str, url: str, body=None, extra=None, timeout: int = 30):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers.update(extra or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        hint = " (possibly bot protection - try later or from another network)" if e.code in (403, 429) else ""
        try:
            detail = e.read(300).decode("utf-8", "replace").strip().replace("\n", " ")
        except Exception:
            detail = ""
        raise AdapterError(f"HTTP {e.code} from {url.split('?')[0]}{hint}" + (f": {detail}" if detail else ""))
    except urllib.error.URLError as e:
        raise AdapterError(f"network error: {e.reason}")
    except json.JSONDecodeError:
        raise AdapterError("response was not JSON (probably an HTML page or bot check)")


def _finish(job: dict) -> dict:
    job["level"] = classify_level(job["title"])
    job["engineering"] = is_engineering(job["title"])
    job.setdefault("min_years", None)
    job.setdefault("entry_words", False)
    return job

# --------------------------------------------------------------------------- #
# Workday adapter
# --------------------------------------------------------------------------- #

def parse_workday_posted(text: str, today: dt.date | None = None):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 5 Days Ago' / 'Posted 30+ Days Ago' -> date."""
    today = today or dt.date.today()
    t = (text or "").lower()
    if "today" in t:
        return today
    if "yesterday" in t:
        return today - dt.timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    return today - dt.timedelta(days=int(m.group(1))) if m else None


def normalize_workday(p: dict, host: str, site: str, company: str = "") -> dict:
    ext = p.get("externalPath") or ""
    bullets = p.get("bulletFields") or []
    return _finish({
        "company": company,
        "id": str(bullets[0] if bullets else ext),
        "title": (p.get("title") or "").strip(),
        # NOTE: multi-location jobs show "2 Locations" here; full detail needs the detail endpoint.
        "location": p.get("locationsText") or "",
        "posted": parse_workday_posted(p.get("postedOn", "")),
        "url": f"https://{host}/en-US/{site}{ext}" if ext else "",
        "external_path": ext,
    })


_WD_VARIANT: dict[str, int] = {}   # host -> request style that worked
_WD_SESSIONS: dict[str, tuple] = {}   # host -> (opener with cookies, csrf token, notes)


def _open_json(opener, req, timeout: int, url: str):
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read(300).decode("utf-8", "replace").strip().replace("\n", " ")
        except Exception:
            detail = ""
        raise AdapterError(f"HTTP {e.code} from {url.split('?')[0]}" + (f": {detail}" if detail else ""))
    except urllib.error.URLError as e:
        raise AdapterError(f"network error: {e.reason}")
    except json.JSONDecodeError:
        raise AdapterError("response was not JSON (probably an HTML page or bot check)")


def _wd_session(host: str, site: str):
    """Behave like a browser: load the public careers page once, keep its cookies, and pick up the CSRF
    token Workday sets. Returns (opener, csrf_token, notes)."""
    if host in _WD_SESSIONS:
        return _WD_SESSIONS[host]
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", UA), ("Accept-Language", "en-US,en;q=0.9"),
                         ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")]
    notes = []
    for path in (f"/en-US/{site}", f"/{site}"):
        try:
            with opener.open(f"https://{host}{path}", timeout=30) as r:
                r.read(2000)
                notes.append(f"GET {path}: HTTP {r.status}")
        except urllib.error.HTTPError as e:
            notes.append(f"GET {path}: HTTP {e.code}")
        except Exception as e:
            notes.append(f"GET {path}: {type(e).__name__}")
    token = next((c.value for c in jar if c.name.upper() == "CALYPSO_CSRF_TOKEN"), "")
    notes.append(f"cookies: {sorted({c.name for c in jar})}; CSRF token: {'yes' if token else 'no'}")
    _WD_SESSIONS[host] = (opener, token, notes)
    return _WD_SESSIONS[host]


def _wd_session_headers(host: str, site: str, token: str) -> dict:
    h = {"Content-Type": "application/json", "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9",
         "Origin": f"https://{host}", "Referer": f"https://{host}/en-US/{site}"}
    if token:
        h["X-Calypso-CSRF-Token"] = token
    return h


def _wd_session_post(opener, url: str, body: dict, headers: dict, timeout: int = 30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    return _open_json(opener, req, timeout, url)


def _wd_session_get(opener, url: str, headers: dict, timeout: int = 30):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return _open_json(opener, req, timeout, url)


def _wd_request(host: str, site: str, offset: int, page_size: int, search: str, applied, variant: int):
    """POST to Workday's job-search endpoint. Styles: 0 = standard, 1 = no Origin/Referer headers,
    2 = bare body (limit/offset only), 3 = smaller pages (10), 4 = browser-like session (cookies + CSRF).
    Some tenants reject the standard request."""
    tenant = host.split(".")[0]
    if variant == 4:  # browser-like session: cookies + CSRF token
        opener, token, _ = _wd_session(host, site)
        body = {"appliedFacets": applied or {}, "limit": page_size, "offset": offset, "searchText": search}
        data = _wd_session_post(opener, f"https://{host}/wday/cxs/{tenant}/{site}/jobs", body,
                                _wd_session_headers(host, site, token))
        return data, page_size
    size = 10 if variant == 3 else page_size
    body = {"limit": size, "offset": offset}
    if variant == 2:
        if applied:
            body["appliedFacets"] = applied
        if search:
            body["searchText"] = search
    else:
        body["appliedFacets"] = applied or {}
        body["searchText"] = search
    extra = {"Origin": f"https://{host}", "Referer": f"https://{host}/{site}"} if variant == 0 else None
    _, data = http_json("POST", f"https://{host}/wday/cxs/{tenant}/{site}/jobs", body, extra=extra)
    return data, size


def _wd_post(host: str, site: str, offset: int, page_size: int, search: str, applied):
    """Try the request styles in order (only after an HTTP 4xx); remember the one that works per host."""
    order = [_WD_VARIANT[host]] if host in _WD_VARIANT else [0, 1, 2, 3, 4]
    first_err = None
    for v in order:
        try:
            data, size = _wd_request(host, site, offset, page_size, search, applied, v)
        except AdapterError as e:
            if not str(e).startswith("HTTP 4"):
                raise
            first_err = first_err or e
            continue
        _WD_VARIANT[host] = v
        return data, size
    raise AdapterError(f"{first_err}" + (" [simplified request styles failed too]" if len(order) > 1 else ""))


def workday_list(host: str, site: str, search: str = "", pages: int = 2,
                 page_size: int = 20, company: str = "", applied: dict | None = None):
    jobs, total, offset = [], None, 0
    for _ in range(pages):
        data, size = _wd_post(host, site, offset, page_size, search, applied)
        if not isinstance(data, dict) or "jobPostings" not in data:
            raise AdapterError("unexpected Workday response shape (no 'jobPostings')")
        if total is None:
            total = data.get("total")
        batch = data["jobPostings"]
        jobs += [normalize_workday(x, host, site, company) for x in batch]
        offset += len(batch)
        if len(batch) < size:
            break
        time.sleep(0.5)
    return jobs, total


# --------------------------------------------------------------------------- #
# Workday extras: facets (server-side filters), job detail, experience parsing
# --------------------------------------------------------------------------- #

def walk_facets(facets, out: dict | None = None) -> dict:
    """Flatten Workday's nested facet tree into {facetParameter: [{descriptor, id, count}]}."""
    out = out if out is not None else {}
    for f in facets or []:
        vals = f.get("values") or []
        leaves = [v for v in vals if "id" in v]
        if f.get("facetParameter") and leaves:
            out.setdefault(f["facetParameter"], []).extend(
                {"descriptor": v.get("descriptor"), "id": v["id"], "count": v.get("count")} for v in leaves)
        walk_facets([v for v in vals if "values" in v], out)
    return out


def find_country_facet(fac: dict, name: str = "United States") -> tuple[str, str] | None:
    """(facet_name, value_id) for the US in whichever facet mentions 'country'
    (sites call it locationCountry, Location_Country, country, ...)."""
    for param, vals in fac.items():
        if "country" in param.lower():
            for v in vals:
                if name.lower() in (v.get("descriptor") or "").lower():
                    return param, v["id"]
    return None


def find_country_id(fac: dict, name: str = "United States") -> str | None:
    found = find_country_facet(fac, name)
    return found[1] if found else None


def workday_facets(host: str, site: str) -> dict:
    data, _ = _wd_post(host, site, 0, 1, "", None)
    if not isinstance(data, dict) or "facets" not in data:
        raise AdapterError("no 'facets' in Workday response")
    return walk_facets(data["facets"])


def workday_detail(host: str, site: str, external_path: str) -> dict:
    tenant = host.split(".")[0]
    url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    if _WD_VARIANT.get(host) == 4:
        opener, token, _ = _wd_session(host, site)
        headers = {k: v for k, v in _wd_session_headers(host, site, token).items() if k != "Content-Type"}
        data = _wd_session_get(opener, url, headers)
    else:
        _, data = http_json("GET", url, extra={"Referer": f"https://{host}/{site}"})
    info = (data or {}).get("jobPostingInfo")
    if not isinstance(info, dict):
        raise AdapterError("no 'jobPostingInfo' in detail response")
    return {"description_html": info.get("jobDescription") or "",
            "country": (info.get("country") or {}).get("descriptor", ""),
            "location": info.get("location") or "",
            "additional_locations": info.get("additionalLocations") or [],
            "time_type": info.get("timeType") or "", "req_id": info.get("jobReqId") or ""}


YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:to|-|\u2013)\s*\d{1,2}\s*)?(?:years?|yrs?)\b(?:\s+of)?(?:\s+\w+){0,4}?\s+experience",
    re.I)
ENTRY_PHRASES = re.compile(
    r"entry[- ]level|recent (?:college )?graduate|new[- ]?grad|early[- ]career|"
    r"no (?:prior |previous )?experience (?:is )?(?:required|necessary)|"
    r"\b0\s*(?:-|to)\s*[12]\s*years?|currently (?:pursuing|enrolled)", re.I)


def experience_signal(text: str) -> dict:
    """Smallest 'N years ... experience' requirement found in a posting, plus entry-level wording."""
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(text or "")))
    years = [int(m.group(1)) for m in YEARS_RE.finditer(plain)]
    return {"min_years": min(years) if years else None, "entry_phrases": bool(ENTRY_PHRASES.search(plain))}


def infer_level(title_level: str, sig: dict, max_years: int = 2) -> str:
    """Trust a clear title label; otherwise use the stated experience requirement."""
    if title_level != "unknown":
        return title_level
    if sig["min_years"] is not None:
        return "early" if sig["min_years"] <= max_years else "experienced"
    return "early" if sig["entry_phrases"] else "unknown"


# --------------------------------------------------------------------------- #
# Oracle HCM (Candidate Experience) adapter
# --------------------------------------------------------------------------- #

def normalize_oracle(r: dict, host: str, site: str, company: str = "") -> dict:
    jid = str(r.get("Id") or "")
    return _finish({
        "company": company,
        "id": jid,
        "title": (r.get("Title") or "").strip(),
        "location": r.get("PrimaryLocation") or "",
        "posted": _iso_date(r.get("PostedDate")),
        "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}" if jid else "",
    })


def _iso_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def oracle_list(host: str, site: str, site_number: str, search: str = "", pages: int = 2,
                page_size: int = 25, company: str = ""):
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    facets = urllib.parse.quote("LOCATIONS;WORK_LOCATIONS;TITLES;CATEGORIES;POSTING_DATES", safe="")
    jobs, total = [], None
    for p in range(pages):
        finder = (f"findReqs;siteNumber={site_number},facetsList={facets},limit={page_size},"
                  f"offset={p * page_size},sortBy=POSTING_DATES_DESC")
        if search:
            finder += f",keyword={urllib.parse.quote(search)}"
        url = f"{base}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
        _, data = http_json("GET", url)
        try:
            item = data["items"][0]
            batch = item["requisitionList"]
        except (KeyError, IndexError, TypeError):
            raise AdapterError("unexpected Oracle response shape (no items[0].requisitionList)")
        if total is None:
            total = item.get("TotalJobsCount")
        jobs += [normalize_oracle(x, host, site, company) for x in batch]
        if len(batch) < page_size:
            break
        time.sleep(0.5)
    return jobs, total


# --------------------------------------------------------------------------- #
# Eightfold adapter (e.g. Lockheed Martin: lockheedmartin.eightfold.ai)
# Eightfold has two public job APIs; we try /api/apply/v2 first, then /api/pcsx, and remember which works.
# --------------------------------------------------------------------------- #
_EF_API: dict[str, str] = {}   # host -> "apply" | "pcsx"


def _epoch_date(ts):
    try:
        ts = float(ts)
        if ts > 1e11:  # milliseconds
            ts /= 1000
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).date() if ts > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _loc_name(x) -> str:
    if isinstance(x, dict):
        return str(x.get("descriptor") or x.get("name") or x.get("location") or "")
    return str(x or "")


def normalize_eightfold(p: dict, host: str, company: str = "") -> dict:
    pid = str(p.get("id") or "")
    raw_locs = p.get("locations") or p.get("standardizedLocations") or ([p["location"]] if p.get("location") else [])
    url = p.get("canonical_url") or p.get("positionUrl") or (f"/careers/job/{pid}" if pid else "")
    if url.startswith("/"):
        url = f"https://{host}{url}"
    job = _finish({
        "company": company,
        "id": str(p.get("display_job_id") or p.get("displayJobId") or p.get("ats_job_id") or pid),
        "title": (p.get("name") or "").strip(),
        "location": "; ".join(n for n in (_loc_name(x) for x in raw_locs) if n),
        "posted": _epoch_date(p.get("t_create") or p.get("t_update") or p.get("postedTs") or p.get("creationTs")),
        "url": url,
    })
    job["ext_id"] = pid
    return job


def _ef_fetch_page(host: str, domain: str, start: int, page_size: int, search: str):
    """One page of positions -> (positions, total). Tries both API flavours; remembers the one that works."""
    ref = {"Referer": f"https://{host}/careers"}
    apis = [_EF_API[host]] if host in _EF_API else ["apply", "pcsx"]
    errors = []
    for api in apis:
        try:
            if api == "apply":
                params = {"domain": domain, "start": start, "num": page_size, "sort_by": "timestamp"}
                if search:
                    params["query"] = search
                _, data = http_json("GET", f"https://{host}/api/apply/v2/jobs?{urllib.parse.urlencode(params)}", extra=ref)
                positions = data.get("positions") if isinstance(data, dict) else None
                if positions is None:
                    raise AdapterError("response has no 'positions'")
                total = data.get("count")
            else:
                params = {"domain": domain, "query": search, "location": "", "start": start, "sort_by": "timestamp"}
                _, data = http_json("GET", f"https://{host}/api/pcsx/search?{urllib.parse.urlencode(params)}", extra=ref)
                body = data.get("data") if isinstance(data, dict) else None
                positions = body.get("positions") if isinstance(body, dict) else None
                if positions is None:
                    raise AdapterError("response has no 'data.positions'")
                total = body.get("count")
            _EF_API[host] = api
            return positions, total
        except AdapterError as e:
            errors.append(f"{api}: {e}")
    raise AdapterError("Eightfold API failed (" + "; ".join(errors) + ")")


def eightfold_list(host: str, domain: str, search: str = "", pages: int = 2,
                   page_size: int = 50, company: str = "", max_jobs: int = 200):
    jobs, total, start = [], None, 0
    for _ in range(pages):
        positions, count = _ef_fetch_page(host, domain, start, page_size, search)
        if total is None:
            total = count
        if not positions:
            break
        jobs += [normalize_eightfold(x, host, company) for x in positions]
        start += len(positions)  # the server may cap page size below what we asked for
        if (isinstance(total, int) and start >= total) or len(jobs) >= max_jobs:
            break
        time.sleep(0.5)
    return jobs, total


def eightfold_detail(host: str, domain: str, ext_id: str) -> dict:
    ref = {"Referer": f"https://{host}/careers"}
    order = sorted(["apply", "pcsx"], key=lambda a: a != _EF_API.get(host))  # known-good API first
    errors = []
    for api in order:
        try:
            if api == "apply":
                q = urllib.parse.urlencode({"domain": domain})
                _, data = http_json("GET", f"https://{host}/api/apply/v2/jobs/{ext_id}?{q}", extra=ref)
                d = data if isinstance(data, dict) else {}
            else:
                q = urllib.parse.urlencode({"position_id": ext_id, "domain": domain, "hl": "en"})
                _, data = http_json("GET", f"https://{host}/api/pcsx/position_details?{q}", extra=ref)
                d = data.get("data") if isinstance(data, dict) else {}
                d = d if isinstance(d, dict) else {}
            desc = d.get("job_description") or d.get("jobDescription") or d.get("description") or ""
            if not desc:
                raise AdapterError("no description field")
            locs = [n for n in (_loc_name(x) for x in (d.get("locations") or [])) if n]
            return {"description_html": desc, "locations": locs}
        except AdapterError as e:
            errors.append(f"{api}: {e}")
    raise AdapterError("Eightfold detail failed (" + "; ".join(errors) + ")")


def http_raw(url: str, timeout: int = 30, method: str = "GET", body=None, headers=None):
    """Diagnostics only: (status, final_url, content_type, first bytes of body), never raises on HTTP errors."""
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.headers.get("Content-Type", ""), r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, url, (e.headers.get("Content-Type", "") if e.headers else ""), e.read(500).decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, url, "", f"network error: {e.reason}"


def diagnose_workday(host: str, site: str) -> None:
    tenant = host.split(".")[0]
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    origin = {"Origin": f"https://{host}", "Referer": f"https://{host}/{site}"}
    full = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    tests = [
        ("standard request", dict(body=full, headers=origin)),
        ("without Origin/Referer", dict(body=full)),
        ("bare body (limit/offset only)", dict(body={"limit": 20, "offset": 0})),
        ("smaller page (limit 10)", dict(body={**full, "limit": 10})),
        ("Windows Chrome user-agent", dict(body=full, headers={**origin, "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})),
    ]
    print(f"Diagnosing Workday site {host} / {site}")
    for label, kw in tests:
        status, final, ctype, text = http_raw(url, method="POST", **kw)
        print(f"\n[{label}] HTTP {status}  type={ctype or '?'}")
        print(f"   {re.sub(r'\s+', ' ', text)[:220]}")
    print("\n[browser-like session: cookies + CSRF token]")
    try:
        opener, token, notes = _wd_session(host, site)
        for n in notes:
            print(f"   {n}")
        data = _wd_session_post(opener, url, full, _wd_session_headers(host, site, token))
        print(f"   POST with cookies + CSRF header: OK, {len(data.get('jobPostings', [])) if isinstance(data, dict) else '?'} "
              f"postings, total={data.get('total') if isinstance(data, dict) else '?'}")
    except AdapterError as e:
        print(f"   POST with cookies + CSRF header failed: {e}")
    status, final, ctype, text = http_raw(f"https://{host}/{site}")
    print(f"\n[careers page] HTTP {status}  type={ctype or '?'}" + (f"  redirected to {final}" if final != f"https://{host}/{site}" else ""))
    print(f"   {re.sub(r'\s+', ' ', text)[:220]}")


def diagnose_eightfold(host: str, domain: str) -> None:
    enc = urllib.parse.urlencode
    tests = [
        ("apply/v2 jobs", f"https://{host}/api/apply/v2/jobs?{enc({'domain': domain, 'start': 0, 'num': 3})}"),
        ("pcsx search", f"https://{host}/api/pcsx/search?{enc({'domain': domain, 'query': '', 'location': '', 'start': 0, 'sort_by': 'timestamp'})}"),
        ("careers page", f"https://{host}/careers"),
    ]
    print(f"Diagnosing Eightfold host {host} (domain={domain})")
    for label, url in tests:
        status, final, ctype, body = http_raw(url)
        snippet = re.sub(r"\s+", " ", body)[:220]
        print(f"\n[{label}] HTTP {status}  type={ctype or '?'}" + (f"  redirected to {final}" if final != url else ""))
        print(f"   {snippet}")


# --------------------------------------------------------------------------- #
# Greenhouse adapter (public job-board API; e.g. SpaceX: boards.greenhouse.io/spacex)
# One request returns the whole board. Locations are free text ("Hawthorne, CA"), so US detection is by text.
# --------------------------------------------------------------------------- #
_US_CODES = ("AL AK AZ AR CA CO CT FL GA HI ID IL IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
             "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()   # IN and DE left out: also India/Germany
_US_CODE_RE = re.compile(r"(?:,|\s[-\u2013]|\()\s*(" + "|".join(_US_CODES) + r")\b(?!\s*,?\s*Canada)")
_US_NAME_RE = re.compile(
    r"\b(Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Hawaii|Idaho|Illinois|"
    r"Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|"
    r"Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|"
    r"Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|"
    r"Virginia|Washington|West Virginia|Wisconsin|Wyoming)\b")


def is_us_location(text: str) -> bool:
    t = text or ""
    if re.search(r"\bunited states\b|\bUSA\b|\bU\.S\.", t, re.I):
        return True
    return bool(_US_CODE_RE.search(t) or _US_NAME_RE.search(t))


_NON_US_RE = re.compile(
    r"\b(India|Germany|United Kingdom|UK|England|Scotland|Wales|Ireland|Canada|Mexico|Brazil|Poland|France|Spain|"
    r"Italy|Netherlands|Belgium|Sweden|Norway|Denmark|Finland|Switzerland|Austria|Czech|Romania|Hungary|Israel|"
    r"Turkey|UAE|Saudi|Qatar|Egypt|South Africa|Australia|New Zealand|Japan|Korea|China|Taiwan|Singapore|"
    r"Malaysia|Philippines|Thailand|Vietnam|Indonesia|Argentina|Chile|Colombia|Peru)\b", re.I)


def looks_non_us(text: str) -> bool:
    """True only when the text names a non-US country and nothing US-looking."""
    return bool(text) and not is_us_location(text) and bool(_NON_US_RE.search(text))


def gh_is_us(c: dict, text: str) -> bool:
    """US test for a Greenhouse board; c may carry a 'us_hint' regex for boards with pseudo-locations."""
    hint = c.get("us_hint")
    return is_us_location(text) or bool(hint and re.search(hint, text or "", re.I))


def _meta_values(p: dict) -> list[tuple[str, str]]:
    out = []
    for m in p.get("metadata") or []:
        v = m.get("value")
        out += [(str(m.get("name") or ""), str(x)) for x in (v if isinstance(v, list) else [v]) if x]
    return out


def normalize_greenhouse(p: dict, company: str = "") -> dict:
    jid = str(p.get("id") or "")
    meta = _meta_values(p)
    job = _finish({
        "company": company,
        "id": jid,
        "title": (p.get("title") or "").strip(),
        "location": ((p.get("location") or {}).get("name") or "").strip(),
        "posted": _iso_date(p.get("first_published") or p.get("updated_at")),
        "url": p.get("absolute_url") or "",
    })
    job["ext_id"] = jid
    if any("employment" in n.lower() and INTERNISH.search(v) for n, v in meta):
        job["level"] = "intern"          # board's own "Employment Type: Intern"
    if any("engineer" in v.lower() for _, v in meta):
        job["engineering"] = True        # board's own "Job Discipline: Engineering - ..."
    return job


def greenhouse_list(board: str, search: str = "", pages: int = 1, company: str = ""):
    """pages is unused (the board comes back in one response); search filters titles client-side."""
    _, data = http_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                        extra={"Referer": f"https://boards.greenhouse.io/{board}"})
    raw = data.get("jobs") if isinstance(data, dict) else None
    if raw is None:
        raise AdapterError("unexpected Greenhouse response shape (no 'jobs') - is the board name right?")
    jobs = [normalize_greenhouse(p, company) for p in raw]
    if search:
        jobs = [j for j in jobs if search.lower() in j["title"].lower()]
    return jobs, (data.get("meta") or {}).get("total", len(raw))


def greenhouse_detail(board: str, job_id: str) -> dict:
    _, data = http_json("GET", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}",
                        extra={"Referer": f"https://boards.greenhouse.io/{board}"})
    content = data.get("content") if isinstance(data, dict) else None
    if not content:
        raise AdapterError("no 'content' in Greenhouse job response")
    return {"description_html": content, "locations": []}   # content is HTML-escaped; experience_signal unescapes it


# --------------------------------------------------------------------------- #
# Multi-company layer used by the Discord bot
# --------------------------------------------------------------------------- #
COUNTRY_US = "United States of America"
MAX_PAGES_PER_COMPANY = 8      # newest ~160 (Workday) / ~200 (Oracle) postings per company per poll
MAX_DETAIL_FETCHES = 30        # description fetches per poll (only for new, relevant, unclear jobs)
INTERNISH = re.compile(r"intern|co-?op|student|apprentice", re.I)
MULTI_LOC = re.compile(r"\b\d+\s+locations?\b", re.I)

# Add companies here (see the docstring for how to get the Workday host/site).
COMPANIES = [
    {"name": "RTX", "provider": "phenom"},
    {"name": "Alcon", "provider": "workday",
     "host": "alcon.wd5.myworkdayjobs.com", "site": "careers_alcon"},
    {"name": "Rockwell Automation", "provider": "workday",
     "host": "rockwellautomation.wd1.myworkdayjobs.com", "site": "External_Rockwell_Automation"},
    {"name": "Texas Instruments", "provider": "oracle",
     "host": "edbz.fa.us2.oraclecloud.com", "site": "CX", "site_number": "CX_1"},
    {"name": "Boeing", "provider": "workday",
     "host": "boeing.wd1.myworkdayjobs.com", "site": "EXTERNAL_CAREERS"},
    # Parked: Dell's Workday site rejects every automated request style we tried (HTTP 422, empty message).
    # To retry later, uncomment and run `py test_adapters.py --company dell --diagnose`.
    # {"name": "Dell Technologies", "provider": "workday",
    #  "host": "dell.wd1.myworkdayjobs.com", "site": "External"},
    {"name": "General Motors", "provider": "workday",
     "host": "generalmotors.wd5.myworkdayjobs.com", "site": "Careers_GM"},
    {"name": "NXP Semiconductors", "provider": "workday",
     "host": "nxp.wd3.myworkdayjobs.com", "site": "careers"},
    # HPE runs several Workday sites on this host (Jobsathpe, WFMathpe, acjobsite). Jobsathpe is the one
    # its external postings use; check the probe's job count against hpe.com/careers.
    {"name": "Hewlett Packard Enterprise", "provider": "workday",
     "host": "hpe.wd5.myworkdayjobs.com", "site": "Jobsathpe"},
    {"name": "SpaceX", "provider": "greenhouse", "board": "spacex", "us_hint": r"any spacex site"},
    {"name": "Rocket Lab", "provider": "greenhouse", "board": "rocketlab"},
    {"name": "Blue Origin", "provider": "workday",
     "host": "blueorigin.wd5.myworkdayjobs.com", "site": "BlueOrigin"},
    {"name": "Northrop Grumman", "provider": "workday",
     "host": "ngc.wd1.myworkdayjobs.com", "site": "Northrop_Grumman_External_Site"},
    # Parked: Lockheed moved to Eightfold, but its server did not cooperate. To retry, uncomment and run
    # `py test_adapters.py --company lockheed --diagnose`.
    # {"name": "Lockheed Martin", "provider": "eightfold",
    #  "host": "lockheedmartin.eightfold.ai", "domain": "lockheedmartin.com"},
]


def company_names() -> list[str]:
    return [c["name"] for c in COMPANIES]


def _watcher():
    """Your RTX watcher script (rtx_job_watcher.py, or renamed rtx.py) in the same folder."""
    try:
        import rtx_job_watcher as w
    except ImportError:
        import rtx as w
    return w


def _fetch_workday(c: dict, max_pages: int) -> list[dict]:
    host, site = c["host"], c["site"]
    fac = workday_facets(host, site)
    us = find_country_facet(fac)
    applied = {us[0]: [us[1]]} if us else {}
    jobs, _ = workday_list(host, site, "", max_pages, company=c["name"], applied=applied)
    intern_ids = [v["id"] for v in fac.get("workerSubType", []) if INTERNISH.search(v.get("descriptor") or "")]
    if intern_ids:  # some tenants (e.g. Alcon) have a real worker-type filter: use it for interns
        ij, _ = workday_list(host, site, "", max_pages, company=c["name"],
                             applied={**applied, "workerSubType": intern_ids})
        ids = {j["id"] for j in ij}
        for j in jobs:
            if j["id"] in ids:
                j["level"] = "intern"
    if not us:  # no country facet on this site: fall back to the location text
        jobs = [j for j in jobs if not looks_non_us(j["location"])]
    for j in jobs:
        j["src"] = c
    return jobs


def _fetch_oracle(c: dict, max_pages: int) -> list[dict]:
    jobs, _ = oracle_list(c["host"], c["site"], c["site_number"], "", max_pages, company=c["name"])
    return [j for j in jobs if "united states" in j["location"].lower()]


def _fetch_phenom(c: dict, max_pages: int) -> list[dict]:
    w = _watcher()
    out = []
    for site_level, level in (("Intern", "intern"), ("Entry Level", "early")):
        res, _ = w.fetch_jobs("", False, max_pages, {"experienceLevel": [site_level], "country": [COUNTRY_US]})
        for j in res:
            if j["experience"] != site_level or COUNTRY_US not in j["country"]:
                continue
            job = _finish({"company": c["name"], "id": j["id"], "title": j["title"],
                           "location": j["location"], "posted": j["posted"], "url": j["url"]})
            job["level"] = level
            job["engineering"] = "engineering" in (j.get("category") or "").lower() or is_engineering(j["title"])
            out.append(job)
    return out


def _fetch_greenhouse(c: dict, max_pages: int) -> list[dict]:
    jobs, _ = greenhouse_list(c["board"], "", 1, company=c["name"])
    us = [j for j in jobs if gh_is_us(c, j["location"])]
    for j in us:
        j["src"] = c
    return us


def _fetch_eightfold(c: dict, max_pages: int) -> list[dict]:
    jobs, _ = eightfold_list(c["host"], c["domain"], "", max_pages * 3, company=c["name"], max_jobs=200)
    us = [j for j in jobs if re.search(r"united states|\busa\b", j["location"], re.I)]
    for j in us:
        j["src"] = c
    return us


def fetch_company_jobs(c: dict, max_pages: int | None = None) -> list[dict]:
    """US-only jobs for one company. Raises AdapterError on failure."""
    mp = max_pages or MAX_PAGES_PER_COMPANY
    fn = {"workday": _fetch_workday, "oracle": _fetch_oracle, "phenom": _fetch_phenom,
          "eightfold": _fetch_eightfold, "greenhouse": _fetch_greenhouse}.get(c["provider"])
    if not fn:
        raise AdapterError(f"unknown provider {c['provider']!r}")
    jobs = fn(c, mp)
    return [j for j in jobs if j["id"] and j["title"] and j["url"]]


def effective_level(job: dict, max_years: int = 2) -> str:
    """Title/site label first; otherwise the stated experience requirement (<= max_years -> early)."""
    sig = {"min_years": job.get("min_years"), "entry_phrases": job.get("entry_words", False)}
    return infer_level(job["level"], sig, max_years)


def enrich_jobs(jobs: list[dict], max_fetches: int = MAX_DETAIL_FETCHES) -> int:
    """Read job descriptions where the title leaves the level unclear (Workday also when the location
    says 'N Locations'); fills min_years / entry_words / fuller location. Returns #fetches made."""
    done = 0
    for j in jobs:
        src = j.get("src")
        if not src:
            continue
        prov = src.get("provider")
        if prov == "workday":
            if not j.get("external_path") or not (j["level"] == "unknown" or MULTI_LOC.search(j["location"])):
                continue
        elif prov in ("eightfold", "greenhouse"):
            if not j.get("ext_id") or j["level"] != "unknown":
                continue
        else:
            continue
        if done >= max_fetches:
            break
        try:
            if prov == "workday":
                d = workday_detail(src["host"], src["site"], j["external_path"])
            elif prov == "greenhouse":
                d = greenhouse_detail(src["board"], j["ext_id"])
            else:
                d = eightfold_detail(src["host"], src["domain"], j["ext_id"])
        except AdapterError as e:
            print(f"[warn] detail fetch failed for {j['title'][:40]!r}: {e}", file=sys.stderr)
            continue
        done += 1
        sig = experience_signal(d["description_html"])
        j["min_years"], j["entry_words"] = sig["min_years"], sig["entry_phrases"]
        raw_locs = ([d["location"], *d["additional_locations"]] if prov == "workday" else d["locations"])
        locs: list[str] = []
        for x in raw_locs:
            name = x.get("descriptor") if isinstance(x, dict) else x
            if name and str(name) not in locs:
                locs.append(str(name))
        if locs:
            j["location"] = "; ".join(locs)
        if prov == "workday" and d["country"] and "united states" not in d["country"].lower():
            j["drop"] = True
        time.sleep(0.3)
    return done