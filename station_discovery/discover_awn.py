"""
Ambient Weather Network — Station Discovery
============================================

Discovers new AWN stations across the 11-county metro Atlanta area by
querying AWN's public bounding-box API with a systematic grid pattern.
Compares results against station_metadata_awn.csv and appends any new
outdoor stations using the canonical schema.

Designed to run quarterly (unattended) on a local machine or as a
GitHub Actions workflow.  The county boundary spatial filter requires
ARC_counties.gpkg to be present alongside this script.

New stations are appended to station_metadata_awn.csv with
total_observations = 0 (pending scraping).  A dated discovery log is
also written for auditing.

Requires:
    pip install requests pandas geopandas shapely
"""

import os
import time
import requests
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

_SCRIPT_DIR = os.path.dirname(__file__)

# ── Metadata file ─────────────────────────────────────────────────────────────
# Point this at station_metadata_awn.csv wherever it lives on this machine.
#
#   Mac / server (scripts-to-retain directory):
METADATA_FILE = os.path.join(_SCRIPT_DIR, 'station_metadata_awn.csv')
#
#   Windows laptop (uncomment and comment out the line above):
# METADATA_FILE = r"D:\Weather\metadata\station_metadata_awn.csv"

# ── County boundary file ──────────────────────────────────────────────────────
# Must be present — discovery aborts if missing to prevent silent out-of-region
# contamination.  Place ARC_counties.gpkg alongside this script, or set an
# absolute path below.
COUNTY_FILE = os.path.join(_SCRIPT_DIR, 'metro_atlanta_counties.geojson')
# COUNTY_FILE = r"D:\Weather\metadata\metro_atlanta_counties.geojson"

# ── Output files ──────────────────────────────────────────────────────────────
# Discovery log is always written alongside this script.
RUN_DATE      = datetime.now().strftime('%Y-%m-%d')
DISCOVERY_LOG = os.path.join(_SCRIPT_DIR, f'discovered_awn_{RUN_DATE}.csv')

# 11-county metro Atlanta bounding box
REGION = {
    'name':  'Metro Atlanta — 11 counties',
    'west':  -85.0,
    'east':  -83.8,
    'south':  33.3,
    'north':  34.5,
}

# Grid cell size in decimal degrees.
# 0.08° ≈ 5.5 miles.  If a cell returns 100 results (API cap)
# the script automatically re-queries at half this size.
GRID_STEP = 0.08

# AWN API rate limit pause between cell queries
REQUEST_DELAY = 1.5


# ============================================================================
# AWN API
# ============================================================================

def query_awn_bbox(west, south, east, north, limit=100):
    """Return list of raw station objects from AWN's public API, or []."""
    try:
        resp = requests.get(
            'https://lightning.ambientweather.net/devices',
            params={
                '$publicBox[0][0]': west,
                '$publicBox[0][1]': south,
                '$publicBox[1][0]': east,
                '$publicBox[1][1]': north,
                '$limit': limit,
            },
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer':    'https://ambientweather.net/',
            },
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get('data', [])
        print(f"      HTTP {resp.status_code}")
        return []
    except Exception as e:
        print(f"      Error: {e}")
        return []


def extract_station_info(raw):
    """Extract canonical fields from a raw AWN station object."""
    info   = raw.get('info', {})
    coords = info.get('coords', {}).get('coords', {})
    elev_m = info.get('coords', {}).get('elevation')
    slug   = info.get('slug')
    return {
        'station_id':   raw.get('_id'),
        'mac_address':  raw.get('macAddress'),
        'name':         info.get('name', raw.get('macAddress')),
        'latitude':     coords.get('lat'),
        'longitude':    coords.get('lon'),
        'elevation_ft': round(elev_m * 3.28084, 1) if elev_m else None,
        'indoor':       info.get('indoor', False),
        'dashboard_url': (f"https://ambientweather.net/dashboard/{slug}"
                          if slug else None),
    }


# ============================================================================
# GRID QUERY
# ============================================================================

