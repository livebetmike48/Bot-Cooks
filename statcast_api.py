"""
Client for Baseball Savant's Statcast CSV export + MLB's official live feed.
Every classification rule in here was validated line-by-line against
Baseball Savant's own numbers (Soto, Witt, Cease, Mason Miller):
- regular season only (spring training was inflating counts ~7%)
- foul tips and missed bunts count as whiffs; bunt attempts count as swings
- sac flies/bunts and catcher's interference are NOT at-bats
- intentional walks count as walks (and are excluded from AB)
- strikeout double plays count as strikeouts
- runner events (caught stealing, pickoffs...) do NOT end a plate appearance
- xBA denominator excludes untracked batted balls (Savant's method)
"""
import csv
import io
import requests

SAVANT_BASE = "https://baseballsavant.mlb.com/statcast_search/csv"
PEOPLE_SEARCH = "https://statsapi.mlb.com/api/v1/people/search"
MLB_BASE = "https://statsapi.mlb.com/api/v1"
MLB_BASE_V1_1 = "https://statsapi.mlb.com/api/v1.1"

NON_PA_EVENTS = {
    "caught_stealing_2b", "caught_stealing_3b", "caught_stealing_home",
    "pickoff_1b", "pickoff_2b", "pickoff_3b",
    "pickoff_caught_stealing_2b", "pickoff_caught_stealing_3b", "pickoff_caught_stealing_home",
    "stolen_base_2b", "stolen_base_3b", "stolen_base_home",
    "wild_pitch", "passed_ball", "truncated_pa", "game_advisory", "runner_double_play", "other_advance",
}

SWING_DESCRIPTIONS = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip", "hit_into_play",
                      "foul_bunt", "missed_bunt", "bunt_foul_tip"}
# foul_tip counts as a whiff in Savant's definition -- empirically confirmed
# (Soto vs RHP: 17.3% with it vs the real 17.4%; 15.4% without).
WHIFF_DESCRIPTIONS = {"swinging_strike", "swinging_strike_blocked", "foul_tip", "missed_bunt"}

HIT_EVENTS = {"single", "double", "triple", "home_run"}
K_EVENTS = {"strikeout", "strikeout_double_play"}
BB_EVENTS = {"walk", "intent_walk"}
NON_AB_EVENTS = K_EVENTS | BB_EVENTS | HIT_EVENTS | {
    "hit_by_pitch", "sac_fly", "sac_bunt", "sac_fly_double_play", "sac_bunt_double_play", "catcher_interf",
}


def resolve_player(name: str) -> dict | None:
    """Returns {'id', 'name', 'is_pitcher', 'bat_side', 'pitch_hand'} or None.
    bat_side is 'L'/'R'/'S' (switch); pitch_hand is 'L'/'R'."""
    resp = requests.get(PEOPLE_SEARCH, params={"names": name}, timeout=15)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    if not people:
        return None
    p = people[0]
    position_code = (p.get("primaryPosition") or {}).get("code")
    return {
        "id": p["id"],
        "name": p.get("fullName", name),
        "is_pitcher": position_code == "1",
        "bat_side": (p.get("batSide") or {}).get("code"),
        "pitch_hand": (p.get("pitchHand") or {}).get("code"),
    }


def effective_bat_side(bat_side: str, opposing_pitch_hand: str) -> str:
    """A switch hitter ('S') bats from the opposite side of the pitcher's
    hand; otherwise their listed side."""
    if bat_side == "S":
        return "L" if opposing_pitch_hand == "R" else "R"
    return bat_side


def fetch_statcast(player_id: int, is_pitcher: bool, start_date: str, end_date: str) -> list[dict]:
    """Pitch-level rows for one player. hfGT 'R|' = REGULAR SEASON ONLY --
    including spring training inflated Soto's fastball count 317 vs the
    real 297 until this was fixed."""
    params = {
        "all": "true", "hfPT": "", "hfAB": "", "hfBBT": "", "hfPR": "", "hfZ": "",
        "stadium": "", "hfBBL": "", "hfNewZones": "", "hfGT": "R|",
        "hfSea": "", "hfSit": "", "hfOuts": "",
        "opponent": "", "pitcher_throws": "", "batter_stands": "", "hfSA": "",
        "game_date_gt": start_date, "game_date_lt": end_date,
        "team": "", "position": "", "hfRO": "",
        "home_road": "", "hfFlag": "", "metric_1": "", "hfInn": "",
        "min_pitches": 0, "min_results": 0, "group_by": "name",
        "sort_col": "pitches", "player_event_sort": "h_launch_speed",
        "sort_order": "desc", "min_abs": 0, "type": "details",
    }
    if is_pitcher:
        params["player_type"] = "pitcher"
        params["pitchers_lookup[]"] = player_id
    else:
        params["player_type"] = "batter"
        params["batters_lookup[]"] = player_id

    resp = requests.get(SAVANT_BASE, params=params, timeout=30)
    resp.raise_for_status()

    text = resp.text
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def fetch_percentile_leaderboard(player_type: str, year: int, team: str = "") -> str:
    """Raw CSV text from Savant's percentile-rankings leaderboard."""
    url = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"
    params = {"type": player_type, "year": year, "team": team, "csv": "true"}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.text


def _pa_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("events") and r.get("events") not in NON_PA_EVENTS]


