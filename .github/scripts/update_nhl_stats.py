#!/usr/bin/env python3
"""
update_nhl_stats.py
───────────────────
Rebuild the NHL stats files for every player currently on an RWHA roster:

    nhl_stats.js       window.NHL_STATS       current NHL season
    nhl_stats_prev.js  window.NHL_STATS_PREV  previous two NHL seasons combined

Player list comes from data.js (so traded / newly signed players are picked up
automatically).  Stats come from NHL.com's public stats feed (no key needed):
a handful of bulk requests per season instead of one request per player.

Matching: by name (accent-insensitive, with common nickname variants), then by
the NHL id rwha.net publishes for some players (accepted only when the last
name also matches, since a few ids on rwha.net are placeholders).

Run from the repository root (done automatically by GitHub Actions).
Uses only Python stdlib — no pip installs required.
"""

import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA_FILE = Path('data.js')
CUR_FILE  = Path('nhl_stats.js')
PREV_FILE = Path('nhl_stats_prev.js')
API_BASE  = os.environ.get('NHL_API_BASE', 'https://api.nhle.com').rstrip('/')

# ── Seasons ───────────────────────────────────────────────────────────────────
# Switch to the new season on September 1 (training camp / preseason).  Until
# the first regular-season game, the current file is empty and the site
# defaults to showing the previous season.
_now   = datetime.now(timezone.utc)
_start = int(os.environ.get('NHL_SEASON_START') or (_now.year if _now.month >= 9 else _now.year - 1))
SEASON      = f'{_start}{_start + 1}'
LABEL       = f'{_start}-{str(_start + 1)[-2:]}'
# "Combined Prev 2 Seasons": the two regular seasons before the current one,
# aggregated by the NHL API (e.g. 2024-25 + 2025-26 while 2026-27 is current).
COMB_FROM   = f'{_start - 2}{_start - 1}'
COMB_TO     = f'{_start - 1}{_start}'
PREV_LABEL  = f'{_start - 2}-{str(_start - 1)[-2:]} + {_start - 1}-{str(_start)[-2:]}'

CUR_FILTER  = f'seasonId={SEASON} and gameTypeId=2'
COMB_FILTER = f'seasonId>={COMB_FROM} and seasonId<={COMB_TO} and gameTypeId=2'

# ── Nickname / spelling mappings (all lowercase) ──────────────────────────────
LONG_TO_SHORT = {
    'aleksander': 'alex', 'alexander': 'alex', 'alexis': 'alex',
    'artem': 'artemi', 'cameron': 'cam', 'christopher': 'chris',
    'daniel': 'dan', 'dmitri': 'dmitry', 'egor': 'yegor', 'evgeni': 'evgeny',
    'jacob': 'jake', 'james': 'jim', 'jonathan': 'jon', 'konstantin': 'kosta',
    'mathew': 'matt', 'matthew': 'matt', 'maximilian': 'max',
    'michael': 'mike', 'mikhail': 'mike', 'mitchell': 'mitch',
    'nicholas': 'nick', 'nicolas': 'nick', 'nikolaj': 'nick', 'nikolai': 'nick',
    'patrick': 'pat', 'richard': 'rick', 'robert': 'rob', 'samuel': 'sam',
    'thomas': 'tom', 'timothy': 'tim', 'william': 'will', 'yevgeni': 'evgeny',
    'zachary': 'zach',
}
SHORT_TO_LONG: dict = {}
for _l, _s in LONG_TO_SHORT.items():
    SHORT_TO_LONG.setdefault(_s, []).append(_l)


def normalize(name: str) -> str:
    name = unicodedata.normalize('NFD', name or '')
    name = ''.join(c for c in name if unicodedata.category(c) != 'Mn')
    name = re.sub(r'[^a-z ]', '', name.lower().replace('-', ' '))
    return re.sub(r'\s+', ' ', name).strip()


