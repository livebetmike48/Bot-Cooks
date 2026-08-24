import os
import logging
import asyncio
from typing import Literal
from datetime import datetime, timedelta, timezone

LegsT = Literal[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

import discord
from discord import app_commands
from dotenv import load_dotenv

import parlay
import statcast_api
import odds_api
import ev_features
import whale
import props
import parlay_track

BETTABLE = ("fanduel", "draftkings", "caesars", "betmgm")
# Real US books stay listed (Fanatics, theScore, Bally...); offshore books
# never render anywhere -- Mike: "i just dont want offshores".
OFFSHORE = ("betonline", "lowvig", "bovada", "mybookie", "betus",
            "everygame", "betanysports")

PRICE_GAME_MARKETS = {"moneyline": "h2h", "total": "totals", "spread": "spreads"}
PRICE_PROP_MARKETS = {
    "strikeouts": "pitcher_strikeouts", "outs": "pitcher_outs",
    "hits allowed": "pitcher_hits_allowed", "walks": "pitcher_walks",
    "batter hits": "batter_hits", "batter hrs": "batter_home_runs",
    "total bases": "batter_total_bases", "rbis": "batter_rbis",
    "runs": "batter_runs_scored",
}
PriceMarketT = Literal["strikeouts", "outs", "hits allowed", "walks",
                       "batter hits", "batter hrs", "total bases", "rbis",
                       "runs", "moneyline", "total", "spread"]


def _strip_offshore(prices: dict) -> dict:
    return {b: p for b, p in (prices or {}).items()
            if p is not None and not any(o in b.strip().lower() for o in OFFSHORE)}


def _extract_side(props: dict, player_name: str, side: str, point, market_key: str) -> dict | None:
    """All books' prices for one side at one point, straight off the raw
    payload -- the repo's player_prop_prices is over-only, so unders are
    read here with the same name/point matching rules."""
    target = (player_name or "").strip().lower()
    target_last = target.split()[-1] if target else ""
    prices = {}
    for book in (props or {}).get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            if market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []) or []:
                if (outcome.get("name") or "").lower() != side:
                    continue
                desc = (outcome.get("description") or "").lower()
                if not desc or (target not in desc and target_last not in desc):
                    continue
                if outcome.get("point") != point:
                    continue
                prices[book.get("title", "?")] = outcome.get("price")
    return {"point": point, "prices": prices} if prices else None


def _prop_ladder_search(name: str, market_key: str):
    """Walk today's events for one player's prop across every book.
    Cached 5 min per event+market, so repeats are free."""
    for ev in odds_api.get_events() or []:
        props = odds_api.get_event_props(ev.get("id"), market_key)
        if not props:
            continue
        over = odds_api.player_prop_prices(props, market_key, name)
        if not over or over.get("point") is None:
            continue
        under = _extract_side(props, name, "under", over["point"], market_key)
        sides = {"O %s" % over["point"]: over}
        if under:
            sides["U %s" % under["point"]] = under
        return {"game": "%s @ %s" % (ev.get("away_team"), ev.get("home_team")),
                "sides": sides}
    return None


def _spread_line(ev: dict, team_folded: str):
    """The queried team's spread at its most-quoted point across books."""
    points = {}
    for book in ev.get("bookmakers", []) or []:
        for market in book.get("markets", []) or []:
            if market.get("key") != "spreads":
                continue
            for outcome in market.get("outcomes", []) or []:
                nm = (outcome.get("name") or "").lower()
                if team_folded not in nm and nm not in team_folded:
                    continue
                pt = outcome.get("point")
                if pt is None:
                    continue
                points.setdefault(pt, {})[book.get("title", "?")] = outcome.get("price")
    if not points:
        return None
    pt = max(points, key=lambda p: len(points[p]))
    return {"point": pt, "prices": points[pt]}


def _game_ladder_search(team: str, label: str):
    """Moneyline / total / spread for a team's game today."""
    gkey = PRICE_GAME_MARKETS[label]
    want = team.strip().lower()
    for ev in odds_api.get_mlb_odds(markets=gkey) or []:
        home = (ev.get("home_team") or "").lower()
        away = (ev.get("away_team") or "").lower()
        if want not in home and want not in away:
            continue
        matched = ev.get("home_team") if want in home else ev.get("away_team")
        game = "%s @ %s" % (ev.get("away_team"), ev.get("home_team"))
        if label == "moneyline":
            prices = odds_api.all_prices(ev, "h2h", matched)
            if not prices:
                return None
            return {"game": game,
                    "sides": {"ML — %s" % matched: {"point": None, "prices": prices}}}
        if label == "total":
            tl = odds_api.totals_line(ev)
            if not tl:
                return None
            return {"game": game, "sides": {
                "O %s" % tl["point"]: {"point": tl["point"], "prices": tl.get("over") or {}},
                "U %s" % tl["point"]: {"point": tl["point"], "prices": tl.get("under") or {}}}}
        sp = _spread_line(ev, (matched or "").lower())
        if not sp:
            return None
        return {"game": game,
                "sides": {"%s %+g" % (matched, sp["point"]): sp}}
    return None


def _price_shop_embed(name: str, market: str, found: dict) -> "discord.Embed":
    emb = discord.Embed(title="💰 %s — %s shop" % (name, market), color=0x2F6FED)
    emb.description = found.get("game") or ""
    shown = 0
    for label, priced in found.get("sides", {}).items():
        prices = _strip_offshore((priced or {}).get("prices"))
        if not prices:
            continue
        best = odds_api.best_price(prices)
        lines = []
        for book, price in sorted(prices.items(), key=lambda x: -x[1])[:12]:
            mark = " ← best" if best and book == best[0] else ""
            dot = "" if book.strip().lower() in BETTABLE else " ·"
            lines.append("%s **%+d**%s%s" % (book, price, mark, dot))
        emb.add_field(name="%s (%d books)" % (label, len(prices)),
                      value=chr(10).join(lines), inline=True)
        shown += 1
    if not shown:
        emb.description = (emb.description or "") + chr(10) + "no non-offshore prices found"
    emb.set_footer(text="· = not one of the four parlay books (real US books only; offshores never shown)")
    return emb


# ---------- /moves: sharp-money steam tracker ----------
# The bot takes its OWN morning snapshot of the slate (one wide pass,
# ~30-50 credits) and /moves ranks the biggest movers open->now. Honest
# label baked in: movement is the observable; "they took money" is the
# inference. Snapshot lives in memory + best-effort sqlite -- if the bot
# restarts midday it says so instead of faking an opener.
MOVES_SNAP_UTC = os.getenv("MOVES_SNAP_UTC", "13:45")  # 9:45 AM ET
_moves_snap: dict = {"date": None, "rows": {}}


