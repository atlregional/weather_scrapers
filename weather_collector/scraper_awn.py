"""
Ambient Weather Network - Historical Data Collector (Windows Version)

Scrapes historical weather data from AWN stations backwards from a start date.
Similar to WU scraper with checkpoint, resume capability, and individual station files.

WINDOWS-SPECIFIC MODIFICATIONS:
- External hard drive export path for CSV storage
- Conda environment 'scraper' instead of 'research'
- Input CSV and checkpoint CSV in same directory
- Windows-compatible subprocess calls
"""

import requests
import pandas as pd
import time
import os
import sys
from datetime import datetime, timedelta
import psychrolib

# Set psychrolib to use Imperial units (Fahrenheit, PSI)
psychrolib.SetUnitSystem(psychrolib.IP)


# ============================================================================
# CONFIGURATION - ADJUST THESE VALUES FOR WINDOWS
# ============================================================================

# UPDATE THIS to your external hard drive root
# Must match scraper_wu.py and generate_update.py
EXTERNAL_DRIVE = "D:\\Weather\\"  # Windows
# EXTERNAL_DRIVE = "/Volumes/Extreme Pro/Weather/" # Mac -> for testing

# ============================================================================
# DIRECTORY STRUCTURE - All paths derive from EXTERNAL_DRIVE above
# ============================================================================
# E:\metadata\       — checkpoints, input CSVs (not version-controlled)
# E:\station-data\   — raw station CSVs, split by source
METADATA_DIR = os.path.join(EXTERNAL_DRIVE, 'metadata')


# Output directory for individual station CSV files
OUTPUT_DIR = os.path.join(EXTERNAL_DRIVE, 'station-data', 'AWN')

# AWN-specific metadata file (each scraper owns its own file to avoid write collisions)
METADATA_FILE = os.path.join(METADATA_DIR, 'station_metadata_awn.csv')

# Starting date for backwards scraping (most recent complete day)
START_DATE = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')

# Number of consecutive days with no data before stopping backwards scrape
NO_DATA_THRESHOLD = 14

# Consecutive days with no data before a station is flagged extinct in loop mode
EXTINCT_THRESHOLD = 30

# Weekday (0=Mon ... 6=Sun) on which the Lazarus probe runs against extinct
# stations.  On this day the loop attempts a single-day fetch for each extinct
# station; if the API returns data the extinct flag is cleared (revived).
PROBE_WEEKDAY = 6

# Delay between requests (seconds) - be respectful to AWN servers
REQUEST_DELAY = 1.2

# Hour of day (24h) to fire the daily update in --loop mode
LOOP_HOUR = 0

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def calculate_wet_bulb(temp_f, humidity_percent, pressure_inhg):
    """
    Calculate wet-bulb temperature from dry-bulb temp, humidity, and pressure.

    Args:
        temp_f: Dry bulb temperature in Fahrenheit
        humidity_percent: Relative humidity as percentage (0-100)
        pressure_inhg: Atmospheric pressure in inches of mercury

    Returns:
        Wet bulb temperature in Fahrenheit, or None if calculation fails
    """
    try:
        # Check for missing values
        if pd.isna(temp_f) or pd.isna(humidity_percent) or pd.isna(pressure_inhg):
            return None

        # Convert pressure from inHg to PSI (1 inHg = 0.491154 PSI)
        pressure_psi = pressure_inhg * 0.491154

        # Calculate wet-bulb temperature
        wet_bulb = psychrolib.GetTWetBulbFromRelHum(
            temp_f,
            humidity_percent / 100.0,  # Convert percentage to decimal
            pressure_psi
        )

        return round(wet_bulb, 2)
    except Exception:
        # Return None for any calculation errors
        return None


