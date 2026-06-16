"""
Weather Underground — Station Discovery
========================================

Discovers new WU stations across the 11-county metro Atlanta area by
driving Playwright across a grid of coordinates and extracting all
station IDs visible in WU's station-selector map at each grid point.

For each grid point the script navigates to the nearest known WU
station's weather page, clicks the "Change" button to open the map
selector, and extracts the embedded station JSON.  Because the map
shows all stations within the visible viewport (not just the anchor
station), a grid of ~120 points covers the full region and surfaces
any station regardless of city prefix — including stations from cities
not yet in the existing list.

New stations found are appended to station_metadata_wu.csv with
total_observations = 0 (pending scraping).  A dated discovery log is
also written alongside this script for auditing.

Requires: pip install playwright pandas && playwright install chromium
"""

import re
import os
import time
import random
import math
import pandas as pd
from datetime import datetime
from playwright.sync_api import sync_playwright

# ============================================================================
# CONFIGURATION
# ============================================================================

# ── Metadata file ─────────────────────────────────────────────────────────────
# Point this at station_metadata_wu.csv wherever it lives on this machine.
#
#   Mac / server (scripts-to-retain directory):
METADATA_FILE = os.path.join(os.path.dirname(__file__), 'station_metadata_wu.csv')
#
#   Windows laptop (uncomment and comment out the line above):
# METADATA_FILE = r"D:\Weather\metadata\station_metadata_wu.csv"

# ── Output files ──────────────────────────────────────────────────────────────
# Discovery log and checkpoint are always written alongside this script.
RUN_DATE        = datetime.now().strftime('%Y-%m-%d')
_SCRIPT_DIR     = os.path.dirname(__file__)
DISCOVERY_LOG   = os.path.join(_SCRIPT_DIR, f'discovered_wu_{RUN_DATE}.csv')
CHECKPOINT_FILE = os.path.join(_SCRIPT_DIR, 'discover_wu_checkpoint.csv')

# 11-county metro Atlanta bounding box
GRID_BOUNDS = {'south': 33.3, 'north': 34.5, 'west': -85.0, 'east': -83.8}

# Grid spacing in decimal degrees (~0.12° ≈ 8 miles).
# Tighten to 0.08 for a more exhaustive search (longer runtime).
GRID_STEP = 0.12

# Seconds to wait after clicking the Change button before reading HTML
MAP_LOAD_WAIT = 2.5

# Seconds between grid-point page navigations
NAV_DELAY = 2.0

# ============================================================================
# KNOWN PREFIX → WU URL CITY SEGMENT
# ============================================================================
# Used to construct /weather/us/ga/{city}/{station_id} URLs.
# Covers the ~70 prefixes where the city name is unambiguous.
# Unknown prefixes fall back to the coordinate-based URL.

PREFIX_TO_CITY = {
    'ACWOR': 'acworth',           'ALPHA': 'alpharetta',
    'ATLAN': 'atlanta',           'AUBUR': 'auburn',
    'AUSTE': 'austell',           'AVOND': 'avondale-estates',
    'BALLG': 'ball-ground',       'BERKE': 'berkeley-lake',
    'BRASE': 'braselton',         'BROOK': 'brookhaven',
    'BUFOR': 'buford',            'CANTO': 'canton',
    'CHAMB': 'chamblee',          'CHATT': 'chattahoochee-hills',
    'CHEST': 'chestnut-mountain', 'CLARK': 'clarkston',
    'CONLE': 'conley',            'CONYE': 'conyers',
    'CUMMI': 'cumming',           'DACUL': 'dacula',
    'DECAT': 'decatur',           'DORAV': 'doraville',
    'DOUGL': 'douglasville',      'DULUT': 'duluth',
    'DUNWO': 'dunwoody',          'EASTP': 'east-point',
    'ELLEN': 'ellenwood',         'FAIRB': 'fairburn',
    'FAYET': 'fayetteville',      'FORES': 'forest-park',
    'GAINE': 'gainesville',       'GRAYS': 'grayson',
    'HAMPT': 'hampton',           'HAPEV': 'hapeville',
    'HOLLY': 'holly-springs',     'HOSCH': 'hoschton',
    'JACKS': 'jackson',           'JOHNS': 'johns-creek',
    'JONES': 'jonesboro',         'KENNE': 'kennesaw',
    'LAWRE': 'lawrenceville',     'LILBU': 'lilburn',
    'LITHI': 'lithia-springs',    'LOCUS': 'locust-grove',
    'LOGAN': 'loganville',        'MABLE': 'mableton',
    'MARIE': 'marietta',          'MCDON': 'mcdonough',
    'MILTO': 'milton',            'NORCR': 'norcross',
    'PALME': 'palmetto',          'PEACH': 'peachtree-city',
    'POWDE': 'powder-springs',    'REX':   'rex',
    'ROSWE': 'roswell',           'SANDY': 'sandy-springs',
    'SCOTT': 'scottdale',         'SMYRN': 'smyrna',
    'SNELL': 'snellville',        'STOCK': 'stockbridge',
    'STONE': 'stone-mountain',    'SUGAR': 'sugar-hill',
    'SUWAN': 'suwanee',           'TUCKE': 'tucker',
    'TYRON': 'tyrone',            'VILLA': 'villa-rica',
    'WALES': 'waleska',           'WHITE': 'white',
    'WOODS': 'woodstock',
}

