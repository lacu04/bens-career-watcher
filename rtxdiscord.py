#!/usr/bin/env python3
"""
rtx_discord_bot.py - multi-user Discord bot for new RTX job postings.

Every user creates their own alerts with slash commands, e.g.
  user 1:  /alert add level:Internships & co-ops
  user 2:  /alert add level:Early career (Entry Level) exclude:intern, co-op location:Texas
New postings are matched against each person's alerts and sent only to them
(by DM by default, or pinged in the channel where they created the alert).

Needs rtx_job_watcher.py (or your renamed rtx.py) in the same folder.
  pip install discord.py

Environment variables:
  DISCORD_TOKEN       bot token (Developer Portal -> your app -> Bot -> Reset Token). Keep secret.
  DISCORD_GUILD_ID    optional; your server's ID makes slash commands appear instantly
  DISCORD_CHANNEL_ID  optional; fallback channel used when a user's DMs are closed
  CHECK_MINUTES       poll interval, default 15 (minimum 5)
  RTX_COUNTRIES       comma-separated, default "United States of America"
  RTX_BOT_DB          path to the SQLite file, default ./rtx_bot.db

Commands: /alert add | list | remove | toggle | preview
Run ONLY this bot - not the scheduled/--loop watcher at the same time.
"""
import asyncio
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

try:
    import rtx as w
except ImportError:
    import rtx as w  # in case you renamed the watcher to rtx.py

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
FALLBACK_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or 0)
CHECK_MINUTES = max(int(os.environ.get("CHECK_MINUTES", "15")), 5)
COUNTRIES = [c.strip() for c in os.environ.get("RTX_COUNTRIES", "United States of America").split(",") if c.strip()]
DB_PATH = Path(os.environ.get("RTX_BOT_DB") or Path(__file__).with_name("rtx_bot.db"))

MAX_ALERTS_PER_USER = 5
MAX_PER_USER_PER_POLL = 8
ALL_LEVELS = ("Intern", "Entry Level")
LEVELS = {"intern": ["Intern"], "entry": ["Entry Level"], "both": ["Intern", "Entry Level"]}
LEVEL_LABEL = {"intern": "Internships & co-ops", "entry": "Early career (Entry Level)",
               "both": "Internships + early career"}
CATEGORIES = ["Engineering", "Quality", "Operations", "Digital Technology", "Finance",
              "General / Program Management", "Supply Chain", "Aftermarket & Service", "Other"]
NO_PINGS = discord.AllowedMentions.none()

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL, name TEXT NOT NULL, level TEXT NOT NULL,
  keywords TEXT NOT NULL DEFAULT '[]', exclude TEXT NOT NULL DEFAULT '[]',
  locations TEXT NOT NULL DEFAULT '[]', category TEXT NOT NULL DEFAULT '',
  delivery TEXT NOT NULL DEFAULT 'dm', channel_id TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen(job_id TEXT PRIMARY KEY);