def get_station_daily_data(mac_address, station_id, station_name, date):
    """
    Get one day of data for a specific station.

    Args:
        mac_address: Station MAC address
        station_id: Station ID for tracking
        station_name: Station name for tracking
        date: datetime object for the day to collect

    Returns:
        DataFrame with weather data for that day, or None if error
    """
    base_url = "https://lightning.ambientweather.net/device-data"

    # Get 24 hours starting from midnight of the date
    start_date = date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_date = start_date + timedelta(days=1)

    # Convert to milliseconds timestamp
    start_ms = int(start_date.timestamp() * 1000)
    end_ms = int(end_date.timestamp() * 1000)

    params = {
        'macAddress': mac_address,
        'start': start_ms,
        'end': end_ms,
        'limit': 2000,
        'dataKey': 'graphDataRefined'
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Referer': 'https://ambientweather.net/'
    }

    try:
        response = requests.get(base_url, params=params, headers=headers, timeout=30)

        if response.status_code == 200:
            data = response.json()
            records = data.get('data', [])

            if not records:
                return None

            # Convert to DataFrame
            df = pd.DataFrame(records)

            # Add station identifier
            df['station_id'] = station_id

            # Convert timestamp to datetime
            if 'dateutc' in df.columns:
                df['timestamp'] = pd.to_datetime(df['dateutc'], unit='ms')

            # Select and rename columns to match WU format exactly
            # Map AWN fields to WU standard fields
            columns_to_keep = {
                'station_id': 'station_id',
                'timestamp': 'timestamp',
                'tempf': 'Temperature (F)',
                'humidity': 'Humidity (%)',
                'baromrelin': 'Pressure (in)',
                'hourlyrainin': 'Precip. Rate (in/hr)',
                'dailyrainin': 'Precip. Accum (in)'
            }

            # Keep only columns that exist in AWN data
            existing_cols = {k: v for k, v in columns_to_keep.items() if k in df.columns}
            df_clean = df[list(existing_cols.keys())].rename(columns=existing_cols)

            # Convert numeric columns to float for wet bulb calculation
            numeric_columns = ['Temperature (F)', 'Humidity (%)', 'Pressure (in)',
                             'Precip. Rate (in/hr)', 'Precip. Accum (in)']
            for col in numeric_columns:
                if col in df_clean.columns:
                    df_clean[col] = pd.to_numeric(df_clean[col], errors='coerce')

            # Calculate wet-bulb temperature (matching WU format)
            if all(col in df_clean.columns for col in ['Temperature (F)', 'Humidity (%)', 'Pressure (in)']):
                df_clean['Wet Bulb (F)'] = df_clean.apply(
                    lambda row: calculate_wet_bulb(row['Temperature (F)'], row['Humidity (%)'], row['Pressure (in)']),
                    axis=1
                )

            # Reorder columns to match WU format exactly:
            # station_id, timestamp, Temperature (F), Humidity (%), Pressure (in),
            # Precip. Rate (in/hr), Precip. Accum (in), Wet Bulb (F)
            desired_order = [
                'station_id', 'timestamp', 'Temperature (F)', 'Humidity (%)',
                'Pressure (in)', 'Precip. Rate (in/hr)', 'Precip. Accum (in)',
                'Wet Bulb (F)'
            ]
            # Always write all 8 columns so the header is consistent across every
            # file, even when the API omits fields for a particular day/station.
            for col in desired_order:
                if col not in df_clean.columns:
                    df_clean[col] = float('nan')
            df_clean = df_clean[desired_order]

            return df_clean

        else:
            return None

    except Exception as e:
        return None


def get_already_scraped_dates(station_id):
    """
    Read the station's CSV file and return a set of dates that have already been scraped.
    Excludes the earliest (oldest) date to ensure it gets re-scraped in case of
    incomplete data from interrupted writes.

    Args:
        station_id: Station ID to check for

    Returns:
        Tuple of (set of scraped dates excluding earliest, earliest date or None)
    """
    station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")

    if not os.path.exists(station_file):
        return set(), None

    try:
        df = pd.read_csv(station_file)
        if 'timestamp' in df.columns and len(df) > 0:
            # Extract dates from timestamps
            df['date'] = pd.to_datetime(df['timestamp'], errors='coerce').dt.date.astype(str)
            # Filter out NaT/None/nan sentinels that arise from unparseable timestamps
            scraped_dates = {d for d in df['date'].unique() if d not in ('NaT', 'None', 'nan', 'NaTType', '')}

            # Find the earliest date
            if not scraped_dates:
                return set(), None
            earliest_date = min(scraped_dates)

            # Exclude earliest date from the set to ensure it gets re-scraped
            scraped_dates.discard(earliest_date)

            return scraped_dates, earliest_date
        else:
            return set(), None
    except Exception as e:
        print(f"  Warning: Error reading {station_file}: {e}")
        return set(), None


