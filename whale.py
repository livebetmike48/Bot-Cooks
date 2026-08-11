"""
Bot Cooks whale watch -- FULLY SELF-CONTAINED module. Exchange money flow
next to sportsbook steam:

  Polymarket (data-api.polymarket.com/trades, public, no key): trades are
    on-chain with persistent wallet addresses -> big-bet alerts AND a
    wallet board (/sharp). v1 board ranks by VOLUME with recent plays --
    honest label: volume is not proven P&L (that needs resolution data;
    a later version can grade wallets).
  Kalshi (external-api.kalshi.com/trade-api/v2/markets/trades, public):
    regulated exchange, anonymous tape -> big prints only, labeled as
    flow, never as named whales.

Thresholds (env-tunable):
  WHALE_GAME_MIN   default 50000  -- moneyline/total/game markets ($)
  WHALE_PROP_MIN   default 5000   -- player-prop-style markets ($)
  WHALE_POLL_MIN   default 3      -- poll cadence, minutes
  WHALE_CHANNEL_ID -- channel for alerts (unset = commands only)
Leagues: MLB, NFL, NBA, NHL. Classification is by title/ticker text --
game-winner style markets get the big threshold, everything else in a
league gets the prop threshold; the embed shows which bucket fired.
"""
import os
import time
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone

import requests
import discord
from discord import app_commands

log = logging.getLogger("whale")

PM_TRADES = "https://data-api.polymarket.com/trades"
KALSHI_TRADES = "https://external-api.kalshi.com/trade-api/v2/markets/trades"

GAME_MIN = float(os.getenv("WHALE_GAME_MIN", "50000") or 50000)
PROP_MIN = float(os.getenv("WHALE_PROP_MIN", "5000") or 5000)
POLL_MIN = max(1, int(os.getenv("WHALE_POLL_MIN", "3") or 3))
CHANNEL_ID = int(os.getenv("WHALE_CHANNEL_ID", "0") or 0)
DB = os.getenv("WHALE_DB", "whale.db")

LEAGUES = ("mlb", "nfl", "nba", "nhl")
PROP_HINTS = ("strikeout", "points", "touchdown", "passing", "rushing",
              "receiving", "rebounds", "assists", "home run", "hits",
              "total bases", "goals", "saves", "shots")