def _et_today() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%d")


def _avg_price(prices: dict) -> float | None:
    clean = list(_strip_offshore(prices).values())
    return round(sum(clean) / len(clean), 1) if clean else None


def _slate_prices() -> dict:
    """One pass over the slate: {key: {'label', 'point', 'avg'}} for
    moneylines, totals, and every posted K line."""
    rows = {}
    for ev in odds_api.get_mlb_odds(markets="h2h") or []:
        for team in (ev.get("home_team"), ev.get("away_team")):
            if not team:
                continue
            avg = _avg_price(odds_api.all_prices(ev, "h2h", team))
            if avg is not None:
                rows[f"ml|{team}"] = {"label": f"{team} ML", "point": None, "avg": avg}
    for ev in odds_api.get_mlb_odds(markets="totals") or []:
        tl = odds_api.totals_line(ev)
        if not tl:
            continue
        game = f"{ev.get('away_team')} @ {ev.get('home_team')}"
        for side in ("over", "under"):
            avg = _avg_price(tl.get(side) or {})
            if avg is not None:
                rows[f"tot|{game}|{side}"] = {"label": f"{game} {side.title()}",
                                              "point": tl["point"], "avg": avg}
    for ev in odds_api.get_events() or []:
        props = odds_api.get_event_props(ev.get("id"), "pitcher_strikeouts")
        if not props:
            continue
        seen = set()
        for book in props.get("bookmakers", []) or []:
            for market in book.get("markets", []) or []:
                if market.get("key") != "pitcher_strikeouts":
                    continue
                for o in market.get("outcomes", []) or []:
                    d = o.get("description")
                    if d:
                        seen.add(d)
        for player in seen:
            priced = odds_api.player_prop_prices(props, "pitcher_strikeouts", player)
            if not priced or priced.get("point") is None:
                continue
            avg = _avg_price(priced["prices"])
            if avg is not None:
                rows[f"k|{player}"] = {"label": f"{player} Ks",
                                       "point": priced["point"], "avg": avg}
    return rows


def _take_moves_snapshot():
    _moves_snap["date"] = _et_today()
    _moves_snap["rows"] = _slate_prices()
    log.info("moves snapshot: %d outcomes stored for %s",
             len(_moves_snap["rows"]), _moves_snap["date"])


def _rank_moves(top: int = 8) -> list[dict] | None:
    if _moves_snap["date"] != _et_today() or not _moves_snap["rows"]:
        return None
    now = _slate_prices()
    moves = []
    for key, cur in now.items():
        opened = _moves_snap["rows"].get(key)
        if not opened:
            continue
        pt_move = 0.0
        if cur.get("point") is not None and opened.get("point") is not None:
            pt_move = cur["point"] - opened["point"]
        cents = cur["avg"] - opened["avg"]
        score = abs(cents) + 80 * abs(pt_move)
        if score < 5:
            continue
        moves.append({"label": cur["label"], "open_avg": opened["avg"],
                      "now_avg": cur["avg"], "cents": round(cents, 1),
                      "open_pt": opened.get("point"), "now_pt": cur.get("point"),
                      "pt_move": pt_move, "score": score})
    moves.sort(key=lambda m: -m["score"])
    return moves[:top]


def _moves_embed(moves: list[dict], snap_date: str) -> "discord.Embed":
    emb = discord.Embed(title="📈 Sharp money watch — biggest moves since open",
                        color=0x2F6FED)
    lines = []
    for i, m in enumerate(moves, 1):
        if m["pt_move"]:
            lines.append(f"**{i}. {m['label']}** — line {m['open_pt']:g} → "
                         f"{m['now_pt']:g} (avg {m['open_avg']:+g} → {m['now_avg']:+g})")
        else:
            arrow = "steam ↑" if m["cents"] < 0 else "drift ↓"
            lines.append(f"**{i}. {m['label']}** — avg {m['open_avg']:+g} → "
                         f"{m['now_avg']:+g} ({m['cents']:+g}c, {arrow})")
    emb.description = chr(10).join(lines)
    emb.set_footer(text=f"vs the {snap_date} 9:45 AM ET snapshot · avg across real US books · "
                        "movement is the observable — the money is the inference")
    return emb


async def _moves_snapshot_task(bot: "ParlayBot"):
    try:
        hh, mm = (int(x) for x in MOVES_SNAP_UTC.split(":"))
    except Exception:
        hh, mm = 13, 45
    log.info("moves snapshot task up — %02d:%02d UTC daily", hh, mm)
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        try:
            await asyncio.to_thread(_take_moves_snapshot)
        except Exception as e:
            log.error("moves snapshot failed: %s", e)


# Daily auto-parlays (one per category, staggered) -- set the channel to arm:
AUTOPOST_CHANNEL_ID = int(os.getenv("PARLAY_AUTOPOST_CHANNEL_ID", "0") or 0)
AUTOPOST_START_UTC = os.getenv("PARLAY_AUTOPOST_START_UTC", "15:00")  # 11am ET
AUTOPOST_GAP_MIN = max(5, int(os.getenv("PARLAY_AUTOPOST_GAP_MIN", "15") or 15))
AUTOPOST_CATEGORIES = os.getenv("PARLAY_AUTOPOST_CATEGORIES",
                                "hr,hit,k,moneyline,totals")

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("parlay_bot")

intents = discord.Intents.default()

MARKET_CONFIG = {
    "hit": {"title": "🎯 Hit Parlay", "shortlist_pct": "xba",
            "note": "ranked by real xBA vs the starter's hand"},
    "hr": {"title": "💣 HR Parlay", "shortlist_pct": "brl_percent",
           "note": "ranked by real xwOBA vs the starter's hand"},
}


def _leg_lines(leg: dict, market: str) -> str:
    lines = [f"vs {leg['starter']} ({leg['starter_hand']}HP)"]
    stat_bits = [f"{leg['pa_vs_hand']} PA vs {leg['starter_hand']}"]
    if leg.get("avg_vs_hand") is not None:
        stat_bits.append(f"AVG {leg['avg_vs_hand']}")
    if leg.get("xba_vs_hand") is not None:
        stat_bits.append(f"xBA {leg['xba_vs_hand']}")
    if market == "hr":
        if leg.get("xwoba_vs_hand") is not None:
            stat_bits.append(f"xwOBA {leg['xwoba_vs_hand']}")
        stat_bits.append(f"{leg.get('hr_vs_hand', 0)} HR vs {leg['starter_hand']}HP")
    lines.append(" • ".join(stat_bits))
    lines.append(f"Hit in {leg['hit_game_pct']}% of {leg['games']} games")
    if leg.get("mix_line"):
        lines.append(leg["mix_line"])
    return "\n".join(lines)