def _core_stats(rows: list[dict]) -> dict:
    """Shared, validated stat computations over any subset of pitch rows."""
    swings = sum(1 for r in rows if r.get("description") in SWING_DESCRIPTIONS)
    whiffs = sum(1 for r in rows if r.get("description") in WHIFF_DESCRIPTIONS)

    pa_rows = _pa_rows(rows)
    strikeouts = sum(1 for r in pa_rows if r.get("events") in K_EVENTS)
    walks = sum(1 for r in pa_rows if r.get("events") in BB_EVENTS)
    hits = sum(1 for r in pa_rows if r.get("events") in HIT_EVENTS)
    balls_in_play_outs = sum(1 for r in pa_rows if r.get("events") not in NON_AB_EVENTS)
    at_bats = hits + balls_in_play_outs + strikeouts

    # xBA: untracked batted balls (no estimate) are EXCLUDED from the
    # denominator, matching Savant (derived: Soto FF .397 -> .405 exact).
    xba_tracked = [
        v for v in (
            _safe_float(r.get("estimated_ba_using_speedangle"))
            for r in rows if r.get("description") == "hit_into_play"
        ) if v is not None
    ]
    xba_numerator = sum(xba_tracked)

    xwoba_values = [_safe_float(r.get("estimated_woba_using_speedangle")) for r in rows]
    xwoba_values = [v for v in xwoba_values if v is not None]

    result = {"pa": len(pa_rows), "swings": swings}
    if swings > 0:
        result["whiff_pct"] = round(whiffs / swings * 100, 1)
    if at_bats > 0:
        result["avg"] = round(hits / at_bats, 3)
    xba_at_bats = len(xba_tracked) + strikeouts
    if xba_at_bats > 0:
        result["xba"] = round(xba_numerator / xba_at_bats, 3)
    if pa_rows:
        result["k_pct"] = round(strikeouts / len(pa_rows) * 100, 1)
        result["bb_pct"] = round(walks / len(pa_rows) * 100, 1)
    if xwoba_values:
        result["xwoba"] = round(sum(xwoba_values) / len(xwoba_values), 3)
    return result


def vs_handedness_stats(rows: list[dict], hand_field: str, hand_value: str) -> dict | None:
    """hand_field: 'p_throws' to split a BATTER's stats by pitcher hand,
    'stand' to split a PITCHER's stats by batter side."""
    filtered = [r for r in rows if r.get(hand_field) == hand_value]
    if not filtered:
        return None
    return _core_stats(filtered)


def vs_pitch_type_stats(rows: list[dict], pitch_type: str) -> dict | None:
    """Performance against one pitch type."""
    pitch_rows = [r for r in rows if r.get("pitch_type") == pitch_type]
    if not pitch_rows:
        return None
    stats = _core_stats(pitch_rows)
    stats["pitches_seen"] = len(pitch_rows)
    stats["pa_ending_on_this_pitch"] = stats.pop("pa")
    return stats


def vs_each_pitch(rows: list[dict], min_pitches: int = 10) -> dict:
    """Stats against EVERY pitch type at once, sorted by pitches seen."""
    pitch_types = {}
    for r in rows:
        pt = r.get("pitch_type")
        if pt:
            pitch_types[pt] = pitch_types.get(pt, 0) + 1

    result = {}
    for pt, count in sorted(pitch_types.items(), key=lambda x: -x[1]):
        if count < min_pitches:
            continue
        stats = vs_pitch_type_stats(rows, pt)
        if stats:
            result[pt] = stats
    return result


def pitch_mix_breakdown(rows: list[dict]) -> dict:
    """A pitcher's usage %, avg velocity, and whiff rate per pitch type.
    Whiff rate = whiffs / swings."""
    total_pitches = len(rows)
    if total_pitches == 0:
        return {}

    by_type: dict[str, dict] = {}
    for r in rows:
        pt = r.get("pitch_type")
        if not pt:
            continue
        bucket = by_type.setdefault(pt, {"count": 0, "speeds": [], "swings": 0, "whiffs": 0})
        bucket["count"] += 1
        speed = _safe_float(r.get("release_speed"))
        if speed is not None:
            bucket["speeds"].append(speed)
        desc = r.get("description")
        if desc in SWING_DESCRIPTIONS:
            bucket["swings"] += 1
        if desc in WHIFF_DESCRIPTIONS:
            bucket["whiffs"] += 1

    result = {}
    for pt, b in by_type.items():
        entry = {
            "usage_pct": round(b["count"] / total_pitches * 100, 1),
            "count": b["count"],
        }
        if b["speeds"]:
            entry["avg_velo"] = round(sum(b["speeds"]) / len(b["speeds"]), 1)
        if b["swings"] > 0:
            entry["whiff_pct"] = round(b["whiffs"] / b["swings"] * 100, 1)
        result[pt] = entry
    return dict(sorted(result.items(), key=lambda x: -x[1]["count"]))


def pitch_mix_by_handedness(rows: list[dict]) -> dict:
    """Pitch mix split by batter side (vs LHH / vs RHH)."""
    return {
        "vs_L": pitch_mix_breakdown([r for r in rows if r.get("stand") == "L"]),
        "vs_R": pitch_mix_breakdown([r for r in rows if r.get("stand") == "R"]),
        "overall": pitch_mix_breakdown(rows),
    }


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