""")
db.commit()


def split_list(text: Optional[str], max_items: int = 10, max_len: int = 40) -> list[str]:
    if not text:
        return []
    return [p.strip()[:max_len] for p in text.split(",") if p.strip()][:max_items]


def alert_matches(a: sqlite3.Row, job: dict) -> bool:
    if job["experience"].lower() not in [x.lower() for x in LEVELS[a["level"]]]:
        return False
    title, loc = job["title"].lower(), job["location"].lower()
    kws, exc, locs = (json.loads(a[k]) for k in ("keywords", "exclude", "locations"))
    if kws and not any(k.lower() in title for k in kws):
        return False
    if any(k.lower() in title for k in exc):
        return False
    if locs and not any(l.lower() in loc for l in locs):
        return False
    if a["category"] and a["category"].lower() not in job["category"].lower():
        return False
    return True


def describe(a: sqlite3.Row) -> str:
    bits = [LEVEL_LABEL[a["level"]]]
    for label, key in (("title has", "keywords"), ("title excludes", "exclude"), ("where", "locations")):
        vals = json.loads(a[key])
        if vals:
            bits.append(f"{label}: {', '.join(vals)}")
    if a["category"]:
        bits.append(f"category: {a['category']}")
    bits.append("via DM" if a["delivery"] == "dm" else f"in <#{a['channel_id']}>")
    status = "active" if a["active"] else "paused"
    return f"**#{a['id']} {a['name']}** ({status})\n" + " | ".join(bits)


def job_embed(j: dict, names: Optional[list[str]] = None) -> discord.Embed:
    e = discord.Embed(title=j["title"][:256], url=j["url"], color=0xC8102E)
    e.add_field(name="Location", value=j["location"] or "n/a", inline=True)
    e.add_field(name="Level", value=j["experience"] or "n/a", inline=True)
    e.add_field(name="Category", value=j["category"] or "n/a", inline=True)
    foot = f"Req {j['id']} - posted {j['posted'] or 'unknown'}"
    if names:
        foot = f"Alert: {', '.join(names)} | " + foot
    e.set_footer(text=foot[:2000])
    return e

# --------------------------------------------------------------------------- #
# Fetching + routing
# --------------------------------------------------------------------------- #

async def fetch_all(levels=ALL_LEVELS, max_pages: Optional[int] = None):
    """Fetch RTX postings for the given experience levels. Returns (jobs_by_id, all_ok)."""
    jobs: dict[str, dict] = {}
    ok = True
    for level in levels:
        try:
            res, _ = await asyncio.to_thread(
                w.fetch_jobs, "", False, max_pages or w.MAX_PAGES,
                {"experienceLevel": [level], "country": COUNTRIES})
            for j in res:  # client-side safety net in case the server ignored a filter
                if j["experience"] in levels and (not COUNTRIES or j["country"] in COUNTRIES):
                    jobs[j["id"]] = j
        except Exception as e:
            ok = False
            print(f"[error] fetching {level!r}: {e}", file=sys.stderr)
    return jobs, ok


async def poll_once() -> int:
    """Fetch, find new jobs, route each to the users whose alerts match. Returns #deliveries."""
    jobs, ok = await fetch_all()
    if not jobs:
        return 0
    seen = {r[0] for r in db.execute("SELECT job_id FROM seen")}
    first_run = not seen
    if first_run and not ok:
        print("[warn] first poll incomplete; will retry before seeding")
        return 0

    new = [j for jid, j in jobs.items() if jid not in seen]
    db.executemany("INSERT OR IGNORE INTO seen(job_id) VALUES (?)", [(jid,) for jid in jobs])
    db.commit()
    if first_run:
        print(f"First poll: recorded {len(jobs)} postings, no alerts sent.")
        return 0

    alerts = db.execute("SELECT * FROM alerts WHERE active=1").fetchall()
    new.sort(key=lambda j: j["posted"] or dt.date.min, reverse=True)
    routes: dict[tuple, dict] = {}
    for j in new:
        if w.too_old(j):
            continue
        for a in alerts:
            if alert_matches(a, j):
                key = (a["user_id"], a["delivery"], a["channel_id"])
                routes.setdefault(key, {}).setdefault(j["id"], (j, []))[1].append(a["name"])

    for key, items in routes.items():
        await deliver(key, list(items.values()))
    print(f"{len(new)} new postings -> {sum(len(v) for v in routes.values())} deliveries "
          f"to {len(routes)} target(s)")
    return sum(len(v) for v in routes.values())


async def send_items(target, user_id: str, items: list, ping: bool, note: str = "") -> None:
    mention = discord.AllowedMentions(users=[discord.Object(id=int(user_id))])
    prefix = f"<@{user_id}> " if ping else ""
    for n, (j, names) in enumerate(items[:MAX_PER_USER_PER_POLL]):
        content = (prefix + (note if n == 0 else "")).strip() or None
        await target.send(content=content, embed=job_embed(j, names), allowed_mentions=mention)
    extra = items[MAX_PER_USER_PER_POLL:]
    if extra:
        lines = "\n".join(f"- [{j['title'][:70]}]({j['url']}) ({j['location']})" for j, _ in extra[:15])
        await target.send(content=f"{prefix}+{len(extra)} more new jobs:\n{lines}"[:2000],
                          allowed_mentions=mention, suppress_embeds=True)


