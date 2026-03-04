#!/usr/bin/env python3
"""
update_standings.py
───────────────────
Auto-update the STANDINGS constant in index.html with projected RWHA standings.

Projection model
  - Scrapes http://www.rwha.net/Schedule.php for W-L-OTL records + remaining opponents
  - Reads team Pro OV ratings from data.js (window.RWHA_DATA)
  - Win probability: logistic function on OV differential (k = 0.20)
  - OT rate calibrated from actual played games this season
  - Expected pts per game = 2·p(win) + ot_rate·(1−p(win))

Run from the repository root (done automatically by GitHub Actions).
Uses only Python stdlib — no pip installs required.
rwha.net has an expired SSL cert; we disable verification intentionally.
"""

import json
import math
import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Files (relative to repo root) ─────────────────────────────────────────────
INDEX_FILE = Path('index.html')
DATA_FILE  = Path('data.js')

# ── Conference assignments ─────────────────────────────────────────────────────
WALES_TEAMS = {
    'Gladiators', 'Warheads', 'Bunnies', 'Fletushkas', 'Clan',
    'Flyers', 'Jets', 'Giants', 'Mongoloids', 'Riots', 'Aces',
}
CAMPBELL_TEAMS = {
    'Oilers', 'Shitbirds', 'Phantoms', 'Meltdown', 'Steamers',
    'Mariners', 'Snowdogs', 'Marauders', 'WaffleBots', 'Cunts', 'Chiefs',
}
ALL_TEAMS = WALES_TEAMS | CAMPBELL_TEAMS

# ── Model hyperparameter ───────────────────────────────────────────────────────
# k=0.20 → a 3 OV advantage gives ~60% win probability
K = 0.20

# ── SSL context (rwha.net has an expired cert — bypass intentionally) ──────────
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ── Helpers ───────────────────────────────────────────────────────────────────
def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
        return r.read().decode('utf-8', errors='replace')


def load_team_ov() -> dict:
    """Extract Pro OV (po) per team from data.js (window.RWHA_DATA = {...})."""
    raw = DATA_FILE.read_text(encoding='utf-8')
    json_str = raw.split('=', 1)[1].strip().rstrip(';').strip()
    data = json.loads(json_str)
    ov = {}
    for team, d in data.items():
        if team in ALL_TEAMS:
            ov[team] = int(d.get('po', 78))
    return ov


def parse_schedule(html: str):
    """Parse Schedule.php HTML into played / unplayed game lists."""
    row_re = re.compile(
        r'<tr><td>(\d+)[^<]*</td><td>(\d+)</td>'
        r'<td[^>]*>.*?href="[^"]*">([^<]+)</a></td>'
        r'<td>([^<]*)</td>'
        r'<td[^>]*>.*?href="[^"]*">([^<]+)</a></td>'
        r'<td>([^<]*)</td>'
        r'(.*?)(?=</tr>)',
        re.DOTALL,
    )
    played, unplayed = [], []
    for m in row_re.finditer(html):
        _, _, vis, vs, home, hs, rest = m.groups()
        vis = vis.strip(); home = home.strip()
        vs  = vs.strip();  hs   = hs.strip()
        if vs == '-' or hs == '-':
            unplayed.append({'vis': vis, 'home': home})
        else:
            try:
                vs_i, hs_i = int(vs), int(hs)
            except ValueError:
                continue
            ot = bool(rest and 'X' in rest)
            played.append({'vis': vis, 'home': home, 'vs': vs_i, 'hs': hs_i, 'ot': ot})
    return played, unplayed


def build_standings(played: list, unplayed: list, team_ov: dict):
    """
    Derive current W-L-OTL records and project final-season points.

    Returns:
        record    – {team: {w, l, otl, pts, gp}}
        rem_games – {team: [opponent, ...]}
        proj_pts  – {team: float}
        ot_rate   – float
    """
    record = {t: {'w': 0, 'l': 0, 'otl': 0, 'pts': 0, 'gp': 0} for t in ALL_TEAMS}
    ot_count = 0

    for g in played:
        v, h = g['vis'], g['home']
        if v not in record or h not in record:
            continue
        ot = g['ot']
        if ot:
            ot_count += 1
        record[v]['gp'] += 1
        record[h]['gp'] += 1
        if g['hs'] > g['vs']:          # home win
            record[h]['w']   += 1;  record[h]['pts'] += 2
            if ot: record[v]['otl'] += 1; record[v]['pts'] += 1
            else:  record[v]['l']   += 1
        else:                           # visitor win
            record[v]['w']   += 1;  record[v]['pts'] += 2
            if ot: record[h]['otl'] += 1; record[h]['pts'] += 1
            else:  record[h]['l']   += 1

    ot_rate = ot_count / len(played) if played else 0.184

    # Build remaining-opponent lists
    rem_games = {t: [] for t in ALL_TEAMS}
    for g in unplayed:
        v, h = g['vis'], g['home']
        if v in rem_games: rem_games[v].append(h)
        if h in rem_games: rem_games[h].append(v)

    # Project final points
    proj_pts = {}
    for t in ALL_TEAMS:
        ov_t = team_ov.get(t, 78)
        add = 0.0
        for opp in rem_games[t]:
            ov_opp = team_ov.get(opp, 78)
            p = 1.0 / (1.0 + math.exp(-K * (ov_t - ov_opp)))
            add += 2 * p + ot_rate * (1 - p)
        proj_pts[t] = record[t]['pts'] + add

    return record, rem_games, proj_pts, ot_rate