def scrape_station_backwards(station_id, mac_address, station_name, start_date):
    """
    Scrape weather data backwards from start_date until NO_DATA_THRESHOLD consecutive days with no data.
    Saves to station-specific CSV file and appends new data.

    Args:
        station_id: AWN station ID
        mac_address: Station MAC address
        station_name: Station name
        start_date: Starting date string 'YYYY-MM-DD' (scrapes backwards from here)

    Returns:
        Tuple of (days_scraped, earliest_date_found, latest_date_found)
    """
    # Create output directory if it doesn't exist
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Station-specific output file
    station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")

    # Check what dates have already been scraped for this station
    scraped_dates, existing_earliest_date = get_already_scraped_dates(station_id)

    if scraped_dates or existing_earliest_date:
        total_scraped = len(scraped_dates) + (1 if existing_earliest_date else 0)
        print(f"  Found existing data with {total_scraped:,} days already scraped")
        if existing_earliest_date:
            print(f"  Will re-scrape earliest date ({existing_earliest_date}) to ensure completeness")
        print(f"  Will skip other dates and continue backwards\n")

    current_date = datetime.strptime(start_date, '%Y-%m-%d')
    consecutive_no_data = 0
    days_scraped = 0
    days_skipped = 0
    earliest_date = None
    latest_date = None

    print(f"  Starting backwards scrape from {start_date}")
    print(f"  Will stop after {NO_DATA_THRESHOLD} consecutive days with no data\n")

    # Scrape backwards
    while consecutive_no_data < NO_DATA_THRESHOLD:
        date_str = current_date.strftime('%Y-%m-%d')

        # Skip if already scraped
        if date_str in scraped_dates:
            days_skipped += 1
            consecutive_no_data = 0  # Reset counter - this date has data (we just already have it)
            print(f"  {date_str}: [SKIP] Already scraped")
            current_date -= timedelta(days=1)
            continue

        print(f"  {date_str}: Scraping...", end=' ')

        df = get_station_daily_data(mac_address, station_id, station_name, current_date)

        if df is not None and len(df) > 0:
            # Special handling if this is the earliest date - remove old data first
            if os.path.exists(station_file) and date_str == existing_earliest_date:
                # Read existing data, remove rows for this date, then append new data
                try:
                    existing_df = pd.read_csv(station_file)
                    existing_df['date_only'] = pd.to_datetime(existing_df['timestamp'], errors='coerce').dt.date.astype(str)
                    # Keep all rows except those matching the earliest date
                    existing_df = existing_df[existing_df['date_only'] != existing_earliest_date]
                    existing_df = existing_df.drop(columns=['date_only'])
                    # Write back without the earliest date
                    existing_df.to_csv(station_file, mode='w', header=True, index=False)
                    print(f"[RE-SCRAPE] Removed old data for {existing_earliest_date}...", end=' ')
                except Exception as e:
                    print(f"Warning: Could not remove old data for {existing_earliest_date}: {e}")

            # Append to station CSV
            if os.path.exists(station_file):
                # Append to existing file
                df.to_csv(station_file, mode='a', header=False, index=False)
            else:
                # Create new file with header
                df.to_csv(station_file, mode='w', header=True, index=False)

            days_scraped += 1
            consecutive_no_data = 0  # Reset counter on successful scrape

            # Track date range
            if earliest_date is None or date_str < earliest_date:
                earliest_date = date_str
            if latest_date is None or date_str > latest_date:
                latest_date = date_str

            print(f"✓ ({len(df):,} observations)")
        else:
            consecutive_no_data += 1
            print(f"✗ No data (consecutive: {consecutive_no_data}/{NO_DATA_THRESHOLD})")

        # Be respectful to the server - add delay between requests
        time.sleep(REQUEST_DELAY)

        current_date -= timedelta(days=1)

    print(f"\n  {'='*56}")
    print(f"  Historical scrape complete for {station_id}!")
    print(f"  Days scraped in this session: {days_scraped:,}")
    print(f"  Days skipped (already existed): {days_skipped:,}")
    if earliest_date and latest_date:
        print(f"  Date range: {earliest_date} to {latest_date}")
    print(f"  Output file: {station_file}")
    print(f"  {'='*56}\n")

    return days_scraped, earliest_date, latest_date


def get_station_metadata(station_id):
    """
    Read a station's CSV file and return metadata about the data collected.

    Args:
        station_id: Station ID to analyze

    Returns:
        Tuple of (earliest_date, latest_date, last_scraped_date, total_days, total_observations)
        or (None, None, None, 0, 0) if no data
    """
    station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")

    if not os.path.exists(station_file):
        return None, None, None, 0, 0

    try:
        df = pd.read_csv(station_file)
        if 'timestamp' in df.columns and len(df) > 0:
            # Extract dates from timestamps
            df['date'] = pd.to_datetime(df['timestamp'], errors='coerce').dt.date
            # Filter out NaT values before computing min/max
            valid_dates = df['date'].dropna().unique()

            if len(valid_dates) == 0:
                return None, None, None, 0, 0

            earliest = str(min(valid_dates))
            latest = str(max(valid_dates))
            last_scraped = latest  # Most recent date in the data
            total_days = len(valid_dates)
            total_observations = len(df)  # Total row count

            return earliest, latest, last_scraped, total_days, total_observations
        else:
            return None, None, None, 0, 0
    except Exception as e:
        print(f"  Warning: Error reading metadata from {station_file}: {e}")
        return None, None, None, 0, 0


