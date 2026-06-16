"""
Daily GitHub push for Metro Atlanta weather pipeline diagnostics.

Regenerates scraper-update.png, writes a daily pipeline stats report
(overwriting the previous one), then commits and pushes both files to GitHub.

Usage:
    python weather_collector/github_update.py          # single run
    python weather_collector/github_update.py --loop   # daily at LOOP_HOUR

Files pushed (inside metadata/diagnostic_repo/):
    scraper-update.png    — diagnostic map (copied from metadata/ after regeneration)
    pipeline_stats.txt    — daily stats report (overwritten each run)

Stats report includes:
    - Cumulative and daily-new extinct station counts by source
    - Average years of data per station by source (WU, AWN, GDOT, UGA)

Snapshot file (not pushed):
    metadata/github_stats_snapshot.json — stores yesterday's extinct counts so
    today's "new extinct" delta can be computed. Auto-created on first run.

Git setup:
    The git repo is assumed to be rooted at REPO_ROOT (see configuration below).
    Ensure 'git push' works without a password prompt (SSH key or credential
    helper) before running in loop mode.
"""

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta

# ── Path config (auto-derived from script location) ──────────────────────────
EXTERNAL_DRIVE  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METADATA_DIR    = os.path.join(EXTERNAL_DRIVE, 'metadata')
GENERATE_SCRIPT = os.path.join(EXTERNAL_DRIVE, 'weather_collector', 'generate_update.py')

METADATA_FILES = {
    'WU':   os.path.join(METADATA_DIR, 'station_metadata_wu.csv'),
    'AWN':  os.path.join(METADATA_DIR, 'station_metadata_awn.csv'),
    'GDOT': os.path.join(METADATA_DIR, 'station_metadata_gdot.csv'),
    'UGA':  os.path.join(METADATA_DIR, 'station_metadata_uga.csv'),
}

SNAPSHOT_FILE = os.path.join(METADATA_DIR, 'github_stats_snapshot.json')

# ── Font config ───────────────────────────────────────────────────────────────
# DINPro fonts live in metadata/ and need to be present in the repo for GitHub Pages.
# copy_fonts() mirrors them into diagnostic_repo/fonts/ on first run (no-op after).
FONT_NAMES     = ['DINPro-Bold.otf', 'DINPro-Medium.otf']
COUNTIES_SRC   = os.path.join(METADATA_DIR, 'metro_atlanta_counties.geojson')

# ── Git configuration ─────────────────────────────────────────────────────────
# REPO_ROOT is where .git lives — the cloned/initialized diagnostic_repo.
# OUTPUT_PNG and STATS_FILE point inside the repo so git can track them.
# generate_update.py writes the PNG to metadata/ first; we copy it in run_once().
REPO_ROOT    = os.path.join(METADATA_DIR, 'diagnostic_repo')
OUTPUT_PNG   = os.path.join(REPO_ROOT, 'scraper-update.png')
STATS_FILE   = os.path.join(REPO_ROOT, 'pipeline_stats.txt')
GEOJSON_FILE   = os.path.join(REPO_ROOT, 'stations.geojson')
INDEX_FILE     = os.path.join(REPO_ROOT, 'index.html')
REPO_FONTS_DIR = os.path.join(REPO_ROOT, 'fonts')
COUNTIES_DST   = os.path.join(REPO_ROOT, 'metro_atlanta_counties.geojson')

# ── Schedule ──────────────────────────────────────────────────────────────────
LOOP_HOUR = 7   # hour (24h local time) to fire the daily push