def name_variants(name: str) -> list:
    base = normalize(name)
    parts = base.split()
    if not parts:
        return [base]
    first, rest = parts[0], parts[1:]
    out = {base}
    if first in LONG_TO_SHORT:
        out.add(' '.join([LONG_TO_SHORT[first]] + rest))
    for lf in SHORT_TO_LONG.get(first, []):
        out.add(' '.join([lf] + rest))
    # also try the canonical short form of any long form (Mikhail ↔ Michael)
    if first in LONG_TO_SHORT:
        for lf in SHORT_TO_LONG.get(LONG_TO_SHORT[first], []):
            out.add(' '.join([lf] + rest))
    return list(out)


def clean_key(name: str) -> str:
    return re.sub(r'\s*\([RCA]\)', '', name).strip()


# ── NHL API ───────────────────────────────────────────────────────────────────
def fetch_json(url: str, retries: int = 3) -> dict:
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (rwha-stats-site)'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise
            print(f'  retry {attempt}: {e}', flush=True)
            time.sleep(3 * attempt)


def fetch_bulk(kind: str, cayenne: str, aggregate: bool) -> list:
    """Page through the NHL stats summary for skaters or goalies.

    aggregate=True sums multiple seasons into one row per player (the NHL API
    also recomputes GAA / SV% correctly across seasons).  Sorting by playerId
    keeps pagination stable so no player is skipped or repeated.
    """
    rows, start = [], 0
    sort = urllib.parse.quote('[{"property":"playerId","direction":"ASC"}]')
    exp = urllib.parse.quote(cayenne)
    while True:
        url = (f'{API_BASE}/stats/rest/en/{kind}/summary'
               f'?isAggregate={"true" if aggregate else "false"}&isGame=false'
               f'&limit=100&start={start}&sort={sort}&cayenneExp={exp}')
        data = fetch_json(url)
        batch = data.get('data', [])
        rows.extend(batch)
        total = data.get('total', len(rows))
        if len(rows) >= total or not batch:
            break
        start += 100
        time.sleep(0.4)
    print(f'  [{cayenne} | {kind}] {len(rows)} rows', flush=True)
    return rows


def v(x) -> str:
    return '' if x is None else str(x)


def nhl_url(pid, full_name: str) -> str:
    return f'https://www.nhl.com/player/{normalize(full_name).replace(" ", "-")}-{pid}'


def skater_entry(r: dict) -> dict:
    pid = r.get('playerId')
    return {
        'gp':  v(r.get('gamesPlayed')), 'g': v(r.get('goals')),
        'a':   v(r.get('assists')),     'pts': v(r.get('points')),
        'pm':  v(r.get('plusMinus')),   'pim': v(r.get('penaltyMinutes')),
        'sog': v(r.get('shots')),
        'id': pid, 'pos': r.get('positionCode', ''),
        'url': nhl_url(pid, r.get('skaterFullName', '')) if pid else '',
        '_last': normalize(r.get('lastName', '') or r.get('skaterFullName', '').split(' ')[-1]),
    }


def goalie_entry(r: dict) -> dict:
    pid = r.get('playerId')
    gaa = float(r.get('goalsAgainstAverage') or 0)
    svp = float(r.get('savePct') or 0)
    return {
        'gp': v(r.get('gamesPlayed')), 'w': v(r.get('wins')),
        'l':  v(r.get('losses')),      'ot': v(r.get('otLosses')),
        'gaa': f'{gaa:.2f}', 'svp': f'{svp:.3f}'.lstrip('0') or '.000',
        'so': v(r.get('shutouts')),
        'id': pid, 'pos': 'G',
        'url': nhl_url(pid, r.get('goalieFullName', '')) if pid else '',
        '_last': normalize(r.get('lastName', '') or r.get('goalieFullName', '').split(' ')[-1]),
    }