def query_grid(region, step):
    """
    Walk a grid over region, querying AWN for each cell.
    Cells that hit the 100-station cap are automatically re-queried
    at half the cell size to avoid missing stations.
    Returns dict of {station_id: station_info}.
    """
    lats = []
    lat  = region['south']
    while lat < region['north']:
        lats.append(lat)
        lat += step
    lats.append(region['north'])

    lons = []
    lon  = region['west']
    while lon < region['east']:
        lons.append(lon)
        lon += step
    lons.append(region['east'])

    total_cells = (len(lats) - 1) * (len(lons) - 1)
    print(f"\n  Querying {total_cells:,} cells "
          f"({step}° ≈ {step * 69:.1f} mi per side)...\n")

    all_stations = {}
    cell_num     = 0

    for i in range(len(lats) - 1):
        for j in range(len(lons) - 1):
            cell_num += 1
            s, n = lats[i], lats[i + 1]
            w, e = lons[j], lons[j + 1]

            print(f"  [{cell_num:3d}/{total_cells}] "
                  f"({s:.2f},{w:.2f})→({n:.2f},{e:.2f}) ... ", end='', flush=True)

            raw_list = query_awn_bbox(w, s, e, n)
            cap_hit  = len(raw_list) >= 100

            if cap_hit:
                # Re-query at half the cell size to avoid missing stations
                print(f"CAP HIT — splitting cell... ", end='', flush=True)
                raw_list = _query_subcells(w, s, e, n, step / 2)

            new_in_cell = 0
            for raw in raw_list:
                sid = raw.get('_id')
                if sid and sid not in all_stations:
                    try:
                        all_stations[sid] = extract_station_info(raw)
                        new_in_cell += 1
                    except Exception:
                        pass

            print(f"{len(raw_list):3d} found, {new_in_cell:2d} new  "
                  f"| total: {len(all_stations):,}")
            time.sleep(REQUEST_DELAY)

    return all_stations


def _query_subcells(west, south, east, north, sub_step):
    """Query a cell at finer resolution and return deduplicated raw results."""
    sub_lats = []
    lat = south
    while lat < north:
        sub_lats.append(lat)
        lat += sub_step
    sub_lats.append(north)

    sub_lons = []
    lon = west
    while lon < east:
        sub_lons.append(lon)
        lon += sub_step
    sub_lons.append(east)

    seen = {}
    for i in range(len(sub_lats) - 1):
        for j in range(len(sub_lons) - 1):
            results = query_awn_bbox(
                sub_lons[j], sub_lats[i], sub_lons[j + 1], sub_lats[i + 1])
            for r in results:
                sid = r.get('_id')
                if sid and sid not in seen:
                    seen[sid] = r
            time.sleep(0.5)

    return list(seen.values())


# ============================================================================
# SPATIAL FILTER
# ============================================================================

def filter_to_counties(df, county_file):
    """Keep only stations whose coordinates fall within the county boundaries."""
    df_valid = df[df['latitude'].notna() & df['longitude'].notna()].copy()
    if df_valid.empty:
        return df_valid

    gdf = gpd.GeoDataFrame(
        df_valid,
        geometry=[Point(xy) for xy in zip(df_valid['longitude'], df_valid['latitude'])],
        crs='EPSG:4326',
    )
    counties = gpd.read_file(county_file)
    if counties.crs != gdf.crs:
        counties = counties.to_crs(gdf.crs)

    joined = gpd.sjoin(gdf, counties, how='inner', predicate='within')
    return pd.DataFrame(joined.drop(columns=['index_right', 'geometry'],
                                    errors='ignore'))


# ============================================================================
# METADATA UPDATE
# ============================================================================

