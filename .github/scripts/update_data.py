#!/usr/bin/env python3
"""
update_data.py
──────────────
Scrape pro and farm rosters from rwha.net and rebuild data.js.

Run from the repository root (done automatically by GitHub Actions).
Requires: pip install beautifulsoup4
rwha.net has an expired SSL cert; we disable verification intentionally.
"""

import json
import os
import re
import ssl
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
URL       = 'http://www.rwha.net/RWHA-ProTeamRoster.php'
DATA_FILE = Path('data.js')

# Real hockey player link domains — any player whose last-column link matches
# one of these is treated as a real player.  Fictional characters link to
# youtube, wikipedia, urbandictionary, imdb, etc. and will be excluded
# automatically even if their names change.
#
# The name list below is a fallback for edge cases where a fictional player
# happens to share a domain with real players (e.g. hockeydb.com).
REAL_DOMAINS = (
    'capfriendly.com/players/',
    'nhl.com/player/',
    'theahl.com/stats/player/',
    'eliteprospects.com/player/',
    'hockeydb.com/ihdb/',
)

# Fictional players who use a domain that also appears for real players.
# Update this list if new edge-case fictional players are added to the league.
FICTIONAL_NAMES = {
    'Lee Mack', 'Nipples Tenderloin', 'Danny Massawhip', 'Chu Kock',
    'Rick Spreadum', 'Manly Rymjob', 'Shitty-Kitty Gangbang',
    'El Burrito Peligroso', 'Cockring Bomber', 'Wrinkles Cumbersnatch',
    'Mulvinder Bitchtits', 'Ricky Cumalot', 'Hugo Drax', 'Wee Kawk',
    'Manson Gluehead', 'Todd Harkness', 'Velyki Hospador',
    'Buck Phucksalot', 'Moxie Manslammer', 'Douche Larouche',
    'Raccoon Willie', 'Brock Knuckledunker',
}

# ── Fetch ───────────────────────────────────────────────────────────────────────
def fetch():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return r.read().decode('utf-8', errors='replace')

# ── Helpers ─────────────────────────────────────────────────────────────────────
def is_real_player(tr):
    """Return True if this row represents a real hockey player.

    Detection uses two signals:
    1. Name-based: if the player's name is in FICTIONAL_NAMES → excluded.
    2. Link-based: the last <td> must contain a link to a known hockey site.
       Fictional characters link to youtube, wikipedia, urbandictionary, etc.
       and are excluded automatically even if their names change.
    """
    tds = tr.find_all('td')
    if not tds:
        return False
    a = tds[-1].find('a')
    href = a.get('href', '') if a else ''
    # Name check (handles shared-domain edge cases like hockeydb)
    # Skater name is tds[1]; goalie name is tds[0] — check both
    for idx in (0, 1):
        if idx < len(tds):
            nm = re.sub(r'\s*\([RCA]\)', '', tds[idx].get_text(strip=True)).strip()
            if nm in FICTIONAL_NAMES:
                return False
    # Link check: known hockey site → include; empty link → include (assume
    # real player with no profile set up); any other domain → exclude
    if not href:
        return True
    return any(d in href for d in REAL_DOMAINS)

def clean_con(val):
    """'100.00' → '100'  (conditioning is stored as an integer string)"""
    try:
        return str(int(float(val)))
    except (ValueError, TypeError):
        return val or ''

def get_cells(tr):
    return [td.get_text(strip=True) for td in tr.find_all('td')]

def parse_position(vals):
    """Build p string from C/L/R/D columns (indices 2–5)."""
    labels = ['C', 'L', 'R', 'D']
    parts = [labels[i] for i, v in enumerate(vals[2:6]) if v == 'X']
    return '/'.join(parts)

# ── Row parsers ─────────────────────────────────────────────────────────────────
# Skater columns: #, Player Name, C, L, R, D, CON, IJ, CK, FG, DI, SK, ST, EN,
#                 DU, PH, FO, PA, SC, DF, PS, EX, LD, PO, MO, OV, TA, SP, Age,
#                 Contract, Salary, Link
def parse_skater(vals):
    if len(vals) < 30:
        return None
    nm = vals[1]
    if not nm:
        return None
    return {
        'n':   vals[0],
        'nm':  nm,
        'p':   parse_position(vals),
        'con': clean_con(vals[6]),
        'ij':  vals[7],
        'ck':  vals[8],
        'fg':  vals[9],
        'di':  vals[10],
        'sk':  vals[11],
        'st':  vals[12],
        'en':  vals[13],
        'du':  vals[14],
        'ph':  vals[15],
        'fo':  vals[16],
        'pa':  vals[17],
        'sc':  vals[18],
        'df':  vals[19],
        'ps':  vals[20],
        'ex':  vals[21],
        'ld':  vals[22],
        'po':  vals[23],
        'mo':  vals[24],
        'ov':  vals[25],
        'ta':  vals[26],
        'sp':  vals[27],
        'age': vals[28],
        'c':   vals[29],
        'sal': vals[30] if len(vals) > 30 else '',
    }