async def deliver(key: tuple, items: list) -> None:
    user_id, delivery, channel_id = key
    try:
        if delivery == "channel" and channel_id:
            target = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
            await send_items(target, user_id, items, ping=True)
        else:
            user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
            await send_items(user, user_id, items, ping=False)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
        print(f"[warn] delivery to {key} failed: {e}", file=sys.stderr)
        if FALLBACK_CHANNEL_ID:
            try:
                ch = client.get_channel(FALLBACK_CHANNEL_ID) or await client.fetch_channel(FALLBACK_CHANNEL_ID)
                await send_items(ch, user_id, items, ping=True,
                                 note="(I couldn't reach you where your alert is set up - check DM settings.)")
            except Exception as e2:
                print(f"[warn] fallback delivery failed: {e2}", file=sys.stderr)

# --------------------------------------------------------------------------- #
# Bot + commands
# --------------------------------------------------------------------------- #

class RTXBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(alert)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        poll.change_interval(minutes=CHECK_MINUTES)
        poll.start()

    async def on_ready(self):
        print(f"Logged in as {self.user} - polling every {CHECK_MINUTES} min")


client = RTXBot()


@tasks.loop(minutes=15)
async def poll():
    try:
        await poll_once()
    except Exception as e:  # never let one bad poll kill the loop
        print(f"[error] poll failed: {e}", file=sys.stderr)


@poll.before_loop
async def _wait():
    await client.wait_until_ready()


alert = app_commands.Group(name="alert", description="Manage your RTX job alerts")


def my_alert(interaction: discord.Interaction, alert_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM alerts WHERE id=? AND user_id=?",
                      (alert_id, str(interaction.user.id))).fetchone()


async def alert_id_autocomplete(interaction: discord.Interaction, current: str):
    rows = db.execute("SELECT id, name FROM alerts WHERE user_id=?", (str(interaction.user.id),)).fetchall()
    return [app_commands.Choice(name=f"#{r['id']} {r['name']}"[:100], value=r["id"])
            for r in rows if current.lower() in r["name"].lower()][:25]


@alert.command(name="add", description="Create a new alert for new RTX postings")
@app_commands.describe(
    level="Which kind of postings",
    name="A label for this alert (optional)",
    keywords="Title must contain ANY of these, comma-separated (e.g. software, embedded)",
    exclude="Skip titles containing any of these (e.g. intern, co-op)",
    location="Only these places, comma-separated (e.g. Texas, Tucson, Remote)",
    category="Only this job category",
    delivery="Where alerts go: your DMs (default) or pinged in this channel")
@app_commands.choices(
    level=[app_commands.Choice(name=v, value=k) for k, v in LEVEL_LABEL.items()],
    category=[app_commands.Choice(name=c, value=c) for c in CATEGORIES],
    delivery=[app_commands.Choice(name="DM me", value="dm"),
              app_commands.Choice(name="Ping me in this channel", value="channel")])