def conf_ranks(teams: set, key_fn) -> dict:
    return {t: i + 1 for i, t in enumerate(sorted(teams, key=key_fn))}


def format_standings_js(record: dict, rem_games: dict, proj_pts: dict,
                         ot_rate: float, today: str) -> str:
    """Render the STANDINGS JS block that replaces the one in index.html."""
    # Current ranks: primary sort = pts desc, secondary = wins desc (tiebreaker)
    cur_rank_W  = conf_ranks(WALES_TEAMS,    lambda t: (-record[t]['pts'], -record[t]['w']))
    cur_rank_C  = conf_ranks(CAMPBELL_TEAMS, lambda t: (-record[t]['pts'], -record[t]['w']))
    # Projected ranks: sort by projected pts desc
    proj_rank_W = conf_ranks(WALES_TEAMS,    lambda t: -proj_pts[t])
    proj_rank_C = conf_ranks(CAMPBELL_TEAMS, lambda t: -proj_pts[t])

    wales_sorted    = sorted(WALES_TEAMS,    key=lambda t: cur_rank_W[t])
    campbell_sorted = sorted(CAMPBELL_TEAMS, key=lambda t: cur_rank_C[t])

    gp_played = sum(r['gp'] for r in record.values()) // len(record) if record else 0

    lines = [
        f'// ── League standings (scraped {today}, ~{gp_played} GP played, 82 GP season) ──────',
        '// Projection model: remaining schedule × team OV win-probability (logistic, k=0.20)',
        f'// OT rate this season: {ot_rate:.3f}',
        '// cur = current conf rank, pts = current points, pct = points pct,',
        '// rem = games remaining, proj = projected final pts, projPos = projected conf rank',
        'const STANDINGS = {',
        '  // Wales Conference  (cur = current rank by pts; projPos = projected rank by model)',
    ]

    def row(t: str, conf: str) -> str:
        r = record[t]
        pct = round(r['pts'] / (r['gp'] * 2), 3) if r['gp'] else 0
        cur_r  = cur_rank_W[t]  if conf == 'W' else cur_rank_C[t]
        proj_r = proj_rank_W[t] if conf == 'W' else proj_rank_C[t]
        return (
            f"  '{t}': {{ conf:'{conf}', cur:{cur_r},  pts:{r['pts']}, "
            f"gp:{r['gp']}, pct:{pct}, rem:{len(rem_games[t])}, "
            f"proj:{round(proj_pts[t])}, projPos:{proj_r}  }},"
        )

    for t in wales_sorted:
        lines.append(row(t, 'W'))
    lines.append('  // Campbell Conference')
    for t in campbell_sorted:
        lines.append(row(t, 'C'))
    lines.append('};')
    return '\n'.join(lines)


def update_index(new_block: str) -> bool:
    """Replace the STANDINGS block in index.html. Returns True if changed."""
    text = INDEX_FILE.read_text(encoding='utf-8')
    pattern = re.compile(
        r'//\s*──+\s*League standings.*?^const STANDINGS\s*=\s*\{.*?^\};',
        re.DOTALL | re.MULTILINE,
    )
    if not pattern.search(text):
        print('ERROR: STANDINGS block not found in index.html', file=sys.stderr)
        sys.exit(1)
    new_text = pattern.sub(new_block, text, count=1)
    if new_text == text:
        print('STANDINGS unchanged — nothing to write.')
        return False
    INDEX_FILE.write_text(new_text, encoding='utf-8')
    print('✓ index.html updated')
    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    for f in (INDEX_FILE, DATA_FILE):
        if not f.exists():
            print(f'ERROR: {f} not found — run from repo root', file=sys.stderr)
            sys.exit(1)

    today = datetime.utcnow().strftime('%Y-%m-%d')

    print('Loading team OV ratings from data.js…', flush=True)
    team_ov = load_team_ov()
    for t, ov in sorted(team_ov.items()):
        print(f'  {t}: OV {ov}', flush=True)

    print('\nFetching schedule from rwha.net…', flush=True)
    html = fetch('http://www.rwha.net/Schedule.php')
    print(f'  {len(html):,} bytes received', flush=True)

    played, unplayed = parse_schedule(html)
    print(f'  Played: {len(played)},  Unplayed: {len(unplayed)}', flush=True)

    record, rem_games, proj_pts, ot_rate = build_standings(played, unplayed, team_ov)
    print(f'  OT rate: {ot_rate:.3f}  ({int(ot_rate * len(played))}/{len(played)} games)', flush=True)

    print('\nProjected standings:', flush=True)
    for t in sorted(ALL_TEAMS, key=lambda t: -proj_pts[t]):
        r = record[t]
        print(f'  {t:15s}  {r["w"]}-{r["l"]}-{r["otl"]}  {r["pts"]}pts → proj {round(proj_pts[t])}', flush=True)

    new_block = format_standings_js(record, rem_games, proj_pts, ot_rate, today)
    changed = update_index(new_block)

    # Export vars for the commit step
    github_env = os.environ.get('GITHUB_ENV')
    if github_env:
        with open(github_env, 'a') as f:
            f.write(f'STANDINGS_CHANGED={"true" if changed else "false"}\n')
            f.write(f'STANDINGS_DATE={today}\n')

    sys.exit(0)


if __name__ == '__main__':
    main()