# Goalie columns: Goalie Name, PO(=G), CON, IJ, SK, DU, EN, SZ, AG, RB, SC,
#                 HS, RT, PH, PS, EX, LD, PO(poise), MO, OV, TA, SP, Age,
#                 Contract, Salary, Link
def parse_goalie(vals):
    if len(vals) < 24:
        return None
    nm = vals[0]
    if not nm:
        return None
    return {
        'nm':  nm,
        'p':   'G',
        'con': clean_con(vals[2]),
        'ij':  vals[3],
        'sk':  vals[4],
        'du':  vals[5],
        'en':  vals[6],
        'sz':  vals[7],
        'ag':  vals[8],
        'rb':  vals[9],
        'sc':  vals[10],
        'hs':  vals[11],
        'rt':  vals[12],
        'ph':  vals[13],
        'ps':  vals[14],
        'ex':  vals[15],
        'ld':  vals[16],
        'po':  vals[17],
        'mo':  vals[18],
        'ov':  vals[19],
        'ta':  vals[20],
        'sp':  vals[21],
        'age': vals[22],
        'c':   vals[23],
        'sal': vals[24] if len(vals) > 24 else '',
    }

# ── Roster div parser ───────────────────────────────────────────────────────────
def parse_roster_div(div):
    """
    Given a roster div (pro or farm), return:
      gm, morale, overall, skaters[], goalies[]
    The div contains 3 tables: info, skaters, goalies.
    """
    tables = div.find_all('table', recursive=False)
    if not tables:
        tables = div.find_all('table')
    if len(tables) < 3:
        return '', '', '', [], []

    # Info table
    info_text = tables[0].get_text()
    gm_m  = re.search(r'General Manager\s*:\s*(.+?)(?:Coach\s*:|$)', info_text, re.DOTALL)
    gm    = re.sub(r'\s+', ' ', gm_m.group(1)).strip() if gm_m else ''
    mo_m  = re.search(r'Morale\s*:\s*(\d+)', info_text)
    morale  = mo_m.group(1) if mo_m else ''
    ov_m  = re.search(r'Team Overall\s*:\s*(\d+)', info_text)
    overall = ov_m.group(1) if ov_m else ''

    # Skater table — skip header rows and fictional players (no capfriendly link)
    skaters = []
    for tr in tables[1].find_all('tr'):
        if not is_real_player(tr):
            continue
        p = parse_skater(get_cells(tr))
        if p:
            skaters.append(p)

    # Goalie table
    goalies = []
    for tr in tables[2].find_all('tr'):
        if not is_real_player(tr):
            continue
        p = parse_goalie(get_cells(tr))
        if p:
            goalies.append(p)

    return gm, morale, overall, skaters, goalies

# ── Main parser ─────────────────────────────────────────────────────────────────
def parse_all(html):
    soup = BeautifulSoup(html, 'html.parser')
    data = {}

    for h1 in soup.find_all('h1'):
        team_name = h1.get_text(strip=True)
        if not team_name:
            continue

        # Walk siblings until the next h1, collecting the two roster divs
        # and the farm name from the h2.TeamRoster_FarmRoster element.
        pro_div   = None
        farm_div  = None
        farm_name = ''

        el = h1.next_sibling
        while el:
            tag = getattr(el, 'name', None)
            if tag == 'h1':
                break
            if tag == 'div':
                classes = el.get('class') or []
                if 'STHSTeamLink' not in classes:
                    # Unnamed div — first is pro roster, second is farm roster
                    if pro_div is None:
                        pro_div = el
                    elif farm_div is None:
                        farm_div = el
            elif tag == 'h2':
                classes = el.get('class') or []
                if 'TeamRoster_FarmRoster' in classes:
                    farm_name = el.get_text(strip=True)\
                                  .replace('Farm Roster - ', '').strip()
            el = el.next_sibling

        if pro_div is None:
            print(f'  WARNING: no pro roster div found for {team_name}', file=sys.stderr)
            continue

        gm, pro_mo, pro_ov, pro_skaters, pro_goalies = parse_roster_div(pro_div)

        farm_mo = farm_ov = ''
        farm_skaters = farm_goalies = []
        if farm_div:
            _, farm_mo, farm_ov, farm_skaters, farm_goalies = parse_roster_div(farm_div)

        data[team_name] = {
            'n':  team_name,
            'gm': gm,
            'fn': farm_name,
            'pm': pro_mo,
            'po': pro_ov,
            'fm': farm_mo,
            'fo': farm_ov,
            'ps': pro_skaters,
            'pg': pro_goalies,
            'fs': farm_skaters,
            'fg': farm_goalies,
        }

    return data

# ── Entry point ─────────────────────────────────────────────────────────────────
def main():
    print('Fetching roster page…', file=sys.stderr)
    html = fetch()

    print('Parsing rosters…', file=sys.stderr)
    data = parse_all(html)

    teams   = len(data)
    players = sum(
        len(v['ps']) + len(v['pg']) + len(v['fs']) + len(v['fg'])
        for v in data.values()
    )
    print(f'Parsed {teams} teams, {players} players', file=sys.stderr)

    if teams == 0:
        print('ERROR: no teams parsed — aborting', file=sys.stderr)
        sys.exit(1)

    js = 'window.RWHA_DATA = ' + json.dumps(data, ensure_ascii=False, indent=2) + ';\n'
    DATA_FILE.write_text(js, encoding='utf-8')
    print(f'Wrote {DATA_FILE} ({DATA_FILE.stat().st_size:,} bytes)', file=sys.stderr)

    date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    env_file = os.environ.get('GITHUB_ENV', '')
    if env_file:
        with open(env_file, 'a') as f:
            f.write(f'DATA_TEAMS={teams}\n')
            f.write(f'DATA_PLAYERS={players}\n')
            f.write(f'DATA_DATE={date_str}\n')

if __name__ == '__main__':
    main()