async def alert_add(interaction: discord.Interaction,
                    level: app_commands.Choice[str],
                    name: Optional[str] = None,
                    keywords: Optional[str] = None,
                    exclude: Optional[str] = None,
                    location: Optional[str] = None,
                    category: Optional[app_commands.Choice[str]] = None,
                    delivery: Optional[app_commands.Choice[str]] = None):
    uid = str(interaction.user.id)
    count = db.execute("SELECT COUNT(*) FROM alerts WHERE user_id=?", (uid,)).fetchone()[0]
    if count >= MAX_ALERTS_PER_USER:
        await interaction.response.send_message(
            f"You already have {MAX_ALERTS_PER_USER} alerts. Remove one with `/alert remove` first.",
            ephemeral=True, allowed_mentions=NO_PINGS)
        return
    mode = delivery.value if delivery else "dm"
    if mode == "channel" and interaction.guild_id is None:
        await interaction.response.send_message(
            "Channel delivery only works inside a server channel - run this there, or choose DM.",
            ephemeral=True)
        return
    kws, exc, locs = split_list(keywords), split_list(exclude), split_list(location)
    label = (name or "").strip()[:40] or (LEVEL_LABEL[level.value] + (f" - {kws[0]}" if kws else ""))[:40]
    cur = db.execute(
        "INSERT INTO alerts(user_id,name,level,keywords,exclude,locations,category,delivery,channel_id,created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (uid, label, level.value, json.dumps(kws), json.dumps(exc), json.dumps(locs),
         category.value if category else "", mode,
         str(interaction.channel_id) if mode == "channel" else "",
         dt.datetime.now(dt.timezone.utc).isoformat()))
    db.commit()
    a = db.execute("SELECT * FROM alerts WHERE id=?", (cur.lastrowid,)).fetchone()
    await interaction.response.send_message(
        f"Alert created:\n{describe(a)}\n\nYou'll be notified about postings that appear from now on. "
        f"Try `/alert preview` to see what currently matches.",
        ephemeral=True, allowed_mentions=NO_PINGS)


@alert.command(name="list", description="Show your alerts")
async def alert_list(interaction: discord.Interaction):
    rows = db.execute("SELECT * FROM alerts WHERE user_id=? ORDER BY id", (str(interaction.user.id),)).fetchall()
    text = "\n\n".join(describe(a) for a in rows) or "You have no alerts yet - create one with `/alert add`."
    await interaction.response.send_message(text[:2000], ephemeral=True, allowed_mentions=NO_PINGS)


@alert.command(name="remove", description="Delete one of your alerts")
@app_commands.describe(alert_id="Which alert")
@app_commands.autocomplete(alert_id=alert_id_autocomplete)
async def alert_remove(interaction: discord.Interaction, alert_id: int):
    a = my_alert(interaction, alert_id)
    if not a:
        await interaction.response.send_message("I couldn't find that alert among yours.", ephemeral=True)
        return
    db.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
    db.commit()
    await interaction.response.send_message(f"Removed alert #{alert_id} ({a['name']}).",
                                            ephemeral=True, allowed_mentions=NO_PINGS)


@alert.command(name="toggle", description="Pause or resume one of your alerts")
@app_commands.describe(alert_id="Which alert")
@app_commands.autocomplete(alert_id=alert_id_autocomplete)
async def alert_toggle(interaction: discord.Interaction, alert_id: int):
    a = my_alert(interaction, alert_id)
    if not a:
        await interaction.response.send_message("I couldn't find that alert among yours.", ephemeral=True)
        return
    new_state = 0 if a["active"] else 1
    db.execute("UPDATE alerts SET active=? WHERE id=?", (new_state, alert_id))
    db.commit()
    await interaction.response.send_message(
        f"Alert #{alert_id} ({a['name']}) is now {'active' if new_state else 'paused'}.",
        ephemeral=True, allowed_mentions=NO_PINGS)


@alert.command(name="preview", description="Show the newest postings that currently match one of your alerts")
@app_commands.describe(alert_id="Which alert")
@app_commands.autocomplete(alert_id=alert_id_autocomplete)
async def alert_preview(interaction: discord.Interaction, alert_id: int):
    a = my_alert(interaction, alert_id)
    if not a:
        await interaction.response.send_message("I couldn't find that alert among yours.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    jobs, _ = await fetch_all(tuple(LEVELS[a["level"]]), max_pages=2)
    hits = sorted((j for j in jobs.values() if alert_matches(a, j)),
                  key=lambda j: j["posted"] or dt.date.min, reverse=True)[:5]
    if not hits:
        await interaction.followup.send("Nothing matches this alert right now.", ephemeral=True)
        return
    await interaction.followup.send(content=f"Newest matches for #{a['id']} {a['name']}:",
                                    embeds=[job_embed(j) for j in hits], ephemeral=True,
                                    allowed_mentions=NO_PINGS)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Set DISCORD_TOKEN first (see the docstring at the top).")
    client.run(TOKEN)