# ============================================================================
# STATION JSON EXTRACTION — reuses the proven regex from wu_latlong_finder.py
# ============================================================================

STATION_PATTERN = re.compile(
    r'"location":\{"stationName":\[([^\]]+)\],'
    r'"stationId":\[([^\]]+)\],'
    r'"qcStatus":\[[^\]]+\],'
    r'"updateTimeUtc":\[[^\]]+\],'
    r'"partnerId":\[[^\]]+\],'
    r'"latitude":\[([^\]]+)\],'
    r'"longitude":\[([^\]]+)\]'
)


def extract_stations_from_html(html):
    """Return list of {station_id, latitude, longitude} from WU map HTML."""
    stations = []
    for m in STATION_PATTERN.finditer(html):
        _, ids_str, lats_str, lons_str = m.groups()
        ids  = [s.strip().strip('"') for s in ids_str.split(',')]
        lats = [float(v.strip()) for v in lats_str.split(',')]
        lons = [float(v.strip()) for v in lons_str.split(',')]
        for sid, lat, lon in zip(ids, lats, lons):
            if sid:
                stations.append({'station_id': sid, 'latitude': lat, 'longitude': lon})
    return stations


# ============================================================================
# GRID GENERATION
# ============================================================================

def generate_grid(bounds, step):
    """Return list of (lat, lon) tuples covering bounds at step spacing."""
    points = []
    lat = bounds['south']
    while lat <= bounds['north']:
        lon = bounds['west']
        while lon <= bounds['east']:
            points.append((round(lat, 4), round(lon, 4)))
            lon += step
        lat += step
    return points


def nearest_station(lat, lon, stations_df):
    """Return the row from stations_df whose station is geographically closest."""
    valid = stations_df[stations_df['latitude'].notna() &
                        stations_df['longitude'].notna()].copy()
    if valid.empty:
        return None
    valid['_dist'] = valid.apply(
        lambda r: math.hypot(r['latitude'] - lat, r['longitude'] - lon), axis=1)
    return valid.loc[valid['_dist'].idxmin()]


def station_url(row):
    """
    Build a WU weather-page URL for a known station.
    Tries the /weather/us/ga/{city}/{id} pattern first;
    falls back to the coordinate-based URL.
    """
    sid = row['station_id']
    prefix = None
    m = re.match(r'^KGA([A-Z]+)\d+$', str(sid))
    if m:
        prefix = m.group(1)
    city = PREFIX_TO_CITY.get(prefix)
    if city:
        return f"https://www.wunderground.com/weather/us/ga/{city}/{sid}"
    # Coordinate fallback
    lat = row.get('latitude')
    lon = row.get('longitude')
    if lat and lon:
        return f"https://www.wunderground.com/weather/{lat:.4f},{lon:.4f}"
    return None


# ============================================================================
# MAP SCRAPING
# ============================================================================

def scrape_grid_point(lat, lon, anchor_row, page):
    """
    Navigate to the anchor station's WU weather page, open the station
    selector, and return all visible station records.
    """
    url = station_url(anchor_row)
    if not url:
        return []

    try:
        page.goto(url, wait_until='load', timeout=30000)
        time.sleep(NAV_DELAY)

        # Try primary selector then fallback
        for selector in ('#station-select-button', '.station-select-button'):
            btn = page.locator(selector)
            if btn.count() > 0:
                btn.first.wait_for(state='visible', timeout=8000)
                btn.first.click()
                time.sleep(MAP_LOAD_WAIT)
                return extract_stations_from_html(page.content())

        return []
    except Exception:
        return []


# ============================================================================
# CHECKPOINT
# ============================================================================

def load_checkpoint():
    """Return set of (lat, lon) string keys already processed."""
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    try:
        df = pd.read_csv(CHECKPOINT_FILE)
        return set(zip(df['lat'].astype(str), df['lon'].astype(str)))
    except Exception:
        return set()


def save_checkpoint_point(lat, lon):
    mode   = 'a' if os.path.exists(CHECKPOINT_FILE) else 'w'
    header = not os.path.exists(CHECKPOINT_FILE)
    pd.DataFrame([{'lat': lat, 'lon': lon}]).to_csv(
        CHECKPOINT_FILE, mode=mode, header=header, index=False)


# ============================================================================
# DISCOVERY LOG
# ============================================================================