def mark_station_complete(station_id, quiet=False):
    """
    Update the unified metadata file to mark an AWN station as complete.

    Args:
        station_id: Station ID to mark as complete
        quiet:      If True, suppress confirmation prints (used in loop mode)
    """
    try:
        # Get metadata from the station's CSV file
        earliest_date, latest_date, last_scraped_date, total_days, total_observations = get_station_metadata(station_id)

        # Read metadata file
        if os.path.exists(METADATA_FILE):
            metadata_df = pd.read_csv(METADATA_FILE)
        else:
            metadata_df = pd.DataFrame(columns=['source', 'station_id', 'name', 'mac_address',
                                                 'earliest_date', 'latest_date',
                                                 'last_scraped_date', 'total_days', 'total_observations',
                                                 'latitude', 'longitude', 'elevation_ft'])

        # Update or add this station's row (AWN only)
        # Static columns (lat/lon/elevation_ft/name/mac_address) are set at init and not touched here
        awn_mask = (metadata_df['source'] == 'AWN') & (metadata_df['station_id'] == station_id)
        if awn_mask.any():
            metadata_df.loc[awn_mask, 'earliest_date'] = earliest_date or ''
            metadata_df.loc[awn_mask, 'latest_date'] = latest_date or ''
            metadata_df.loc[awn_mask, 'last_scraped_date'] = last_scraped_date or ''
            metadata_df.loc[awn_mask, 'total_days'] = total_days
            metadata_df.loc[awn_mask, 'total_observations'] = total_observations
        else:
            new_row = pd.DataFrame([{
                'source': 'AWN',
                'station_id': station_id,
                'name': None,
                'mac_address': None,
                'earliest_date': earliest_date or '',
                'latest_date': latest_date or '',
                'last_scraped_date': last_scraped_date or '',
                'total_days': total_days,
                'total_observations': total_observations,
                'latitude': None,
                'longitude': None,
                'elevation_ft': None,
            }])
            metadata_df = pd.concat([metadata_df, new_row], ignore_index=True)

        # Save — preserves WU rows unchanged
        metadata_df.to_csv(METADATA_FILE, index=False)

        if not quiet:
            print(f"  ✓ Marked {station_id} as complete in metadata file")
            if earliest_date and latest_date:
                print(f"    Date range: {earliest_date} to {latest_date}")
                print(f"    Total: {total_days:,} days, {total_observations:,} observations")

    except Exception as e:
        print(f"  ⚠️  Warning: Could not update metadata file: {e}")


def mark_station_extinct(station_id):
    """
    Flag an AWN station as extinct in the metadata file.
    Called when a station has had no data for EXTINCT_THRESHOLD+ consecutive days.
    The flag can be manually cleared by setting 'extinct' to False in the CSV.
    """
    try:
        if not os.path.exists(METADATA_FILE):
            return
        metadata_df = pd.read_csv(METADATA_FILE)
        if 'extinct' not in metadata_df.columns:
            metadata_df['extinct'] = False
        awn_mask = (metadata_df['source'] == 'AWN') & (metadata_df['station_id'] == station_id)
        if awn_mask.any():
            metadata_df.loc[awn_mask, 'extinct'] = True
            metadata_df.to_csv(METADATA_FILE, index=False)
            print(f"  !! {station_id} marked extinct ({EXTINCT_THRESHOLD}+ days with no data)")
    except Exception as e:
        print(f"  Warning: Error marking station extinct: {e}")


def revive_station(station_id):
    """
    Clear an AWN station's extinct flag after a successful probe revival.
    Called from probe_extinct_stations() when yesterday's fetch returns data
    for a previously-extinct station.
    """
    try:
        if not os.path.exists(METADATA_FILE):
            return
        metadata_df = pd.read_csv(METADATA_FILE)
        if 'extinct' not in metadata_df.columns:
            return
        awn_mask = (metadata_df['source'] == 'AWN') & (metadata_df['station_id'] == station_id)
        if awn_mask.any():
            metadata_df.loc[awn_mask, 'extinct'] = False
            metadata_df.to_csv(METADATA_FILE, index=False)
            print(f"  ✓✓ {station_id} revived — yesterday returned data")
    except Exception as e:
        print(f"  Warning: Error reviving station: {e}")