def append_new_stations(new_df, existing_ids):
    """
    Append new stations to station_metadata_awn.csv using the canonical
    schema, with 0 observations (pending scraping by scraper_awn.py).
    """
    meta  = pd.read_csv(METADATA_FILE)
    added = 0

    for _, row in new_df.iterrows():
        sid = row['station_id']
        if sid in existing_ids:
            continue
        new_row = pd.DataFrame([{
            'source':              'AWN',
            'station_id':          sid,
            'name':                row.get('name'),
            'mac_address':         row.get('mac_address'),
            'earliest_date':       '',
            'latest_date':         '',
            'last_scraped_date':   '',
            'total_days':          0,
            'total_observations':  0,
            'latitude':            row.get('latitude'),
            'longitude':           row.get('longitude'),
            'elevation_ft':        row.get('elevation_ft'),
            'extinct':             False,
        }])
        meta = pd.concat([meta, new_row], ignore_index=True)
        existing_ids.add(sid)
        added += 1

    meta.to_csv(METADATA_FILE, index=False)
    return added


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 70)
    print("Ambient Weather Network — Station Discovery")
    print("=" * 70)
    print(f"Region:        {REGION['name']}")
    print(f"Metadata file: {METADATA_FILE}")
    print(f"County file:   {COUNTY_FILE}")
    print(f"Grid step:     {GRID_STEP}°  (~{GRID_STEP * 69:.1f} miles)")

    # Hard stop if the spatial filter file is missing — silent fallback
    # would let out-of-region stations pollute the dataset.
    if not os.path.exists(COUNTY_FILE):
        print(f"\nERROR: County boundary file not found: {COUNTY_FILE}")
        print("Place ARC_counties.gpkg alongside this script and re-run.")
        raise SystemExit(1)

    if not os.path.exists(METADATA_FILE):
        print(f"ERROR: Metadata file not found: {METADATA_FILE}")
        raise SystemExit(1)

    meta         = pd.read_csv(METADATA_FILE)
    existing_ids = set(meta[meta['source'] == 'AWN']['station_id'].dropna())
    print(f"Known AWN stations: {len(existing_ids):,}")

    # ── Scan the grid ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    raw_stations = query_grid(REGION, GRID_STEP)
    df_scraped   = pd.DataFrame(list(raw_stations.values()))
    print(f"\nRaw results from API grid: {len(df_scraped):,} stations")

    if df_scraped.empty:
        print("No stations returned — check network and API availability.")
        return

    # ── Filter indoor stations ───────────────────────────────────────────────
    before   = len(df_scraped)
    df_scraped = df_scraped[df_scraped['indoor'] == False].copy()
    print(f"After removing indoor stations: {len(df_scraped):,}  "
          f"(removed {before - len(df_scraped):,})")

    # ── Spatial filter ───────────────────────────────────────────────────────
    before   = len(df_scraped)
    df_scraped = filter_to_counties(df_scraped, COUNTY_FILE)
    print(f"After county spatial filter:    {len(df_scraped):,}  "
          f"(removed {before - len(df_scraped):,})")

    # ── Identify new stations ────────────────────────────────────────────────
    df_new  = df_scraped[~df_scraped['station_id'].isin(existing_ids)].copy()
    df_known = df_scraped[df_scraped['station_id'].isin(existing_ids)].copy()
    print(f"\nAlready known: {len(df_known):,}")
    print(f"New stations:  {len(df_new):,}")

    if df_new.empty:
        print("\nNo new stations found — station_metadata_awn.csv is current.")
        return

    # ── Write discovery log ──────────────────────────────────────────────────
    df_new.to_csv(DISCOVERY_LOG, index=False)
    print(f"Discovery log: {DISCOVERY_LOG}")

    # ── Append to metadata ───────────────────────────────────────────────────
    added = append_new_stations(df_new, existing_ids)

    print("\n" + "=" * 70)
    print("DISCOVERY COMPLETE")
    print("=" * 70)
    print(f"New stations added to metadata: {added:,}")
    print(f"Discovery log:                  {DISCOVERY_LOG}")
    print()
    if added:
        print("Sample of new stations:")
        show_cols = [c for c in ['name', 'latitude', 'longitude', 'elevation_ft']
                     if c in df_new.columns]
        print(df_new[show_cols].head(10).to_string(index=False))
        print()
        print("Next step: run scraper_awn.py to collect data for new stations.")
    print("=" * 70)


if __name__ == '__main__':
    main()
