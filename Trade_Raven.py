import os
import re
import json
import asyncio
import aiohttp
import discord
import requests
import csv
import time
import sqlite3
from keep_alive import keep_alive
from datetime import datetime, timedelta, UTC
from discord.ext import commands, tasks
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from difflib import get_close_matches
from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, ElementClickInterceptedException

load_dotenv()

# ——— CONFIGURATION ———
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SLEEPER_LEAGUE_ID = os.getenv("SLEEPER_LEAGUE_ID")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

DB_PATH = "./ktc.db"  # SQLite DB file path

DP_CSV_URL = "https://raw.githubusercontent.com/dynastyprocess/data/refs/heads/master/files/values-players.csv"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

known_trade_ids = set()
ktc_values = {}
ktc_source_used = "unknown"  # Tracks which KTC source was last successful

user_map = {}  # roster_id -> team name or display name
manual_ktc_overrides = {
    "Ken Walker": "Kenneth Walker III",
    "DJ Moore": "D.J. Moore",
    "CMC": "Christian McCaffrey",
    "JJ": "Justin Jefferson",
    "Bijan": "Bijan Robinson",
    "Tyreek": "Tyreek Hill",
}

current_bot_week = 1  # Starts at Week 1

# ——— SQLITE DB FUNCTIONS ———

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS players (
        player_name TEXT PRIMARY KEY,
        ktc_value REAL,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def save_ktc_to_db(ktc_dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    for name, val in ktc_dict.items():
        print(f"📥 Saving to DB: {name} = {val}")
        c.execute("""
        INSERT INTO players (player_name, ktc_value, last_updated)
        VALUES (?, ?, ?)
        ON CONFLICT(player_name) DO UPDATE SET
            ktc_value=excluded.ktc_value,
            last_updated=excluded.last_updated
        """, (name, val, datetime.now(UTC)))
    conn.commit()
    conn.close()
    print("✅ All KTC values saved to the database.")

def load_ktc_from_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_name, ktc_value FROM players")
    data = c.fetchall()
    conn.close()
    return {name: val for name, val in data}

# === DEBUGGING ADDITIONS START ===

def print_all_db_player_names():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_name FROM players")
    all_names = c.fetchall()
    conn.close()

    print("Players stored in SQLite DB (sample 20):")
    for name_tuple in all_names[:20]:  # print first 20
        print(f"- {name_tuple[0]}")

def find_similar_names_in_db(query):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_name FROM players WHERE player_name LIKE ?", (f"%{query}%",))
    results = c.fetchall()
    conn.close()
    print(f"Names in DB similar to '{query}':")
    for r in results:
        print(f"- {r[0]}")

# === DEBUGGING ADDITIONS END ===

# ——— SELENIUM SCRAPER FOR FANTASYPROS ———

def load_all_players(driver):
    # Click "Load More" repeatedly until no more button
    while True:
        try:
            load_more_button = driver.find_element(By.CSS_SELECTOR, ".fp-load-more")
            if load_more_button.is_displayed():
                load_more_button.click()
                time.sleep(2)
            else:
                break
        except (NoSuchElementException, ElementClickInterceptedException):
            break

def fetch_dp_values():
    DP_CSV_URL = "https://raw.githubusercontent.com/dynastyprocess/data/refs/heads/master/files/values-players.csv"
    resp = requests.get(DP_CSV_URL, timeout=10)
    resp.raise_for_status()
    reader = csv.DictReader(resp.text.splitlines())
    return {row["player"]: float(row["value_1qb"]) for row in reader}

# ——— KTC FETCHING ———

def update_ktc():
    try:
        dp = fetch_dp_values()
        if dp:
            ktc_values.clear()
            ktc_values.update(dp)
            ktc_source_used = "DynastyProcess CSV"
            save_ktc_to_db(ktc_values)
            print(f"✅ Loaded KTC values for {len(dp)} players from DynastyProcess")
        else:
            print("❗ No values found in DynastyProcess CSV.")
    except Exception as e:
        print(f"❗ Error loading DynastyProcess data: {e}")

def get_ktc_value(player_name, return_best_match=False):
    name_input = player_name.strip()
    print(f"Looking up KTC value for player name: '{name_input}'")

    if name_input in manual_ktc_overrides:
        name_input = manual_ktc_overrides[name_input]
        print(f"Overriding name to '{name_input}'")

    if name_input in ktc_values:
        print(f"Exact match found for '{name_input}'")
        return (ktc_values[name_input], name_input) if return_best_match else ktc_values[name_input]

    close = get_close_matches(name_input, ktc_values.keys(), n=1, cutoff=0.5)
    if close:
        best = close[0]
        print(f"Close match found: '{best}' for input '{name_input}'")
        return (ktc_values[best], best) if return_best_match else ktc_values[best]

    print(f"No match found for '{name_input}'")
    # Debug: check DB for similar names
    find_similar_names_in_db(name_input)

    return (0, None) if return_best_match else 0

# ——— SLEEPER FETCH ———
async def fetch_json(session, url):
    async with session.get(url) as r:
        return await r.json()

def get_player_name_map(players_json):
    return {pid: pdata["full_name"] for pid, pdata in players_json.items() if "full_name" in pdata}

async def load_users():
    league_users_url = f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/users"
    league_rosters_url = f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/rosters"

    async with aiohttp.ClientSession() as session:
        users = await fetch_json(session, league_users_url)
        rosters = await fetch_json(session, league_rosters_url)

    user_id_to_name = {u["user_id"]: u.get("metadata", {}).get("team_name") or u.get("display_name") for u in users}

    for roster in rosters:
        user_id = roster.get("owner_id")
        roster_id = roster.get("roster_id")
        if user_id and roster_id:
            name = user_id_to_name.get(user_id, f"Team {roster_id}")
            user_map[roster_id] = name

    print(f"✅ Mapped {len(user_map)} rosters to team names.")

# ——— BOT EVENTS ———
@bot.event
async def on_ready():
    print(f"🦅 Messenger Falcon is live as {bot.user}")
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        print(f"❗ Could NOT find channel with ID {DISCORD_CHANNEL_ID}")
    else:
        print(f"✅ Found Discord channel: {channel.name} (ID: {channel.id})")
        try:
            await channel.send("Messenger Falcon is online and ready!")
        except discord.Forbidden:
            print("❗ Bot does NOT have permission to send messages in this channel.")

    # DB Init
    init_db()
    db_values = load_ktc_from_db()
    ktc_values.update(db_values)
    print(f"✅ Loaded {len(db_values)} cached KTC values from DB")

    await load_users()

    # Start loops
    poll_trades.start()
    ktc_update_loop.start()

    # ⌛ Set when Week 2 begins (e.g., in 2 days) and start updater loop
    global WEEK_2_START_DATE
    WEEK_2_START_DATE = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)
    update_bot_week.start()

@tasks.loop(hours=24)
async def ktc_update_loop():
    print("🔄 24‑hour Player Value Update")
    update_ktc()

@tasks.loop(hours=24)
async def update_bot_week():
    global current_bot_week
    today = datetime.now(UTC)  # aware datetime
    if today >= WEEK_2_START_DATE:
        days_passed = (today - WEEK_2_START_DATE).days
        current_bot_week = 2 + (days_passed // 7)
        print(f"📅 Updated bot week to Week {current_bot_week}")

@bot.command()
async def currentweek(ctx):
    await ctx.send(f"📅 Current tracked week is: **Week {current_bot_week}**")

@bot.command()
async def forceweek(ctx, week: int):
    """Manually override the current week number."""
    global current_bot_week
    current_bot_week = week
    await ctx.send(f"✅ Bot week manually set to Week {current_bot_week}")

@tasks.loop(seconds=30)
async def poll_trades():
    await fetch_and_announce_trades()

async def fetch_and_announce_trades(week_override=None):
    print("🔍 Checking for new trades...")

    async with aiohttp.ClientSession() as session:
        global current_bot_week
        current_week = week_override if week_override else current_bot_week
        txns_url = f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/transactions/{current_week}"
        players_url = "https://api.sleeper.app/v1/players/nfl"
        transactions = await fetch_json(session, txns_url)
        player_data = await fetch_json(session, players_url)

    print(f"📦 Fetched {len(transactions)} transactions from Sleeper (Week {current_week})")

    name_map = get_player_name_map(player_data)

    for txn in transactions:
        tid = txn.get("transaction_id")
        ttype = txn.get("type")
        tstatus = txn.get("status")
        print(f"🔎 TXN ID {tid} | Type: {ttype} | Status: {tstatus}")

        if ttype != "trade" or tstatus != "complete":
            continue

        if tid in known_trade_ids:
            continue

        adds = txn.get("adds", {})
        reverse = {}
        for pid, rid in adds.items():
            reverse.setdefault(rid, []).append(pid)

        rosters = txn.get("roster_ids", [])[:2]
        if len(rosters) < 2:
            print(f"❗ Not enough roster_ids in transaction {tid}, skipping.")
            continue

        t0_ids = reverse.get(rosters[0], [])
        t1_ids = reverse.get(rosters[1], [])
        t0_names = [name_map.get(pid, pid) for pid in t0_ids]
        t1_names = [name_map.get(pid, pid) for pid in t1_ids]

        v0 = sum(get_ktc_value(n) for n in t0_names)
        v1 = sum(get_ktc_value(n) for n in t1_names)

        team0 = user_map.get(rosters[0], f"Team {rosters[0]}")
        team1 = user_map.get(rosters[1], f"Team {rosters[1]}")

        embed = discord.Embed(
            title="🔁 New Trade Completed!",
            color=discord.Color.green(),
            timestamp=datetime.now(UTC)
        )
        embed.add_field(name=f"🔮 {team0} gets", value=", ".join(t0_names) or "None", inline=False)
        embed.add_field(name=f"🔧 {team1} gets", value=", ".join(t1_names) or "None", inline=False)
        embed.add_field(name="💰 KTC Value", value=f"{team0}: {v0:,} — {team1}: {v1:,}", inline=False)

        diff = v0 - v1
        if abs(diff) < 200:
            result_text = "🤝 Fair trade!"
        else:
            winner = team0 if diff > 0 else team1
            result_text = f"✅ {winner} wins by {abs(diff):,} points!"
        embed.set_footer(text=result_text)

        channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            print("❗ Could not find Discord channel.")
            return

        try:
            msg = await channel.send(embed=embed)
            await msg.add_reaction("👍")
            await msg.add_reaction("👎")
            known_trade_ids.add(tid)
            print(f"✅ Trade message sent for TXN {tid}")
        except discord.Forbidden:
            print("❗ Bot does NOT have permission to send messages in the channel.")
        except Exception as e:
            print(f"❗ Failed to send trade embed: {e}")

# ——— COMMANDS ———
@bot.command(aliases=["check_week"])
async def checkweek(ctx, week: int):
    await ctx.send(f"🔍 Manually checking trades for week {week}...")
    await fetch_and_announce_trades(week_override=week)

@bot.command(name="roster")
async def roster(ctx, *, user_name: str):
    # 1. Fetch league users and find user by display_name or username matching user_name arg
    users_url = f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/users"
    rosters_url = f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/rosters"
    players_url = "https://api.sleeper.app/v1/players/nfl"

    async with aiohttp.ClientSession() as session:
        users = await fetch_json(session, users_url)
        rosters = await fetch_json(session, rosters_url)
        players_data = await fetch_json(session, players_url)

    # Map player_id to full_name
    player_names = {pid: pdata.get("full_name", "Unknown Player") for pid, pdata in players_data.items()}

    # Find the user in league users
    matched_user = None
    for user in users:
        if user_name.lower() in (user.get("display_name", "").lower(), user.get("username", "").lower()):
            matched_user = user
            break

    if not matched_user:
        await ctx.send(f"User '{user_name}' not found in the league.")
        return

    owner_id = matched_user["user_id"]

    # Find roster for this user
    user_roster = None
    for roster in rosters:
        if roster.get("owner_id") == owner_id:
            user_roster = roster
            break

    if not user_roster:
        await ctx.send(f"No roster found for user '{user_name}'.")
        return

    # Get team name if available, fallback to display_name or username
    metadata = user_roster.get("metadata") or {}
    settings = user_roster.get("settings") or {}
    team_name = metadata.get("team_name") or settings.get("team_name") or matched_user.get("display_name") or matched_user.get("username")
    # List player names on this roster
    player_ids = user_roster.get("players", [])
    if not player_ids:
        await ctx.send(f"No players found on team '{team_name}'.")
        return

    player_list = [player_names.get(pid, f"Unknown Player ({pid})") for pid in player_ids]

    # Prepare output (limit if too long)
    if len(player_list) > 30:
        player_list = player_list[:30] + ["... and more"]

    roster_str = "\n".join(f"- {p}" for p in player_list)
    await ctx.send(f"**Roster for {team_name}:**\n{roster_str}")

@bot.command(aliases=["player_trades"])
async def playertrades(ctx, *, player_name: str):
    week = datetime.now().isocalendar().week
    async with aiohttp.ClientSession() as session:
        txns_url = f"https://api.sleeper.app/v1/league/{SLEEPER_LEAGUE_ID}/transactions/{week}"
        players_url = "https://api.sleeper.app/v1/players/nfl"
        transactions = await fetch_json(session, txns_url)
        player_data = await fetch_json(session, players_url)

    name_map = get_player_name_map(player_data)
    found = False

    for txn in transactions:
        if txn.get("type") != "trade" or txn.get("status") != "complete":
            continue
        adds = txn.get("adds", {})
        reverse = {}
        for pid, rid in adds.items():
            reverse.setdefault(rid, []).append(pid)

        rosters = txn.get("roster_ids")[:2]
        if len(rosters) < 2:
            continue
        t0_ids = reverse.get(rosters[0], [])
        t1_ids = reverse.get(rosters[1], [])
        t0_names = [name_map.get(pid, pid) for pid in t0_ids]
        t1_names = [name_map.get(pid, pid) for pid in t1_ids]

        if any(player_name.lower() in name.lower() for name in t0_names + t1_names):
            team0 = user_map.get(rosters[0], f"Team {rosters[0]}")
            team1 = user_map.get(rosters[1], f"Team {rosters[1]}")
            found = True
            await ctx.send(f"🔁 Trade in Week {week} | {team0}: {', '.join(t0_names)} ➔ {team1}: {', '.join(t1_names)}")

    if not found:
        await ctx.send(f"❌ No trades involving '{player_name}' found in week {week}.")

@bot.command()
async def ktcvalue(ctx, *, player_name: str):
    value, matched_name = get_ktc_value(player_name, return_best_match=True)
    if value > 0:
        if matched_name.lower() != player_name.lower():
            await ctx.send(f"🔍 Did you mean **{matched_name}**?\n💰 KTC value: {value}")
        else:
            await ctx.send(f"💰 KTC value for **{matched_name}**: {value}")
    else:
        await ctx.send(f"❌ Could not find KTC value for **{player_name}**.\nTry checking spelling or use part of the name.")

@bot.command()
async def tradecompare(ctx, *, players: str):
    names = [n.strip() for n in players.split(",") if n.strip()]
    values = []
    for name in names:
        value, matched = get_ktc_value(name, return_best_match=True)
        values.append((matched or name, value))

    response = "📊 KTC Trade Value Comparison:\n"
    for name, value in values:
        response += f"• **{name}**: {value}\n"

    await ctx.send(response)

@bot.command()
async def ktctrend(ctx, *, player_name: str):
    await ctx.send(f"📈 KTC trend feature coming soon for {player_name}!")

@bot.command()
async def ktcteam(ctx, *, team: str):
    await ctx.send("📦 Team-based summaries not yet implemented. Placeholder.")

@bot.command()
async def ktcsource(ctx):
    await ctx.send(f"🧾 Current KTC data source: **{ktc_source_used}**")

@bot.command()
async def ktcupdate(ctx):
    """Manually update KTC values by scraping FantasyPros."""
    await ctx.send("🔄 Updating KTC values now, please wait...")
    success = fetch_ktc_fantasypros_selenium()
    if success:
        save_ktc_to_db(ktc_values)
        await ctx.send(f"✅ KTC values updated successfully for {len(ktc_values)} players!")
    else:
        await ctx.send("❌ Failed to update KTC values.")

@bot.command()
async def ktcplayers(ctx):
    """Print all player names currently stored in the KTC database."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT player_name, ktc_value FROM players ORDER BY player_name ASC")
    rows = c.fetchall()
    conn.close()

    if not rows:
        await ctx.send("❌ No player data found in the database.")
        return

    # Chunk results because of Discord message limits
    chunk_size = 50
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i:i+chunk_size]
        lines = [f"{name}: {value}" for name, value in chunk]
        msg = "\n".join(lines)
        await ctx.send(f"📋 KTC Players:\n```\n{msg}\n```")

@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    msg = reaction.message
    if msg.author != bot.user:
        return
    if reaction.emoji in ("👍", "👎"):
        print(f"Vote on {msg.id}: {reaction.emoji} by {user}")

keep_alive()
bot.run(DISCORD_TOKEN)
