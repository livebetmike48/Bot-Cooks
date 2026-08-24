"""
PROPS TRACKER -- pitcher + batter props: silent all-day tracking, command-only.

NO automated messages, ever. The bot polls quietly in the background (you
can't show an opening line you never saw) and only speaks when asked:

  /prop <player> [market]     -- OPEN -> NOW per book, with movement
  /propboard <market>         -- current slate board, latest per player/book
  /propgraph <player> <market> -- PNG chart of today's line + Over price

Markets tracked (Mike's set): Outs, K's, Hits Allowed, Walks,
Hits, Total Bases, H+R+RBI -- all in ONE Odds API call per event
(7 credits/event/poll; 15 games @ 10-min polls ~= 7.5K/day ~= 230K/mo).

Env:
  PROPS_DB        -- sqlite path (volume, e.g. /data/props.db)
  PROPS_POLL_MIN  -- default 10
  PROPS_BOOKS     -- default fanduel,draftkings,betmgm,williamhill_us
  PROPS=0         -- kill switch
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import odds_api

log = logging.getLogger("props")

ET = ZoneInfo("America/New_York")
ENABLED = os.getenv("PROPS", "1") not in ("0", "false", "off")
POLL_MIN = max(2, int(os.getenv("PROPS_POLL_MIN", "10") or 10))
DB = os.getenv("PROPS_DB", "props.db")
BOOKS = [b.strip().lower() for b in os.getenv(
    "PROPS_BOOKS", "fanduel,draftkings,betmgm,williamhill_us").split(",") if b.strip()]
BOOK_NAMES = {"fanduel": "FanDuel", "draftkings": "DraftKings",
              "betmgm": "BetMGM", "williamhill_us": "Caesars"}

MARKETS = {
    "pitcher_outs": "Outs",
    "pitcher_strikeouts": "K's",
    "pitcher_hits_allowed": "Hits Allowed",
    "pitcher_walks": "Walks",
    "batter_hits": "Hits",
    "batter_total_bases": "Total Bases",
    "batter_hits_runs_rbis": "H+R+RBI",
}
MARKET_PARAM = ",".join(MARKETS)
CHOICES = [app_commands.Choice(name=v, value=k) for k, v in MARKETS.items()]


def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS props_history (
        ts INTEGER, day TEXT, event_id TEXT, market TEXT, player TEXT,
        book TEXT, line REAL, over INTEGER, under INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ph ON props_history "
              "(day, market, player, book, ts)")
    return c


def _today() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _tlabel(ts: int) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%-I:%M%p").lower()


def _fmt(p) -> str:
    if p is None:
        return "—"
    return f"+{p}" if p > 0 else str(p)


# ------------------------------------------------------------------ polling

def extract_quotes(event_data: dict) -> dict[tuple[str, str, str], dict]:
    """(market, player, book) -> {line, over, under} from one event payload."""
    out: dict[tuple[str, str, str], dict] = {}
    for bm in (event_data or {}).get("bookmakers", []):
        bk = (bm.get("key") or "").lower()
        if bk not in BOOKS:
            continue
        for mk in bm.get("markets", []):
            mkey = mk.get("key")
            if mkey not in MARKETS:
                continue
            per: dict[str, dict] = {}
            for oc in mk.get("outcomes", []):
                name = oc.get("description") or ""
                side = (oc.get("name") or "").lower()
                if not name:
                    continue
                q = per.setdefault(name, {})
                if "over" in side:
                    q["line"] = oc.get("point")
                    q["over"] = oc.get("price")
                elif "under" in side:
                    q.setdefault("line", oc.get("point"))
                    q["under"] = oc.get("price")
            for player, q in per.items():
                if q.get("line") is not None:
                    out[(mkey, player, bk)] = q
    return out


def record_poll(quotes: dict[tuple[str, str, str], dict], event_id: str,
                ts: int | None = None) -> int:
    """Snapshot changed quotes only. Returns rows written. Never speaks."""
    ts = ts or int(time.time())
    day = _today()
    wrote = 0
    with _conn() as c:
        for (market, player, book), q in quotes.items():
            last = c.execute(
                "SELECT line, over, under FROM props_history WHERE day=? AND "
                "event_id=? AND market=? AND player=? AND book=? "
                "ORDER BY ts DESC LIMIT 1",
                (day, event_id, market, player, book)).fetchone()
            cur = (q.get("line"), q.get("over"), q.get("under"))
            if last is None or tuple(last) != cur:
                c.execute("INSERT INTO props_history VALUES (?,?,?,?,?,?,?,?,?)",
                          (ts, day, event_id, market, player, book, *cur))
                wrote += 1
    return wrote


async def poll_once() -> int:
    events = await asyncio.to_thread(odds_api.get_events)
    wrote = 0
    for ev in events or []:
        data = await asyncio.to_thread(odds_api.get_event_props, ev.get("id"),
                                       MARKET_PARAM)
        if not data:
            continue
        wrote += record_poll(extract_quotes(data), ev.get("id") or "")
    return wrote


async def poll_task(bot):
    if not ENABLED:
        log.info("PROPS=0 — props tracker off")
        return
    await bot.wait_until_ready()
    log.info("Props tracker: silent polling every %dm, %d markets, books %s "
             "— command-only output", POLL_MIN, len(MARKETS), ",".join(BOOKS))
    while not bot.is_closed():
        try:
            n = await poll_once()
            if n:
                log.info("props: %d snapshots stored", n)
        except Exception:
            log.exception("props poll failed")
        await asyncio.sleep(POLL_MIN * 60)


# ------------------------------------------------------------------ queries

def open_now(player: str | None, market: str | None = None):
    """[(market, matched_player, book, open_row, now_row)] for today.
    Rows are (ts, line, over, under). player=None matches everyone."""
    day = _today()
    with _conn() as c:
        rows = c.execute(
            "SELECT market, player, book, ts, line, over, under "
            "FROM props_history WHERE day=? ORDER BY ts", (day,)).fetchall()
    firsts: dict[tuple, tuple] = {}
    lasts: dict[tuple, tuple] = {}
    for mk, p, b, ts, line, over, under in rows:
        if player and player.lower() not in p.lower():
            continue
        if market and mk != market:
            continue
        k = (mk, p, b)
        if k not in firsts:
            firsts[k] = (ts, line, over, under)
        lasts[k] = (ts, line, over, under)
    return [(k[0], k[1], k[2], firsts[k], lasts[k]) for k in firsts]


def board(market: str) -> dict[str, dict[str, tuple]]:
    """player -> book -> latest (ts, line, over, under) for today."""
    day = _today()
    with _conn() as c:
        rows = c.execute(
            "SELECT player, book, line, over, under, MAX(ts) FROM props_history "
            "WHERE day=? AND market=? GROUP BY player, book", (day, market)).fetchall()
    out: dict[str, dict[str, tuple]] = {}
    for p, b, line, over, under, ts in rows:
        out.setdefault(p, {})[b] = (ts, line, over, under)
    return out


def day_series(player: str, market: str):
    day = _today()
    with _conn() as c:
        rows = c.execute(
            "SELECT player, book, ts, line, over FROM props_history "
            "WHERE day=? AND market=? ORDER BY ts", (day, market)).fetchall()
    series: dict[str, list] = {}
    for p, b, ts, line, over in rows:
        if player.lower() in p.lower():
            series.setdefault(b, []).append((ts, line, over))
    return series


def render_all(player: str) -> list[tuple[str, io.BytesIO]]:
    """One graph per market that has data for this player today."""
    out = []
    for mk in MARKETS:
        buf = render_graph(player, mk)
        if buf:
            out.append((mk, buf))
    return out


def render_graph(player: str, market: str) -> io.BytesIO | None:
    series = day_series(player, market)
    if not series:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 6), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    for book, pts in series.items():
        xs = [datetime.fromtimestamp(t, ET) for t, _, _ in pts]
        ax1.step(xs, [l for _, l, _ in pts], where="post",
                 label=BOOK_NAMES.get(book, book), linewidth=2)
        ax2.step(xs, [o for _, _, o in pts], where="post", linewidth=1.5)
    ax1.set_ylabel(f"{MARKETS.get(market, market)} line")
    ax1.set_title(f"{player} — {MARKETS.get(market, market)} today (ET)")
    ax1.legend(loc="best", fontsize=8)
    ax1.grid(alpha=0.3)
    ax2.set_ylabel("Over price")
    ax2.grid(alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%-I%p", tz=ET))
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    buf.seek(0)
    return buf


def _imp(american) -> float | None:
    """American odds -> implied probability (0..1)."""
    if american is None:
        return None
    a = float(american)
    return 100.0 / (a + 100.0) if a > 0 else -a / (-a + 100.0)


def movers(market: str | None = None):
    """Today's biggest movers from stored quotes. Returns
    (line_moves, price_moves): line_moves = [(delta_line, mk, player, book,
    open_row, now_row)] sorted by |line change|; price_moves (same line only)
    = [(prob_pts, mk, player, book, open_row, now_row)] sorted by Over-price
    probability-point change. No averaging — every row is one real book."""
    line_moves, price_moves = [], []
    for mk, p, b, o, n in open_now(None, market):
        _, ol, oo, ou = o
        _, nl, no, nu = n
        if ol is not None and nl is not None and ol != nl:
            line_moves.append((abs(nl - ol), mk, p, b, o, n))
        elif oo is not None and no is not None and oo != no:
            i0, i1 = _imp(oo), _imp(no)
            if i0 is not None and i1 is not None:
                price_moves.append((abs(i1 - i0) * 100, mk, p, b, o, n))
    line_moves.sort(key=lambda x: -x[0])
    price_moves.sort(key=lambda x: -x[0])
    return line_moves, price_moves


# ------------------------------------------------------------------ commands

def _move_line(open_row, now_row) -> str:
    _, ol, oo, ou = open_row
    ts, nl, no, nu = now_row
    opened = f"{ol} (O {_fmt(oo)}/U {_fmt(ou)})"
    if open_row[1:] == now_row[1:]:
        return f"open {opened} — unmoved"
    return (f"open {opened} → now {nl} (O {_fmt(no)}/U {_fmt(nu)}) "
            f"as of {_tlabel(ts)}")


def setup(bot):
    tree = bot.tree

    @tree.command(name="prop",
                  description="Open → current per book: one player, or a whole market")
    @app_commands.describe(player="Optional: player name (partial fine). Omit for the whole market",
                           market="Optional: one market")
    @app_commands.choices(market=CHOICES)
    async def prop_cmd(interaction: discord.Interaction,
                       player: str | None = None,
                       market: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        if not player and not market:
            await interaction.followup.send(
                "Give me a player, a market, or both — e.g. `/prop skubal`, "
                "`/prop market:Outs`, `/prop skubal market:K's`.")
            return
        rows = open_now(player, market.value if market else None)
        if not rows:
            who = f"“{player}”" if player else "anyone"
            await interaction.followup.send(
                f"No tracked quotes for {who} today"
                + (f" on {market.name}" if market else "") + ".")
            return
        if player:
            by_mp: dict[tuple[str, str], list] = {}
            for mk, p, b, o, n in rows:
                by_mp.setdefault((mk, p), []).append((b, o, n))
            e = discord.Embed(title=f"Props — {rows[0][1]}", color=0x3498db)
            for (mk, p), items in sorted(by_mp.items()):
                val = "\n".join(f"{BOOK_NAMES.get(b, b)}: {_move_line(o, n)}"
                                 for b, o, n in sorted(items))
                e.add_field(name=MARKETS.get(mk, mk), value=val[:1024], inline=False)
            e.set_footer(text="open = first quote the tracker saw today")
            await interaction.followup.send(embed=e)
            return
        # market-only: the whole slate, movers first, unmoved collapsed
        by_p: dict[str, list] = {}
        for mk, p, b, o, n in rows:
            by_p.setdefault(p, []).append((b, o, n))
        def _pscore(items):
            return max((abs((n[1] or 0) - (o[1] or 0)) for _, o, n in items), default=0)
        e = discord.Embed(title=f"{market.name} — open → now (whole slate)",
                          color=0x3498db)
        for p in sorted(by_p, key=lambda x: -_pscore(by_p[x]))[:24]:
            moved = [(b, o, n) for b, o, n in sorted(by_p[p]) if o[1:] != n[1:]]
            still = len(by_p[p]) - len(moved)
            lines = [f"{BOOK_NAMES.get(b, b)}: {_move_line(o, n)}" for b, o, n in moved]
            if still:
                ref = by_p[p][0]
                lines.append(f"{still} book(s) unmoved at {ref[2][1]}")
            e.add_field(name=p, value="\n".join(lines)[:1024], inline=False)
        e.set_footer(text="sorted by biggest line change • open = first quote seen today")
        await interaction.followup.send(embed=e)

    @tree.command(name="propboard", description="Current slate board for one market")
    @app_commands.choices(market=CHOICES)
    async def propboard_cmd(interaction: discord.Interaction,
                            market: app_commands.Choice[str]):
        await interaction.response.defer()
        b = board(market.value)
        if not b:
            await interaction.followup.send(
                f"No {market.name} quotes tracked yet today.")
            return
        e = discord.Embed(title=f"{market.name} — current board", color=0x3498db)
        for p in sorted(b)[:24]:
            lines = [f"{BOOK_NAMES.get(bk, bk)}: {q[1]} "
                     f"(O {_fmt(q[2])}/U {_fmt(q[3])})"
                     for bk, q in sorted(b[p].items())]
            e.add_field(name=p, value="\n".join(lines), inline=True)
        await interaction.followup.send(embed=e)

    @tree.command(name="propmoves",
                  description="Biggest prop movers today — line moves + price moves")
    @app_commands.describe(market="Optional: one market. Omit = all seven")
    @app_commands.choices(market=CHOICES)
    async def propmoves_cmd(interaction: discord.Interaction,
                            market: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        lm, pm = movers(market.value if market else None)
        if not lm and not pm:
            await interaction.followup.send(
                "Nothing has moved yet today"
                + (f" in {market.name}" if market else "") + ".")
            return
        e = discord.Embed(title="Biggest prop movers today"
                          + (f" — {market.name}" if market else ""),
                          color=0xe67e22)
        if lm:
            val = "\n".join(
                f"**{p}** {MARKETS.get(mk, mk)} — {BOOK_NAMES.get(b, b)}: "
                f"{o[1]} → {n[1]} (O {_fmt(o[2])} → {_fmt(n[2])})"
                for _, mk, p, b, o, n in lm[:10])
            e.add_field(name="📏 Line moves", value=val[:1024], inline=False)
        if pm:
            val = "\n".join(
                f"**{p}** {MARKETS.get(mk, mk)} — {BOOK_NAMES.get(b, b)}: "
                f"O {_fmt(o[2])} → {_fmt(n[2])} at {n[1]} ({pts:.1f} pts)"
                for pts, mk, p, b, o, n in pm[:10])
            e.add_field(name="💰 Price moves (same line)", value=val[:1024], inline=False)
        e.set_footer(text="every row is one real book • price moves ranked in probability points")
        await interaction.followup.send(embed=e)

    @tree.command(name="propgraph",
                  description="Chart line + price moves — one market, or ALL of them")
    @app_commands.describe(player="Player name (partial fine)",
                           market="Optional: one market. Omit = every prop with data")
    @app_commands.choices(market=CHOICES)
    async def propgraph_cmd(interaction: discord.Interaction, player: str,
                            market: app_commands.Choice[str] | None = None):
        await interaction.response.defer()
        safe = player.replace(" ", "_")
        if market:
            buf = await asyncio.to_thread(render_graph, player, market.value)
            if not buf:
                await interaction.followup.send(
                    f"No tracked {market.name} data for “{player}” today — the "
                    "history fills in as the tracker polls.")
                return
            await interaction.followup.send(file=discord.File(
                buf, filename=f"props_{safe}_{market.value}.png"))
            return
        charts = await asyncio.to_thread(render_all, player)
        if not charts:
            await interaction.followup.send(
                f"No tracked prop data for “{player}” today — the history "
                "fills in as the tracker polls.")
            return
        files = [discord.File(buf, filename=f"props_{safe}_{mk}.png")
                 for mk, buf in charts]
        await interaction.followup.send(
            content=f"**{player}** — {len(files)} market(s) tracked today",
            files=files)
