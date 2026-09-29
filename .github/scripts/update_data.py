#!/usr/bin/env python3
"""
update_data.py
──────────────
Pull pro and farm rosters from rwha.net and rebuild data.js.

rwha.net was redesigned for the 2026-27 season: the old STHS page
RWHA-ProTeamRoster.php is gone (404).  The new site renders each team page
from JSON endpoints, which we read directly:

    /auth/league.php          → list of 22 teams (number, name, conference…)
    /auth/team.php?n=<number> → rosters.pro / rosters.scratch / rosters.farm

That is 23 small requests per run instead of scraping HTML.

Run from the repository root (done automatically by GitHub Actions).
Uses only Python stdlib — no pip installs required.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_URL  = os.environ.get('RWHA_BASE_URL', 'http://www.rwha.net').rstrip('/')
DATA_FILE = Path('data.js')

# ── Fictional players (league in-jokes) are left off the stats site ──────────
# Names change often, so detection doesn't rely on them.  Every fictional
# "GM player" on rwha.net shares a fingerprint no real player has:
#   • Potential (PO) rating of 1, and
#   • a salary of exactly $8,500,000
# (Checked Sep 2026: matches all 22 fictional players, 0 of 814 real ones.)
FICTIONAL_SALARY = 8_500_000
FICTIONAL_MAX_PO = 1

# rwha.net player ids (the number in players/p<ID>.html) stay the same when a
# player is renamed, so they're a stable way to force a decision either way.
FICTIONAL_IDS: set = {
    2, 5, 8, 13, 15, 19, 24, 25, 26, 37, 87, 409, 472, 494, 635, 841,
    1311, 2062, 2070, 2071, 2075, 2077,
}
ALWAYS_REAL_IDS: set = set()   # add an id here if a real player is ever caught by mistake


def is_fictional(p: dict) -> bool:
    pid = p.get('id')
    if pid in ALWAYS_REAL_IDS:
        return False
    if pid in FICTIONAL_IDS:
        return True
    po = (p.get('ratings') or {}).get('PO')
    return po is not None and po <= FICTIONAL_MAX_PO and p.get('salary') == FICTIONAL_SALARY


# Team names change (WaffleBots → Boobys, Shitdawgs → Shitbirds), so teams are
# matched to last run's data by rwha.net's permanent team number instead
# (league.php "number", e.g. Aces = 2).  Names are only a fallback.
GM_REFRESH_WEEKDAY = 0   # Monday: re-read every team's GM in case one changed

# ── Fetch ───────────────────────────────────────────────────────────────────────
# rwha.net has had an expired SSL cert in the past; verification is disabled
# intentionally (the site is plain http anyway).
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def fetch_json(path: str, retries: int = 4):
    """GET a rwha.net JSON endpoint.

    rwha.net is a small server: it sometimes stalls, and while the league
    data is being regenerated its JSON endpoints can briefly return an empty
    or non-JSON body.  Retry with a long backoff, and log what came back so a
    failure in the Actions log shows the real response.
    """
    url = f'{BASE_URL}{path}'
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (rwha-stats-site)',
                'Accept': 'application/json',
                'Cache-Control': 'no-cache',
            })
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=60) as r:
                status, ctype = r.status, r.headers.get('Content-Type', '')
                body = r.read().decode('utf-8', errors='replace')
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                raise ValueError(f'HTTP {status}, {ctype or "no content-type"}, '
                                 f'{len(body)} bytes, not JSON: {body[:200]!r}')
        except Exception as e:  # noqa: BLE001
            if attempt == retries:
                raise
            wait = 30 * attempt
            print(f'  {path}: {e} — retry {attempt}/{retries - 1} in {wait}s', file=sys.stderr, flush=True)
            time.sleep(wait)

# ── Helpers ─────────────────────────────────────────────────────────────────────
POS_MAP = {'C': 'C', 'LW': 'L', 'RW': 'R', 'D': 'D', 'G': 'G'}


def fmt_salary(n) -> str:
    try:
        return f'${int(n):,}'
    except (TypeError, ValueError):
        return ''


def s(v) -> str:
    return '' if v is None else str(v)


def display_name(p: dict) -> str:
    # The site's nameTags() turns a trailing "(R)" into a rookie badge.
    nm = (p.get('name') or '').strip()
    return f'{nm} (R)' if p.get('rookie') else nm


def base_fields(p: dict) -> dict:
    return {
        'nm':  display_name(p),
        'nid': s(p.get('nhl_id')),          # NHL.com id when rwha.net has one
        'rid': s(p.get('id')),              # rwha.net player id
        'con': '',                          # conditioning: not published any more
        'ij':  s(p.get('injury')),
        'ov':  s(p.get('ovr')),
        'ta':  '',
        'sp':  '0',
        'age': s(p.get('age')),
        'c':   s(p.get('contract')),
        'sal': fmt_salary(p.get('salary')),
    }


def parse_skater(p: dict) -> dict:
    r = p.get('ratings') or {}
    out = {
        'n': s(p.get('jersey')),
        **base_fields(p),
        'p': '/'.join(POS_MAP.get(x, x) for x in (p.get('positions') or [])),
    }
    for k in ('CK', 'FG', 'DI', 'SK', 'ST', 'EN', 'DU', 'PH', 'FO',
              'PA', 'SC', 'DF', 'PS', 'EX', 'LD', 'PO', 'MO'):
        out[k.lower()] = s(r.get(k))
    return out


def parse_goalie(p: dict) -> dict:
    r = p.get('ratings') or {}
    out = {**base_fields(p), 'p': 'G'}
    for k in ('SK', 'DU', 'EN', 'SZ', 'AG', 'RB', 'SC', 'HS', 'RT',
              'PH', 'PS', 'EX', 'LD', 'PO', 'MO'):
        out[k.lower()] = s(r.get(k))
    return out


EXCLUDED: list = []


def split(players: list):
    skaters, goalies = [], []
    for p in players:
        if is_fictional(p):
            EXCLUDED.append(f"{p.get('name')} (id {p.get('id')})")
            continue
        if p.get('kind') == 'goalie':
            goalies.append(parse_goalie(p))
        else:
            skaters.append(parse_skater(p))
    return skaters, goalies


def avg(players: list, key: str) -> str:
    vals = [p[key] for p in players if isinstance(p.get(key), (int, float))]
    return str(round(sum(vals) / len(vals))) if vals else ''


def team_rating(players: list) -> str:
    """Team overall = mean OV of the dressed lineup (all players, incl. fictional)."""
    return avg(players, 'ovr')


def team_morale(players: list) -> str:
    return avg([p.get('ratings') or {} for p in players], 'MO')


def load_previous() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        raw = DATA_FILE.read_text(encoding='utf-8')
        return json.loads(raw.split('=', 1)[1].strip().rstrip(';').strip())
    except Exception:  # noqa: BLE001
        return {}


# ── Entry point ─────────────────────────────────────────────────────────────────
def main():
    previous = load_previous()

    print('Fetching league…', file=sys.stderr)
    league = fetch_json('/auth/league.php')
    teams = league.get('teams') or []
    if not teams:
        print('ERROR: no teams in league.php — aborting', file=sys.stderr)
        sys.exit(1)

    prev_by_num = {v['num']: v for v in previous.values() if isinstance(v, dict) and 'num' in v}
    refresh_gms = datetime.now(timezone.utc).weekday() == GM_REFRESH_WEEKDAY

    data = {}
    for t in sorted(teams, key=lambda x: x['name']):
        name = t['name']
        print(f'  {name} (#{t["number"]})…', file=sys.stderr)
        team = fetch_json(f'/auth/team.php?n={t["number"]}')
        ro = team.get('rosters') or {}
        pro, scratch, farm = ro.get('pro') or [], ro.get('scratch') or [], ro.get('farm') or []

        pro_s, pro_g = split(pro + scratch)
        farm_s, farm_g = split(farm)

        prev = prev_by_num.get(t['number']) or previous.get(name) or {}
        if prev and prev.get('n') and prev['n'] != name:
            print(f'  (team #{t["number"]} renamed: {prev["n"]} → {name})', file=sys.stderr)
        data[name] = {
            'n':    name,
            'num':  t['number'],          # permanent rwha.net team id
            'city': t.get('city', ''),
            'abbr': t.get('abbre', ''),
            'conf': t.get('conference', ''),
            'div':  t.get('division', ''),
            'url':  f'{BASE_URL}/{t.get("slug", "")}',
            'gm':   prev.get('gm', ''),   # GM isn't in the JSON; filled from team page below
            'fn':   prev.get('fn', '') or f'{name} Farm',
            'pm':   team_morale(pro),
            'po':   team_rating(pro),
            'fm':   team_morale(farm),
            'fo':   team_rating(farm),
            'ps':   pro_s,
            'pg':   pro_g,
            'fs':   farm_s,
            'fg':   farm_g,
        }
        time.sleep(1.0)   # be polite to rwha.net

    # GM names only appear in the team page HTML ("GM: <b>Name</b>").
    for name, d in data.items():
        if d['gm'] and not refresh_gms:
            continue          # already known from last run — skip the page fetch
        try:
            req = urllib.request.Request(d['url'], headers={'User-Agent': 'Mozilla/5.0 (rwha-stats-site)'})
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=30) as r:
                html = r.read().decode('utf-8', errors='replace')
            m = re.search(r'GM:\s*<b>([^<]+)</b>', html)
            if m:
                d['gm'] = m.group(1).strip()
        except Exception as e:  # noqa: BLE001
            print(f'  (GM lookup failed for {name}: {e})', file=sys.stderr)
        time.sleep(1.0)

    n_teams = len(data)
    players = sum(len(v['ps']) + len(v['pg']) + len(v['fs']) + len(v['fg']) for v in data.values())
    print(f'Parsed {n_teams} teams, {players} players', file=sys.stderr)
    print(f'Left off {len(EXCLUDED)} fictional players: ' + ', '.join(sorted(EXCLUDED)), file=sys.stderr)

    if n_teams < 20 or players < 300:
        print('ERROR: suspiciously little data — not overwriting data.js', file=sys.stderr)
        sys.exit(1)

    js = 'window.RWHA_DATA = ' + json.dumps(data, ensure_ascii=False, indent=2) + ';\n'
    DATA_FILE.write_text(js, encoding='utf-8')
    print(f'Wrote {DATA_FILE} ({DATA_FILE.stat().st_size:,} bytes)', file=sys.stderr)

    env_file = os.environ.get('GITHUB_ENV', '')
    if env_file:
        with open(env_file, 'a') as f:
            f.write(f'DATA_TEAMS={n_teams}\n')
            f.write(f'DATA_PLAYERS={players}\n')
            f.write(f'DATA_DATE={datetime.now(timezone.utc).strftime("%Y-%m-%d")}\n')


if __name__ == '__main__':
    main()
