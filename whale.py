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

ALERT POLICY (Mike, Aug 21 v2): three market classes, two alert bars,
one real-time rule.
  Moneylines + game totals  -> alert at WHALE_GAME_MIN (default $250,000)
  PLAYER props (strikeouts, outs, points, passing yards, ...) -> alert
      at WHALE_PROP_MIN (default $25,000) -- player props trade thin, a
      $25K single actor IS the whale there
  NRFI/YRFI + first-inning markets, spreads, series winners, anything
      unrecognized -> NEVER notify (ledger only). The old bot's "prop"
      alerts were almost all NRFI/YRFI -- the liquid junk drowned the
      real player props. That class is now explicitly silent.
Real-time = a single print over the class bar, or ONE ACTOR's fills
summing past it inside the burst window (auto-widened to cover the poll
gap, so any WHALE_POLL_MIN works). One alert per market/side/actor per
ET day. /whale and /sharp still see everything.

Env:
  WHALE_GAME_MIN         default 250000 -- moneyline/total bar ($)
  WHALE_PROP_MIN         default 25000  -- PLAYER prop bar ($)
  WHALE_BURST_WINDOW_MIN default 10     -- same-actor burst window (0=off)
  WHALE_POLL_MIN         default 3      -- poll cadence, minutes
  WHALE_CHANNEL_ID       -- channel for alerts (unset = commands only)
  WHALE_ACCUM_WINDOW_MIN -- RETIRED, ignored
