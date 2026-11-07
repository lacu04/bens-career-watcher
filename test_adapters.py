#!/usr/bin/env python3
"""
test_adapters.py - prototype Workday + Oracle HCM job-feed adapters, with tests.

Live test (run on your own machine; hits the real career sites, a few requests each):
  py test_adapters.py                          # all companies configured below
  py test_adapters.py --company rockwell       # just one (substring match)
  py test_adapters.py --probe                  # step 2: facets, US filter, description-based level
  py test_adapters.py --workday-url "<any Workday job or search URL>"
  py test_adapters.py --oracle-url  "<any Oracle careers URL>" --site-number CX_1

Offline test (no network; checks the parsing + classifier logic):
  py test_adapters.py --selftest

Each company gets PASS / WARN / FAIL lines. Exit code is 1 if anything FAILs.
Stdlib only.
"""
import argparse
import datetime as dt
import json
import re
import sys
import time

import ats_adapters as A
from ats_adapters import *  # noqa: F401,F403  (adapter functions used below)

# Workday / Oracle companies from the shared config (RTX/phenom is tested by your watcher script).
COMPANIES = [c for c in A.COMPANIES if c["provider"] in ("workday", "oracle", "eightfold", "greenhouse")]

# --------------------------------------------------------------------------- #
# Test runner
# --------------------------------------------------------------------------- #

def fetch_for(c: dict, search: str = "", pages: int = 2):
    if c["provider"] == "workday":
        return workday_list(c["host"], c["site"], search, pages, company=c["name"])
    if c["provider"] == "oracle":
        return oracle_list(c["host"], c["site"], c["site_number"], search, pages, company=c["name"])
    if c["provider"] == "eightfold":
        return eightfold_list(c["host"], c["domain"], search, pages, company=c["name"])
    if c["provider"] == "greenhouse":
        return greenhouse_list(c["board"], search, pages, company=c["name"])
    raise AdapterError(f"unknown provider {c['provider']!r}")


