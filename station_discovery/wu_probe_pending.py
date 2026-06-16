"""
WU Pending Station Probe — Battleship Algorithm
================================================

Probes the WU stations in station_metadata_wu.csv that have zero
observations, using stratified random date sampling to locate any
historical data window, then expands outward in both directions using
the same 14-day gap threshold as scraper_wu.py.

Designed to run on the Windows laptop alongside scraper_wu.py.
After this script finishes, scraper_wu.py --loop will automatically
pick up any newly populated stations for ongoing daily updates.

Usage:
    python wu_probe_pending.py
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime, timedelta
import time
import os
import random
import psychrolib

psychrolib.SetUnitSystem(psychrolib.IP)

# ============================================================================
# CONFIGURATION — must match scraper_wu.py
# ============================================================================

EXTERNAL_DRIVE  = "D:\\Weather\\"
METADATA_FILE   = os.path.join(EXTERNAL_DRIVE, 'metadata', 'station_metadata_wu.csv')
OUTPUT_DIR      = os.path.join(EXTERNAL_DRIVE, 'station-data', 'WU')
CHECKPOINT_FILE = os.path.join(EXTERNAL_DRIVE, 'metadata', 'probe_pending_checkpoint.csv')

# How many stratified probe dates to test before giving up on a station
PROBE_ATTEMPTS = 10

# How many years back to spread probe dates across
PROBE_YEARS_BACK = 9

# Consecutive empty days before stopping expansion in either direction
NO_DATA_THRESHOLD = 14

# Seconds between requests
REQUEST_DELAY = 1.0


# ============================================================================
# DATA COLLECTION — mirrors scraper_wu.py exactly
# ============================================================================

def scrape_daily_data(station_id, date):
    url = (f"https://www.wunderground.com/dashboard/pws/"
           f"{station_id}/table/{date}/{date}/daily")
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                 'AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')
        table = soup.find('table', class_='history-table desktop-table')
        if not table:
            return None
        col_headers = [th.get_text(strip=True)
                       for th in table.find('thead').find('tr').find_all('th')]
        rows = [[td.get_text(strip=True) for td in tr.find_all('td')]
                for tr in table.find('tbody').find_all('tr')]
        rows = [r for r in rows if r]
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=col_headers)
        df['station_id'] = station_id
        df['date'] = date
        return df
    except Exception:
        return None


def calculate_wet_bulb(temp_f, humidity_pct, pressure_inhg):
    try:
        if pd.isna(temp_f) or pd.isna(humidity_pct) or pd.isna(pressure_inhg):
            return None
        wet_bulb = psychrolib.GetTWetBulbFromRelHum(
            temp_f, humidity_pct / 100.0, pressure_inhg * 0.491154)
        return round(wet_bulb, 2)
    except Exception:
        return None


def clean_and_filter_data(df):
    keep = ['station_id', 'date', 'Time', 'Temperature', 'Humidity',
            'Pressure', 'Precip. Rate.', 'Precip. Accum.']
    df = df[[c for c in keep if c in df.columns]].copy()

    strip_chars = {'Temperature': ['°F'], 'Humidity': ['°%', '%'],
                   'Pressure': ['°in', 'in'], 'Precip. Rate.': ['°in', 'in'],
                   'Precip. Accum.': ['°in', 'in']}
    for col, chars in strip_chars.items():
        if col in df.columns:
            for ch in chars:
                df[col] = df[col].str.replace(ch, '', regex=False)
            df[col] = df[col].str.strip()

    if 'date' in df.columns and 'Time' in df.columns:
        df['timestamp'] = pd.to_datetime(df['date'] + ' ' + df['Time'],
                                         format='%Y-%m-%d %I:%M %p',
                                         errors='coerce')

    for col in ['Temperature', 'Humidity', 'Pressure',
                'Precip. Rate.', 'Precip. Accum.']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    if all(c in df.columns for c in ['Temperature', 'Humidity', 'Pressure']):
        df['Wet Bulb (F)'] = df.apply(
            lambda r: calculate_wet_bulb(
                r['Temperature'], r['Humidity'], r['Pressure']), axis=1)

    df = df.rename(columns={
        'Temperature':    'Temperature (F)',
        'Humidity':       'Humidity (%)',
        'Pressure':       'Pressure (in)',
        'Precip. Rate.':  'Precip. Rate (in/hr)',
        'Precip. Accum.': 'Precip. Accum (in)',
    })
    df = df.drop(columns=[c for c in ['date', 'Time'] if c in df.columns])
    cols = df.columns.tolist()
    ordered = ['station_id', 'timestamp'] + [c for c in cols
                                              if c not in ('station_id', 'timestamp')]
    return df[[c for c in ordered if c in df.columns]]


# ============================================================================
# FILE I/O
# ============================================================================

def append_to_station_csv(station_id, df_cleaned):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
    if os.path.exists(path):
        with open(path, 'rb') as f:
            f.seek(0, 2)
            if f.tell() > 0:
                f.seek(-1, 2)
                if f.read(1) != b'\n':
                    with open(path, 'ab') as fw:
                        fw.write(b'\n')
        df_cleaned.to_csv(path, mode='a', header=False, index=False)
    else:
        df_cleaned.to_csv(path, mode='w', header=True, index=False)


def update_metadata(station_id):
    """Recompute date range and obs count from the station CSV and write to metadata."""
    try:
        path = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
        if not os.path.exists(path):
            return
        sdf = pd.read_csv(path)
        if 'timestamp' not in sdf.columns or sdf.empty:
            return
        sdf['_date'] = pd.to_datetime(sdf['timestamp'], errors='coerce').dt.date
        valid = sdf['_date'].dropna()
        if valid.empty:
            return
        earliest      = str(valid.min())
        latest        = str(valid.max())
        total_days    = valid.nunique()
        total_obs     = len(sdf)

        meta = pd.read_csv(METADATA_FILE)
        mask = (meta['source'] == 'WU') & (meta['station_id'] == station_id)
        if mask.any():
            meta.loc[mask, 'earliest_date']     = earliest
            meta.loc[mask, 'latest_date']        = latest
            meta.loc[mask, 'last_scraped_date']  = latest
            meta.loc[mask, 'total_days']         = total_days
            meta.loc[mask, 'total_observations'] = total_obs
            meta.to_csv(METADATA_FILE, index=False)
            print(f"  Metadata updated: {earliest} → {latest}  "
                  f"({total_days:,} days, {total_obs:,} obs)")
    except Exception as e:
        print(f"  Warning: could not update metadata — {e}")


# ============================================================================
# BATTLESHIP ALGORITHM
# ============================================================================

def generate_probe_dates():
    """
    Divide the last PROBE_YEARS_BACK years into PROBE_ATTEMPTS equal intervals
    and pick one random date from each, returned most-recent-first.
    This maximises the chance of hitting an active window regardless of
    when the station came online.
    """
    today      = datetime.now()
    start      = today - timedelta(days=PROBE_YEARS_BACK * 365)
    span_days  = (today - start).days
    interval   = span_days // PROBE_ATTEMPTS
    dates = []
    for i in range(PROBE_ATTEMPTS):
        lo = i * interval
        hi = (i + 1) * interval - 1
        d  = start + timedelta(days=random.randint(lo, hi))
        if d < today:
            dates.append(d.strftime('%Y-%m-%d'))
    return sorted(dates, reverse=True)  # most-recent first


def expand_backward(station_id, anchor_date):
    """Scrape day-by-day backward from anchor_date - 1 until NO_DATA_THRESHOLD
    consecutive empty days.  Returns the earliest date with data."""
    print(f"\n  Phase 2: Expanding backward from {anchor_date}...")
    earliest = anchor_date
    consecutive_empty = 0
    current = datetime.strptime(anchor_date, '%Y-%m-%d') - timedelta(days=1)

    while consecutive_empty < NO_DATA_THRESHOLD:
        date_str = current.strftime('%Y-%m-%d')
        print(f"    {date_str} ... ", end='', flush=True)
        df = scrape_daily_data(station_id, date_str)
        if df is not None and len(df) > 0:
            append_to_station_csv(station_id, clean_and_filter_data(df))
            earliest = date_str
            consecutive_empty = 0
            print(f"data  ({len(df):,} obs)")
        else:
            consecutive_empty += 1
            print(f"empty  ({consecutive_empty}/{NO_DATA_THRESHOLD})")
        time.sleep(REQUEST_DELAY)
        current -= timedelta(days=1)

    return earliest


def expand_forward(station_id, anchor_date):
    """Scrape day-by-day forward from anchor_date + 1 to yesterday until
    NO_DATA_THRESHOLD consecutive empty days.  Returns the latest date with data."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    print(f"\n  Phase 3: Expanding forward from {anchor_date} to {yesterday}...")
    latest = anchor_date
    consecutive_empty = 0
    current = datetime.strptime(anchor_date, '%Y-%m-%d') + timedelta(days=1)
    end     = datetime.strptime(yesterday, '%Y-%m-%d')

    while current <= end and consecutive_empty < NO_DATA_THRESHOLD:
        date_str = current.strftime('%Y-%m-%d')
        print(f"    {date_str} ... ", end='', flush=True)
        df = scrape_daily_data(station_id, date_str)
        if df is not None and len(df) > 0:
            append_to_station_csv(station_id, clean_and_filter_data(df))
            latest = date_str
            consecutive_empty = 0
            print(f"data  ({len(df):,} obs)")
        else:
            consecutive_empty += 1
            print(f"empty  ({consecutive_empty}/{NO_DATA_THRESHOLD})")
        time.sleep(REQUEST_DELAY)
        current += timedelta(days=1)

    return latest


