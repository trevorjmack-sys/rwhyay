#!/usr/bin/env python3
"""
update_nhl_stats.py
───────────────────
Auto-update nhl_stats.js with current-season NHL data.

Run from the repository root (done automatically by GitHub Actions).
Uses only Python stdlib — no pip installs required.
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Current season (auto-detected) ───────────────────────────────────────────
# NHL regular season runs Oct–Jun; before October = we're still in last season.
_now = datetime.utcnow()
_start = _now.year if _now.month >= 10 else _now.year - 1
SEASON = f'{_start}{_start + 1}'

STATS_FILE = Path('nhl_stats.js')   # relative to repo root (CWD in Actions)

print(f"NHL season: {SEASON}  ({_start}–{_start+1})", flush=True)

# ── Nickname / spelling mappings ──────────────────────────────────────────────
# Long/legal form → common NHL display name  (all lowercase)
LONG_TO_SHORT: dict[str, str] = {
    'aleksander': 'alex',
    'alexander':  'alex',
    'alexis':     'alex',
    'andrei':     'andrei',     # keep — some are Andrei, some Andrey
    'artem':      'artemi',     # Artem → Artemi (Panarin)
    'cameron':    'cam',
    'christopher':'chris',
    'daniel':     'dan',
    'dmitri':     'dmitry',
    'egor':       'yegor',      # RWHA "Egor" ↔ NHL "Yegor"
    'evgeni':     'evgeny',
    'jacob':      'jake',
    'james':      'jim',
    'jonathan':   'jon',
    'konstantin': 'kosta',
    'mathew':     'matt',
    'matthew':    'matt',
    'maximilian': 'max',
    'michael':    'mike',
    'mikhail':    'mike',
    'mitchell':   'mitch',
    'nicholas':   'nick',
    'nicolas':    'nick',
    'nikolaj':    'nick',
    'nikolai':    'nick',
    'patrick':    'pat',
    'richard':    'rick',
    'robert':     'rob',
    'samuel':     'sam',
    'thomas':     'tom',
    'timothy':    'tim',
    'william':    'will',
    'yevgeni':    'evgeny',
    'zachary':    'zach',
}

# Build reverse: short → [legal long forms]
SHORT_TO_LONG: dict[str, list[str]] = {}
for _long, _short in LONG_TO_SHORT.items():
    SHORT_TO_LONG.setdefault(_short, []).append(_long)


def normalize(name: str) -> str:
    """Remove accents, lowercase, strip punctuation, collapse spaces."""
    name = unicodedata.normalize('NFD', name)
    name = ''.join(c for c in name if unicodedata.category(c) != 'Mn')
    name = re.sub(r"[^a-z ]", '', name.lower())
    return re.sub(r'\s+', ' ', name).strip()


def name_variants(name: str) -> list[str]:
    """Return all plausible normalized variants of a player name."""
    base = normalize(name)
    parts = base.split()
    if not parts:
        return [base]
    first, rest = parts[0], parts[1:]
    variants: set[str] = {base}

    # Long → short  (Mitchell → Mitch)
    if first in LONG_TO_SHORT:
        variants.add(' '.join([LONG_TO_SHORT[first]] + rest))

    # Short → long  (Matt → Matthew / Mathew)
    if first in SHORT_TO_LONG:
        for long_form in SHORT_TO_LONG[first]:
            variants.add(' '.join([long_form] + rest))

    return list(variants)


def clean_rwha_key(name: str) -> str:
    """Strip RWHA captain/rookie annotations: (R), (C), (A)."""
    return re.sub(r'\s*\([RCA]\)', '', name).strip()


# ── NHL API helpers ───────────────────────────────────────────────────────────
def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch_bulk(kind: str) -> list:
    """Paginate NHL stats API, returning all rows for 'skater' or 'goalie'."""
    rows: list = []
    start = 0
    sort = 'points' if kind == 'skater' else 'wins'
    while True:
        url = (
            f'https://api.nhle.com/stats/rest/en/{kind}/summary'
            f'?limit=100&start={start}&sort={sort}&direction=DESC'
            f'&cayenneExp=seasonId%3D{SEASON}%20and%20gameTypeId%3D2'
        )
        data = fetch_json(url)
        batch = data.get('data', [])
        rows.extend(batch)
        total = data.get('total', len(rows))
        print(f"  [{kind}] {len(rows)}/{total}", flush=True)
        if len(rows) >= total or not batch:
            break
        start += 100
        time.sleep(0.4)
    return rows


# ── Build name → stats lookups ────────────────────────────────────────────────
def build_skater_lookup(rows: list) -> dict:
    lookup: dict = {}
    for r in rows:
        name = normalize(r.get('skaterFullName', ''))
        if not name:
            continue
        lookup[name] = {
            'gp':  str(r.get('gamesPlayed',    '') or ''),
            'g':   str(r.get('goals',          '') or ''),
            'a':   str(r.get('assists',        '') or ''),
            'pts': str(r.get('points',         '') or ''),
            'pm':  str(r.get('plusMinus',      '') or ''),
            'pim': str(r.get('penaltyMinutes', '') or ''),
            'sog': str(r.get('shots',          '') or ''),
        }
    return lookup


def build_goalie_lookup(rows: list) -> dict:
    lookup: dict = {}
    for r in rows:
        name = normalize(r.get('goalieFullName', ''))
        if not name:
            continue
        gaa = float(r.get('goalsAgainstAverage', 0) or 0)
        svp = float(r.get('savePct',              0) or 0)
        lookup[name] = {
            'gp':  str(r.get('gamesPlayed', '') or ''),
            'w':   str(r.get('wins',        '') or ''),
            'l':   str(r.get('losses',      '') or ''),
            'ot':  str(r.get('otLosses',    '') or ''),
            'gaa': f'{gaa:.2f}',
            'svp': f'{svp:.3f}'.lstrip('0') or '.000',
            'so':  str(r.get('shutouts',    '') or ''),
        }
    return lookup


def match_player(name: str, skaters: dict, goalies: dict):
    """Try all name variants against both lookups. Returns stats dict or None."""
    for variant in name_variants(name):
        if variant in skaters:
            return skaters[variant]
        if variant in goalies:
            return goalies[variant]
    return None


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    if not STATS_FILE.exists():
        print(f'ERROR: {STATS_FILE} not found — run from repo root', file=sys.stderr)
        sys.exit(1)

    # Parse existing nhl_stats.js  (format: window.NHL_STATS = {...};)
    raw = STATS_FILE.read_text(encoding='utf-8').strip()
    json_str = raw.split('=', 1)[1].strip().rstrip(';').strip()
    stats: dict = json.loads(json_str)
    print(f'Loaded {len(stats)} RWHA player keys from nhl_stats.js\n')

    # Fetch bulk stats from NHL API
    print(f'Fetching skater stats…')
    skater_rows = fetch_bulk('skater')
    print(f'\nFetching goalie stats…')
    goalie_rows  = fetch_bulk('goalie')

    skater_lookup = build_skater_lookup(skater_rows)
    goalie_lookup  = build_goalie_lookup(goalie_rows)
    print(f'\nNHL index: {len(skater_lookup)} skaters, {len(goalie_lookup)} goalies\n')

    # Match each RWHA player to NHL stats
    matched: int = 0
    unmatched: list[str] = []

    for key in stats:
        clean = clean_rwha_key(key)
        result = match_player(clean, skater_lookup, goalie_lookup)
        if result is not None:
            stats[key] = result
            matched += 1
        else:
            stats[key] = {}   # retired / AHL / injured / no NHL stats this season
            unmatched.append(clean)

    print(f'Matched: {matched}/{len(stats)}')
    if unmatched:
        print(f'No NHL stats found for {len(unmatched)} players'
              ' (retired / AHL / injured — stored as empty):')
        for name in sorted(unmatched):
            print(f'  – {name}')

    # Write updated file
    updated_json = json.dumps(stats, ensure_ascii=False, separators=(',', ':'))
    STATS_FILE.write_text(f'window.NHL_STATS = {updated_json};\n', encoding='utf-8')
    size_kb = STATS_FILE.stat().st_size / 1024
    print(f'\n✓ nhl_stats.js written  ({size_kb:.1f} KB,  {matched} players with stats)')

    # Export counts to GitHub Actions environment so the commit step can read them
    github_env = os.environ.get('GITHUB_ENV')
    if github_env:
        with open(github_env, 'a') as f:
            f.write(f'NHL_MATCHED={matched}\n')
            f.write(f'NHL_TOTAL={len(stats)}\n')

    sys.exit(0)


if __name__ == '__main__':
    main()