def parlay_ticket(priced_legs: list, same_game: bool, verb: str = "Parlay",
                  leg_names: list | None = None) -> tuple[str, list]:
    """The ticket treatment every command shares: exact combined price in
    the header for cross-game parlays (real math), NO number for same-game
    (only the book knows its SGP price), and full-slip/build-slip buttons.
    When NO single book prices every leg (common on HR props), degrade
    honestly: per-leg best-price buttons instead of silence."""
    by_book = odds_api.parlay_by_book(priced_legs)
    if not by_book:
        # No book covers EVERY leg. Build the biggest PARTIAL parlay we can
        # at the best-covering book, singles only for the stragglers --
        # a parlay ask deserves a parlay answer, honestly labeled.
        def _name(i):
            return leg_names[i] if leg_names and i < len(leg_names) else f"Leg {i + 1}"
        coverage = {}
        for i, p in enumerate(priced_legs):
            for bk in (p or {}).get("prices") or {}:
                coverage.setdefault(bk, []).append(i)
        multi = {bk: idxs for bk, idxs in coverage.items() if len(idxs) >= 2}

        def _combined(bk, idxs):
            dec = 1.0
            for i in idxs:
                dec *= odds_api.american_to_decimal(priced_legs[i]["prices"][bk])
            return odds_api.decimal_to_american(dec)

        # Prefer the book that parlays the MOST legs; among ties, the one
        # with a one-tap slip (FanDuel/DK), then the better combined price.
        best_bk, best_idxs, best_slip = None, [], None
        for bk, idxs in sorted(multi.items(), key=lambda kv: -len(kv[1])):
            legs_sub = [{"sid": ((priced_legs[i].get("sids") or {}).get(bk)),
                         "link": ((priced_legs[i].get("links") or {}).get(bk))}
                        for i in idxs]
            slip = odds_api.build_slip_link(bk, legs_sub)
            better = (best_bk is None
                      or len(idxs) > len(best_idxs)
                      or (len(idxs) == len(best_idxs) and slip and not best_slip)
                      or (len(idxs) == len(best_idxs) and bool(slip) == bool(best_slip)
                          and _combined(bk, idxs) > _combined(best_bk, best_idxs)))
            if better:
                best_bk, best_idxs, best_slip = bk, idxs, slip

        buttons = []
        covered_idx = set()
        if best_bk:
            covered_idx = set(best_idxs)
            combined = _combined(best_bk, best_idxs)
            label = f"{len(best_idxs)}/{len(priced_legs)} legs @ {best_bk} {combined:+d}"
            if best_slip:
                buttons.append((f"{label} — partial slip", best_slip))
            else:
                # No one-tap scheme at this book (Caesars/BetRivers/etc):
                # give each leg's link AT THAT BOOK so taps land in ONE slip.
                for i in best_idxs:
                    url = (priced_legs[i].get("links") or {}).get(best_bk)
                    if url:
                        buttons.append((f"{_name(i)} {priced_legs[i]['prices'][best_bk]:+d} "
                                        f"@ {best_bk}", url))
                if not any(b for b in buttons):
                    covered_idx = set()

        unpriced = 0
        for i, p in enumerate(priced_legs):
            if i in covered_idx:
                continue
            if not p or not p.get("prices"):
                unpriced += 1
                continue
            bp = odds_api.best_price(p["prices"])
            if not bp:
                unpriced += 1
                continue
            url = (p.get("links") or {}).get(bp[0]) or next(iter((p.get("links") or {}).values()), None)
            if url:
                buttons.append((f"{_name(i)} {bp[1]:+d} @ {bp[0]}", url))
        if not buttons:
            return "", []
        if covered_idx and best_slip:
            header = (f"🎟️ **No single book prices every leg** — biggest parlay is "
                      f"**{len(covered_idx)}/{len(priced_legs)} legs @ {best_bk} "
                      f"{_combined(best_bk, best_idxs):+d}** (one tap loads them all); "
                      "any leftover leg below at its best price"
                      + (f" · {unpriced} unpriced right now" if unpriced else "") + "\n\n")
        elif covered_idx:
            header = (f"🎟️ **No single book prices every leg** — biggest parlay is "
                      f"**{len(covered_idx)}/{len(priced_legs)} legs @ {best_bk} "
                      f"{_combined(best_bk, best_idxs):+d}**. {best_bk} has no one-tap slip link, "
                      "so tap each leg below (they land in the same slip); "
                      "leftovers after that are separate bets"
                      + (f" · {unpriced} unpriced right now" if unpriced else "") + "\n\n")
        else:
            header = ("🎟️ **No single book prices every leg** — and no book prices two of them "
                      "together, so these can only be singles right now"
                      + (f" ({unpriced} leg(s) unpriced)" if unpriced else "") + "\n\n")
        return header, buttons[:25]
    slips = odds_api.parlay_slips(priced_legs, by_book)
    if same_game:
        header = "🎟️ **Same-game parlay** — tap a book below to load the full slip; the book shows its exact SGP price there\n\n"
    else:
        best = max(by_book, key=lambda bk: by_book[bk]["combined"])
        header = f"🎟️ **{verb} pays {by_book[best]['combined']:+d}** best @ {best}\n\n"
    buttons = []
    for bk in sorted(by_book, key=lambda bk: -by_book[bk]["combined"]):
        url = slips.get(bk) or by_book[bk]["link"]
        if not url:
            continue
        if bk in slips:
            label = f"Full slip @ {bk}" if same_game else f"Full slip @ {bk} {by_book[bk]['combined']:+d}"
        else:
            label = f"{bk} (build slip)" if same_game else f"{bk} {by_book[bk]['combined']:+d} (build slip)"
        buttons.append((label, url))
    return header, buttons[:5]


import random


def diversify(evaluated: list, want: int) -> list:
    """Same data, different ticket. The top of the shortlist is a cluster of
    near-equal candidates -- always taking 1..N means every user gets the
    same parlay. Weighted-shuffle the top pool (better rank = better odds of
    being picked) so tickets rotate WITHOUT dipping into worse legs: the
    pool is capped at the top 3x, and everything below stays in rank order
    as backup for pick_legs' game-diversity rules."""
    pool_size = min(len(evaluated), max(want * 3, want + 4))
    pool = list(evaluated[:pool_size])
    rest = evaluated[pool_size:]
    picked = []
    while pool:
        weights = [1.0 / (i + 1.5) for i in range(len(pool))]
        idx = random.choices(range(len(pool)), weights=weights, k=1)[0]
        picked.append(pool.pop(idx))
    return picked + rest