def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS whale_trades (
        source TEXT, trade_id TEXT, wallet TEXT, title TEXT, side TEXT,
        outcome TEXT, notional REAL, price REAL, bucket TEXT, league TEXT,
        ts INTEGER, PRIMARY KEY (source, trade_id))""")
    return c


def _league_of(text: str) -> str | None:
    t = (text or "").lower()
    for lg in LEAGUES:
        if lg in t:
            return lg
    return None


def _bucket_of(title: str) -> str:
    t = (title or "").lower()
    return "prop" if any(h in t for h in PROP_HINTS) else "game"


def _fmt_usd(x: float) -> str:
    return f"${x:,.0f}"


# ---------- Polymarket ----------

def poll_polymarket(state: dict) -> list[dict]:
    """New league trades over threshold since the last poll. Every trade
    (big or not) from tracked leagues goes into the wallet ledger so the
    /sharp board sees full volume, not only alerts."""
    try:
        r = requests.get(PM_TRADES, params={"limit": 500, "takerOnly": "true"},
                         timeout=20)
        r.raise_for_status()
        trades = r.json()
        if isinstance(trades, dict):
            trades = trades.get("trades") or trades.get("data") or []
    except Exception as e:
        log.warning("polymarket poll failed: %s", e)
        return []
    alerts = []
    last_ts = state.get("pm_last_ts", 0)
    newest = last_ts
    rows = []
    for t in trades:
        ts = int(t.get("timestamp") or 0)
        if ts <= last_ts:
            continue
        newest = max(newest, ts)
        blob = " ".join(str(t.get(k) or "") for k in ("title", "slug", "eventSlug"))
        league = _league_of(blob)
        if not league:
            continue
        try:
            size = float(t.get("size") or 0)
            price = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        notional = float(t.get("usdcSize") or 0) or round(size * price, 2)
        title = t.get("title") or t.get("eventSlug") or "?"
        bucket = _bucket_of(title)
        wallet = t.get("proxyWallet") or t.get("user") or "?"
        trade_id = t.get("transactionHash") or f"{wallet}-{ts}-{t.get('asset','')}"
        rows.append(("polymarket", trade_id, wallet, title,
                     (t.get("side") or "").upper(), t.get("outcome") or "",
                     notional, price, bucket, league, ts))
        if notional >= (GAME_MIN if bucket == "game" else PROP_MIN):
            alerts.append({"source": "Polymarket", "title": title,
                           "side": (t.get("side") or "").upper(),
                           "outcome": t.get("outcome") or "",
                           "notional": notional, "price": price,
                           "bucket": bucket, "league": league,
                           "wallet": wallet, "ts": ts})
    if rows:
        with _conn() as c:
            c.executemany("INSERT OR IGNORE INTO whale_trades VALUES "
                          "(?,?,?,?,?,?,?,?,?,?,?)", rows)
    state["pm_last_ts"] = newest
    return alerts


# ---------- Kalshi ----------

def poll_kalshi(state: dict) -> list[dict]:
    """Big prints on league markets. Anonymous by design -- no wallet is
    stored or implied; block trades flagged as such."""
    try:
        r = requests.get(KALSHI_TRADES, params={"limit": 500}, timeout=20)
        r.raise_for_status()
        trades = (r.json() or {}).get("trades") or []
    except Exception as e:
        log.warning("kalshi poll failed: %s", e)
        return []
    alerts = []
    last = state.get("kalshi_last", "")
    newest = last
    rows = []
    for t in trades:
        created = t.get("created_time") or ""
        if created <= last:
            continue
        newest = max(newest, created)
        ticker = t.get("ticker") or ""
        league = _league_of(ticker)
        if not league:
            continue
        try:
            count = float(t.get("count_fp") or t.get("count") or 0)
            price = float(t.get("yes_price_dollars") or 0) or \
                (float(t.get("yes_price") or 0) / 100.0)
        except (TypeError, ValueError):
            continue
        notional = round(count * price, 2)
        # Kalshi tickers are abbreviated (PASSYDS, RECYDS...), so word
        # hints don't work: game bucket = GAME/TOTAL/SPREAD series, every
        # other league series is a stat/prop market -> prop threshold.
        tu = ticker.upper()
        bucket = ("game" if any(k in tu for k in ("GAME", "TOTAL", "SPREAD"))
                  else "prop")
        trade_id = t.get("trade_id") or f"{ticker}-{created}"
        try:
            ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
        except Exception:
            ts = int(time.time())
        rows.append(("kalshi", trade_id, None, ticker,
                     (t.get("taker_side") or "").upper(), "",
                     notional, price, bucket, league, ts))
        if notional >= (GAME_MIN if bucket == "game" else PROP_MIN):
            alerts.append({"source": "Kalshi", "title": ticker,
                           "side": (t.get("taker_side") or "").upper(),
                           "outcome": "block trade" if t.get("is_block_trade") else "",
                           "notional": notional, "price": price,
                           "bucket": bucket, "league": league,
                           "wallet": None, "ts": ts})
    if rows:
        with _conn() as c:
            c.executemany("INSERT OR IGNORE INTO whale_trades VALUES "
                          "(?,?,?,?,?,?,?,?,?,?,?)", rows)
    state["kalshi_last"] = newest
    return alerts


def alert_embed(a: dict) -> discord.Embed:
    thresh = "game" if a["bucket"] == "game" else "prop"
    emb = discord.Embed(
        title=f"🐋 {_fmt_usd(a['notional'])} on {a['source']}",
        description=f"**{a['title']}**", color=0x1B7F4D)
    bits = []
    if a.get("side"):
        bits.append(a["side"].title())
    if a.get("outcome"):
        bits.append(a["outcome"])
    bits.append(f"@ {a['price']:.2f}")
    emb.add_field(name="Trade", value=" · ".join(bits), inline=True)
    emb.add_field(name="Bucket", value=f"{a['league'].upper()} {thresh}", inline=True)
    if a.get("wallet"):
        w = a["wallet"]
        emb.add_field(name="Wallet", value=f"`{w[:6]}…{w[-4:]}`", inline=True)
        emb.set_footer(text="Polymarket wallets persist — /sharp tracks them")
    else:
        emb.set_footer(text="Kalshi tape is anonymous — flow, not a named whale")
    return emb


def whale_today() -> list[dict]:
    cutoff = int(time.time()) - 24 * 3600
    with _conn() as c:
        rows = c.execute(
            "SELECT source, title, side, notional, price, bucket, league, wallet "
            "FROM whale_trades WHERE ts >= ? AND "
            "((bucket='game' AND notional >= ?) OR (bucket='prop' AND notional >= ?)) "
            "ORDER BY notional DESC LIMIT 10",
            (cutoff, GAME_MIN, PROP_MIN)).fetchall()
    return [{"source": s, "title": t, "side": sd, "notional": n, "price": p,
             "bucket": b, "league": lg, "wallet": w}
            for s, t, sd, n, p, b, lg, w in rows]


def sharp_board() -> list[dict]:
    """Top Polymarket wallets by 7-day league volume + their biggest play.
    Volume board, honestly labeled -- not proven P&L."""
    cutoff = int(time.time()) - 7 * 24 * 3600
    with _conn() as c:
        rows = c.execute(
            "SELECT wallet, COUNT(*), SUM(notional), MAX(notional) "
            "FROM whale_trades WHERE source='polymarket' AND wallet IS NOT NULL "
            "AND ts >= ? GROUP BY wallet ORDER BY SUM(notional) DESC LIMIT 8",
            (cutoff,)).fetchall()
        out = []
        for wallet, n, vol, biggest in rows:
            top = c.execute(
                "SELECT title, side, notional FROM whale_trades "
                "WHERE wallet=? AND ts>=? ORDER BY notional DESC LIMIT 1",
                (wallet, cutoff)).fetchone()
            out.append({"wallet": wallet, "trades": n, "volume": vol,
                        "biggest": biggest, "top_play": top})
    return out


async def poll_task(bot):
    state: dict = {}
    log.info("whale watch up — poll %dmin, game>=%s prop>=%s, channel %s",
             POLL_MIN, _fmt_usd(GAME_MIN), _fmt_usd(PROP_MIN),
             CHANNEL_ID or "unset (commands only)")
    # first pass primes the last-seen cursors without alert-spamming history
    try:
        await asyncio.to_thread(poll_polymarket, state)
        await asyncio.to_thread(poll_kalshi, state)
    except Exception as e:
        log.warning("whale prime failed: %s", e)
    while True:
        await asyncio.sleep(POLL_MIN * 60)
        alerts = []
        for fn in (poll_polymarket, poll_kalshi):
            try:
                alerts.extend(await asyncio.to_thread(fn, state))
            except Exception as e:
                log.error("whale poll error: %s", e)
        if not alerts or not CHANNEL_ID:
            continue
        ch = bot.get_channel(CHANNEL_ID)
        if not ch:
            continue
        for a in alerts[:6]:
            try:
                await ch.send(embed=alert_embed(a))
            except Exception as e:
                log.error("whale alert send failed: %s", e)


async def whale_callback(interaction: discord.Interaction):
    await interaction.response.defer()
    rows = await asyncio.to_thread(whale_today)
    if not rows:
        await interaction.followup.send(
            "No threshold-size exchange bets in the last 24h "
            f"(game ≥ {_fmt_usd(GAME_MIN)}, props ≥ {_fmt_usd(PROP_MIN)}).")
        return
    emb = discord.Embed(title="🐋 Whale watch — last 24h", color=0x1B7F4D)
    lines = []
    for i, r in enumerate(rows, 1):
        who = f" `{r['wallet'][:6]}…`" if r.get("wallet") else ""
        lines.append(f"**{i}. {_fmt_usd(r['notional'])}** — {r['title']} "
                     f"({r['source']}, {r['league'].upper()} {r['bucket']}, "
                     f"@{r['price']:.2f}){who}")
    emb.description = "\n".join(lines)
    emb.set_footer(text="Polymarket + Kalshi public tape · thresholds env-tunable")
    await interaction.followup.send(embed=emb)


async def sharp_callback(interaction: discord.Interaction):
    await interaction.response.defer()
    board = await asyncio.to_thread(sharp_board)
    if not board:
        await interaction.followup.send(
            "No tracked Polymarket league wallets yet — the ledger builds "
            "as the poll runs.")
        return
    emb = discord.Embed(title="🎯 Sharp board — Polymarket wallets, 7d volume",
                        color=0x1B7F4D)
    lines = []
    for i, w in enumerate(board, 1):
        top = w["top_play"]
        play = f" · top: {top[0]} ({_fmt_usd(top[2])})" if top else ""
        lines.append(f"**{i}. `{w['wallet'][:6]}…{w['wallet'][-4:]}`** — "
                     f"{_fmt_usd(w['volume'])} across {w['trades']} trades{play}")
    emb.description = "\n".join(lines)
    emb.set_footer(text="Volume board — big and active, not proven P&L. "
                        "Graded wallet records need resolution data (later version).")
    await interaction.followup.send(emb=None, embed=emb)


def setup(bot):
    """Register /whale and /sharp on the bot's tree. Call from setup_hook."""
    bot.tree.add_command(app_commands.Command(
        name="whale", description="Biggest exchange bets (Polymarket + Kalshi), last 24h",
        callback=whale_callback))
    bot.tree.add_command(app_commands.Command(
        name="sharp", description="Top Polymarket wallets by league volume (7d)",
        callback=sharp_callback))