# ── Thresholds ────────────────────────────────────────────────────────────────
EXTINCT_DAYS = 30   # days without new data → station considered extinct


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date(val):
    """Parse M/D/YY or YYYY-MM-DD date strings. Returns datetime or None."""
    if not val or str(val).strip() == '':
        return None
    for fmt in ('%m/%d/%y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            return datetime.strptime(str(val).strip(), fmt)
        except ValueError:
            pass
    return None


def load_metadata(path):
    """Load a metadata CSV as a list of row dicts. Returns [] if not found."""
    if not os.path.exists(path):
        return []
    with open(path, newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def load_snapshot():
    """Load yesterday's stats snapshot. Returns {} if none exists yet."""
    if os.path.exists(SNAPSHOT_FILE):
        try:
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_snapshot(data):
    """Persist the current stats snapshot for tomorrow's delta calculation."""
    with open(SNAPSHOT_FILE, 'w') as f:
        json.dump(data, f, indent=2)


# ── Font mirror ───────────────────────────────────────────────────────────────

def copy_fonts():
    """
    Copy DINPro .otf fonts from metadata/ into diagnostic_repo/fonts/ so that
    GitHub Pages can serve them alongside index.html.  No-op once present.
    """
    os.makedirs(REPO_FONTS_DIR, exist_ok=True)
    for name in FONT_NAMES:
        src = os.path.join(METADATA_DIR, name)
        dst = os.path.join(REPO_FONTS_DIR, name)
        if not os.path.exists(dst):
            if os.path.exists(src):
                shutil.copy2(src, dst)
                print(f'  Font copied → {dst}')
            else:
                print(f'  Warning: font not found at {src}')
    if not os.path.exists(COUNTIES_DST):
        if os.path.exists(COUNTIES_SRC):
            shutil.copy2(COUNTIES_SRC, COUNTIES_DST)
            print(f'  Counties GeoJSON copied → {COUNTIES_DST}')
        else:
            print(f'  Warning: counties GeoJSON not found at {COUNTIES_SRC}')


# ── GeoJSON builder ───────────────────────────────────────────────────────────

def _fmt_date(dt):
    """Return YYYY-MM-DD string or '' if dt is None."""
    return dt.strftime('%Y-%m-%d') if dt else ''


def build_stations_geojson():
    """
    Merge all four metadata CSVs into a GeoJSON FeatureCollection.

    Each station becomes a Point feature with properties:
        source, station_id, name, earliest_date, latest_date,
        total_days, total_observations, years_active,
        days_since_update, extinct

    Stations missing lat/lon are skipped.  mac_address is intentionally
    excluded.  Dates are normalised to YYYY-MM-DD.
    """
    today    = datetime.now().date()
    features = []

    for source, path in METADATA_FILES.items():
        for r in load_metadata(path):
            lat_s = str(r.get('latitude',  '')).strip()
            lon_s = str(r.get('longitude', '')).strip()
            if not lat_s or not lon_s:
                continue
            try:
                lat, lon = float(lat_s), float(lon_s)
            except ValueError:
                continue

            total_days = int(float(r.get('total_days',         0) or 0))
            total_obs  = int(float(r.get('total_observations', 0) or 0))
            if total_obs == 0:
                continue   # skip stations with no scraped data (matches generate_update.py)
            years_active = round(total_days / 365.25, 2)

            latest_dt  = parse_date(r.get('latest_date',   ''))
            earliest_dt = parse_date(r.get('earliest_date', ''))
            days_since  = (today - latest_dt.date()).days if latest_dt else None

            extinct = str(r.get('extinct', '')).strip().lower() == 'true'

            features.append({
                'type': 'Feature',
                'geometry': {
                    'type': 'Point',
                    'coordinates': [lon, lat],
                },
                'properties': {
                    'source':             source,
                    'station_id':         r.get('station_id', ''),
                    'name':               r.get('name', ''),
                    'earliest_date':      _fmt_date(earliest_dt),
                    'latest_date':        _fmt_date(latest_dt),
                    'total_days':         total_days,
                    'total_observations': total_obs,
                    'years_active':       years_active,
                    'days_since_update':  days_since,
                    'extinct':            extinct,
                },
            })

    return {'type': 'FeatureCollection', 'features': features}


# ── Stats computation ─────────────────────────────────────────────────────────

# Real-time sources use latest_date as a health signal; historical sources do not.
# WU and AWN latest_date reflects historical coverage end, not a heartbeat —
# only the explicit extinct flag (set by the scraper itself) counts for them.
REALTIME_SOURCES = {'GDOT', 'UGA'}


def compute_stats():
    """
    Read all four metadata files and compute per-source stats.

    Returns a dict keyed by source ('WU', 'AWN', 'GDOT', 'UGA'), each with:
        total         — stations that have at least one observation
        extinct_count — stations with no data for EXTINCT_DAYS+ days
        active        — total - extinct_count
        avg_years     — mean years of data per station (active + extinct)
    """
    today  = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff = today - timedelta(days=EXTINCT_DAYS)
    results = {}

    for source, path in METADATA_FILES.items():
        rows     = load_metadata(path)
        has_data = [r for r in rows if int(r.get('total_observations', 0) or 0) > 0]

        extinct_count  = 0
        total_days_acc = []

        for r in has_data:
            # Always respect the explicit flag set by the scraper.
            is_extinct = str(r.get('extinct', '')).strip().lower() == 'true'
            # For real-time sources only, also flag stations whose latest_date
            # has gone stale — historical scrapers' latest_date is their coverage
            # endpoint, not a heartbeat, so the recency check doesn't apply.
            if not is_extinct and source in REALTIME_SOURCES:
                latest = parse_date(r.get('latest_date', ''))
                if latest and latest < cutoff:
                    is_extinct = True
            if is_extinct:
                extinct_count += 1

            try:
                d = float(r.get('total_days', 0) or 0)
                if d > 0:
                    total_days_acc.append(d)
            except (ValueError, TypeError):
                pass

        avg_years = (
            sum(total_days_acc) / len(total_days_acc) / 365.25
            if total_days_acc else 0.0
        )

        results[source] = {
            'total':         len(has_data),
            'extinct_count': extinct_count,
            'active':        len(has_data) - extinct_count,
            'avg_years':     avg_years,
        }

    return results


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(stats, snapshot):
    """
    Build the plain-text daily stats report.

    Sections:
        1. Extinct station counts (cumulative + today's delta by source)
        2. Average years of data per station by source
    """
    now          = datetime.now()
    prev_extinct = snapshot.get('extinct_by_source', {})
    has_baseline = bool(prev_extinct)

    total_extinct = sum(s['extinct_count'] for s in stats.values())
    total_active  = sum(s['active']        for s in stats.values())
    total_all     = sum(s['total']         for s in stats.values())
    prev_total    = sum(prev_extinct.values()) if has_baseline else None
    daily_new     = (total_extinct - prev_total) if has_baseline else None

    W = 60
    lines = []
    lines.append('=' * W)
    lines.append('METRO ATLANTA WEATHER PIPELINE — DAILY STATS')
    lines.append(f'Generated: {now.strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('=' * W)
    lines.append('')

    # ── Section 1: Extinct stations ───────────────────────────────────────────
    lines.append(f'EXTINCT STATIONS  (>={EXTINCT_DAYS} consecutive days without new data)')
    lines.append('-' * W)
    lines.append(f'  {"Cumulative total extinct:":<28} {total_extinct:>7,}  /  {total_all:,} stations')

    if has_baseline:
        sign = '+' if daily_new >= 0 else ''
        lines.append(f'  {"New extinct today:":<28} {sign}{daily_new:>6,}')
    else:
        lines.append(f'  {"New extinct today:":<28}      N/A  (first run — no prior snapshot)')

    lines.append(f'  {"Currently active:":<28} {total_active:>7,}')
    lines.append('')
    lines.append(f'  {"Source":<8}  {"Extinct":>7}  {"Total":>7}  {"New today":>10}')
    lines.append(f'  {"-"*8}  {"-"*7}  {"-"*7}  {"-"*10}')

    for source in ('WU', 'AWN', 'GDOT', 'UGA'):
        s     = stats.get(source, {})
        ext   = s.get('extinct_count', 0)
        total = s.get('total', 0)
        if has_baseline and source in prev_extinct:
            delta   = ext - prev_extinct[source]
            new_str = f'+{delta:,}' if delta >= 0 else f'{delta:,}'
        else:
            new_str = 'N/A'
        lines.append(f'  {source:<8}  {ext:>7,}  {total:>7,}  {new_str:>10}')

    lines.append('')

    # ── Section 2: Average years per station ──────────────────────────────────
    lines.append('AVERAGE YEARS OF DATA PER STATION')
    lines.append('-' * W)
    for source in ('WU', 'AWN', 'GDOT', 'UGA'):
        s      = stats.get(source, {})
        avg    = s.get('avg_years', 0.0)
        active = s.get('active', 0)
        total  = s.get('total', 0)
        lines.append(
            f'  {source:<8}  {avg:>6.2f} yrs / station'
            f'   ({active:,} active / {total:,} total)'
        )

    lines.append('')
    lines.append('=' * W)
    return '\n'.join(lines)


# ── PNG regeneration ──────────────────────────────────────────────────────────

def regenerate_png():
    """Run generate_update.py as a subprocess. Returns True on success."""
    if not os.path.exists(GENERATE_SCRIPT):
        print(f'  Warning: {GENERATE_SCRIPT} not found — skipping PNG regeneration')
        return False
    print('  Regenerating scraper-update.png ...')
    try:
        result = subprocess.run(
            [sys.executable, GENERATE_SCRIPT],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            print('  PNG regenerated.')
            return True
        print(f'  PNG generation failed (exit {result.returncode}):')
        if result.stderr:
            print('  ' + result.stderr[-400:].strip())
        return False
    except Exception as e:
        print(f'  PNG generation error: {e}')
        return False


# ── Git push ──────────────────────────────────────────────────────────────────

def git_push():
    """
    Stage scraper-update.png and pipeline_stats.txt, commit, and push.
    Returns True if the push succeeded (or there was nothing new to push).
    """
    font_files = [os.path.join(REPO_FONTS_DIR, n) for n in FONT_NAMES]
    candidates = [OUTPUT_PNG, STATS_FILE, GEOJSON_FILE, INDEX_FILE, COUNTIES_DST] + font_files
    to_add = [f for f in candidates if os.path.exists(f)]
    if not to_add:
        print('  Nothing to add — skipping git push.')
        return False

    today_str  = datetime.now().strftime('%Y-%m-%d')
    commit_msg = f'Daily pipeline update {today_str}'

    # git add
    try:
        subprocess.run(
            ['git', 'add'] + to_add,
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f'  git add failed: {e.stderr.strip()}')
        return False

    # git commit
    result = subprocess.run(
        ['git', 'commit', '-m', commit_msg],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).lower()
        if 'nothing to commit' in combined or 'nothing added' in combined:
            print('  Nothing new to commit — files unchanged since last push.')
            return True
        print(f'  git commit failed: {result.stderr.strip()}')
        return False

    # git push — retry transient network failures (e.g. SSH connection resets)
    push_delays = [30, 120, 300]   # seconds between attempts; len+1 total tries
    for attempt in range(len(push_delays) + 1):
        result = subprocess.run(
            ['git', 'push'],
            cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f'  Pushed to GitHub: "{commit_msg}"')
            return True

        err = result.stderr.strip()
        if attempt < len(push_delays):
            wait = push_delays[attempt]
            print(f'  git push failed (attempt {attempt + 1}): {err}')
            print(f'  Retrying in {wait}s ...')
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                print('\n  Push interrupted.')
                return False
        else:
            print(f'  git push failed (final attempt {attempt + 1}): {err}')
            return False


# ── Main run ──────────────────────────────────────────────────────────────────

def run_once():
    label = datetime.now().strftime('[%a %H:%M]')
    print(f'\n{label}  Daily GitHub update starting')
    print('=' * 60)

    # 1a. Mirror fonts into the repo (one-time; no-op once files exist)
    copy_fonts()

    # 1b. Regenerate the diagnostic map PNG (generate_update.py writes to metadata/)
    regenerate_png()
    _png_source = os.path.join(METADATA_DIR, 'scraper-update.png')
    if os.path.exists(_png_source):
        shutil.copy2(_png_source, OUTPUT_PNG)
        print(f'  PNG copied → {OUTPUT_PNG}')
    else:
        print('  Warning: scraper-update.png not found in metadata/ — skipping copy')

    # 2. Compute stats, build the report, write it (overwrites previous)
    print('  Computing pipeline stats ...')
    stats    = compute_stats()
    snapshot = load_snapshot()
    report   = build_report(stats, snapshot)

    with open(STATS_FILE, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f'  Stats written → {STATS_FILE}')
    print()
    print(report)

    # 3a. Build and write stations.geojson for the web map
    print('  Building stations.geojson ...')
    geojson = build_stations_geojson()
    with open(GEOJSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(geojson, f, separators=(',', ':'))
    print(f'  GeoJSON written → {GEOJSON_FILE} ({len(geojson["features"]):,} stations)')

    # 3b. Save snapshot for tomorrow's delta calculation
    new_snapshot = {
        'date':              datetime.now().strftime('%Y-%m-%d'),
        'extinct_by_source': {src: stats[src]['extinct_count'] for src in stats},
    }
    save_snapshot(new_snapshot)

    # 4. Commit and push
    print('  Pushing to GitHub ...')
    git_push()
    print(f'  Done.\n{"=" * 60}')


# ── Loop mode ─────────────────────────────────────────────────────────────────

def seconds_until_next_fire():
    """Return seconds until the next LOOP_HOUR:00 local time."""
    now       = datetime.now()
    next_fire = now.replace(hour=LOOP_HOUR, minute=0, second=0, microsecond=0)
    if now >= next_fire:
        next_fire += timedelta(days=1)
    return (next_fire - now).total_seconds()


def run_loop():
    print(f'Loop mode — daily push fires at {LOOP_HOUR:02d}:00 local time.  '
          f'Press Ctrl+C to stop.\n')
    while True:
        wait      = seconds_until_next_fire()
        next_fire = datetime.fromtimestamp(time.time() + wait)
        _d = next_fire.strftime('%m/%d/%y').lstrip('0').replace('/0', '/')
        print(
            f'  Waiting... next push at {LOOP_HOUR:02d}:00 on '
            f'{next_fire.strftime("%A")}, {_d}    ',
            end='\r', flush=True,
        )
        time.sleep(wait)
        try:
            run_once()
        except KeyboardInterrupt:
            return
        except Exception as e:
            label = datetime.now().strftime('[%a %H:%M]')
            print(f'\n{label}  ERROR: {e}')


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if '--loop' in sys.argv:
        run_loop()
    else:
        run_once()