def test_company(c: dict, pages: int = 2) -> bool:
    print(f"\n=== {c['name']} ({c['provider']}) - {c.get('host') or c.get('board')} ===")
    results = []

    def report(status, name, detail=""):
        results.append(status)
        print(f"  {status:4}  {name}" + (f" - {detail}" if detail else ""))

    # 1. connectivity + shape
    try:
        jobs, total = fetch_for(c, "", pages)
    except AdapterError as e:
        report("FAIL", "fetch jobs", str(e))
        return False
    report("PASS", "fetch jobs", f"{len(jobs)} fetched, site reports total={total}")

    # 2. non-empty
    if not jobs:
        report("FAIL", "returns at least one job")
        return False
    report("PASS", "returns at least one job")

    # 3. required fields
    bad = [j for j in jobs if not (j["id"] and j["title"] and j["url"])]
    if bad:
        # a stray odd posting is tolerated (the bot skips it); many bad ones mean the adapter is wrong
        status = "FAIL" if len(bad) > max(1, len(jobs) // 10) else "WARN"
        first = {k: bad[0].get(k) for k in ("id", "title", "url", "external_path", "location")}
        report(status, "every job has id, title and url", f"{len(bad)} incomplete (bot skips these); first: {first}")
    else:
        report("PASS", "every job has id, title and url")

    # 4. unique ids (pagination sanity)
    ids = [j["id"] for j in jobs]
    report("FAIL" if len(ids) != len(set(ids)) else "PASS", "no duplicate ids across pages",
           f"{len(ids) - len(set(ids))} duplicates" if len(ids) != len(set(ids)) else "")

    # 5. posted dates
    dated = sum(1 for j in jobs if j["posted"])
    report("PASS" if dated else "WARN", "posted dates parsed", f"{dated}/{len(jobs)}")

    # 6. keyword search actually filters
    try:
        hits, _ = fetch_for(c, "intern", 1)
        match = sum(1 for j in hits if j["level"] == "intern")
        if not hits:
            report("WARN", "keyword search 'intern'", "no results (may just mean none open)")
        else:
            report("PASS", "keyword search 'intern'", f"{len(hits)} results, {match} classified as intern")
    except AdapterError as e:
        report("FAIL", "keyword search 'intern'", str(e))

    # 7. classifier spread
    counts = {k: sum(1 for j in jobs if j["level"] == k) for k in ("intern", "early", "experienced", "unknown")}
    eng = sum(1 for j in jobs if j["engineering"])
    report("PASS" if counts["intern"] + counts["early"] + counts["experienced"] else "WARN",
           "level classifier", f"{counts}, engineering-like titles: {eng}/{len(jobs)}")

    print("  newest jobs:")
    for j in sorted(jobs, key=lambda j: j["posted"] or dt.date.min, reverse=True)[:5]:
        print(f"    {str(j['posted'] or '?'):10} {j['level']:11} {j['title'][:55]:55} {j['location'][:28]}")
    ok = "FAIL" not in results
    print(f"  => {'OK' if ok else 'NEEDS ATTENTION'}")
    return ok

# --------------------------------------------------------------------------- #
# Probe (step 2): facets, US filter, description-based level inference
# --------------------------------------------------------------------------- #

def probe_company(c: dict, samples: int = 4) -> bool:
    print(f"\n=== PROBE {c['name']} ({c['provider']}) ===")
    ok = True
    if c["provider"] == "workday":
        host, site = c["host"], c["site"]
        try:
            fac = workday_facets(host, site)
        except AdapterError as e:
            print(f"  FAIL  facets - {e}")
            return False
        print(f"  PASS  facets available: { {k: len(v) for k, v in fac.items()} }")
        for k in ("workerSubType", "timeType"):
            if k in fac:
                print(f"        {k}: " + ", ".join(f"{v['descriptor']} ({v['count']})" for v in fac[k][:8]))
        us = find_country_facet(fac)
        applied = None
        if us:
            try:
                _, t_all = workday_list(host, site, "", 1, company=c["name"])
                _, t_us = workday_list(host, site, "", 1, company=c["name"], applied={us[0]: [us[1]]})
                good = isinstance(t_us, int) and 0 < t_us <= (t_all or 10**9)
                print(f"  {'PASS' if good else 'WARN'}  US country filter ({us[0]}): total {t_all} -> {t_us}")
                if good:
                    applied = {us[0]: [us[1]]}
            except AdapterError as e:
                print(f"  FAIL  US country filter - {e}")
                ok = False
        else:
            print("  INFO  no US country facet on this site (see the location-text fallback below)")
        try:
            jobs, _ = workday_list(host, site, "", 2, company=c["name"], applied=applied)
        except AdapterError as e:
            print(f"  FAIL  list jobs - {e}")
            return False
        if not us:
            foreign = [j["location"] for j in jobs if looks_non_us(j["location"])]
            print(f"  INFO  no country facet, so the bot filters on location text: it would drop "
                  f"{len(foreign)}/{len(jobs)} of the first jobs"
                  + (f" (e.g. {sorted(set(foreign))[:4]})" if foreign else "")
                  + f". Sample location texts: {sorted({j['location'] for j in jobs})[:5]}")
        cand = [j for j in jobs if j["level"] == "unknown" and j["engineering"]][:samples]
        detail_ok = 0
        for j in cand:
            try:
                d = workday_detail(host, site, j["external_path"])
            except AdapterError as e:
                print(f"  FAIL  detail for {j['title'][:40]!r} - {e}")
                ok = False
                continue
            detail_ok += 1
            sig = experience_signal(d["description_html"])
            print(f"    {j['title'][:42]:42} {d['country'][:14] or '?':14} min_years={str(sig['min_years']):4} "
                  f"entry_words={str(sig['entry_phrases']):5} -> {infer_level('unknown', sig)}")
        if cand:
            print(f"  {'PASS' if detail_ok else 'FAIL'}  job detail + experience parsing ({detail_ok}/{len(cand)})")
        else:
            print("  (no unknown-level engineering titles in the first 40 jobs to sample)")
    elif c["provider"] == "eightfold":
        try:
            positions, _ = A._ef_fetch_page(c["host"], c["domain"], 0, 3, "")
            first = positions[0] if positions else {}
            print(f"  PASS  Eightfold API flavour that works: {A._EF_API.get(c['host'])!r}; "
                  f"position keys: {sorted(first.keys())}")
            jobs, total = fetch_for(c, "", 2)
        except AdapterError as e:
            print(f"  FAIL  list jobs - {e}")
            return False
        us = sum(bool(re.search(r"united states|\busa\b", j["location"], re.I)) for j in jobs)
        dated = [j["posted"] for j in jobs if j["posted"]]
        newest_first = dated == sorted(dated, reverse=True)
        print(f"  PASS  {len(jobs)} jobs fetched (site reports {total}), {us} in the US")
        print(f"  {'PASS' if newest_first else 'WARN'}  results come back newest-first"
              + ("" if newest_first else " - NOT sorted; the bot could miss new postings"))
        cand = [j for j in jobs if j["level"] == "unknown" and j["engineering"]][:samples]
        got = 0
        for j in cand:
            try:
                d = eightfold_detail(c["host"], c["domain"], j["ext_id"])
            except AdapterError as e:
                print(f"  FAIL  detail for {j['title'][:40]!r} - {e}")
                ok = False
                continue
            got += 1
            sig = experience_signal(d["description_html"])
            print(f"    {j['title'][:45]:45} min_years={str(sig['min_years']):4} "
                  f"entry_words={str(sig['entry_phrases']):5} -> {infer_level('unknown', sig)}")
        if cand:
            print(f"  {'PASS' if got else 'FAIL'}  job detail + experience parsing ({got}/{len(cand)})")
    elif c["provider"] == "greenhouse":
        try:
            jobs, total = fetch_for(c, "", 1)
        except AdapterError as e:
            print(f"  FAIL  list jobs - {e}")
            return False
        us_jobs = [j for j in jobs if gh_is_us(c, j["location"])]
        print(f"  PASS  {len(jobs)} jobs on the board (site reports {total}), {len(us_jobs)} look US by location text")
        other = sorted({j["location"] for j in jobs if not gh_is_us(c, j["location"])})[:6]
        if other:
            print(f"        non-US / unrecognized locations, sample: {other}")
        counts = {k: sum(1 for j in us_jobs if j["level"] == k) for k in ("intern", "early", "experienced", "unknown")}
        print(f"  PASS  US jobs by title/employment-type level: {counts}")
        cand = [j for j in us_jobs if j["level"] == "unknown" and j["engineering"]][:samples]
        got = 0
        for j in cand:
            try:
                d = greenhouse_detail(c["board"], j["ext_id"])
            except AdapterError as e:
                print(f"  FAIL  detail for {j['title'][:40]!r} - {e}")
                ok = False
                continue
            got += 1
            sig = experience_signal(d["description_html"])
            print(f"    {j['title'][:45]:45} min_years={str(sig['min_years']):4} "
                  f"entry_words={str(sig['entry_phrases']):5} -> {infer_level('unknown', sig)}")
        if cand:
            print(f"  {'PASS' if got else 'FAIL'}  job detail + experience parsing ({got}/{len(cand)})")
    else:
        try:
            jobs, total = fetch_for(c, "", 2)
        except AdapterError as e:
            print(f"  FAIL  list jobs - {e}")
            return False
        us = sum("united states" in j["location"].lower() for j in jobs)
        print(f"  {'PASS' if us else 'WARN'}  country readable from location text: {us}/{len(jobs)} are US "
              f"(client-side filter works if this is plausible)")
        early = [j for j in jobs if j["level"] in ("early", "intern")]
        print(f"  early/intern by title: {len(early)}/{len(jobs)}")
    print(f"  => {'OK' if ok else 'NEEDS ATTENTION'}")
    return ok


# --------------------------------------------------------------------------- #
# Offline self-test (no network)
# --------------------------------------------------------------------------- #

def selftest() -> bool:
    failures = []

    def expect(label, got, want):
        if got != want:
            failures.append(f"{label}: got {got!r}, wanted {want!r}")

    cases = {
        "Summer 2027 Mechanical Engineering Intern": "intern",
        "Co-op - Electrical Engineering": "intern",
        "Engineer I, Controls": "early",
        "Associate Engineer": "early",
        "Leadership Development Program - Supply Chain": "early",
        "Engineering Development Program": "early",
        "Senior Software Engineer": "experienced",
        "Principal Systems Engineer": "experienced",
        "Software Engineer II": "experienced",
        "Manufacturing Technician": "unknown",
    }
    for title, want in cases.items():
        expect(f"classify {title!r}", classify_level(title), want)
    expect("engineering flag", is_engineering("Controls Engineer I"), True)
    expect("non-engineering flag", is_engineering("Payroll Specialist"), False)

    today = dt.date(2026, 11, 6)
    expect("posted today", parse_workday_posted("Posted Today", today), today)
    expect("posted yesterday", parse_workday_posted("Posted Yesterday", today), dt.date(2026, 11, 5))
    expect("posted 5 days", parse_workday_posted("Posted 5 Days Ago", today), dt.date(2026, 11, 1))
    expect("posted 30+ days", parse_workday_posted("Posted 30+ Days Ago", today), dt.date(2026, 10, 7))
    expect("posted junk", parse_workday_posted("", today), None)

    wd = normalize_workday({"title": "Engineer I", "externalPath": "/job/Milwaukee/Engineer-I_R1",
                            "locationsText": "Milwaukee, WI", "postedOn": "Posted Today",
                            "bulletFields": ["R1"]}, "x.wd1.myworkdayjobs.com", "Site", "Acme")
    expect("workday id", wd["id"], "R1")
    expect("workday url", wd["url"], "https://x.wd1.myworkdayjobs.com/en-US/Site/job/Milwaukee/Engineer-I_R1")
    orc = normalize_oracle({"Id": 4242, "Title": "Systems Engineer Intern", "PostedDate": "2026-11-05",
                            "PrimaryLocation": "Dallas, TX"}, "h.oraclecloud.com", "CX", "Acme")
    expect("oracle level", orc["level"], "intern")
    expect("oracle url", orc["url"], "https://h.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/4242")

    # Full runner against canned responses (pagination + search + both providers)
    def fake(method, url, body=None, extra=None, timeout=30):
        if "wday/cxs" in url:
            off = body["offset"]
            if body["searchText"] == "intern":
                posts = [{"title": "Engineering Intern", "externalPath": "/job/a/I_R9", "bulletFields": ["R9"],
                          "locationsText": "Austin, TX", "postedOn": "Posted Today"}]
            else:
                posts = [{"title": f"Engineer {i}", "externalPath": f"/job/a/E_{i}", "bulletFields": [f"R{i}"],
                          "locationsText": "Austin, TX", "postedOn": "Posted 3 Days Ago"}
                         for i in range(off, off + 20)]
            return 200, {"total": 100, "jobPostings": posts}
        reqs = [{"Id": 100 + i, "Title": "Controls Engineer I" if i % 2 else "Co-op Engineer",
                 "PostedDate": "2026-11-05", "PrimaryLocation": "Dallas, TX"} for i in range(3)]
        return 200, {"items": [{"TotalJobsCount": 3, "requisitionList": reqs}]}

    saved = A.http_json
    A.http_json = fake
    try:
        quiet_ok = []
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            quiet_ok.append(test_company({"name": "W", "provider": "workday", "host": "w.wd1.myworkdayjobs.com",
                                          "site": "S"}))
            quiet_ok.append(test_company({"name": "O", "provider": "oracle", "host": "o.oraclecloud.com",
                                          "site": "CX", "site_number": "CX_1"}))
        expect("runner passes on healthy mock feeds", quiet_ok, [True, True])
        jobs, _ = workday_list("w.wd1.myworkdayjobs.com", "S", pages=2)
        expect("workday pagination gives 40 jobs", len(jobs), 40)
        expect("workday ids unique", len({j["id"] for j in jobs}), 40)
    finally:
        A.http_json = saved

    # Runner must FAIL on a broken feed
    def broken(method, url, body=None, extra=None, timeout=30):
        return 200, {"unexpected": True}
    A.http_json = broken
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            bad = test_company({"name": "B", "provider": "workday", "host": "b.wd1.myworkdayjobs.com", "site": "S"})
        expect("runner fails on wrong response shape", bad, False)
    finally:
        A.http_json = saved

    # --- step 2: facets, experience parsing, probe runner ---
    sample_facets = [
        {"facetParameter": "locationMainGroup", "descriptor": "Location", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country", "values": [
                {"descriptor": "United States of America", "id": "US1", "count": 5},
                {"descriptor": "Mexico", "id": "MX1", "count": 2}]},
            {"facetParameter": "locations", "descriptor": "Locations", "values": [
                {"descriptor": "Austin", "id": "A1", "count": 3}]}]},
        {"facetParameter": "workerSubType", "descriptor": "Worker Type", "values": [
            {"descriptor": "Intern", "id": "W1", "count": 4}]}]
    fac = walk_facets(sample_facets)
    expect("facet US id", find_country_id(fac), "US1")
    expect("facet worker types", [v["descriptor"] for v in fac.get("workerSubType", [])], ["Intern"])

    exp_cases = [
        ("Bachelor's degree and 0-2 years of experience in controls", 0, "early"),
        ("Minimum of 5 years of relevant experience required", 5, "experienced"),
        ("3+ years of professional engineering experience", 3, "experienced"),
        ("Recent graduates welcome. 1 year of experience preferred", 1, "early"),
        ("We offer a 5 years of service award and 4 weeks vacation", None, "unknown"),
        ("<p>Entry level position. Degree required.</p>", None, "early"),
    ]
    for text, want_min, want_level in exp_cases:
        sig = experience_signal(text)
        expect(f"min_years {text[:28]!r}", sig["min_years"], want_min)
        expect(f"inferred level {text[:28]!r}", infer_level("unknown", sig), want_level)
    expect("title label wins", infer_level("experienced", experience_signal("entry level")), "experienced")
    expect("career accelerator", classify_level("Career Accelerator Program - Systems Engineer"), "early")

    def fake_probe(method, url, body=None, extra=None, timeout=30):
        if method == "GET" and "wday/cxs" in url:
            return 200, {"jobPostingInfo": {"jobDescription": "<p>BS degree and 0-2 years of experience.</p>",
                                            "country": {"descriptor": "United States of America"},
                                            "location": "Austin", "jobReqId": "R1"}}
        if "wday/cxs" in url:
            applied = (body or {}).get("appliedFacets") or {}
            posts = [{"title": "Controls Engineer", "externalPath": f"/job/a/C_{i}", "bulletFields": [f"R{i}"],
                      "locationsText": "2 Locations", "postedOn": "Posted Today"} for i in range(3)]
            return 200, {"total": 7 if applied else 20, "jobPostings": posts, "facets": sample_facets}
        reqs = [{"Id": 1, "Title": "Test Engineer", "PostedDate": "2026-11-05",
                 "PrimaryLocation": "Tucson, AZ, United States"}]
        return 200, {"items": [{"TotalJobsCount": 1, "requisitionList": reqs}]}

    A.http_json = fake_probe
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            p1 = probe_company({"name": "W", "provider": "workday", "host": "w.wd1.myworkdayjobs.com", "site": "S"})
            p2 = probe_company({"name": "O", "provider": "oracle", "host": "o.oraclecloud.com",
                                "site": "CX", "site_number": "CX_1"})
        expect("probe passes on healthy mock feeds", [p1, p2], [True, True])
    finally:
        A.http_json = saved

    # --- multi-company layer: fetch_company_jobs + enrich_jobs ---
    def fake_multi(method, url, body=None, extra=None, timeout=30):
        if method == "GET" and "wday/cxs" in url:
            return 200, {"jobPostingInfo": {
                "jobDescription": "<p>Requires 1 year of experience.</p>",
                "country": {"descriptor": "United States of America"}, "location": "Austin, TX",
                "additionalLocations": ["Dallas, TX", {"descriptor": "Houston, TX"}], "jobReqId": "R2"}}
        if "wday/cxs" in url:
            applied = (body or {}).get("appliedFacets") or {}
            stu = {"title": "Student Technician", "externalPath": "/job/a/S_1", "bulletFields": ["R1"],
                   "locationsText": "Austin, TX", "postedOn": "Posted Today"}
            ctl = {"title": "Controls Engineer", "externalPath": "/job/a/C_2", "bulletFields": ["R2"],
                   "locationsText": "2 Locations", "postedOn": "Posted Today"}
            posts = [stu] if "workerSubType" in applied else [stu, ctl]
            return 200, {"total": len(posts), "jobPostings": posts, "facets": sample_facets}
        reqs = [{"Id": 1, "Title": "Test Engineer", "PostedDate": "2026-11-05",
                 "PrimaryLocation": "Tucson, AZ, United States"},
                {"Id": 2, "Title": "Process Engineer", "PostedDate": "2026-11-05",
                 "PrimaryLocation": "Melaka, Malaysia"}]
        return 200, {"items": [{"TotalJobsCount": 2, "requisitionList": reqs}]}

    class FakeWatcher:
        @staticmethod
        def fetch_jobs(q, debug, max_pages, filters):
            lvl = filters["experienceLevel"][0]
            return [{"id": lvl[:1] + "1", "title": "Engineer", "location": "Tucson, Arizona, United States",
                     "country": "United States of America", "experience": lvl, "category": "Engineering",
                     "posted": dt.date.today(), "url": "u"}], 1

    saved_sleep, saved_watcher = A.time.sleep, A._watcher
    A.http_json = fake_multi
    A.time.sleep = lambda s_: None
    A._watcher = lambda: FakeWatcher
    try:
        wd_cfg = {"name": "W", "provider": "workday", "host": "w.wd1.myworkdayjobs.com", "site": "S"}
        jobs = A.fetch_company_jobs(wd_cfg)
        by = {j["title"]: j for j in jobs}
        expect("workday returns 2 jobs", len(jobs), 2)
        expect("worker-type facet marks intern", by["Student Technician"]["level"], "intern")
        expect("unlabeled title stays unknown", by["Controls Engineer"]["level"], "unknown")
        n = A.enrich_jobs(jobs)
        ctl = by["Controls Engineer"]
        expect("only the unclear/multi-location job fetched", n, 1)
        expect("min_years from description", ctl["min_years"], 1)
        expect("locations expanded", ctl["location"], "Austin, TX; Dallas, TX; Houston, TX")
        expect("effective level max_years=2", A.effective_level(ctl, 2), "early")
        expect("effective level max_years=0", A.effective_level(ctl, 0), "experienced")
        ora = A.fetch_company_jobs({"name": "O", "provider": "oracle", "host": "o.oraclecloud.com",
                                    "site": "CX", "site_number": "CX_1"})
        expect("oracle keeps only US", [j["id"] for j in ora], ["1"])
        ph = A.fetch_company_jobs({"name": "RTX", "provider": "phenom"})
        expect("phenom maps site levels", sorted(j["level"] for j in ph), ["early", "intern"])
    finally:
        A.http_json, A.time.sleep, A._watcher = saved, saved_sleep, saved_watcher

    # --- Eightfold adapter + Lockheed-style titles ---
    for title, want in {"Electronics Engineer V, Responsible Product Engineer": "experienced",
                        "Maintenance Supervisor - Level 3": "experienced",
                        "Support Chain - Material Program Management - (MPM) - F35 - Level 5": "experienced",
                        "Systems Engineer Sr": "experienced", "Engineer Vehicle Systems": "unknown",
                        "Software Engineer - Level 2": "unknown"}.items():
        expect(f"classify {title[:40]!r}", classify_level(title), want)

    ef_positions = [
        {"id": 111, "name": "Systems Engineer", "display_job_id": "733480BR", "t_create": 1730851200,
         "location": "Fort Worth, Texas, United States of America",
         "locations": ["Fort Worth, Texas, United States of America"], "canonical_url": "https://h.eightfold.ai/careers/job/111"},
        {"id": 112, "name": "Software Engineering Intern", "display_job_id": "733481BR", "t_create": 1730851200,
         "locations": ["Littleton, Colorado, United States of America"]},
        {"id": 113, "name": "Engineer", "display_job_id": "733482BR", "t_create": 1730851200,
         "locations": ["Sydney, Australia"]}]

    def fake_ef(method, url, body=None, extra=None, timeout=30):
        if "/api/apply/v2/jobs/" in url:
            return 200, {"id": 111, "job_description": "<p>0-2 years of experience preferred.</p>",
                         "locations": ["Fort Worth, Texas, United States of America",
                                       "Marietta, Georgia, United States of America"]}
        start = int(re.search(r"start=(\d+)", url).group(1))
        return 200, {"count": 3, "positions": ef_positions[start:start + 2]}   # server caps pages at 2

    saved_sleep = A.time.sleep
    A.http_json = fake_ef
    A.time.sleep = lambda s_: None
    try:
        ef_cfg = {"name": "LM", "provider": "eightfold", "host": "h.eightfold.ai", "domain": "h.com"}
        efj = A.fetch_company_jobs(ef_cfg, 5)
        expect("eightfold pagination survives server page cap + US filter", [j["id"] for j in efj],
               ["733480BR", "733481BR"])
        expect("eightfold intern by title", efj[1]["level"], "intern")
        expect("eightfold epoch date", efj[0]["posted"], dt.date(2024, 11, 6))
        n = A.enrich_jobs(efj)
        expect("eightfold enrich fetches only the unclear job", n, 1)
        expect("eightfold min_years", efj[0]["min_years"], 0)
        expect("eightfold locations expanded", efj[0]["location"].count(";"), 1)
        expect("eightfold effective level", A.effective_level(efj[0], 2), "early")
        with contextlib.redirect_stdout(io.StringIO()):
            ok_ef = test_company(ef_cfg)
            pr_ef = probe_company(ef_cfg)
        expect("eightfold runner + probe pass on mock", [ok_ef, pr_ef], [True, True])
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep

    # --- round 3: Boeing-style titles, tolerant runner, pcsx fallback, diagnostics ---
    for title, want in {"F-22 Flight Test Instrumentation Engineer (Associate or Experienced)": "early",
                        "Payloads Design Engineer (Associate and Experienced)": "early",
                        "Manufacturing Engineer (Associate, Mid-Level, or Senior)": "early",
                        "Experienced Material Financial Analyst": "experienced",
                        "Mid-Level Software Engineer": "experienced",
                        "Entry-Level Propulsion Engineer": "early",
                        "Associate Engineer": "early"}.items():
        expect(f"classify {title[:44]!r}", classify_level(title), want)

    def fake_bad(method, url, body=None, extra=None, timeout=30):
        good = [{"title": f"Engineer {i}", "externalPath": f"/job/a/E_{i}", "bulletFields": [f"R{i}"],
                 "locationsText": "Austin, TX", "postedOn": "Posted Today"} for i in range(19)]
        if (body or {}).get("offset", 0) > 0:   # only the first page has jobs
            return 200, {"total": 20, "jobPostings": [], "facets": []}
        return 200, {"total": 20, "jobPostings": good + [{"title": "", "bulletFields": []}], "facets": []}

    saved_sleep = A.time.sleep
    A.http_json = fake_bad
    A.time.sleep = lambda s_: None
    try:
        wd_bad = {"name": "B", "provider": "workday", "host": "b.wd1.myworkdayjobs.com", "site": "S"}
        with contextlib.redirect_stdout(io.StringIO()):
            tolerant = test_company(wd_bad)
        expect("one odd posting is a warning, not a failure", tolerant, True)
        expect("bot path drops the incomplete posting", len(A.fetch_company_jobs(wd_bad)), 19)
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep

    px_positions = [
        {"id": 201, "displayJobId": "X1", "name": "Controls Engineer", "postedTs": 1730851200,
         "locations": ["Fort Worth, Texas, United States of America"], "positionUrl": "/careers/job/201"},
        {"id": 202, "displayJobId": "X2", "name": "Mechanical Engineering Intern", "postedTs": 1730851200,
         "locations": [{"name": "Littleton, Colorado, United States of America"}], "positionUrl": "/careers/job/202"},
        {"id": 203, "displayJobId": "X3", "name": "Engineer", "postedTs": 1730851200, "locations": ["Sydney, Australia"]}]

    def fake_px(method, url, body=None, extra=None, timeout=30):
        if "/api/apply/v2/" in url:
            raise A.AdapterError("HTTP 404 (simulated: this tenant has no apply API)")
        if "/api/pcsx/position_details" in url:
            return 200, {"data": {"jobDescription": "<p>3+ years of experience required.</p>"}}
        if "/api/pcsx/search" in url:
            start = int(re.search(r"start=(\d+)", url).group(1))
            return 200, {"status": 200, "data": {"count": 3, "positions": px_positions[start:start + 2]}}
        raise A.AdapterError("HTTP 404")

    A.http_json = fake_px
    A.time.sleep = lambda s_: None
    try:
        px_cfg = {"name": "PX", "provider": "eightfold", "host": "p.eightfold.ai", "domain": "p.com"}
        pj = A.fetch_company_jobs(px_cfg)
        expect("pcsx fallback ids (US only)", [j["id"] for j in pj], ["X1", "X2"])
        expect("pcsx relative url made absolute", pj[0]["url"], "https://p.eightfold.ai/careers/job/201")
        expect("pcsx epoch date", pj[0]["posted"], dt.date(2024, 11, 6))
        expect("pcsx remembered", A._EF_API.get("p.eightfold.ai"), "pcsx")
        A.enrich_jobs(pj)
        expect("pcsx detail min_years", pj[0]["min_years"], 3)
        expect("pcsx effective level", A.effective_level(pj[0], 2), "experienced")
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep

    def fake_dead(method, url, body=None, extra=None, timeout=30):
        raise A.AdapterError("HTTP 403")
    A.http_json = fake_dead
    try:
        try:
            A.fetch_company_jobs({"name": "D", "provider": "eightfold", "host": "dead.eightfold.ai", "domain": "d.com"})
            expect("both APIs failing raises", False, True)
        except A.AdapterError as e:
            expect("error mentions both API flavours", ("apply:" in str(e)) and ("pcsx:" in str(e)), True)
    finally:
        A.http_json = saved

    saved_raw = A.http_raw
    A.http_raw = lambda url, timeout=30: (403, url, "text/html", "<html>Access denied</html>")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.diagnose_eightfold("h.eightfold.ai", "h.com")
        out = buf.getvalue()
        expect("diagnose reports all three probes", all(k in out for k in ("apply/v2", "pcsx", "careers page")), True)
        expect("diagnose shows status", "HTTP 403" in out, True)
    finally:
        A.http_raw = saved_raw

    # --- Greenhouse adapter + SpaceX-style titles ---
    for text, want in {"Hawthorne, CA": True, "Starbase, TX": True, "Auckland, NZ": False,
                       "Toronto, Canada": False, "Hawthorne, CA; Redmond, WA": True, "Remote - United States": True,
                       "Bastrop, Texas": True, "Vancouver, BC, Canada": False, "London, UK": False}.items():
        expect(f"is_us_location {text!r}", is_us_location(text), want)
    for title, want in {"Avionics Hardware Engineer (Entry/Intermediate)": "early",
                        "Structures Engineer (Entry)": "early", "Propulsion Engineer (Intermediate)": "experienced",
                        "Mechanical Engineer (Senior)": "experienced", "Data Entry Clerk": "unknown"}.items():
        expect(f"classify {title[:40]!r}", classify_level(title), want)

    gh_posts = [
        {"id": 1, "title": "Avionics Hardware Engineer (Entry/Intermediate)", "location": {"name": "Hawthorne, CA"},
         "first_published": "2026-11-05T10:00:00-05:00", "absolute_url": "https://job-boards.greenhouse.io/x/jobs/1",
         "metadata": [{"name": "Employment Type", "value": "Regular"}]},
        {"id": 2, "title": "Propulsion Analyst", "location": {"name": "Starbase, TX"},
         "updated_at": "2026-11-05T10:00:00-05:00", "absolute_url": "https://job-boards.greenhouse.io/x/jobs/2",
         "metadata": [{"name": "Employment Type", "value": "Regular"},
                      {"name": "Job Discipline", "value": ["Engineering - Propulsion"]}]},
        {"id": 3, "title": "Summer Associate", "location": {"name": "Hawthorne, CA"},
         "absolute_url": "https://job-boards.greenhouse.io/x/jobs/3",
         "metadata": [{"name": "Employment Type", "value": "Intern"}]},
        {"id": 4, "title": "Engineer", "location": {"name": "Auckland, NZ"},
         "absolute_url": "https://job-boards.greenhouse.io/x/jobs/4", "metadata": None}]

    def fake_gh(method, url, body=None, extra=None, timeout=30):
        if url.endswith("/jobs/2"):
            return 200, {"content": "&lt;p&gt;Requires 2 years of experience.&lt;/p&gt;"}
        if "/jobs/" in url:
            raise A.AdapterError("HTTP 404")
        return 200, {"jobs": gh_posts, "meta": {"total": 4}}

    A.http_json = fake_gh
    A.time.sleep = lambda s_: None
    try:
        gh_cfg = {"name": "GH", "provider": "greenhouse", "board": "x"}
        ghj = A.fetch_company_jobs(gh_cfg)
        by = {j["id"]: j for j in ghj}
        expect("greenhouse keeps US jobs only", sorted(by), ["1", "2", "3"])
        expect("greenhouse entry/intermediate title", by["1"]["level"], "early")
        expect("greenhouse employment-type intern wins over title", by["3"]["level"], "intern")
        expect("greenhouse discipline marks engineering", by["2"]["engineering"], True)
        expect("greenhouse first_published date", by["1"]["posted"], dt.date(2026, 11, 5))
        n = A.enrich_jobs(ghj)
        expect("greenhouse enrich fetches only the unclear job", n, 1)
        expect("greenhouse min_years from escaped html", by["2"]["min_years"], 2)
        expect("greenhouse effective level", A.effective_level(by["2"], 2), "early")
        with contextlib.redirect_stdout(io.StringIO()):
            ok_gh = test_company(gh_cfg)
            pr_gh = probe_company(gh_cfg)
        expect("greenhouse runner + probe pass on mock", [ok_gh, pr_gh], [True, True])
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep

    # --- round 4: SpaceX remote locations, "all levels" titles, Workday sites without a country facet ---
    for text, want in {"Remote - CA": True, "Remote - CO": True, "Remote (TX)": True,
                       "Toronto, CAN": False, "Auckland, NZ": False, "Hawthorne, CA": True}.items():
        expect(f"is_us_location {text!r}", is_us_location(text), want)
    expect("gh_is_us with hint", gh_is_us({"us_hint": "any spacex site"}, "Flexible - Any SpaceX Site"), True)
    expect("gh_is_us without hint", gh_is_us({}, "Flexible - Any SpaceX Site"), False)
    for text, want in {"Bangalore, India": True, "United States-Utah-Corinne": False, "Austin, TX": False,
                       "2 Locations": False, "Aguadilla, Puerto Rico": False, "Sydney, Australia": True}.items():
        expect(f"looks_non_us {text!r}", looks_non_us(text), want)
    for title, want in {"Avionics Technician, all levels": "early",
                        "Launch Operations Engineer (All levels)": "early",
                        "Automation Integration Technician | All Levels": "early"}.items():
        expect(f"classify {title[:42]!r}", classify_level(title), want)

    def fake_nofacet(method, url, body=None, extra=None, timeout=30):
        def post(t, loc, i):
            return {"title": t, "externalPath": f"/job/a/J_{i}", "bulletFields": [f"R{i}"],
                    "locationsText": loc, "postedOn": "Posted Today"}
        if method == "GET":   # job detail
            return 200, {"jobPostingInfo": {"jobDescription": "<p>1 year of experience.</p>",
                                            "country": {"descriptor": "United States of America"},
                                            "location": "Austin, TX", "jobReqId": "R1"}}
        if (body or {}).get("offset", 0) > 0:
            return 200, {"total": 3, "jobPostings": [], "facets": []}
        return 200, {"total": 3, "facets": [{"facetParameter": "timeType", "descriptor": "Time",
                                              "values": [{"descriptor": "Full time", "id": "T1", "count": 3}]}],
                     "jobPostings": [post("Engineer A", "Austin, TX", 1), post("Engineer B", "Bangalore, India", 2),
                                     post("Engineer C", "2 Locations", 3)]}
    A.http_json = fake_nofacet
    A.time.sleep = lambda s_: None
    try:
        kept = A.fetch_company_jobs({"name": "NF", "provider": "workday", "host": "n.wd1.myworkdayjobs.com", "site": "S"})
        expect("no country facet: non-US dropped, unknown kept", sorted(j["title"] for j in kept),
               ["Engineer A", "Engineer C"])
        with contextlib.redirect_stdout(io.StringIO()):
            pr_nf = probe_company({"name": "NF", "provider": "workday", "host": "n.wd1.myworkdayjobs.com", "site": "S"})
        expect("probe handles a site without a country facet", pr_nf, True)
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep

    A.http_json = fake_gh
    try:
        hint_cfg = {"name": "GH", "provider": "greenhouse", "board": "x", "us_hint": "any spacex site"}
        gh_posts.append({"id": 5, "title": "Flexible Role", "location": {"name": "Flexible - Any SpaceX Site"},
                         "absolute_url": "https://job-boards.greenhouse.io/x/jobs/5"})
        expect("greenhouse hint keeps pseudo-location", "5" in {j["id"] for j in A.fetch_company_jobs(hint_cfg)}, True)
        expect("greenhouse without hint drops it", "5" in {j["id"] for j in A.fetch_company_jobs(
            {"name": "GH", "provider": "greenhouse", "board": "x"})}, False)
    finally:
        A.http_json = saved

    # --- round 5: Workday request-style fallback, richer HTTP errors, Workday diagnostics ---
    import http.server, os, threading
    os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1"

    class Err422(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'{"errorCode":"HTTP_422","error":"simulated detail"}'
            self.send_response(422)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Err422)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        A.http_json("GET", f"http://127.0.0.1:{srv.server_port}/x")
        expect("http 422 raises", False, True)
    except A.AdapterError as e:
        expect("http error message includes the server's reply", "HTTP 422" in str(e) and "simulated detail" in str(e), True)
    finally:
        srv.shutdown()

    def wd_mock(reject):
        def f(method, url, body=None, extra=None, timeout=30):
            if reject(body or {}, extra):
                raise A.AdapterError("HTTP 422 from x (simulated)")
            if (body or {}).get("offset", 0) > 0:
                return 200, {"total": 1, "jobPostings": [], "facets": []}
            return 200, {"total": 1, "facets": [], "jobPostings": [
                {"title": "Engineer", "externalPath": "/job/a/E_1", "bulletFields": ["R1"],
                 "locationsText": "Austin, TX", "postedOn": "Posted Today"}]}
        return f

    saved_sleep = A.time.sleep
    A.time.sleep = lambda s_: None
    try:
        for host, reject, want in [("v1.wd1.myworkdayjobs.com", lambda b, e: bool(e), 1),
                                   ("v2.wd1.myworkdayjobs.com", lambda b, e: "appliedFacets" in b, 2),
                                   ("v3.wd1.myworkdayjobs.com", lambda b, e: b.get("limit") != 10, 3)]:
            A.http_json = wd_mock(reject)
            got, _ = A.workday_list(host, "S", pages=1)
            expect(f"workday fallback style {want} returns the job", len(got), 1)
            expect(f"workday remembers style {want}", A._WD_VARIANT.get(host), want)

        saved_sess, saved_spost = A._wd_session, A._wd_session_post
        A._wd_session = lambda h, s_: (None, "", [])
        def sess_422(opener, url, body, headers, timeout=30):
            raise A.AdapterError("HTTP 422 from x (simulated)")
        A._wd_session_post = sess_422
        calls = []
        def always_422(method, url, body=None, extra=None, timeout=30):
            calls.append(1)
            raise A.AdapterError("HTTP 422 from x (simulated)")
        A.http_json = always_422
        try:
            A.workday_list("dead1.wd1.myworkdayjobs.com", "S")
            expect("all styles failing raises", False, True)
        except A.AdapterError as e:
            expect("error says simplified styles failed too", "simplified" in str(e), True)
        expect("all four plain styles were tried", len(calls), 4)

        calls.clear()
        def net_down(method, url, body=None, extra=None, timeout=30):
            calls.append(1)
            raise A.AdapterError("network error: unreachable")
        A.http_json = net_down
        try:
            A.workday_list("dead2.wd1.myworkdayjobs.com", "S")
        except A.AdapterError:
            pass
        expect("network errors fail fast (no style retries)", len(calls), 1)
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep
        A._wd_session, A._wd_session_post = saved_sess, saved_spost

    saved_raw2 = A.http_raw
    A.http_raw = lambda url, timeout=30, method="GET", body=None, headers=None: (
        422, url, "application/json", '{"errorCode":"HTTP_422"}')
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.diagnose_workday("d.wd1.myworkdayjobs.com", "External")
        out = buf.getvalue()
        expect("workday diagnose runs every style", all(k in out for k in
               ("standard request", "bare body", "smaller page", "careers page")), True)
        expect("workday diagnose shows status and body", "HTTP 422" in out and "HTTP_422" in out, True)
    finally:
        A.http_raw = saved_raw2

    # --- round 6: country facet under other names (e.g. GM's Location_Country) ---
    gm_facets = [
        {"facetParameter": "Location_Country", "descriptor": "Country", "values": [
            {"descriptor": "United States of America", "id": "US9", "count": 5},
            {"descriptor": "Mexico", "id": "MX9", "count": 2}]},
        {"facetParameter": "workerSubType", "descriptor": "Worker Type", "values": [
            {"descriptor": "Co-Op (Fixed Term)", "id": "CO1", "count": 2},
            {"descriptor": "Regular", "id": "RG1", "count": 9}]}]
    expect("country facet found under another name", A.find_country_facet(walk_facets(gm_facets)),
           ("Location_Country", "US9"))
    expect("no country facet -> None", A.find_country_facet(walk_facets(
        [{"facetParameter": "timeType", "descriptor": "t", "values": [{"descriptor": "Full time", "id": "T", "count": 1}]}])), None)

    seen_bodies = []
    def fake_gm(method, url, body=None, extra=None, timeout=30):
        seen_bodies.append(body)
        applied = (body or {}).get("appliedFacets") or {}
        def post(t, i):
            return {"title": t, "externalPath": f"/job/a/J_{i}", "bulletFields": [f"R{i}"],
                    "locationsText": "Austin, Texas, United States of America", "postedOn": "Posted Today"}
        if (body or {}).get("offset", 0) > 0:
            return 200, {"total": 2, "jobPostings": [], "facets": gm_facets}
        posts = [post("Software Co-op", 1)] if "workerSubType" in applied else [post("Software Co-op", 1), post("Quality Engineer", 2)]
        return 200, {"total": len(posts), "jobPostings": posts, "facets": gm_facets}

    saved_sleep = A.time.sleep
    A.http_json = fake_gm
    A.time.sleep = lambda s_: None
    try:
        gm_jobs = A.fetch_company_jobs({"name": "GM", "provider": "workday", "host": "gm9.wd5.myworkdayjobs.com", "site": "S"})
        applied_seen = [b.get("appliedFacets") for b in seen_bodies if b and b.get("appliedFacets")]
        expect("country filter sent under the site's own facet name", {"Location_Country": ["US9"]} in applied_seen, True)
        expect("intern filter combined with country filter",
               {"Location_Country": ["US9"], "workerSubType": ["CO1"]} in applied_seen, True)
        expect("co-op marked intern via worker type", {j["title"]: j["level"] for j in gm_jobs}["Software Co-op"], "intern")
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep

    # --- round 7: browser-like session (cookies + CSRF) style ---
    saved_sess, saved_spost, saved_sget = A._wd_session, A._wd_session_post, A._wd_session_get
    saved_sleep = A.time.sleep
    A.time.sleep = lambda s_: None
    sess_calls = {"post": 0, "get": 0}

    def plain_422(method, url, body=None, extra=None, timeout=30):
        raise A.AdapterError("HTTP 422 from x (simulated: plain requests rejected)")
    A.http_json = plain_422
    A._wd_session = lambda h, s_: ("opener", "tok123", ["GET /en-US/S: HTTP 200", "cookies: ['X']; CSRF token: yes"])

    def sess_post(opener, url, body, headers, timeout=30):
        sess_calls["post"] += 1
        if headers.get("X-Calypso-CSRF-Token") != "tok123":
            raise A.AdapterError("HTTP 422 (no csrf)")
        if body.get("offset", 0) > 0:
            return {"total": 1, "jobPostings": [], "facets": []}
        return {"total": 1, "facets": [], "jobPostings": [
            {"title": "Engineer", "externalPath": "/job/a/E_1", "bulletFields": ["R1"],
             "locationsText": "Austin, TX", "postedOn": "Posted Today"}]}

    def sess_get(opener, url, headers, timeout=30):
        sess_calls["get"] += 1
        return {"jobPostingInfo": {"jobDescription": "<p>2 years of experience.</p>",
                                   "country": {"descriptor": "United States of America"}, "location": "Austin, TX"}}
    A._wd_session_post, A._wd_session_get = sess_post, sess_get
    try:
        got, _ = A.workday_list("sess1.wd1.myworkdayjobs.com", "S", pages=1)
        expect("session style returns jobs when plain styles are rejected", len(got), 1)
        expect("session style remembered", A._WD_VARIANT.get("sess1.wd1.myworkdayjobs.com"), 4)
        d = A.workday_detail("sess1.wd1.myworkdayjobs.com", "S", "/job/a/E_1")
        expect("detail uses the session too", (sess_calls["get"], "2 years" in d["description_html"]), (1, True))
        with contextlib.redirect_stdout(io.StringIO()):
            ok_s = test_company({"name": "S", "provider": "workday", "host": "sess1.wd1.myworkdayjobs.com", "site": "S"})
        expect("test runner works through the session style", ok_s, True)
        # diagnose prints the session section
        saved_raw3 = A.http_raw
        A.http_raw = lambda url, timeout=30, method="GET", body=None, headers=None: (422, url, "application/json", "{}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            A.diagnose_workday("sess1.wd1.myworkdayjobs.com", "S")
        A.http_raw = saved_raw3
        out = buf.getvalue()
        expect("diagnose shows session test", "browser-like session" in out and "POST with cookies + CSRF header: OK" in out, True)
        expect("diagnose shows Windows UA test", "Windows Chrome user-agent" in out, True)
    finally:
        A.http_json, A.time.sleep = saved, saved_sleep
        A._wd_session, A._wd_session_post, A._wd_session_get = saved_sess, saved_spost, saved_sget

    if failures:
        print("SELFTEST FAILED:\n  " + "\n  ".join(failures))
        return False
    print(f"SELFTEST PASSED ({len(cases)} classifier cases, date parsing, both adapters, runner pass+fail paths)")
    return True

# --------------------------------------------------------------------------- #

def main() -> None:
    ap = argparse.ArgumentParser(description="Test the Workday / Oracle HCM job-feed adapters.")
    ap.add_argument("--selftest", action="store_true", help="offline logic tests, no network")
    ap.add_argument("--company", help="only test configured companies whose name contains this")
    ap.add_argument("--workday-url", help="test any Workday careers/job URL")
    ap.add_argument("--oracle-url", help="test any Oracle HCM careers URL (needs --site-number)")
    ap.add_argument("--site-number", help="Oracle siteNumber, e.g. CX_1 (from DevTools)")
    ap.add_argument("--eightfold-url", help="test any Eightfold careers URL, e.g. https://acme.eightfold.ai/careers")
    ap.add_argument("--domain", help="Eightfold domain param (default: <tenant>.com)")
    ap.add_argument("--greenhouse-board", help="test a Greenhouse board: its name (e.g. spacex) or any job-board URL")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--diagnose", action="store_true", help="Workday / Eightfold: show raw server responses")
    ap.add_argument("--probe", action="store_true", help="step 2: facets, US filter, description-based level")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(0 if selftest() else 1)

    targets = []
    if args.workday_url:
        m = re.match(r"https?://([^/]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Za-z]{2}/)?([^/?#]+)", args.workday_url)
        if not m:
            sys.exit("Couldn't find host/site in that URL. Expected https://<tenant>.wdN.myworkdayjobs.com/<site>/...")
        targets.append({"name": m.group(1).split(".")[0], "provider": "workday",
                        "host": m.group(1), "site": m.group(2)})
    elif args.greenhouse_board:
        m = re.search(r"greenhouse\.io/([^/?#]+)", args.greenhouse_board)
        board = m.group(1) if m else args.greenhouse_board.strip().strip("/")
        targets.append({"name": board.title(), "provider": "greenhouse", "board": board})
    elif args.eightfold_url:
        m = re.match(r"https?://([^/]+\.eightfold\.ai)", args.eightfold_url)
        if not m:
            sys.exit("Expected a URL like https://<tenant>.eightfold.ai/careers")
        tenant = m.group(1).split(".")[0]
        targets.append({"name": tenant, "provider": "eightfold", "host": m.group(1),
                        "domain": args.domain or f"{tenant}.com"})
    elif args.oracle_url:
        m = re.match(r"https?://([^/]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+)",
                     args.oracle_url)
        if not (m and args.site_number):
            sys.exit("Need an Oracle careers URL (.../hcmUI/CandidateExperience/en/sites/<site>/...) "
                     "plus --site-number (from DevTools).")
        targets.append({"name": m.group(1).split(".")[0], "provider": "oracle", "host": m.group(1),
                        "site": m.group(2), "site_number": args.site_number})
    else:
        targets = [c for c in COMPANIES if not args.company or args.company.lower() in c["name"].lower()]
        if not targets:
            sys.exit("No configured company matches that name.")

    if args.diagnose:
        for c in targets:
            if c["provider"] == "eightfold":
                diagnose_eightfold(c["host"], c["domain"])
            elif c["provider"] == "workday":
                diagnose_workday(c["host"], c["site"])
            else:
                print(f"--diagnose supports Workday and Eightfold; use --probe for {c['name']}")
        sys.exit(0)
    oks = [(probe_company(c) if args.probe else test_company(c, args.pages)) for c in targets]
    print(f"\n{sum(oks)}/{len(oks)} companies OK")
    if (args.workday_url or args.oracle_url or args.eightfold_url or args.greenhouse_board) and all(oks):
        c = targets[0]
        cfg = {"name": c["name"].title(), **{k: v for k, v in c.items() if k != "name"}}
        print("\nLooks good. Add this line to COMPANIES in ats_adapters.py (adjust the name if you like):")
        print("    " + json.dumps(cfg) + ",")
    if not all(oks):
        print("\nIf something FAILed: open the company's careers page, press F12 -> Network -> Fetch/XHR, "
              "reload, and look for the request that returns the job list. Paste me the URL and a few "
              "lines of the response and I'll adjust the adapter.")
    sys.exit(0 if all(oks) else 1)


if __name__ == "__main__":
    main()