def probe_station(station_id):
    """
    Full Battleship workflow for one station.
    Returns (status, earliest_date, latest_date)
    where status is 'found' or 'empty'.
    """
    print(f"\n  Phase 1: Probing {PROBE_ATTEMPTS} stratified dates "
          f"across {PROBE_YEARS_BACK} years...")
    probe_dates = generate_probe_dates()

    anchor_date = None
    for i, date in enumerate(probe_dates, 1):
        print(f"    [{i:2d}/{PROBE_ATTEMPTS}] {date} ... ", end='', flush=True)
        df = scrape_daily_data(station_id, date)
        if df is not None and len(df) > 0:
            print("HIT")
            anchor_date = date
            append_to_station_csv(station_id, clean_and_filter_data(df))
            break
        print("miss")
        time.sleep(REQUEST_DELAY)

    if anchor_date is None:
        print(f"  No data across all {PROBE_ATTEMPTS} probes — station appears empty.")
        return 'empty', None, None

    print(f"\n  Anchor: {anchor_date}")
    earliest = expand_backward(station_id, anchor_date)
    latest   = expand_forward(station_id, anchor_date)
    return 'found', earliest, latest


# ============================================================================
# CHECKPOINT
# ============================================================================

def load_checkpoint():
    if not os.path.exists(CHECKPOINT_FILE):
        return {}
    try:
        df = pd.read_csv(CHECKPOINT_FILE)
        return dict(zip(df['station_id'], df['status']))
    except Exception:
        return {}