def probe_extinct_stations(yesterday_dt, label):
    """
    Weekly Lazarus probe: try fetching yesterday's data for every extinct AWN
    station.  If the API returns observations, the row is appended to the
    station CSV, mark_station_complete() syncs metadata, and revive_station()
    clears the extinct flag so tomorrow's normal run picks the station back up
    and fills any remaining gap.

    Stations that still return no data keep their extinct flag and get
    re-probed next PROBE_WEEKDAY.
    """
    try:
        metadata_df = pd.read_csv(METADATA_FILE)
    except Exception as e:
        print(f"{label}  PROBE ERROR reading metadata: {e}")
        return

    if 'extinct' not in metadata_df.columns:
        return

    extinct_rows = metadata_df[
        (metadata_df['source'] == 'AWN') &
        metadata_df['extinct'].apply(lambda v: str(v).strip().lower() == 'true') &
        metadata_df['mac_address'].notna() &
        metadata_df['station_id'].notna()
    ][['station_id', 'mac_address']].copy()

    total = len(extinct_rows)
    if total == 0:
        return

    yesterday = yesterday_dt.strftime('%Y-%m-%d')
    print(f"{label}  Weekly probe — testing {total:,} extinct AWN station(s) for {yesterday}")
    revived = 0

    for i, (_, row) in enumerate(extinct_rows.iterrows(), 1):
        station_id  = row['station_id']
        mac_address = row['mac_address']
        prefix      = f"  [probe {i:3d}/{total}]  {station_id}"
        PAD         = " " * 30

        try:
            print(f"{prefix}  {yesterday}{PAD}", end='\r', flush=True)
            df = get_station_daily_data(
                mac_address, station_id, '',
                datetime(yesterday_dt.year, yesterday_dt.month, yesterday_dt.day),
            )
            if df is not None and len(df) > 0:
                station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
                if os.path.exists(station_file):
                    with open(station_file, 'rb') as _f:
                        _f.seek(0, 2)
                        if _f.tell() > 0:
                            _f.seek(-1, 2)
                            if _f.read(1) != b'\n':
                                with open(station_file, 'ab') as _fw:
                                    _fw.write(b'\n')
                    mode, hdr = 'a', False
                else:
                    mode, hdr = 'w', True
                df.to_csv(station_file, mode=mode, header=hdr, index=False)
                print()
                mark_station_complete(station_id, quiet=True)
                revive_station(station_id)
                revived += 1
            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"\n{prefix}  probe FAILED: {e}")

    print(f"\n{label}  Probe done: {revived:,}/{total:,} station(s) revived")


def remove_station_from_checkpoint(station_id):
    """
    Remove an AWN station from the unified metadata file (for inactive stations with no data).

    Args:
        station_id: Station ID to remove
    """
    try:
        if not os.path.exists(METADATA_FILE):
            print(f"  ℹ️  Metadata file does not exist, nothing to remove")
            return

        metadata_df = pd.read_csv(METADATA_FILE)

        awn_mask = (metadata_df['source'] == 'AWN') & (metadata_df['station_id'] == station_id)
        if awn_mask.any():
            metadata_df = metadata_df[~awn_mask]
            metadata_df.to_csv(METADATA_FILE, index=False)
            print(f"  ✓ Removed {station_id} from metadata file (inactive AWN station)")
        else:
            print(f"  ℹ️  AWN station {station_id} not found in metadata file")

    except Exception as e:
        print(f"  ⚠️  Warning: Could not remove station from metadata file: {e}")


def initialize_checkpoint(stations_df):
    """
    Add any AWN stations not yet in the unified metadata file, in 'pending' status.
    Includes geographic data (lat, lon, elevation_ft) from the station list CSV.
    AWN elevation is stored in meters and converted to feet on write.

    Args:
        stations_df: DataFrame with station information
    """
    try:
        if os.path.exists(METADATA_FILE):
            existing_df = pd.read_csv(METADATA_FILE)
            already_tracked = set(existing_df[existing_df['source'] == 'AWN']['station_id'].tolist())
            new_stations = stations_df[~stations_df['station_id'].isin(already_tracked)]
        else:
            existing_df = pd.DataFrame(columns=['source', 'station_id', 'name', 'mac_address',
                                                 'earliest_date', 'latest_date',
                                                 'last_scraped_date', 'total_days', 'total_observations',
                                                 'latitude', 'longitude', 'elevation_ft'])
            new_stations = stations_df

        if len(new_stations) == 0:
            return

        # Convert elevation from meters to feet (1 m = 3.28084 ft)
        if 'elevation_m' in new_stations.columns:
            elevation_ft = (new_stations['elevation_m'] * 3.28084).round(1).values
        else:
            elevation_ft = None

        new_rows = pd.DataFrame({
            'source': 'AWN',
            'station_id': new_stations['station_id'].values,
            'name': new_stations['name'].values if 'name' in new_stations.columns else None,
            'mac_address': new_stations['mac_address'].values if 'mac_address' in new_stations.columns else None,
            'earliest_date': '',
            'latest_date': '',
            'last_scraped_date': '',
            'total_days': 0,
            'total_observations': 0,
            'latitude': new_stations['latitude'].values if 'latitude' in new_stations.columns else None,
            'longitude': new_stations['longitude'].values if 'longitude' in new_stations.columns else None,
            'elevation_ft': elevation_ft,
        })
        combined = pd.concat([existing_df, new_rows], ignore_index=True)
        combined.to_csv(METADATA_FILE, index=False)
        print(f"✓ Added {len(new_stations):,} AWN stations to metadata file\n")
    except Exception as e:
        print(f"⚠️  Warning: Error updating metadata file: {e}\n")