Leagues: MLB, NFL, NBA, NHL.
"""
import os
import re
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

GAME_MIN = float(os.getenv("WHALE_GAME_MIN", "250000") or 250000)
PROP_MIN = float(os.getenv("WHALE_PROP_MIN", "25000") or 25000)
POLL_MIN = max(1, int(os.getenv("WHALE_POLL_MIN", "3") or 3))
# Big orders FRAGMENT: a $250K wager sweeps the book as many smaller
# fills, none of which trips the single-print bar (Mike proved it with
# his own test bet). The burst window keeps that lesson without the old
# spam: ONE ACTOR (a wallet on Polymarket; a market+side on Kalshi's
# anonymous tape) summing past the bar inside a few minutes is a whale
# landing in real time. Market-wide hour-long accumulation is retired.
BURST_WINDOW_MIN = max(0, int(os.getenv("WHALE_BURST_WINDOW_MIN", "10") or 10))
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
    c.execute("""CREATE TABLE IF NOT EXISTS whale_accum_alerts (
        akey TEXT PRIMARY KEY, ts INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS whale_day_alerts (
        dkey TEXT PRIMARY KEY, ts INTEGER)""")
    return c


def _league_of(text: str) -> str | None:
    t = (text or "").lower()
    for lg in LEAGUES:
        if lg in t:
            return lg
    return None


TOTAL_HINTS = ("total", "o/u", "over/under", "combined", "over ", "under ")
# first-inning junk that used to masquerade as "props" and spam every day
INNING_HINTS = ("nrfi", "yrfi", "1st inning", "first inning", "run in the")
# Kalshi ticker series tokens that mean a PLAYER stat market
KALSHI_STAT_SERIES = ("PASSYDS", "RUSHYDS", "RECYDS", "RECEP", "PTS", "REB",
                      "AST", "STRIKEOUT", "SO", "KS", "OUTS", "HITS", "HR",
                      "TB", "SAVES", "GOALS", "SHOTS", "TDS", "COMP")


def _bucket_of(title: str) -> str:
    t = (title or "").lower()
    return "prop" if any(h in t for h in PROP_HINTS) else "game"


def _pm_market_type(title: str) -> str:
    """moneyline / total / inning / spread / prop for a Polymarket title.
    Totals before prop hints ('Total points scored' is a game total);
    first-inning junk named before anything can call it a prop."""
    t = (title or "").lower()
    if any(h in t for h in INNING_HINTS):
        return "inning"
    if any(h in t for h in TOTAL_HINTS):
        return "total"
    if "spread" in t or re.search(r"[+-]\d+\.5", t):
        return "spread"
    if any(h in t for h in PROP_HINTS):
        return "prop"
    return "moneyline"


def _kalshi_market_type(ticker: str) -> str:
    """From the ticker alone: inning / moneyline / total / spread /
    series / prop / other. 'other' gets one shot at the REAL market
    title (cached) before the alert decision -- never before."""
    tu = (ticker or "").upper()
    if "RFI" in tu:
        return "inning"
    if "TOTAL" in tu:
        return "total"
    if "SPREAD" in tu:
        return "spread"
    if "GAME" in tu:
        return "moneyline"
    if "SERIES" in tu:
        return "series"
    if any(k in tu for k in KALSHI_STAT_SERIES):
        return "prop"
    return "other"


def _resolve_other(source: str, title: str, mtype: str) -> str:
    """A Kalshi 'other' resolves through its real market title exactly
    once (cached), at alert-decision time only."""
    if mtype != "other" or (source or "").lower() != "kalshi":
        return mtype
    real = kalshi_title(title).lower()
    if any(h in real for h in INNING_HINTS):
        return "inning"
    if any(h in real for h in PROP_HINTS) or " record " in real or "outs" in real:
        return "prop"
    if any(h in real for h in TOTAL_HINTS):
        return "total"
    return "other"


def _bar_for(mtype: str) -> float | None:
    """The alert bar per market class. None = that class never notifies:
    first-inning markets, spreads, series winners, unrecognized."""
    if mtype in ("moneyline", "total"):
        return GAME_MIN
    if mtype == "prop":
        return PROP_MIN
    return None


def _alertable(mtype: str) -> bool:
    return _bar_for(mtype) is not None


def _et_day(ts: int | None = None) -> str:
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(ts or time.time(), tz=ZoneInfo("America/New_York"))
    except Exception:
        dt = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _day_dedupe(c, source: str, wallet: str | None, title: str, side: str,
                ts: int) -> bool:
    """True exactly once per (market, side, actor) per ET day. Every alert
    path runs through this -- the same whale laddering, re-printing, or
    the market staying hot NEVER notifies twice in a day."""
    dkey = f"{source}|{wallet or ''}|{title}|{side}|{_et_day(ts)}"
    row = c.execute("SELECT 1 FROM whale_day_alerts WHERE dkey=?", (dkey,)).fetchone()
    if row:
        return False
    c.execute("INSERT OR REPLACE INTO whale_day_alerts VALUES (?, ?)", (dkey, ts))
    return True


KALSHI_MARKET_URL = "https://external-api.kalshi.com/trade-api/v2/markets/{}"
_kalshi_titles: dict = {}
_KALSHI_SERIES = {"RFI": "Run in the 1st inning", "GAME": "Game winner",
                  "TOTAL": "Total", "SPREAD": "Spread",
                  "PASSYDS": "Passing yards", "RUSHYDS": "Rushing yards",
                  "RECYDS": "Receiving yards", "PTS": "Points",
                  "SERIES": "Series winner", "HR": "Home run"}


def _decode_kalshi_ticker(ticker: str) -> str:
    """Best-effort human label straight from the ticker, for when the
    title lookup fails: KXMLBRFI-26AUG111845CHCWSH ->
    'MLB CHC/WSH — Run in the 1st inning'."""
    try:
        head = ticker.split("-", 1)[0].upper()
        if head.startswith("KX"):
            head = head[2:]
        league = next((lg.upper() for lg in LEAGUES if head.startswith(lg.upper())), "")
        series = head[len(league):]
        series_name = _KALSHI_SERIES.get(series, series.title())
        tail = ticker.split("-")[-1]
        alpha = "".join(ch for ch in tail if ch.isalpha())
        # team codes are the LAST six letters (the date contributes "AUG")
        teams = f"{alpha[-6:-3]}/{alpha[-3:]}" if len(alpha) >= 6 else alpha
        parts = [p for p in (league, teams) if p]
        return f"{' '.join(parts)} — {series_name}" if parts else series_name
    except Exception:
        return ticker


def kalshi_title(ticker: str) -> str:
    """The market's REAL question ('Will a run be scored in the 1st
    inning...?') from Kalshi's public market endpoint, cached per ticker
    for the process lifetime; decoder fallback so display never breaks."""
    if ticker in _kalshi_titles:
        return _kalshi_titles[ticker]
    title = None
    try:
        r = requests.get(KALSHI_MARKET_URL.format(ticker), timeout=10)
        if r.status_code == 200:
            m = (r.json() or {}).get("market") or {}
            title = m.get("title") or None
            sub = m.get("yes_sub_title") or ""
            if title and sub and sub.lower() not in title.lower():
                title = f"{title} ({sub})"
    except Exception as e:
        log.warning("kalshi title lookup failed for %s: %s", ticker, e)
    if not title:
        title = _decode_kalshi_ticker(ticker)
    _kalshi_titles[ticker] = title
    return title


def display_title(source: str, title: str) -> str:
    return kalshi_title(title) if (source or "").lower() == "kalshi" else title


def _fmt_usd(x: float) -> str:
    return f"${x:,.0f}"


# ---------- Polymarket ----------

def poll_polymarket(state: dict) -> list[dict]:
    """New league trades over threshold since the last poll. Every trade
    (big or not) from tracked leagues goes into the wallet ledger so the
    /sharp board sees full volume, not only alerts."""
    trades = []
    try:
        for offset in (0, 500):
            r = requests.get(PM_TRADES,
                             params={"limit": 500, "offset": offset,
                                     "takerOnly": "true"}, timeout=20)
            r.raise_for_status()
            page = r.json()
            if isinstance(page, dict):
                page = page.get("trades") or page.get("data") or []
            if not page:
                break
            trades.extend(page)
            oldest = min(int(t.get("timestamp") or 0) for t in page)
            if oldest <= state.get("pm_last_ts", 0):
                break
    except Exception as e:
        log.warning("polymarket poll failed: %s", e)
        if not trades:
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
        mtype = _pm_market_type(title)
        bar = _bar_for(mtype)
        if bar is not None and notional >= bar:
            alerts.append({"source": "Polymarket", "title": title,
                           "side": (t.get("side") or "").upper(),
                           "outcome": t.get("outcome") or "",
                           "notional": notional, "price": price,
                           "bucket": bucket, "mtype": mtype, "league": league,
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
    trades = []
    cursor = None
    last_seen = state.get("kalshi_last", "")
    try:
        for _page in range(4):  # busy tape can exceed one page per poll
            params = {"limit": 500}
            if cursor:
                params["cursor"] = cursor
            r = requests.get(KALSHI_TRADES, params=params, timeout=20)
            r.raise_for_status()
            j = r.json() or {}
            page = j.get("trades") or []
            trades.extend(page)
            cursor = j.get("cursor")
            if not cursor or not page:
                break
            oldest = min((t.get("created_time") or "") for t in page)
            if last_seen and oldest <= last_seen:
                break  # walked back past what we've already seen
    except Exception as e:
        log.warning("kalshi poll failed: %s", e)
        if not trades:
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
        mtype = _kalshi_market_type(ticker)
        if mtype == "other" and notional >= PROP_MIN:
            mtype = _resolve_other("kalshi", ticker, mtype)
        bar = _bar_for(mtype)
        if bar is not None and notional >= bar:
            alerts.append({"source": "Kalshi", "title": ticker,
                           "side": (t.get("taker_side") or "").upper(),
                           "outcome": "block trade" if t.get("is_block_trade") else "",
                           "notional": notional, "price": price,
                           "bucket": bucket, "mtype": mtype, "league": league,
                           "wallet": None, "ts": ts})
    if rows:
        with _conn() as c:
            c.executemany("INSERT OR IGNORE INTO whale_trades VALUES "
                          "(?,?,?,?,?,?,?,?,?,?,?)", rows)
    state["kalshi_last"] = newest
    return alerts


def burst_alerts() -> list[dict]:
    """ONE ACTOR crossing the bar in fragments, in real time: a wallet
    (Polymarket) or a market+side (Kalshi, anonymous tape) whose fills
    inside the last BURST_WINDOW_MIN sum past GAME_MIN without any single
    print doing so. Moneyline/total markets only, like every alert. The
    old rolling-hour market-wide accumulation is gone -- that was crowd
    volume, not a whale, and it re-fired all day."""
    if BURST_WINDOW_MIN <= 0:
        return []
    # a 30-min poll with a 10-min window would drop fills between polls --
    # the effective window always covers at least one full poll gap
    eff_window = max(BURST_WINDOW_MIN, POLL_MIN + 2)
    cutoff = int(time.time()) - eff_window * 60
    out = []
    with _conn() as c:
        rows = c.execute(
            "SELECT source, COALESCE(wallet, ''), title, side, bucket, league, "
            "SUM(notional), COUNT(*), MAX(notional), MAX(ts), MAX(price) "
            "FROM whale_trades WHERE ts >= ? "
            "GROUP BY source, COALESCE(wallet, ''), title, side",
            (cutoff,)).fetchall()
        for (src_, wallet, title, side, bucket, league,
             total, n, biggest, last_ts, last_px) in rows:
            mtype = (_kalshi_market_type(title) if src_ == "kalshi"
                     else _pm_market_type(title))
            bar = _bar_for(mtype)
            if bar is None and mtype == "other" and total >= PROP_MIN:
                mtype = _resolve_other(src_, title, mtype)
                bar = _bar_for(mtype)
            if bar is None:
                continue
            if total < bar or biggest >= bar or n < 2:
                continue  # single big print already alerts on its own
            if not _day_dedupe(c, src_, wallet or None, title, side or "", last_ts):
                continue
            out.append({"source": src_.title(), "title": title,
                        "side": side or "",
                        "outcome": f"{n} fills in {eff_window}m",
                        "notional": round(total, 2), "price": last_px or 0.0,
                        "bucket": bucket, "mtype": mtype, "league": league,
                        "wallet": wallet or None, "accum": True, "ts": last_ts})
    return out


def alert_embed(a: dict) -> discord.Embed:
    mtype = a.get("mtype") or ("game" if a["bucket"] == "game" else "prop")
    emb = discord.Embed(
        title=(f"🐋 {_fmt_usd(a['notional'])} burst on {a['source']}"
               if a.get("accum") else
               f"🐋 {_fmt_usd(a['notional'])} on {a['source']}"),
        description=(f"**{display_title(a['source'], a['title'])}**"
                     + (f"\n`{a['title']}`"
                        if (a['source'] or '').lower() == 'kalshi' else "")),
        color=0x1B7F4D)
    bits = []
    if a.get("side"):
        # the bet, plainly: Kalshi YES/NO on the titled question;
        # Polymarket BUY/SELL of the named outcome
        side = a["side"].title()
        bits.append(f"{side} {a['outcome']}".strip()
                    if a.get("outcome") and not a.get("accum") else side)
    elif a.get("outcome") and not a.get("accum"):
        bits.append(a["outcome"])
    if a.get("price"):
        bits.append(f"@ {a['price']:.2f} ({a['price']:.0%} implied)")
    if a.get("accum"):
        bits.append(a.get("outcome") or "burst")
    emb.add_field(name="The bet", value=" · ".join(bits) or "—", inline=True)
    label = {"moneyline": "Moneyline", "total": "Total", "prop": "Player Prop",
             "inning": "1st Inning", "spread": "Spread"}.get(mtype, mtype.title())
    emb.add_field(name="Market", value=f"{a['league'].upper()} {label}",
                  inline=True)
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
            "SELECT source, title, MAX(side), SUM(notional), MAX(price), bucket, "
            "league, COALESCE(wallet,'') "
            "FROM whale_trades WHERE ts >= ? "
            "GROUP BY source, COALESCE(wallet,''), title, bucket, league "
            "HAVING (bucket='game' AND SUM(notional) >= ?) "
            "    OR (bucket='prop' AND SUM(notional) >= ?) "
            "ORDER BY SUM(notional) DESC LIMIT 10",
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
    log.info("whale watch up — poll %dmin, ML/totals >= %s, player props >= %s, "
             "burst %smin, channel %s (NRFI/inning + spreads: ledger only)",
             POLL_MIN, _fmt_usd(GAME_MIN), _fmt_usd(PROP_MIN),
             BURST_WINDOW_MIN or "off", CHANNEL_ID or "unset (commands only)")
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
        try:
            alerts.extend(await asyncio.to_thread(burst_alerts))
        except Exception as e:
            log.error("whale burst error: %s", e)
        # one alert per market/side/actor per ET day -- prints included,
        # so a whale re-printing every hour is one notification, not ten
        def _dedupe_prints(items):
            keep = []
            with _conn() as c:
                for a in items:
                    if a.get("accum"):
                        keep.append(a)  # burst path already deduped
                    elif _day_dedupe(c, a["source"].lower(), a.get("wallet"),
                                     a["title"], a.get("side") or "", a["ts"]):
                        keep.append(a)
            return keep
        try:
            alerts = await asyncio.to_thread(_dedupe_prints, alerts)
        except Exception as e:
            log.error("whale dedupe error: %s", e)
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
            f"(ML/totals ≥ {_fmt_usd(GAME_MIN)}, player props ≥ {_fmt_usd(PROP_MIN)}).")
        return
    emb = discord.Embed(title="🐋 Whale watch — last 24h", color=0x1B7F4D)
    lines = []
    for i, r in enumerate(rows, 1):
        who = f" `{r['wallet'][:6]}…`" if r.get("wallet") else ""
        lines.append(f"**{i}. {_fmt_usd(r['notional'])}** — "
                     f"{display_title(r['source'], r['title'])} "
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