def save_checkpoint(checkpoint):
    rows = [{'station_id': sid, 'status': st}
            for sid, st in checkpoint.items()]
    pd.DataFrame(rows).to_csv(CHECKPOINT_FILE, index=False)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 65)
    print("WU Pending Station Probe")
    print("=" * 65)

    if not os.path.exists(EXTERNAL_DRIVE):
        print(f"ERROR: External drive not found: {EXTERNAL_DRIVE}")
        raise SystemExit(1)
    if not os.path.exists(METADATA_FILE):
        print(f"ERROR: Metadata file not found: {METADATA_FILE}")
        raise SystemExit(1)

    meta    = pd.read_csv(METADATA_FILE)
    pending = meta[
        (meta['source'] == 'WU') &
        (meta['extinct'] == False) &
        (meta['total_observations'].fillna(0) == 0)
    ]['station_id'].tolist()

    print(f"Pending stations (0 observations): {len(pending)}")

    checkpoint  = load_checkpoint()
    already_done = {sid for sid, st in checkpoint.items()
                    if st in ('found', 'empty')}
    to_probe    = [s for s in pending if s not in already_done]

    print(f"Already probed:     {len(already_done)}")
    print(f"Remaining to probe: {len(to_probe)}")
    print(f"Output dir:         {OUTPUT_DIR}")
    print(f"Metadata file:      {METADATA_FILE}")
    print("=" * 65)

    if not to_probe:
        print("Nothing left to probe.")
        return

    found_count = 0
    empty_count = 0

    for i, station_id in enumerate(to_probe, 1):
        print(f"\n[{i:2d}/{len(to_probe)}] {station_id}")
        print("-" * 45)

        status, earliest, latest = probe_station(station_id)
        checkpoint[station_id] = status
        save_checkpoint(checkpoint)

        if status == 'found':
            update_metadata(station_id)
            found_count += 1
        else:
            empty_count += 1

    print("\n" + "=" * 65)
    print("PROBE COMPLETE")
    print("=" * 65)
    print(f"Stations with data found:  {found_count}")
    print(f"Stations confirmed empty:  {empty_count}")
    print(f"Checkpoint:                {CHECKPOINT_FILE}")
    print()
    if found_count:
        print("scraper_wu.py --loop will now include these stations "
              "in its nightly forward-scrape automatically.")


if __name__ == '__main__':
    main()