def get_completed_stations():
    """
    Read the metadata file and return AWN station IDs that have completed historical scraping.

    Returns:
        Set of AWN station IDs that are complete
    """
    if not os.path.exists(METADATA_FILE):
        return set()

    try:
        df = pd.read_csv(METADATA_FILE)
        awn_df = df[df['source'] == 'AWN']
        if 'status' not in awn_df.columns:
            return set()
        completed = set(awn_df[awn_df['status'] == 'complete']['station_id'].tolist())
        return completed
    except Exception as e:
        print(f"⚠️  Warning: Error reading metadata file: {e}")
        return set()


def scrape_single_day(station_id, mac_address, date_str):
    """
    Scrape one specific day for an AWN station and append it if not already present.
    Returns True if data was found (including if it was already present).
    Used by run_loop() for daily catch-up once historical scraping is complete.
    """
    scraped_dates, _ = get_already_scraped_dates(station_id)
    if date_str in scraped_dates:
        return True
    date_dt = datetime.strptime(date_str, '%Y-%m-%d')
    df = get_station_daily_data(mac_address, station_id, '', date_dt)
    if df is not None and len(df) > 0:
        station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
        mode, header = ('a', False) if os.path.exists(station_file) else ('w', True)
        df.to_csv(station_file, mode=mode, header=header, index=False)
        return True
    return False


def seconds_until_next_fire():
    """Return seconds until the next LOOP_HOUR:00."""
    now = datetime.now()
    next_fire = now.replace(hour=LOOP_HOUR, minute=0, second=0, microsecond=0)
    if now >= next_fire:
        next_fire += timedelta(days=1)
    return (next_fire - now).total_seconds()