def build_index(cayenne: str, aggregate: bool):
    by_name, by_id = {}, {}
    for kind, name_key, make in (('skater', 'skaterFullName', skater_entry),
                                 ('goalie', 'goalieFullName', goalie_entry)):
        for r in fetch_bulk(kind, cayenne, aggregate):
            e = make(r)
            by_name.setdefault(normalize(r.get(name_key, '')), e)
            if e['id']:
                by_id[str(e['id'])] = e
    return by_name, by_id


def match(player: dict, by_name: dict, by_id: dict):
    name = clean_key(player['nm'])
    for v in name_variants(name):
        if v in by_name:
            return by_name[v]
    nid = player.get('nid') or ''
    if nid and nid in by_id:
        last = normalize(name).split(' ')[-1] if name else ''
        if by_id[nid]['_last'] and by_id[nid]['_last'].split(' ')[-1] == last:
            return by_id[nid]
    return None


def load_players() -> list:
    raw = DATA_FILE.read_text(encoding='utf-8')
    data = json.loads(raw.split('=', 1)[1].strip().rstrip(';').strip())
    seen, out = set(), []
    for team in data.values():
        for grp in ('ps', 'pg', 'fs', 'fg'):
            for p in team.get(grp, []):
                k = clean_key(p['nm'])
                if k not in seen:
                    seen.add(k)
                    out.append(p)
    return out


def write_file(path: Path, var: str, stats: dict, labels: dict) -> None:
    body = json.dumps(stats, ensure_ascii=False, separators=(',', ':'))
    extra = ''.join(f'window.{k} = {json.dumps(v)};\n' for k, v in labels.items())
    path.write_text(f'window.{var} = {body};\n{extra}', encoding='utf-8')


def run_season(cayenne: str, aggregate: bool, players: list):
    by_name, by_id = build_index(cayenne, aggregate)
    stats, matched, missing = {}, 0, []
    for p in players:
        e = match(p, by_name, by_id)
        key = clean_key(p['nm'])
        if e:
            stats[key] = {k: v for k, v in e.items() if not k.startswith('_')}
            matched += 1
        else:
            stats[key] = {}
            missing.append(key)
    return stats, matched, missing, len(by_name)


def main() -> None:
    if not DATA_FILE.exists():
        print('ERROR: data.js not found — run from repo root', file=sys.stderr)
        sys.exit(1)
    players = load_players()
    print(f'NHL current season {LABEL}; combined {PREV_LABEL}; {len(players)} RWHA players\n', flush=True)

    labels = {'NHL_SEASON_LABEL': LABEL, 'NHL_PREV_LABEL': PREV_LABEL}

    cur, cur_m, cur_miss, cur_n = run_season(CUR_FILTER, False, players)
    prev, prev_m, _, prev_n = run_season(COMB_FILTER, True, players)

    if prev_n == 0:
        print('ERROR: NHL API returned no data for the combined previous seasons — not writing files',
              file=sys.stderr)
        sys.exit(1)

    write_file(CUR_FILE, 'NHL_STATS', cur, labels)
    write_file(PREV_FILE, 'NHL_STATS_PREV', prev, labels)

    print(f'\n{LABEL}: matched {cur_m}/{len(players)}'
          + ('  (season not started yet — no games in the NHL feed)' if cur_n == 0 else ''))
    print(f'{PREV_LABEL}: matched {prev_m}/{len(players)}')
    if cur_n and cur_miss:
        print(f'\nNo {LABEL} NHL stats for {len(cur_miss)} players (AHL / junior / Europe / injured / name mismatch):')
        for n in sorted(cur_miss):
            print(f'  – {n}')

    github_env = os.environ.get('GITHUB_ENV')
    if github_env:
        with open(github_env, 'a') as f:
            f.write(f'NHL_MATCHED={cur_m if cur_n else prev_m}\n')
            f.write(f'NHL_TOTAL={len(players)}\n')
            f.write(f'NHL_LABEL={LABEL if cur_n else PREV_LABEL}\n')


if __name__ == '__main__':
    main()
