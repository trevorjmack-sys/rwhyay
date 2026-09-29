#!/usr/bin/env python3
"""
update_standings.py
───────────────────
Auto-update the STANDINGS constant in index.html with current + projected
RWHA standings.

Data sources (rwha.net JSON endpoints used by the redesigned 2026-27 site):
    /auth/league.php    → teams, conference, division, current record & points
    /auth/schedule.php  → every game, played/unplayed (for remaining schedule)
    data.js             → team Pro OV (po) for the projection model

Projection model
  - Win probability: logistic function on OV differential (k = 0.20)
  - OT rate calibrated from games played so far this season
  - Expected pts per remaining game = 2·p(win) + ot_rate·(1−p(win))

Run from the repository root (done automatically by GitHub Actions).
Uses only Python stdlib — no pip installs required.
"""

import json
import math
import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

INDEX_FILE = Path('index.html')
DATA_FILE  = Path('data.js')
BASE_URL   = os.environ.get('RWHA_BASE_URL', 'http://www.rwha.net').rstrip('/')

K = 0.20   # 3 OV advantage ≈ 60% win probability

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def fetch_json(path: str):
    req = urllib.request.Request(f'{BASE_URL}{path}',
                                 headers={'User-Agent': 'Mozilla/5.0 (rwha-stats-site)'})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
        return json.loads(r.read().decode('utf-8', errors='replace'))


def load_team_ov() -> dict:
    raw = DATA_FILE.read_text(encoding='utf-8')
    data = json.loads(raw.split('=', 1)[1].strip().rstrip(';').strip())
    ov = {}
    for team, d in data.items():
        try:
            ov[team] = int(d.get('po') or 78)
        except ValueError:
            ov[team] = 78
    return ov


def build(league: dict, schedule: dict, team_ov: dict):
    by_num = {t['number']: t for t in league['teams']}
    teams  = {t['name']: t for t in league['teams']}

    games    = schedule.get('games') or []
    played   = [g for g in games if g.get('played')]
    unplayed = [g for g in games if not g.get('played')]
    ot_games = sum(1 for g in played if g.get('overtime') or g.get('shootout'))
    ot_rate  = ot_games / len(played) if played else 0.184

    rem = {n: [] for n in teams}
    for g in unplayed:
        h, v = by_num.get(g['home_team']), by_num.get(g['visitor_team'])
        if h and v:
            rem[h['name']].append(v['name'])
            rem[v['name']].append(h['name'])

    proj = {}
    for n, t in teams.items():
        ov_t = team_ov.get(n, 78)
        add = 0.0
        for opp in rem[n]:
            p = 1.0 / (1.0 + math.exp(-K * (ov_t - team_ov.get(opp, 78))))
            add += 2 * p + ot_rate * (1 - p)
        proj[n] = (t['record'].get('pts') or 0) + add
    return teams, rem, proj, ot_rate


def format_block(teams, rem, proj, ot_rate, today) -> str:
    def rec(n):
        return teams[n]['record']

    def wins(n):   # regulation + OT + shootout wins
        r = rec(n)
        return (r.get('w') or 0) + (r.get('otw') or 0) + (r.get('sow') or 0)

    confs = {}
    for n, t in teams.items():
        confs.setdefault(t['conference'], []).append(n)

    lines = [
        f'// ── League standings (scraped {today} from rwha.net) ──────',
        '// Projection model: remaining schedule × team OV win-probability (logistic, k=0.20)',
        f'// OT rate this season: {ot_rate:.3f}',
        '// cur = current conf rank, pts = current points, pct = points pct,',
        '// rem = games remaining, proj = projected final pts, projPos = projected conf rank',
        'const STANDINGS = {',
    ]
    for conf in sorted(confs, key=lambda c: (c != 'Wales', c)):
        members = confs[conf]
        cur_sorted  = sorted(members, key=lambda n: (-(rec(n).get('pts') or 0), -wins(n), n))
        proj_sorted = sorted(members, key=lambda n: -proj[n])
        proj_rank   = {n: i + 1 for i, n in enumerate(proj_sorted)}
        lines.append(f'  // {conf} Conference')
        for i, n in enumerate(cur_sorted, 1):
            r = rec(n)
            gp  = r.get('gp') or 0
            pts = r.get('pts') or 0
            otl = (r.get('otl') or 0) + (r.get('sol') or 0)
            pct = round(pts / (gp * 2), 3) if gp else 0
            lines.append(
                f"  {json.dumps(n)}: {{ conf:'{conf[0]}', div:{json.dumps(teams[n].get('division', ''))}, "
                f"cur:{i}, pts:{pts}, gp:{gp}, w:{wins(n)}, l:{r.get('l') or 0}, otl:{otl}, "
                f"pct:{pct}, rem:{len(rem[n])}, proj:{round(proj[n])}, projPos:{proj_rank[n]} }},"
            )
    lines.append('};')
    return '\n'.join(lines)


def update_index(new_block: str) -> bool:
    text = INDEX_FILE.read_text(encoding='utf-8')
    pattern = re.compile(
        r'//\s*──+\s*League standings.*?^const STANDINGS\s*=\s*\{.*?^\};',
        re.DOTALL | re.MULTILINE,
    )
    if not pattern.search(text):
        print('ERROR: STANDINGS block not found in index.html', file=sys.stderr)
        sys.exit(1)
    new_text = pattern.sub(lambda _: new_block, text, count=1)
    if new_text == text:
        print('STANDINGS unchanged — nothing to write.')
        return False
    INDEX_FILE.write_text(new_text, encoding='utf-8')
    print('✓ index.html updated')
    return True


def main() -> None:
    for f in (INDEX_FILE, DATA_FILE):
        if not f.exists():
            print(f'ERROR: {f} not found — run from repo root', file=sys.stderr)
            sys.exit(1)

    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    team_ov = load_team_ov()

    print('Fetching league + schedule from rwha.net…', flush=True)
    league   = fetch_json('/auth/league.php')
    schedule = fetch_json('/auth/schedule.php')
    if len(league.get('teams') or []) < 20:
        print('ERROR: league.php returned too few teams — aborting', file=sys.stderr)
        sys.exit(1)

    teams, rem, proj, ot_rate = build(league, schedule, team_ov)
    print(f'  {len(schedule.get("games") or [])} games in schedule, OT rate {ot_rate:.3f}')
    for n in sorted(teams, key=lambda n: -proj[n]):
        r = teams[n]['record']
        print(f'  {n:12s} {teams[n]["conference"]:9s} {r.get("pts", 0):3d} pts → proj {round(proj[n])}')

    changed = update_index(format_block(teams, rem, proj, ot_rate, today))

    github_env = os.environ.get('GITHUB_ENV')
    if github_env:
        with open(github_env, 'a') as f:
            f.write(f'STANDINGS_CHANGED={"true" if changed else "false"}\n')
            f.write(f'STANDINGS_DATE={today}\n')


if __name__ == '__main__':
    main()