def log_discovery(station_id, lat, lon):
    mode   = 'a' if os.path.exists(DISCOVERY_LOG) else 'w'
    header = not os.path.exists(DISCOVERY_LOG)
    pd.DataFrame([{
        'station_id':  station_id,
        'latitude':    lat,
        'longitude':   lon,
        'discovered':  datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }]).to_csv(DISCOVERY_LOG, mode=mode, header=header, index=False)


# ============================================================================
# METADATA UPDATE
# ============================================================================

def append_new_stations(new_stations, existing_ids):
    """
    Append genuinely new stations to station_metadata_wu.csv
    with the canonical schema and 0 observations (pending scraping).
    """
    if not new_stations:
        return 0
    meta = pd.read_csv(METADATA_FILE)
    added = 0
    for s in new_stations:
        if s['station_id'] in existing_ids:
            continue
        new_row = pd.DataFrame([{
            'source':              'WU',
            'station_id':          s['station_id'],
            'name':                None,
            'mac_address':         None,
            'earliest_date':       '',
            'latest_date':         '',
            'last_scraped_date':   '',
            'total_days':          0,
            'total_observations':  0,
            'latitude':            s.get('latitude'),
            'longitude':           s.get('longitude'),
            'elevation_ft':        None,
            'extinct':             False,
        }])
        meta = pd.concat([meta, new_row], ignore_index=True)
        existing_ids.add(s['station_id'])
        added += 1
    meta.to_csv(METADATA_FILE, index=False)
    return added


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 70)
    print("Weather Underground — Station Discovery")
    print("=" * 70)
    print(f"Metadata file : {METADATA_FILE}")
    print(f"Grid step     : {GRID_STEP}°  (~{GRID_STEP * 69:.1f} miles)")
    print(f"Bounds        : {GRID_BOUNDS}")

    if not os.path.exists(METADATA_FILE):
        print(f"ERROR: Metadata file not found: {METADATA_FILE}")
        raise SystemExit(1)

    stations_df  = pd.read_csv(METADATA_FILE)
    wu_df        = stations_df[stations_df['source'] == 'WU'].copy()
    existing_ids = set(wu_df['station_id'].dropna().tolist())
    print(f"Known WU stations: {len(existing_ids):,}")

    grid   = generate_grid(GRID_BOUNDS, GRID_STEP)
    done   = load_checkpoint()
    remaining = [(lat, lon) for lat, lon in grid
                 if (str(lat), str(lon)) not in done]

    print(f"Grid points total:     {len(grid):,}")
    print(f"Already processed:     {len(done):,}")
    print(f"Remaining this run:    {len(remaining):,}")
    est_min = len(remaining) * (NAV_DELAY + MAP_LOAD_WAIT + 1) / 60
    print(f"Estimated runtime:     ~{est_min:.0f} minutes")
    print("=" * 70)

    total_found   = 0
    total_new     = 0
    all_new       = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()

        for idx, (lat, lon) in enumerate(remaining, 1):
            anchor = nearest_station(lat, lon, wu_df)
            if anchor is None:
                save_checkpoint_point(lat, lon)
                continue

            print(f"[{idx:3d}/{len(remaining)}] ({lat:.4f}, {lon:.4f})  "
                  f"anchor: {anchor['station_id']} ... ", end='', flush=True)

            stations = scrape_grid_point(lat, lon, anchor, page)
            found    = len(stations)
            new_here = [s for s in stations if s['station_id'] not in existing_ids]

            print(f"{found:3d} visible,  {len(new_here):2d} new")

            for s in new_here:
                log_discovery(s['station_id'], s.get('latitude'), s.get('longitude'))
                all_new.append(s)
                total_new += 1

            total_found += found
            save_checkpoint_point(lat, lon)
            time.sleep(random.uniform(0.5, 1.0))

        browser.close()

    # Write new stations to metadata
    if all_new:
        added = append_new_stations(all_new, existing_ids)
    else:
        added = 0

    print("\n" + "=" * 70)
    print("DISCOVERY COMPLETE")
    print("=" * 70)
    print(f"Grid points scanned:       {len(remaining):,}")
    print(f"Total station sightings:   {total_found:,}")
    print(f"New stations found:        {total_new:,}")
    print(f"Added to metadata CSV:     {added:,}")
    if total_new:
        print(f"Discovery log:             {DISCOVERY_LOG}")
        print()
        print("Next steps:")
        print("  1. Review discovered_wu_{date}.csv for any anomalies")
        print("  2. Run wu_probe_pending.py on the Windows machine to")
        print("     probe these new stations for historical data")
    else:
        print("No new stations found — metadata is current.")
    print("=" * 70)

    # Clean up checkpoint after a clean full run
    if not remaining or len(remaining) == len(grid):
        pass  # partial run — keep checkpoint
    else:
        if os.path.exists(CHECKPOINT_FILE):
            os.remove(CHECKPOINT_FILE)
            print(f"Checkpoint file removed (run complete).")


if __name__ == '__main__':
    main()