def _leg_key(leg, game_of) -> str:
    return str(leg.get("batter") or leg.get("starter") or leg.get("name")
               or leg.get("team") or f"g{game_of.get(id(leg))}")


def _fresh_pick(category: str, pool: list, game_of, want: int, **kw) -> list:
    """pick_legs, but never the same combination twice in one day (Mike's
    no-repeat rule: the bot has the stats -- new request, new parlay).
    Retries the weighted shuffle until the combo is one we haven't posted
    today; if the pool is too thin to vary, posts the best anyway rather
    than nothing, and says so in the log."""
    used = parlay_track.todays_leg_sets(category)
    chosen = []
    for attempt in range(10):
        chosen = parlay.pick_legs(diversify(pool, want), game_of, want, **kw)
        if not chosen:
            return chosen
        names = frozenset(_leg_key(l, game_of) for l in chosen)
        if all(names != u and not names <= u for u in used):
            if attempt:
                log.info("parlay %s: fresh combo found on attempt %d", category, attempt + 1)
            return chosen
    log.warning("parlay %s: pool too thin to avoid a repeat today -- "
                "posting best available", category)
    return chosen


def _track(category: str, chosen: list, priced_legs: list, header: str,
           kind_of, interaction, game_of=None):
    """Record the posted parlay at 1U. Best-effort: never breaks a command."""
    try:
        by_book = odds_api.parlay_by_book(priced_legs)
        if by_book:
            book = max(by_book, key=lambda bk: by_book[bk]["combined"])
            price = by_book[book]["combined"]
        else:
            # No single book covers every leg (common outside HR) -- this
            # used to silently skip recording, which is why the season
            # record showed only HR parlays. Fall back to what the bot
            # actually displays in that case: each leg's BEST price,
            # combined. book="mixed" marks it honestly.
            book, dec = "mixed", 1.0
            for priced in priced_legs:
                prices = (priced or {}).get("prices") or {}
                if not prices:
                    log.warning("parlay tracking: %s parlay NOT recorded -- "
                                "a leg has no live price at any book", category)
                    return
                dec *= max(odds_api.american_to_decimal(p) for p in prices.values())
            price = int(round((dec - 1) * 100)) if dec >= 2 else -int(round(100 / (dec - 1)))
        legs = []
        for i, (leg, priced) in enumerate(zip(chosen, priced_legs)):
            spec = kind_of(leg, priced)
            if not spec:
                # NEVER silent: name the category and leg so a broken spec
                # builder is visible in logs instead of erasing the parlay.
                log.warning("parlay tracking: %s parlay NOT recorded -- "
                            "spec builder returned None for leg %d", category, i + 1)
                return
            best_at = (priced or {}).get("prices", {}).get(book)
            if best_at is None and book == "mixed":
                prices = (priced or {}).get("prices") or {}
                best_at = max(prices.values(),
                              key=lambda p: odds_api.american_to_decimal(p)) if prices else None
            spec.setdefault("price", best_at)
            spec.setdefault("book", book)
            legs.append(spec)
        pid = parlay_track.record(category, legs, price, book,
                                  requested_by=str(getattr(interaction.user, "id", "")))
        if pid is None:
            log.warning("parlay tracking: %s parlay rejected by recorder "
                        "(see parlay_track log line above)", category)
    except Exception as e:
        log.warning("parlay tracking skipped: %s", e)


def build_bet_buttons(leg_links: list[tuple[str, str]]) -> discord.ui.View | None:
    """Link buttons: [('Leg 1: Soto @ BetRivers', url), ...]. Discord caps
    at 25 buttons; we stay well under. None if no book gave us links."""
    if not leg_links:
        return None
    view = discord.ui.View()
    for label, url in leg_links[:25]:
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, label=label[:80], url=url))
    return view


class ParlayBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        whale.setup(self)
        props.setup(self)
        for name, market, desc in [
            ("hitparlay", "hit", "Build a hits parlay from today's real matchup data"),
            ("hrparlay", "hr", "Build a home run parlay from today's real matchup data"),
        ]:
            cmd = app_commands.Command(
                name=name,
                description=desc,
                callback=self._make_batter_callback(market),
            )
            self.tree.add_command(cmd)

        moves_cmd = app_commands.Command(
            name="moves",
            description="Top movers since open — the sharp money watch",
            callback=self._moves_callback,
        )
        self.tree.add_command(moves_cmd)

        price_cmd = app_commands.Command(
            name="price",
            description="Shop any MLB market across every book — props, ML, totals, spreads",
            callback=self._price_callback,
        )
        self.tree.add_command(price_cmd)

        streak_cmd = app_commands.Command(
            name="streakparlay",
            description="Parlay every hitter on today's slate riding an active hit streak",
            callback=self._streak_callback,
        )
        self.tree.add_command(streak_cmd)

        sgp_cmd = app_commands.Command(
            name="samegameparlay",
            description="Build a same-game parlay: 1 strikeouts leg + hit legs from one game",
            callback=self._sgp_callback,
        )
        self.tree.add_command(sgp_cmd)
        sgp_cmd.autocomplete("game")(self._game_autocomplete)

        ml_cmd = app_commands.Command(
            name="moneylineparlay",
            description="Build a moneyline parlay from real starter-quality gaps + recent scoring",
            callback=self._moneyline_callback,
        )
        self.tree.add_command(ml_cmd)

        totals_cmd = app_commands.Command(
            name="totalsparlay",
            description="Rank today's run environments for over/under leans (compare vs your book's line)",
            callback=self._totals_callback,
        )
        self.tree.add_command(totals_cmd)

        k_cmd = app_commands.Command(
            name="strikeoutsparlay",
            description="Build a pitcher-strikeouts parlay from today's real K/whiff splits",
            callback=self._strikeouts_callback,
        )
        self.tree.add_command(k_cmd)

        # Bot Cooks merge: nightly EV pick + grading + live EV commands
        # (/setevchannel, /topev, /evcheck, /evrecord, /postevpick).
        # Fully self-contained module -- own Odds API calls, own sqlite
        # ledger, zero coupling to the parlay code above.
        ev_features.register_commands(self.tree)
        parlay_track.register_commands(self.tree)

        try:
            guild_id = os.getenv("GUILD_ID")
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands globally", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    def _make_batter_callback(self, market: str):
        async def callback(interaction: discord.Interaction, legs: LegsT = 3,
                           min_odds: int = None, max_odds: int = None):
            await self._batter_parlay(interaction, market, legs, min_odds, max_odds)
        return callback

    async def _batter_parlay(self, interaction: discord.Interaction, market: str, legs: int,
                              min_odds: int = None, max_odds: int = None):
        await interaction.response.defer()
        cfg = MARKET_CONFIG[market]
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        # Build candidate list: top hitters per team (Savant's own percentile
        # scores), each mapped to the OPPOSING starter they'd face
        candidates = []
        game_of = {}
        for g in slate:
            for side, opp_side in (("home", "away"), ("away", "home")):
                team = g["teams"][side]
                opp = g["teams"][opp_side]
                if not opp["starter_id"]:
                    continue  # no probable starter announced yet
                try:
                    hand = await asyncio.to_thread(parlay.get_starter_hand, opp["starter_id"])
                except Exception:
                    continue
                if hand not in ("L", "R"):
                    continue
                short = await asyncio.to_thread(
                    parlay.shortlist_hitters, [team["abbrev"]], cfg["shortlist_pct"], 2
                )
                for batter in short:
                    candidates.append((batter, opp, hand, g["game_pk"]))

        if not candidates:
            await interaction.followup.send("No probable starters announced yet -- try closer to game time.")
            return

        # Evaluate with real pitch-level data (top candidates only -- each is
        # a full season fetch, so this takes a bit)
        evaluated = []
        for batter, opp, hand, game_pk in candidates[:24]:
            leg = await asyncio.to_thread(
                parlay.evaluate_hit_leg, batter, opp["starter_id"], opp["starter_name"], hand, market
            )
            if leg:
                evaluated.append(leg)
                game_of[id(leg)] = game_pk

        if not evaluated:
            await interaction.followup.send("Couldn't build qualified legs from today's matchups.")
            return

        # Prop odds: match each leg's game to an odds event, price the prop
        game_names = {g["game_pk"]: (g["teams"]["home"]["name"], g["teams"]["away"]["name"]) for g in slate}
        events = await asyncio.to_thread(odds_api.get_events)
        market_key = odds_api.PROP_MARKETS.get(market)

        def _price_leg(leg):
            gpk = game_of.get(id(leg))
            names = game_names.get(gpk)
            if not names or not events or not market_key:
                return None
            ev = odds_api.find_event(events, names[0], names[1])
            if not ev:
                return None
            props = odds_api.get_event_props(ev.get("id"), market_key)
            return odds_api.player_prop_prices(props, market_key, leg["batter"]) if props else None

        # PRICE-GATE FIRST: a player only makes a parlay if a book is
        # actually offering his prop right now. This drops injured/benched/
        # unlisted players (e.g. an out star the season stats still love)
        # BEFORE selection, so we never post a leg you can't bet.
        priced_pool = []
        seen_prices = []
        for leg in evaluated:
            priced = await asyncio.to_thread(_price_leg, leg)
            bp = odds_api.best_price(priced["prices"]) if priced else None
            if bp is None:
                continue  # no live price -> not a real, bettable leg
            if min_odds is not None and bp[1] < min_odds:
                seen_prices.append(bp[1]); continue
            if max_odds is not None and bp[1] > max_odds:
                seen_prices.append(bp[1]); continue
            seen_prices.append(bp[1])
            leg["_priced"] = priced
            priced_pool.append(leg)
        evaluated = priced_pool
        if not evaluated:
            if min_odds is not None or max_odds is not None:
                if seen_prices:
                    lo, hi = min(seen_prices), max(seen_prices)
                    await interaction.followup.send(
                        f"No legs fit that odds range — priced legs today ran "
                        f"**{lo:+d} to {hi:+d}**. Widen the range (or drop min/max). "
                        f"Heads up: hit props on top hitters usually live around -250 to -400.")
                    return
            await interaction.followup.send(
                "No legs have live prices right now — props are unposted or suspended "
                "(books pull props once games go live; lines post closer to game time).")
            return

        chosen = _fresh_pick("hr" if market == "hr" else "hit",
                             evaluated, game_of, legs)

        # Every chosen leg already carries its live price from the gate above
        priced_legs = [leg.get("_priced") for leg in chosen]
        same_game = len({game_of.get(id(l)) for l in chosen}) < len(chosen)
        header, bet_buttons = parlay_ticket(
            priced_legs, same_game, leg_names=[l["batter"] for l in chosen])
        _track("hr" if market == "hr" else "hit", chosen, priced_legs, header,
               lambda leg, priced: {
                   "kind": "batter_hr" if market == "hr" else "batter_hit",
                   "name": leg["batter"], "team": leg.get("team"),
                   "game_pk": game_of.get(id(leg)),
                   "point": (priced or {}).get("point", 0.5), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"{cfg['title']} — {len(chosen)} legs", color=discord.Color.gold())
        embed.description = header + cfg["note"] + " • best legs win, any game"
        for i, leg in enumerate(chosen, 1):
            embed.add_field(
                name=f"Leg {i}: {leg['batter']} ({leg['team']})",
                value=_leg_lines(leg, market),
                inline=False,
            )
        embed.set_footer(text="Research, not advice — confirm lineups before betting • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _strikeouts_callback(self, interaction: discord.Interaction, legs: LegsT = 3):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        evaluated = []
        game_of = {}
        for g in slate:
            for side, opp_side in (("home", "away"), ("away", "home")):
                team = g["teams"][side]
                opp = g["teams"][opp_side]
                if not team["starter_id"]:
                    continue
                leg = await asyncio.to_thread(
                    parlay.evaluate_k_leg, team["starter_id"], team["starter_name"],
                    team["abbrev"], opp["abbrev"],
                )
                if leg:
                    evaluated.append(leg)
                    game_of[id(leg)] = g["game_pk"]

        if not evaluated:
            await interaction.followup.send("No probable starters with enough data yet -- try closer to game time.")
            return

        game_names = {g["game_pk"]: (g["teams"]["home"]["name"], g["teams"]["away"]["name"]) for g in slate}
        events = await asyncio.to_thread(odds_api.get_events)

        def _price_k_leg(leg):
            names = game_names.get(game_of.get(id(leg)))
            if not names or not events:
                return None
            ev = odds_api.find_event(events, names[0], names[1])
            if not ev:
                return None
            props = odds_api.get_event_props(ev.get("id"), "pitcher_strikeouts")
            return odds_api.player_prop_prices(props, "pitcher_strikeouts", leg["starter"]) if props else None

        # Price-gate: only starters with a live K prop qualify (drops
        # scratched/unlisted arms the season stats still rate highly)
        gated = []
        for leg in evaluated:
            priced = await asyncio.to_thread(_price_k_leg, leg)
            if priced and odds_api.best_price(priced["prices"]):
                leg["_priced"] = priced
                gated.append(leg)
        if not gated:
            await interaction.followup.send(
                "Starters found, but none have a live strikeouts prop right now "
                "(K props post closer to game time).")
            return
        chosen = _fresh_pick("k", gated, game_of, legs)
        priced_legs, k_lines = [], {}
        for leg in chosen:
            priced = leg.get("_priced")
            priced_legs.append(priced)
            if priced:
                k_lines[id(leg)] = priced["point"]
        same_game = len({game_of.get(id(l)) for l in chosen}) < len(chosen)
        header, bet_buttons = parlay_ticket(priced_legs, same_game, verb="Overs parlay",
                                           leg_names=[l["starter"] for l in chosen])
        _track("k", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "pitcher_k", "name": leg["starter"],
                                    "team": leg.get("team"),
                                    "game_pk": game_of.get(id(leg)),
                                    "point": (priced or {}).get("point"), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"⚔️ Strikeouts Parlay — {len(chosen)} legs", color=discord.Color.red())
        embed.description = header + "ranked by real K% vs either side"
        for i, leg in enumerate(chosen, 1):
            value = (f"K%: {leg['k_pct_vs_l']}% vs L | {leg['k_pct_vs_r']}% vs R\n"
                     f"Whiff%: {leg['whiff_vs_l']}% vs L | {leg['whiff_vs_r']}% vs R\n"
                     f"{leg['pa']} PA faced this season")
            if id(leg) in k_lines:
                value += f"\nBet: over {k_lines[id(leg)]} strikeouts"
            embed.add_field(
                name=f"Leg {i}: {leg['starter']} ({leg['team']}) vs {leg['opponent']}",
                value=value,
                inline=False,
            )

        embed.set_footer(text="Research, not advice — K prop lines vary by book • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _moves_callback(self, interaction: discord.Interaction):
        """Top movers since this morning's snapshot."""
        await interaction.response.defer()
        try:
            moves = await asyncio.to_thread(_rank_moves)
        except Exception as e:
            log.warning("/moves failed: %s", e)
            await interaction.followup.send("Moves lookup failed — try again in a minute.")
            return
        if moves is None:
            await interaction.followup.send(
                "No morning snapshot for today (bot restarted or before 9:45 AM ET) — "
                "the next one lands at 9:45 tomorrow, then /moves is live.")
            return
        if not moves:
            await interaction.followup.send("Quiet board — nothing has moved meaningfully since open.")
            return
        await interaction.followup.send(embed=_moves_embed(moves, _moves_snap["date"]))

    async def _price_callback(self, interaction: discord.Interaction,
                              name: str, market: PriceMarketT = "strikeouts"):
        """Shop any MLB market across every book: game markets take a team
        name, props take a player name."""
        await interaction.response.defer()
        try:
            if market in PRICE_GAME_MARKETS:
                found = await asyncio.to_thread(_game_ladder_search, name, market)
            else:
                found = await asyncio.to_thread(
                    _prop_ladder_search, name, PRICE_PROP_MARKETS[market])
        except Exception as e:
            log.warning("/price failed: %s", e)
            await interaction.followup.send("Price lookup failed — try again in a minute.")
            return
        if not found:
            kind = "team" if market in PRICE_GAME_MARKETS else "player"
            await interaction.followup.send(
                "No %s line found for **%s** today — check the %s name or "
                "lines may not be posted yet." % (market, name, kind))
            return
        await interaction.followup.send(embed=_price_shop_embed(name, market, found))

    async def _streak_callback(self, interaction: discord.Interaction,
                                min_streak: Literal[3, 4, 5, 6, 7, 8, 10] = 5,
                                legs: LegsT = 5):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        legs_found, game_of = await asyncio.to_thread(parlay.streak_candidates, slate, min_streak)
        if not legs_found:
            await interaction.followup.send(
                f"No scanned hitter on today's slate is riding a {min_streak}+ game hit streak."
            )
            return

        game_names = {g["game_pk"]: (g["teams"]["home"]["name"], g["teams"]["away"]["name"]) for g in slate}
        events = await asyncio.to_thread(odds_api.get_events)

        def _price_streak_leg(leg):
            names = game_names.get(game_of.get(id(leg)))
            if not names or not events:
                return None
            ev = odds_api.find_event(events, names[0], names[1])
            if not ev:
                return None
            props = odds_api.get_event_props(ev.get("id"), "batter_hits")
            return odds_api.player_prop_prices(props, "batter_hits", leg["batter"]) if props else None

        # Price-gate: only streak hitters with a live hits prop qualify
        gated = []
        for leg in legs_found:
            priced = await asyncio.to_thread(_price_streak_leg, leg)
            if priced and odds_api.best_price(priced["prices"]):
                leg["_priced"] = priced
                gated.append(leg)
        if not gated:
            await interaction.followup.send(
                "Streak hitters found, but none have a live hits prop right now "
                "(props post closer to game time).")
            return
        chosen = _fresh_pick("streak", gated, game_of, legs)
        priced_legs = [leg.get("_priced") for leg in chosen]
        same_game = len({game_of.get(id(l)) for l in chosen}) < len(chosen)
        header, bet_buttons = parlay_ticket(priced_legs, same_game,
                                            leg_names=[l["batter"] for l in chosen])
        _track("streak", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "batter_hit", "name": leg["batter"],
                                    "team": leg.get("team"),
                                    "game_pk": game_of.get(id(leg)),
                                    "point": (priced or {}).get("point", 0.5), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"🔥 Streak Parlay — {len(chosen)} legs (streaks of {min_streak}+)", color=discord.Color.orange())
        embed.description = header + "each leg = hitter to extend their ACTIVE hit streak • ranked by streak length"
        for i, leg in enumerate(chosen, 1):
            embed.add_field(
                name=f"Leg {i}: {leg['batter']} ({leg['team']}) — 🔥 {leg['streak']}-game hit streak",
                value=_leg_lines(leg, "hit"),
                inline=False,
            )
        embed.set_footer(text="Streaks computed from real game logs • Research, not advice — confirm lineups • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _game_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception:
            return []
        choices = []
        cur = current.lower()
        for g in slate:
            label = f"{g['teams']['away']['abbrev']} @ {g['teams']['home']['abbrev']}"
            if cur in label.lower():
                choices.append(app_commands.Choice(name=label, value=str(g["game_pk"])))
            if len(choices) >= 25:
                break
        return choices

    async def _sgp_callback(self, interaction: discord.Interaction, game: str,
                             legs: LegsT = 3):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        target = next((g for g in slate if str(g["game_pk"]) == game), None)
        if target is None:
            await interaction.followup.send("Couldn't find that game on today's slate -- pick one from the dropdown.")
            return

        cands = await asyncio.to_thread(parlay.sgp_candidates, target)
        chosen = []
        if cands["k_legs"]:
            chosen.append(("k", cands["k_legs"][0]))
        for hit in cands["hit_legs"]:
            if len(chosen) >= legs:
                break
            chosen.append(("hit", hit))

        if len(chosen) < 2:
            await interaction.followup.send(
                "Not enough qualified legs in this game yet (starters unannounced or thin samples) -- try closer to game time."
            )
            return

        matchup = f"{target['teams']['away']['abbrev']} @ {target['teams']['home']['abbrev']}"

        events = await asyncio.to_thread(odds_api.get_events)
        ev = odds_api.find_event(events, target["teams"]["home"]["name"], target["teams"]["away"]["name"]) if events else None

        def _price_sgp_leg(kind, leg):
            if not ev:
                return None
            market_key = "pitcher_strikeouts" if kind == "k" else "batter_hits"
            player = leg["starter"] if kind == "k" else leg["batter"]
            props = odds_api.get_event_props(ev.get("id"), market_key)
            return odds_api.player_prop_prices(props, market_key, player) if props else None

        priced_legs = []
        for kind, leg in chosen:
            priced_legs.append(await asyncio.to_thread(_price_sgp_leg, kind, leg))
        header, bet_buttons = parlay_ticket(priced_legs, same_game=True)
        _track("sgp", chosen, priced_legs, header,
               lambda pair, priced: {
                   "kind": "pitcher_k" if pair[0] == "k" else "batter_hit",
                   "name": pair[1].get("starter") if pair[0] == "k" else pair[1].get("batter"),
                   "team": pair[1].get("team"), "game_pk": target,
                   "point": (priced or {}).get("point", 0.5), "side": "over"},
               interaction)

        embed = discord.Embed(title=f"🎰 Same Game Parlay — {matchup}", color=discord.Color.purple())
        embed.description = header + "structure: best strikeouts leg + top hit legs (xBA vs hand) • all one game"
        for i, (kind, leg) in enumerate(chosen, 1):
            if kind == "k":
                embed.add_field(
                    name=f"Leg {i}: {leg['starter']} strikeouts ({leg['team']})",
                    value=(f"K%: {leg['k_pct_vs_l']}% vs L | {leg['k_pct_vs_r']}% vs R\n"
                           f"Whiff%: {leg['whiff_vs_l']}% vs L | {leg['whiff_vs_r']}% vs R"),
                    inline=False,
                )
            else:
                embed.add_field(
                    name=f"Leg {i}: {leg['batter']} ({leg['team']}) to record a hit",
                    value=_leg_lines(leg, "hit"),
                    inline=False,
                )
        embed.set_footer(text="SGP legs are correlated — the book shows its exact price on the slip • Research, not advice • Data: Baseball Savant / MLB / The Odds API")
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _moneyline_callback(self, interaction: discord.Interaction, legs: LegsT = 3,
                                   min_odds: int = None, max_odds: int = None):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        evaluated, game_of = [], {}
        for g in slate:
            leg = await asyncio.to_thread(parlay.evaluate_moneyline_leg, g)
            if leg:
                evaluated.append(leg)
                game_of[id(leg)] = g["game_pk"]
        if not evaluated:
            await interaction.followup.send("No games with both probable starters qualified yet -- try closer to game time.")
            return

        odds_events = await asyncio.to_thread(odds_api.get_mlb_odds, "h2h")
        # Price-gate: a moneyline leg must have a live h2h price (drops
        # games with no market up yet); also applies any odds filter.
        filtered = []
        seen_prices = []
        for leg in evaluated:
            event = odds_api.find_event(odds_events, leg["pick_team"], leg["opp_team"]) if odds_events else None
            bp = odds_api.best_price(odds_api.all_prices(event, "h2h", leg["pick_team"])) if event else None
            if bp is None:
                continue
            seen_prices.append(bp[1])
            if min_odds is not None and bp[1] < min_odds:
                continue
            if max_odds is not None and bp[1] > max_odds:
                continue
            filtered.append(leg)
        evaluated = filtered
        if not evaluated:
            if seen_prices and (min_odds is not None or max_odds is not None):
                lo, hi = min(seen_prices), max(seen_prices)
                await interaction.followup.send(
                    f"No moneyline legs fit that odds range — priced legs today ran "
                    f"**{lo:+d} to {hi:+d}**. Widen the range (or drop min/max).")
            else:
                await interaction.followup.send("No moneyline legs have live prices right now.")
            return
        chosen = _fresh_pick("moneyline", evaluated, game_of, legs, max_per_game=1)

        priced_legs = []
        for leg in chosen:
            event = odds_api.find_event(odds_events, leg["pick_team"], leg["opp_team"]) if odds_events else None
            if event:
                prices, links, sids = odds_api.all_prices_and_links(event, "h2h", leg["pick_team"])
                priced_legs.append({"prices": prices, "links": links, "sids": sids} if prices else None)
            else:
                priced_legs.append(None)
        header, bet_buttons = parlay_ticket(priced_legs, same_game=False,
                                            leg_names=[l["pick_team"] for l in chosen])
        _track("moneyline", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "moneyline", "name": leg["pick_team"],
                                    "team": leg["pick_team"],
                                    "game_pk": game_of.get(id(leg)),
                                    "point": None, "side": "win"},
               interaction)

        embed = discord.Embed(title=f"💰 Moneyline Parlay — {len(chosen)} legs", color=discord.Color.green())
        embed.description = header + "ranked by real starter xwOBA-against gap • one leg per game"
        for i, leg in enumerate(chosen, 1):
            lines = [
                f"{leg['pick_starter']} xwOBA-against {leg['pick_xwoba']} vs {leg['opp_starter']} {leg['opp_xwoba']} (gap {leg['rank_metric']})",
                f"K%: {leg['pick_k']}% vs {leg['opp_k']}%",
            ]
            if leg.get("pick_runs") and leg.get("opp_runs"):
                lines.append(
                    f"Last 10 runs/gm: {leg['pick_abbrev']} {leg['pick_runs']['runs_pg']} scored / {leg['pick_runs']['runs_allowed_pg']} allowed"
                    f" • opp {leg['opp_runs']['runs_pg']} / {leg['opp_runs']['runs_allowed_pg']}"
                )
            embed.add_field(
                name=f"Leg {i}: {leg['pick_team']} ML over {leg['opp_team']}",
                value="\n".join(lines),
                inline=False,
            )
        embed.set_footer(text="Research, not advice — starter-quality gap, not a win probability • Data: Baseball Savant / MLB / The Odds API")

        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def _totals_callback(self, interaction: discord.Interaction,
                                lean: Literal["overs", "unders"] = "overs",
                                legs: LegsT = 3):
        await interaction.response.defer()
        try:
            slate = await asyncio.to_thread(parlay.get_today_slate)
        except Exception as e:
            await interaction.followup.send(f"Couldn't load today's slate: {e}")
            return
        if not slate:
            await interaction.followup.send("No games on today's slate (or all finished).")
            return

        evaluated, game_of = [], {}
        for g in slate:
            leg = await asyncio.to_thread(parlay.evaluate_total_leg, g)
            if leg:
                if lean == "unders":
                    leg["rank_metric"] = -leg["rank_metric"]  # lowest environments first
                evaluated.append(leg)
                game_of[id(leg)] = g["game_pk"]
        if not evaluated:
            await interaction.followup.send("Couldn't compute run environments yet -- try closer to game time.")
            return

        chosen = parlay.pick_one_per_game(evaluated, game_of, legs)
        odds_events = await asyncio.to_thread(odds_api.get_mlb_odds, "totals")
        arrow = "⬆️" if lean == "overs" else "⬇️"
        embed = discord.Embed(title=f"{arrow} Totals Leans ({lean}) — {len(chosen)} games", color=discord.Color.blue())
        embed.description = "ranked by combined runs/gm (last 10) • starters shown for context"
        priced_legs = []
        side_name = "Over" if lean == "overs" else "Under"
        for leg in chosen:
            priced = None
            if odds_events:
                names = [t["team"]["name"] for t in leg["teams"]]
                event = odds_api.find_event(odds_events, names[0], names[1])
                if event:
                    tl = odds_api.totals_line(event)
                    if tl:
                        leg["_point"] = tl["point"]
                        prices, links, sids = odds_api.all_prices_and_links(event, "totals", side_name, point=tl["point"])
                        if prices:
                            priced = {"prices": prices, "links": links, "sids": sids}
            priced_legs.append(priced)
        header, bet_buttons = parlay_ticket(priced_legs, same_game=False, verb=f"{side_name}s parlay",
                                            leg_names=[f"{side_name} {l.get('_point')}" for l in chosen])
        _track("totals", chosen, priced_legs, header,
               lambda leg, priced: {"kind": "total", "name": f"{side_name} {leg.get('_point')}",
                                    "team": None, "game_pk": game_of.get(id(leg)),
                                    "point": leg.get("_point"), "side": side_name.lower()},
               interaction)
        if header:
            embed.description = header + embed.description

        for i, leg in enumerate(chosen, 1):
            lines = [f"Combined recent scoring: {leg['combined_runs_pg']} runs/gm"]
            if leg.get("_point") is not None:
                lines.append(f"Bet: {side_name.lower()} {leg['_point']} total runs")
            for t in leg["teams"]:
                s = t["starter_stats"]
                starter_bit = f" — {t['team']['starter_name']} xwOBA-against {s['xwoba']}" if s and s.get("xwoba") is not None else ""
                lines.append(f"{t['team']['abbrev']}: {t['runs']['runs_pg']} scored / {t['runs']['runs_allowed_pg']} allowed{starter_bit}")
            embed.add_field(name=f"{i}. {leg['matchup']}", value="\n".join(lines), inline=False)
        footer = ("Data: MLB / Baseball Savant / The Odds API" if odds_events
                  else "No totals lines on current odds plan — compare vs your book • Data: MLB / Baseball Savant")
        embed.set_footer(text=footer)
        view = build_bet_buttons(bet_buttons)
        if view:
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.followup.send(embed=embed)

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        ev_features.start_tasks(self)
        parlay_track.start_tasks(self)
        if AUTOPOST_CHANNEL_ID:
            self.loop.create_task(_autopost_task(self))
            self.loop.create_task(_moves_snapshot_task(self))
            self.loop.create_task(whale.poll_task(self))
            self.loop.create_task(props.poll_task(self))
        else:
            log.info("PARLAY_AUTOPOST_CHANNEL_ID not set — daily auto-parlays off")


class _AutoInteraction:
    """Interaction stand-in for scheduled posts: the same command code
    runs unchanged, its output lands in the auto-post channel. defer() is
    a no-op, followup.send -> channel.send, user is None (recorded as an
    unrequested post)."""
    class _Resp:
        async def defer(self, *a, **k):
            return None
    class _Follow:
        def __init__(self, channel):
            self._ch = channel
        async def send(self, content=None, **kw):
            kw.pop("ephemeral", None)
            return await self._ch.send(content, **kw)
    def __init__(self, channel):
        self.channel = channel
        self.response = self._Resp()
        self.followup = self._Follow(channel)
        self.user = None
        self.guild = getattr(channel, "guild", None)
        self.channel_id = getattr(channel, "id", None)


async def _autopost_task(bot: "ParlayBot"):
    """Every day, post one parlay per category on a stagger (11:00,
    11:15, 11:30 ET, ...) even if nobody tags the bot -- the record
    builds a sample size on its own. Uses the SAME command paths as
    requests, so tracking, pricing, dedupe, and buttons all apply."""
    try:
        hh, mm = (int(x) for x in AUTOPOST_START_UTC.split(":"))
    except Exception:
        hh, mm = 15, 0
    cats = [c.strip() for c in AUTOPOST_CATEGORIES.split(",") if c.strip()]
    log.info("autopost task up — %02d:%02d UTC, %d categories, %dmin gaps",
             hh, mm, len(cats), AUTOPOST_GAP_MIN)
    runners = {
        "hr": lambda ai: bot._batter_parlay(ai, "hr", 3),
        "hit": lambda ai: bot._batter_parlay(ai, "hit", 3),
        "k": lambda ai: bot._strikeouts_callback(ai, 3),
        "streak": lambda ai: bot._streak_callback(ai),
        "moneyline": lambda ai: bot._moneyline_callback(ai, 3),
        "totals": lambda ai: bot._totals_callback(ai),
    }
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        ch = bot.get_channel(AUTOPOST_CHANNEL_ID)
        if not ch:
            log.warning("autopost channel %d not found", AUTOPOST_CHANNEL_ID)
            continue
        for i, cat in enumerate(cats):
            if i:
                await asyncio.sleep(AUTOPOST_GAP_MIN * 60)
            run = runners.get(cat)
            if not run:
                log.warning("autopost: unknown category %r — skipping", cat)
                continue
            try:
                await run(_AutoInteraction(ch))
                log.info("autopost: %s parlay posted", cat)
            except Exception as e:
                log.error("autopost: %s failed (continuing): %s", cat, e)


client = ParlayBot()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
