"""
Savant's percentile-rankings leaderboards. IMPORTANT: every stat column in
this endpoint is Savant's own PRE-COMPUTED 0-100 percentile score, not a
raw stat value -- read them directly, never re-rank (re-ranking inverted
K% until this was caught: Soto showed 10th percentile instead of the real
91st). All column names confirmed from live data (batter: 544 rows/23 cols;
pitcher: 587 rows/22 cols).
"""
import csv
import io
import requests

LEADERBOARD_URL = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"

BATTER_STAT_COLUMNS = {
    "xwoba": ("xwoba", True),
    "xba": ("xba", True),
    "xslg": ("xslg", True),
    "xiso": ("xiso", True),
    "xobp": ("xobp", True),
    "barrel_pct": ("brl_percent", True),
    "exit_velo": ("exit_velocity", True),
    "max_ev": ("max_ev", True),
    "hard_hit_pct": ("hard_hit_percent", True),
    "k_pct": ("k_percent", True),   # Savant's own percentile, already
    "bb_pct": ("bb_percent", True), # correctly oriented -- don't re-invert
}

# Confirmed real column names from a live test (587 pitchers, 22 columns).
PITCHER_STAT_COLUMNS = {
    "xera": ("xera", True),
    "xwoba": ("xwoba", True),
    "xba": ("xba", True),
    "xslg": ("xslg", True),
    "xiso": ("xiso", True),
    "xobp": ("xobp", True),
    "barrel_pct": ("brl_percent", True),
    "exit_velo": ("exit_velocity", True),
    "max_ev": ("max_ev", True),
    "hard_hit_pct": ("hard_hit_percent", True),
    "k_pct": ("k_percent", True),
    "bb_pct": ("bb_percent", True),
    "whiff_pct": ("whiff_percent", True),
    "chase_pct": ("chase_percent", True),
    "fastball_velo": ("fb_velocity", True),
    "fastball_spin": ("fb_spin", True),
    "curve_spin": ("curve_spin", True),
    "arm_strength": ("arm_strength", True),
}

# Kept for backward compatibility
STAT_COLUMNS = BATTER_STAT_COLUMNS


def fetch_leaderboard(player_type: str = "batter", year: int = 2026, team: str = "") -> list[dict]:
    """Returns qualified players only (rows with actual data, not the
    empty placeholder rows for unqualified players). team: standard MLB
    abbreviation (e.g. NYY, BOS) to filter to one team, empty for all."""
    resp = requests.get(
        LEADERBOARD_URL,
        params={"type": player_type, "year": year, "team": team, "csv": "true"},
        timeout=20,
    )
    resp.raise_for_status()
    text = resp.text
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text))
    all_rows = list(reader)
    # Qualified players have a real xwoba value; unqualified rows are empty
    return [r for r in all_rows if r.get("xwoba")]


def _name_matches(input_name: str, csv_name: str) -> bool:
    """CSV names are 'Last, First'; people type 'First Last'. Word-subset
    matching handles both orders."""
    input_words = set(input_name.lower().replace(",", "").split())
    csv_words = set(csv_name.lower().replace(",", "").split())
    return bool(input_words) and input_words.issubset(csv_words)


def get_leaders(rows: list[dict], stat_key: str, limit: int = 10, stat_columns: dict = None, worst: bool = False) -> list[dict]:
    """Top (or bottom, worst=True) N players by a stat's percentile."""
    stat_columns = stat_columns or BATTER_STAT_COLUMNS
    if stat_key not in stat_columns:
        return []
    column, _ = stat_columns[stat_key]

    parsed = []
    for r in rows:
        raw = r.get(column)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        parsed.append({"name": r.get("player_name", "?"), "percentile": round(value)})

    parsed.sort(key=lambda p: p["percentile"], reverse=not worst)
    return parsed[:limit]


def get_percentile(rows: list[dict], stat_key: str, player_name: str, stat_columns: dict = None) -> dict | None:
    """This player's percentile, read directly from Savant's own column."""
    stat_columns = stat_columns or BATTER_STAT_COLUMNS
    if stat_key not in stat_columns:
        return None
    column, _ = stat_columns[stat_key]

    for r in rows:
        if _name_matches(player_name, r.get("player_name", "")):
            raw = r.get(column)
            if not raw:
                return None
            try:
                percentile = round(float(raw))
            except ValueError:
                return None
            return {"percentile": percentile, "sample_size": len(rows)}
    return None