def run_loop():
    """
    Forward-scrapes all missing dates for every AWN station, then repeats
    daily at LOOP_HOUR.  Handles both historical catch-up and ongoing daily
    maintenance in one mode — no need to switch scripts once caught up.

    Each run:
      1. Loads active (non-extinct) stations from metadata.
      2. Sorts them most-behind first so lagging stations are prioritised.
      3. For each station, scrapes forward from latest_date+1 through yesterday.
      4. Updates metadata after each station so Ctrl+C never loses progress.
    """
    print(f"Loop mode active — firing daily at {LOOP_HOUR:02d}:00.  Press Ctrl+C to stop.\n")

    def parse_date(s):
        for fmt in ('%m/%d/%y', '%Y-%m-%d', '%m/%d/%Y'):
            try:
                return datetime.strptime(str(s).strip(), fmt).date()
            except (ValueError, TypeError):
                pass
        return None

    first_run = True
    while True:
        if first_run:
            first_run = False
        else:
            wait = seconds_until_next_fire()
            next_fire = datetime.fromtimestamp(time.time() + wait)
            _d = next_fire.strftime("%m/%d/%y").lstrip("0").replace("/0", "/")
            print(f"  Waiting... next run at {LOOP_HOUR:02d}:00 on {next_fire.strftime('%A')}, {_d}    ", end="\r", flush=True)
            time.sleep(wait)

        cycle_start  = time.time()
        yesterday_dt = (datetime.now() - timedelta(days=1)).date()
        yesterday    = yesterday_dt.strftime('%Y-%m-%d')
        label        = datetime.now().strftime("[%a %H:%M]")
        print(f"\n{label}  Daily run — scraping up to {yesterday} for all AWN stations")

        # ── Weekly Lazarus probe (Sundays) ─────────────────────────────────
        # Revive any extinct station whose sensor has come back online.
        # Runs before the metadata load so freshly-revived rows flow naturally
        # into the main rotation below.
        if datetime.now().weekday() == PROBE_WEEKDAY:
            probe_extinct_stations(yesterday_dt, label)

        try:
            metadata_df = pd.read_csv(METADATA_FILE)
            cols = ['station_id', 'mac_address', 'latest_date']
            if 'extinct' in metadata_df.columns:
                cols.append('extinct')
            awn_df = metadata_df[
                (metadata_df['source'] == 'AWN') &
                metadata_df['mac_address'].notna() &
                metadata_df['station_id'].notna()
            ][cols].copy()
            if 'extinct' in awn_df.columns:
                awn_df = awn_df[
                    awn_df['extinct'].apply(lambda v: str(v).strip().lower() != 'true')
                ].copy()
        except Exception as e:
            print(f"{label}  ERROR reading metadata: {e}")
            continue

        # Sort most-behind stations first so catch-up work is prioritised
        awn_df = awn_df.copy()
        awn_df['_latest_dt'] = awn_df['latest_date'].apply(parse_date)
        awn_df = awn_df.sort_values('_latest_dt', na_position='first').reset_index(drop=True)

        total            = len(awn_df)
        stations_updated = 0
        print(f"  {total:,} active stations to process\n")

        for i, (_, row) in enumerate(awn_df.iterrows(), 1):
            station_id  = row['station_id']
            mac_address = row['mac_address']
            latest_dt   = row['_latest_dt']

            # If metadata has no latest_date, check the actual CSV so we don't
            # miss the gap between the last written row and today
            if latest_dt is None:
                station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
                if os.path.exists(station_file):
                    _, csv_latest, _, _, _ = get_station_metadata(station_id)
                    if csv_latest:
                        latest_dt = parse_date(csv_latest)

            from_dt = (latest_dt + timedelta(days=1)) if latest_dt else yesterday_dt

            # Build the list of dates that need fetching
            dates_to_fill = []
            d = from_dt
            while d <= yesterday_dt:
                dates_to_fill.append(d)
                d += timedelta(days=1)

            if not dates_to_fill:
                continue  # already current — nothing to do

            days_gap = len(dates_to_fill)
            prefix   = f"  [{i:3d}/{total}]  {station_id}"
            PAD      = " " * 30  # trailing spaces to overwrite any longer previous line

            # Read cached dates once — avoids a full file read per date in the loop
            cached_dates, _ = get_already_scraped_dates(station_id)

            days_fetched = 0  # dates where an HTTP call was made
            days_scraped = 0  # dates where the HTTP call returned data
            try:
                for j, date_dt in enumerate(dates_to_fill, 1):
                    date_str = date_dt.strftime('%Y-%m-%d')
                    if date_str in cached_dates:
                        continue  # already have it — no HTTP call
                    days_fetched += 1
                    if days_gap == 1:
                        print(f"{prefix}  {date_str}{PAD}", end='\r', flush=True)
                    else:
                        print(f"{prefix}  [{j:,}/{days_gap:,}]  {date_str}{PAD}", end='\r', flush=True)
                    df = get_station_daily_data(mac_address, station_id, '', datetime(date_dt.year, date_dt.month, date_dt.day))
                    if df is not None and len(df) > 0:
                        station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
                        if os.path.exists(station_file):
                            with open(station_file, 'rb') as _f:
                                _f.seek(0, 2)
                                if _f.tell() > 0:
                                    _f.seek(-1, 2)
                                    if _f.read(1) != b'\n':
                                        with open(station_file, 'ab') as _fw:
                                            _fw.write(b'\n')
                            mode, hdr = 'a', False
                        else:
                            mode, hdr = 'w', True
                        df.to_csv(station_file, mode=mode, header=hdr, index=False)
                        days_scraped += 1
                    time.sleep(REQUEST_DELAY)

                if days_scraped > 0:
                    stations_updated += 1
                    if days_gap == 1:
                        print(f"{prefix}  ✓{PAD}", end='\r', flush=True)
                    else:
                        print(f"{prefix}  ✓  {days_scraped:,}/{days_gap:,} days{PAD}", end='\r', flush=True)
                    mark_station_complete(station_id, quiet=True)
                elif days_fetched == 0:
                    # All dates already in the CSV — metadata is just stale; sync it
                    print(f"{prefix}  — already current{PAD}", end='\r', flush=True)
                    mark_station_complete(station_id, quiet=True)
                else:
                    # HTTP calls were made but every date returned empty
                    if days_gap == 1:
                        print(f"{prefix}  — no data{PAD}", end='\r', flush=True)
                    else:
                        print(f"{prefix}  — no data ({days_gap:,} dates){PAD}", end='\r', flush=True)
                    # Check extinction: no data and gap exceeds threshold
                    if latest_dt and (yesterday_dt - latest_dt).days >= EXTINCT_THRESHOLD:
                        print()  # end the \r line before the extinct message
                        mark_station_extinct(station_id)

            except Exception as e:
                print(f"\n{prefix}  FAILED: {e}")

        elapsed = int(time.time() - cycle_start)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        dur_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
        print(f"\n{label}  Done: {stations_updated:,}/{total:,} stations updated, "
              f"run ending {yesterday}  ({dur_str})")


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def main():
    print("=" * 70)
    print("AMBIENT WEATHER NETWORK - HISTORICAL DATA COLLECTOR (WINDOWS)")
    print("=" * 70)
    print(f"\nConfiguration:")
    print(f"  Start date: {START_DATE}")
    print(f"  No-data threshold: {NO_DATA_THRESHOLD} consecutive days")
    print(f"  Request delay: {REQUEST_DELAY} seconds")
    print(f"  Output directory (external drive): {OUTPUT_DIR}")
    print(f"  Metadata file: {METADATA_FILE}")

    # Verify external drive is accessible
    if not os.path.exists(EXTERNAL_DRIVE):
        print(f"\n⚠️  WARNING: External drive path does not exist: {EXTERNAL_DRIVE}")
        print(f"   Please verify the drive is connected and update EXTERNAL_DRIVE in the script.")
        # response = input("\n   Continue anyway? (yes/no): ")
        # if response.lower() != 'yes':
        #     print("\n❌ Cancelled.")
        #     return

    # Load AWN station list from unified metadata file
    if not os.path.exists(METADATA_FILE):
        print(f"\n❌ ERROR: {METADATA_FILE} not found!")
        print("   Ensure station_metadata.csv exists in the metadata directory.")
        return

    try:
        metadata_df = pd.read_csv(METADATA_FILE)
        df_stations = metadata_df[
            (metadata_df['source'] == 'AWN') &
            metadata_df['mac_address'].notna() &
            metadata_df['station_id'].notna()
        ].copy()
    except Exception as e:
        print(f"\n❌ ERROR reading {METADATA_FILE}: {e}")
        return

    print(f"\n📂 Loaded {len(df_stations):,} AWN stations from metadata file\n")

    # Get list of completed stations
    completed_stations = get_completed_stations()

    if completed_stations:
        print(f"📊 Found {len(completed_stations):,} already completed station(s)")
        print(f"   Will skip these and continue with remaining stations\n")


    # Filter out completed stations
    df_pending = df_stations[~df_stations['station_id'].isin(completed_stations)]

    if len(df_pending) == 0:
        print("✅ All stations have been scraped!")
        return

    # Check for interrupted stations (CSV exists but status is still 'pending')
    existing_csvs = set()
    if os.path.exists(OUTPUT_DIR):
        csv_files = [f for f in os.listdir(OUTPUT_DIR)
                     if f.endswith('.csv')]
        existing_csvs = {f.replace('.csv', '') for f in csv_files}

    # Split pending into interrupted (have CSV) and not-yet-started (no CSV)
    df_interrupted = df_pending[df_pending['station_id'].isin(existing_csvs)]
    df_not_started = df_pending[~df_pending['station_id'].isin(existing_csvs)]

    # Randomize the not-yet-started stations for geographic diversity
    df_not_started = df_not_started.sample(frac=1, random_state=42).reset_index(drop=True)

    # Concatenate: interrupted stations first, then randomized not-yet-started
    df_pending = pd.concat([df_interrupted, df_not_started], ignore_index=True)

    print(f"📋 {len(df_pending):,} station(s) remaining to scrape")
    if len(df_interrupted) > 0:
        print(f"   ⚡ {len(df_interrupted):,} interrupted station(s) will be completed first")
    if len(df_not_started) > 0:
        print(f"   🎲 {len(df_not_started):,} remaining station(s) randomized for geographic diversity")
    print()

    # # Confirm before starting
    # proceed = input("\n▶️  Proceed with data collection? (yes/no): ")
    # if proceed.lower() != 'yes':
    #     print("\n❌ Cancelled.")
    #     return

    # Process each station
    print("\n" + "=" * 70)
    print("📊 COLLECTING HISTORICAL DATA")
    print("=" * 70 + "\n")

    successful_stations = 0
    failed_stations = []

    start_time = time.time()

    for idx, row in df_pending.iterrows():
        station_id = row['station_id']
        mac_address = row['mac_address']
        name = row['name'] if pd.notna(row.get('name')) else 'Unknown'

        print(f"\n[{idx+1}/{len(df_stations):,}] Station: {str(name)[:50]}")
        print(f"  ID: {station_id}")
        print(f"  MAC: {mac_address}\n")

        try:
            days_scraped, _, _ = scrape_station_backwards(
                station_id=station_id,
                mac_address=mac_address,
                station_name=name,
                start_date=START_DATE
            )

            if days_scraped > 0:
                # Station has data - mark as complete
                mark_station_complete(station_id)
                successful_stations += 1

            else:
                # Station has no data (went full 14 days back without finding anything)
                # Remove it from checkpoint entirely to avoid tracking inactive stations
                remove_station_from_checkpoint(station_id)
                failed_stations.append(name)
                print(f"  ⚠️  No data found for this station (removed from checkpoint)\n")

        except KeyboardInterrupt:
            print("\n\n⚠️  Interrupted by user!")
            print("Progress has been saved. Run the script again to resume.\n")
            return

        except Exception as e:
            print(f"  ❌ FAILED: {e}\n")
            failed_stations.append(name)

    # Summary
    elapsed_time = time.time() - start_time
    elapsed_hours = elapsed_time / 3600

    print("\n" + "=" * 70)
    print("✅ COLLECTION COMPLETE!")
    print("=" * 70)
    print(f"📊 Statistics:")
    print(f"   Successful stations: {successful_stations:,}/{len(df_pending):,}")
    print(f"   Failed stations: {len(failed_stations):,}")
    print(f"\n⏱  Time elapsed: {elapsed_hours:.2f} hours")
    print(f"💾 Data saved to: {OUTPUT_DIR}/")
    print(f"📋 Metadata file: {METADATA_FILE}")

    if failed_stations:
        print(f"\n⚠️  Failed stations ({len(failed_stations):,}):")
        for station in failed_stations[:10]:  # Show first 10
            print(f"   - {station}")
        if len(failed_stations) > 10:
            print(f"   ... and {len(failed_stations) - 10:,} more")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    if '--loop' in sys.argv:
        run_loop()
    else:
        main()
