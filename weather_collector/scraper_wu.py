"""
Scraper for Weather Underground historical data with incremental append.
WINDOWS VERSION - Configured for external hard drive storage.

This is the main workhorse script that will be running continuously in the background.
"""
import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime, timedelta
import time
import os
import psychrolib
import sys
import random
# Set psychrolib to use Imperial units (Fahrenheit, PSI)
psychrolib.SetUnitSystem(psychrolib.IP)

# ============================================================================
# EXTERNAL HARD DRIVE CONFIGURATION
# ============================================================================
# UPDATE THIS to your external hard drive root (e.g. "E:\\", "F:\\", "D:\\")
# Must match scraper_awn.py and generate_update.py
EXTERNAL_DRIVE = "D:\\Weather\\"  # Windows
# EXTERNAL_DRIVE = "/Volumes/Extreme Pro/Weather/" # Mac -> for testing



# ============================================================================
# DIRECTORY STRUCTURE - All paths derive from EXTERNAL_DRIVE above
# =====================
METADATA_DIR = os.path.join(EXTERNAL_DRIVE, 'metadata')

# Station CSV output directory
OUTPUT_DIR = os.path.join(EXTERNAL_DRIVE, 'station-data', 'WU')

# WU-specific metadata file (each scraper owns its own file to avoid write collisions)
METADATA_FILE = os.path.join(METADATA_DIR, 'station_metadata_wu.csv')


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

# Hour of day (24h) to fire the daily update in --loop mode
LOOP_HOUR = 0


def scrape_daily_data(station_id, date):
    """
    Scrape daily weather data for a specific station and date.

    Args:
        station_id: Weather Underground station ID (e.g., 'KGANORTH4')
        date: Date string in format 'YYYY-MM-DD'

    Returns:
        pandas DataFrame with the daily observations, or None if error
    """
    url = f"https://www.wunderground.com/dashboard/pws/{station_id}/table/{date}/{date}/daily"

    try:
        # Add headers to mimic a browser request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()

        # Parse HTML
        soup = BeautifulSoup(response.content, 'lxml')

        # Find the table with class="history-table desktop-table"
        table = soup.find('table', class_='history-table desktop-table')

        if not table:
            return None

        # Extract headers
        headers_row = table.find('thead').find('tr')
        headers = [th.get_text(strip=True) for th in headers_row.find_all('th')]

        # Extract data rows
        tbody = table.find('tbody')
        rows = []

        for tr in tbody.find_all('tr'):
            cells = tr.find_all('td')
            row_data = [td.get_text(strip=True) for td in cells]

            if row_data:  # Only add non-empty rows
                rows.append(row_data)

        # Create DataFrame
        if rows:
            df = pd.DataFrame(rows, columns=headers)
            df['station_id'] = station_id
            df['date'] = date
            return df
        else:
            return None

    except requests.exceptions.RequestException:
        return None
    except Exception:
        return None


def clean_and_filter_data(df):
    """
    Clean data values, calculate wet-bulb temperature, and create timestamp.

    Args:
        df: Raw DataFrame from scraping

    Returns:
        Cleaned DataFrame with timestamp, wet-bulb temp, and required columns
    """
    # Select only the columns we need
    columns_to_keep = ['station_id', 'date', 'Time', 'Temperature', 'Humidity',
                       'Pressure', 'Precip. Rate.', 'Precip. Accum.']

    # Filter to only columns that exist
    available_columns = [col for col in columns_to_keep if col in df.columns]
    df_filtered = df[available_columns].copy()

    # Clean the values by removing units
    if 'Temperature' in df_filtered.columns:
        df_filtered['Temperature'] = df_filtered['Temperature'].str.replace('°F', '', regex=False).str.strip()

    if 'Humidity' in df_filtered.columns:
        df_filtered['Humidity'] = df_filtered['Humidity'].str.replace('°%', '', regex=False).str.replace('%', '', regex=False).str.strip()

    if 'Pressure' in df_filtered.columns:
        df_filtered['Pressure'] = df_filtered['Pressure'].str.replace('°in', '', regex=False).str.replace('in', '', regex=False).str.strip()

    if 'Precip. Rate.' in df_filtered.columns:
        df_filtered['Precip. Rate.'] = df_filtered['Precip. Rate.'].str.replace('°in', '', regex=False).str.replace('in', '', regex=False).str.strip()

    if 'Precip. Accum.' in df_filtered.columns:
        df_filtered['Precip. Accum.'] = df_filtered['Precip. Accum.'].str.replace('°in', '', regex=False).str.replace('in', '', regex=False).str.strip()

    # Create timestamp from date + Time
    if 'date' in df_filtered.columns and 'Time' in df_filtered.columns:
        df_filtered['timestamp'] = pd.to_datetime(
            df_filtered['date'] + ' ' + df_filtered['Time'],
            format='%Y-%m-%d %I:%M %p',
            errors='coerce'
        )

    # Convert numeric columns to float for calculations
    numeric_columns = ['Temperature', 'Humidity', 'Pressure', 'Precip. Rate.', 'Precip. Accum.']
    for col in numeric_columns:
        if col in df_filtered.columns:
            df_filtered[col] = pd.to_numeric(df_filtered[col], errors='coerce')

    # Calculate wet-bulb temperature
    if all(col in df_filtered.columns for col in ['Temperature', 'Humidity', 'Pressure']):
        df_filtered['Wet Bulb (F)'] = df_filtered.apply(
            lambda row: calculate_wet_bulb(row['Temperature'], row['Humidity'], row['Pressure']),
            axis=1
        )

    # Rename columns to include units (except those already calculated)
    column_mapping = {
        'Temperature': 'Temperature (F)',
        'Humidity': 'Humidity (%)',
        'Pressure': 'Pressure (in)',
        'Precip. Rate.': 'Precip. Rate (in/hr)',
        'Precip. Accum.': 'Precip. Accum (in)'
    }

    df_filtered = df_filtered.rename(columns=column_mapping)

    # Drop the original date and Time columns, keep timestamp
    columns_to_drop = ['date', 'Time']
    df_filtered = df_filtered.drop(columns=[col for col in columns_to_drop if col in df_filtered.columns])

    # Reorder columns to put timestamp first
    cols = df_filtered.columns.tolist()
    if 'timestamp' in cols:
        cols.remove('timestamp')
        cols = ['timestamp'] + cols
    if 'station_id' in cols:
        cols.remove('station_id')
        cols = ['station_id', 'timestamp'] + [col for col in cols if col not in ['station_id', 'timestamp']]
    df_filtered = df_filtered[cols]

    return df_filtered


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
    except Exception as e:
        # Return None for any calculation errors
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
            df['date'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S', errors='coerce').dt.date.astype(str)
            scraped_dates = set(df['date'].unique())

            # Find the earliest date
            earliest_date = min(scraped_dates)

            # Exclude earliest date from the set to ensure it gets re-scraped
            scraped_dates.discard(earliest_date)

            return scraped_dates, earliest_date
        else:
            return set(), None
    except Exception as e:
        print(f"  Warning: Error reading {station_file}: {e}")
        return set(), None


def scrape_station_backwards(station_id, start_date, delay=1):
    """
    Scrape weather data backwards from start_date until NO_DATA_THRESHOLD consecutive days with no data.
    Saves to station-specific CSV file and appends new data.

    Args:
        station_id: Weather Underground station ID
        start_date: Starting date string 'YYYY-MM-DD' (scrapes backwards from here)
        delay: Delay in seconds between requests (default 1)

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

        df = scrape_daily_data(station_id, date_str)

        if df is not None and len(df) > 0:
            # Clean and filter the data
            df_cleaned = clean_and_filter_data(df)

            # Special handling if this is the earliest date - remove old data first
            if os.path.exists(station_file) and date_str == existing_earliest_date:
                # Read existing data, remove rows for this date, then append new data
                try:
                    existing_df = pd.read_csv(station_file)
                    existing_df['date_only'] = pd.to_datetime(existing_df['timestamp'], format='%Y-%m-%d %H:%M:%S', errors='coerce').dt.date.astype(str)
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
                df_cleaned.to_csv(station_file, mode='a', header=False, index=False)
            else:
                # Create new file with header
                df_cleaned.to_csv(station_file, mode='w', header=True, index=False)

            days_scraped += 1
            consecutive_no_data = 0  # Reset counter on successful scrape

            # Track date range
            if earliest_date is None or date_str < earliest_date:
                earliest_date = date_str
            if latest_date is None or date_str > latest_date:
                latest_date = date_str

            print(f"✓ ({len(df_cleaned):,} observations)")
        else:
            consecutive_no_data += 1
            print(f"✗ No data (consecutive: {consecutive_no_data}/{NO_DATA_THRESHOLD})")

        # Be respectful to the server - add delay between requests
        time.sleep(delay)

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
            df['date'] = pd.to_datetime(df['timestamp'], format='%Y-%m-%d %H:%M:%S', errors='coerce').dt.date
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


def initialize_checkpoint(stations_df):
    """
    Add any WU stations not yet in the unified metadata file, in 'pending' status.
    Includes geographic data (lat, lon, elevation_ft) from the station list CSV.

    Args:
        stations_df: DataFrame of new stations to add (with station_id, latitude, longitude, elevation columns)
    """
    try:
        if os.path.exists(METADATA_FILE):
            existing_df = pd.read_csv(METADATA_FILE)
            already_tracked = set(existing_df[existing_df['source'] == 'WU']['station_id'].tolist())
            new_stations = stations_df[~stations_df['station_id'].isin(already_tracked)]
        else:
            existing_df = pd.DataFrame(columns=['source', 'station_id', 'name', 'mac_address',
                                                 'earliest_date', 'latest_date',
                                                 'last_scraped_date', 'total_days', 'total_observations',
                                                 'latitude', 'longitude', 'elevation_ft'])
            new_stations = stations_df

        if len(new_stations) == 0:
            return

        new_rows = pd.DataFrame({
            'source': 'WU',
            'station_id': new_stations['station_id'].values,
            'name': None,
            'mac_address': None,
            'earliest_date': '',
            'latest_date': '',
            'last_scraped_date': '',
            'total_days': 0,
            'total_observations': 0,
            'latitude': new_stations['latitude'].values if 'latitude' in new_stations.columns else None,
            'longitude': new_stations['longitude'].values if 'longitude' in new_stations.columns else None,
            'elevation_ft': new_stations['elevation'].values if 'elevation' in new_stations.columns else None,
        })
        combined = pd.concat([existing_df, new_rows], ignore_index=True)
        combined.to_csv(METADATA_FILE, index=False)
        print(f"Added {len(new_stations):,} WU stations to metadata file")
    except Exception as e:
        print(f"Warning: Error updating metadata file: {e}")


def get_completed_stations():
    """
    Read the metadata file and return WU station IDs that have completed historical scraping.

    Returns:
        Set of WU station IDs that are complete
    """
    if not os.path.exists(METADATA_FILE):
        return set()

    try:
        df = pd.read_csv(METADATA_FILE)
        wu_df = df[df['source'] == 'WU']
        if 'status' not in wu_df.columns:
            return set()
        completed = set(wu_df[wu_df['status'] == 'complete']['station_id'].tolist())
        return completed
    except Exception as e:
        print(f"Warning: Error reading metadata file: {e}")
        return set()


def checkpoint_sync():
    """
    Sync checkpoint file with actual station CSV files at startup.

    IMPORTANT: This function does NOT automatically mark stations as complete.
    Stations are only marked complete when scraping naturally finishes (hits
    the NO_DATA_THRESHOLD consecutive days with no data).

    This function only updates metadata (date ranges, observation counts) for
    stations that are already marked as complete, to handle cases where the
    checkpoint file update failed due to file locking during a previous run.

    Raises:
        Exception: If checkpoint file cannot be written (e.g., locked by Excel).
    """
    print("\n" + "="*60)
    print("CHECKPOINT SYNC - Verifying checkpoint against station data")
    print("="*60)

    # Check if output directory exists
    if not os.path.exists(OUTPUT_DIR):
        print("No station data found yet. Skipping sync.\n")
        return

    # Get all station CSV files
    station_files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.csv')]

    if not station_files:
        print("No station CSV files found. Skipping sync.\n")
        return

    print(f"Found {len(station_files):,} station CSV file(s)")

    # Read metadata file
    if os.path.exists(METADATA_FILE):
        metadata_df = pd.read_csv(METADATA_FILE)
    else:
        print("No metadata file found. Stations will be marked pending on first run.\n")
        return

    updates_needed = []

    # Check each station CSV — only consider WU rows
    for station_file in station_files:
        station_id = station_file.replace('.csv', '')

        wu_mask = (metadata_df['source'] == 'WU') & (metadata_df['station_id'] == station_id)
        if wu_mask.any():
            # Verify metadata is up to date
            earliest, latest, last_scraped, total_days, total_obs = get_station_metadata(station_id)
            current_obs = metadata_df.loc[wu_mask, 'total_observations'].values[0]

            # Update if metadata changed
            if earliest and latest and total_obs != current_obs:
                updates_needed.append((station_id, earliest, latest, last_scraped, total_days, total_obs))

    if not updates_needed:
        print("✓ Metadata file is in sync with station data\n")
        return

    # Updates needed - try to update metadata
    print(f"\n{len(updates_needed):,} completed WU station(s) need metadata updates")
    print("Updating metadata...")

    try:
        for station_id, earliest, latest, last_scraped, total_days, total_obs in updates_needed:
            wu_mask = (metadata_df['source'] == 'WU') & (metadata_df['station_id'] == station_id)
            metadata_df.loc[wu_mask, 'earliest_date'] = earliest
            metadata_df.loc[wu_mask, 'latest_date'] = latest
            metadata_df.loc[wu_mask, 'last_scraped_date'] = last_scraped
            metadata_df.loc[wu_mask, 'total_days'] = total_days
            metadata_df.loc[wu_mask, 'total_observations'] = total_obs
            print(f"  ✓ Updated {station_id}: {earliest} to {latest} ({total_obs:,} obs)")

        # Try to write the metadata file - ERROR OUT if locked
        metadata_df.to_csv(METADATA_FILE, index=False)
        print(f"\n✓ Successfully updated metadata file")
        print("="*60 + "\n")

    except PermissionError as e:
        # File is locked (likely open in Excel) - ERROR OUT
        print(f"\n{'='*60}")
        print("ERROR: METADATA FILE IS LOCKED")
        print(f"{'='*60}")
        print(f"Found {len(updates_needed):,} station(s) that need metadata updates,")
        print(f"but cannot write to metadata file (likely open in Excel).")
        print(f"\nPlease close '{METADATA_FILE}' and restart the script.")
        print(f"{'='*60}\n")
        raise Exception(f"Cannot update metadata file - file is locked: {e}")
    except Exception as e:
        # Other error - also ERROR OUT during sync
        print(f"\n{'='*60}")
        print("ERROR: FAILED TO UPDATE METADATA")
        print(f"{'='*60}")
        print(f"Error: {e}")
        print(f"{'='*60}\n")
        raise


def clean_station_data(station_id):
    """
    Remove rows from a station's CSV that don't have temperature or humidity data.
    These are the two most critical fields for analysis.

    Args:
        station_id: Station ID to clean

    Returns:
        Tuple of (rows_before, rows_after) or (0, 0) if error
    """
    station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")

    if not os.path.exists(station_file):
        return 0, 0

    try:
        df = pd.read_csv(station_file)
        rows_before = len(df)

        # Drop rows where EITHER Temperature OR Humidity is missing
        # (We need both to calculate wet bulb temperature)
        if 'Temperature (F)' in df.columns and 'Humidity (%)' in df.columns:
            df = df.dropna(subset=['Temperature (F)', 'Humidity (%)'], how='any')
            rows_after = len(df)

            # Write back the cleaned data
            df.to_csv(station_file, index=False)

            if rows_before != rows_after:
                print(f"  Cleaned data: Removed {rows_before - rows_after:,} rows without temp/humidity")

            return rows_before, rows_after
        else:
            return rows_before, rows_before

    except Exception as e:
        print(f"  Warning: Error cleaning {station_file}: {e}")
        return 0, 0


def mark_station_complete(station_id):
    """
    Update the unified metadata file to mark a WU station as complete.

    Args:
        station_id: Station ID to mark as complete
    """
    try:
        # Clean the station data first (remove rows without temp/humidity)
        clean_station_data(station_id)

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

        # Update or add this station's row (WU only)
        # Static columns (lat/lon/elevation_ft/name/mac_address) are set at init and not touched here
        wu_mask = (metadata_df['source'] == 'WU') & (metadata_df['station_id'] == station_id)
        if wu_mask.any():
            metadata_df.loc[wu_mask, 'earliest_date'] = earliest_date or ''
            metadata_df.loc[wu_mask, 'latest_date'] = latest_date or ''
            metadata_df.loc[wu_mask, 'last_scraped_date'] = last_scraped_date or ''
            metadata_df.loc[wu_mask, 'total_days'] = total_days
            metadata_df.loc[wu_mask, 'total_observations'] = total_observations
        else:
            new_row = pd.DataFrame([{
                'source': 'WU',
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

        # Save — preserves AWN rows unchanged
        metadata_df.to_csv(METADATA_FILE, index=False)

    except Exception as e:
        print(f"Warning: Error updating checkpoint file: {e}")


def mark_station_extinct(station_id):
    """
    Flag a WU station as extinct in the metadata file.
    Called when a station has had no data for EXTINCT_THRESHOLD+ consecutive days.
    The flag can be manually cleared by setting 'extinct' to False in the CSV.
    """
    try:
        if not os.path.exists(METADATA_FILE):
            return
        metadata_df = pd.read_csv(METADATA_FILE)
        if 'extinct' not in metadata_df.columns:
            metadata_df['extinct'] = False
        wu_mask = (metadata_df['source'] == 'WU') & (metadata_df['station_id'] == station_id)
        if wu_mask.any():
            metadata_df.loc[wu_mask, 'extinct'] = True
            metadata_df.to_csv(METADATA_FILE, index=False)
            print(f"  !! {station_id} marked extinct ({EXTINCT_THRESHOLD}+ days with no data)")
    except Exception as e:
        print(f"Warning: Error marking station extinct: {e}")


def revive_station(station_id):
    """
    Clear a WU station's extinct flag after a successful probe revival.
    Called from probe_extinct_stations() when yesterday's fetch returns data
    for a previously-extinct station.
    """
    try:
        if not os.path.exists(METADATA_FILE):
            return
        metadata_df = pd.read_csv(METADATA_FILE)
        if 'extinct' not in metadata_df.columns:
            return
        wu_mask = (metadata_df['source'] == 'WU') & (metadata_df['station_id'] == station_id)
        if wu_mask.any():
            metadata_df.loc[wu_mask, 'extinct'] = False
            metadata_df.to_csv(METADATA_FILE, index=False)
            print(f"  ✓✓ {station_id} revived — yesterday returned data")
    except Exception as e:
        print(f"Warning: Error reviving station: {e}")


def probe_extinct_stations(yesterday, label):
    """
    Weekly Lazarus probe: try fetching yesterday's data for every extinct WU
    station.  If the API returns observations, the row is written to the
    station CSV by scrape_station_forward(), then mark_station_complete()
    syncs metadata and revive_station() clears the extinct flag so tomorrow's
    normal run picks the station back up and fills any remaining gap.

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

    extinct_mask = (
        (metadata_df['source'] == 'WU') &
        metadata_df['extinct'].apply(lambda v: str(v).strip().lower() == 'true')
    )
    extinct_ids = metadata_df.loc[extinct_mask, 'station_id'].dropna().tolist()
    if not extinct_ids:
        return

    total = len(extinct_ids)
    print(f"{label}  Weekly probe — testing {total:,} extinct WU station(s) for {yesterday}")
    revived = 0
    for i, station_id in enumerate(extinct_ids, 1):
        prefix = f"  [probe {i:4d}/{total}]  {station_id}"
        try:
            days_scraped, _ = scrape_station_forward(
                station_id, yesterday, yesterday, live_prefix=prefix,
            )
            if days_scraped > 0:
                print()
                mark_station_complete(station_id)
                revive_station(station_id)
                revived += 1
        except Exception as e:
            print(f"\n{prefix}  probe FAILED: {e}")

    print(f"\n{label}  Probe done: {revived:,}/{total:,} station(s) revived")


def scrape_single_day(station_id, date_str):
    """
    Scrape one specific day for a WU station and append it if not already present.
    Returns True if data was found (including if it was already present).
    Used by run_loop() for daily catch-up once historical scraping is complete.
    """
    scraped_dates, _ = get_already_scraped_dates(station_id)
    if date_str in scraped_dates:
        return True
    df = scrape_daily_data(station_id, date_str)
    if df is not None and len(df) > 0:
        df_cleaned = clean_and_filter_data(df)
        station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
        mode, header = ('a', False) if os.path.exists(station_file) else ('w', True)
        df_cleaned.to_csv(station_file, mode=mode, header=header, index=False)
        return True
    return False


def seconds_until_next_fire():
    """Return seconds until the next LOOP_HOUR:00."""
    now = datetime.now()
    next_fire = now.replace(hour=LOOP_HOUR, minute=0, second=0, microsecond=0)
    if now >= next_fire:
        next_fire += timedelta(days=1)
    return (next_fire - now).total_seconds()


def scrape_station_forward(station_id, from_date_str, to_date_str, live_prefix=None):
    """
    Forward-scrape all missing dates for station_id from from_date_str through
    to_date_str (both inclusive).  Dates already present in the station CSV are
    skipped.  New rows are appended to the CSV.

    When live_prefix is provided, progress overwrites a single terminal line
    (loop mode).  Otherwise the original verbose per-date output is used.
    Returns (days_scraped, latest_date_str_or_None).
    """
    station_file = os.path.join(OUTPUT_DIR, f"{station_id}.csv")
    already_scraped, _ = get_already_scraped_dates(station_id)

    from_dt    = datetime.strptime(from_date_str, '%Y-%m-%d')
    to_dt      = datetime.strptime(to_date_str,   '%Y-%m-%d')
    total_days = (to_dt - from_dt).days + 1
    PAD        = " " * 30

    days_scraped = 0
    latest_date  = None
    current      = from_dt
    j            = 0

    while current <= to_dt:
        j       += 1
        date_str = current.strftime('%Y-%m-%d')

        if date_str not in already_scraped:
            if live_prefix:
                print(f"{live_prefix}  [{j:,}/{total_days:,}]  {date_str}{PAD}", end='\r', flush=True)
            else:
                print(f"    {date_str}: Scraping...", end=' ', flush=True)
            df = scrape_daily_data(station_id, date_str)
            if df is not None and len(df) > 0:
                df_cleaned = clean_and_filter_data(df)
                # Trailing newline guard: prevents row-merge when appending
                if os.path.exists(station_file):
                    with open(station_file, 'rb') as _f:
                        _f.seek(0, 2)
                        if _f.tell() > 0:
                            _f.seek(-1, 2)
                            if _f.read(1) != b'\n':
                                with open(station_file, 'ab') as _fw:
                                    _fw.write(b'\n')
                    mode, header = 'a', False
                else:
                    mode, header = 'w', True
                df_cleaned.to_csv(station_file, mode=mode, header=header, index=False)
                already_scraped.add(date_str)
                days_scraped += 1
                latest_date = date_str
                if not live_prefix:
                    print(f"✓ ({len(df_cleaned):,} obs)")
            else:
                if not live_prefix:
                    print("✗ no data")
            time.sleep(random.uniform(1.0, 1.2))

        current += timedelta(days=1)

    return days_scraped, latest_date


def run_loop():
    """
    Catch-up and daily maintenance loop for WU.

    Each nightly cycle (fires at LOOP_HOUR):
      1. Reads all WU stations from metadata and sorts them by latest_date
         ascending — the most behind station is processed first.  Stations
         with no data at all are placed at the end.
      2. For each station, forward-scrapes every missing date from
         (latest_date + 1) through yesterday.
      3. Updates metadata after each station and sleeps until the next
         LOOP_HOUR once all stations have been processed.

    During the initial weeks of catch-up each cycle will take many hours.
    Once every station reaches yesterday the nightly cycle adds only one
    day per station and completes quickly.
    """
    print(f"Loop mode active — firing daily at {LOOP_HOUR:02d}:00.  Press Ctrl+C to stop.\n")

    first_run = True
    while True:
        # ── Wait until the next firing time ───────────────────────────────
        if first_run:
            first_run = False
        else:
            wait      = seconds_until_next_fire()
            next_fire = datetime.fromtimestamp(time.time() + wait)
            _d = next_fire.strftime("%m/%d/%y").lstrip("0").replace("/0", "/")
            print(f"  Waiting... next run at {LOOP_HOUR:02d}:00 on {next_fire.strftime('%A')}, {_d}    ",
                  end="\r", flush=True)
            time.sleep(wait)

        cycle_start  = time.time()
        yesterday_dt = (datetime.now() - timedelta(days=1)).date()
        yesterday    = yesterday_dt.strftime('%Y-%m-%d')
        label        = datetime.now().strftime("[%a %H:%M]")
        print(f"\n{label}  WU catch-up run — target: {yesterday}")

        # ── Weekly Lazarus probe (Sundays) ─────────────────────────────────
        # Revive any extinct station whose sensor has come back online.
        # Runs before the metadata load so freshly-revived rows flow naturally
        # into the main rotation below.
        if datetime.now().weekday() == PROBE_WEEKDAY:
            probe_extinct_stations(yesterday, label)

        # ── Load and sort stations ─────────────────────────────────────────
        try:
            metadata_df = pd.read_csv(METADATA_FILE)
            wu_rows = metadata_df[metadata_df['source'] == 'WU'].copy()
        except Exception as e:
            print(f"{label}  ERROR reading metadata: {e}")
            continue

        def _parse_date(s):
            """Parse M/D/YY or YYYY-MM-DD date strings; return date or None."""
            if pd.isna(s) or str(s).strip() == '':
                return None
            for fmt in ('%m/%d/%y', '%Y-%m-%d', '%m/%d/%Y'):
                try:
                    return datetime.strptime(str(s).strip(), fmt).date()
                except ValueError:
                    continue
            return None

        wu_rows['_latest'] = wu_rows['latest_date'].apply(_parse_date)

        # ── Sync _latest from actual CSV files ────────────────────────────
        # If the scraper was killed mid-station, mark_station_complete() never
        # ran, so metadata latest_date is stale.  Read each station's CSV max
        # date and use whichever is newer so the sort order is accurate.
        for idx_row in wu_rows.index:
            sid = wu_rows.loc[idx_row, 'station_id']
            if pd.isna(sid):
                continue
            station_file = os.path.join(OUTPUT_DIR, f"{sid}.csv")
            if not os.path.exists(station_file):
                continue
            _, csv_latest, _, _, _ = get_station_metadata(sid)
            if not csv_latest:
                continue
            try:
                csv_latest_dt = datetime.strptime(csv_latest, '%Y-%m-%d').date()
            except ValueError:
                continue
            meta_latest = wu_rows.loc[idx_row, '_latest']
            if meta_latest is None or csv_latest_dt > meta_latest:
                wu_rows.loc[idx_row, '_latest'] = csv_latest_dt

        # Skip stations already marked extinct
        if 'extinct' in wu_rows.columns:
            wu_rows = wu_rows[
                wu_rows['extinct'].apply(lambda v: str(v).strip().lower() != 'true')
            ].copy()

        # Stations with a known latest_date sorted oldest-first, then no-data stations
        has_data  = wu_rows[wu_rows['_latest'].notna()].sort_values('_latest')
        no_data   = wu_rows[wu_rows['_latest'].isna()]
        wu_sorted = pd.concat([has_data, no_data], ignore_index=True)

        station_ids = wu_sorted['station_id'].dropna().tolist()
        total       = len(station_ids)

        already_current = 0
        updated         = []

        print(f"{label}  {total:,} stations — most-behind first")

        # ── Process each station ───────────────────────────────────────────
        for i, station_id in enumerate(station_ids, 1):
            latest = wu_sorted.loc[wu_sorted['station_id'] == station_id, '_latest'].values[0]
            prefix = f"  [{i:4d}/{total}]  {station_id}"
            PAD    = " " * 30

            # Nothing to do if station is already current
            if latest is not None and latest >= yesterday_dt:
                already_current += 1
                print(f"{prefix}  — already current{PAD}", end='\r', flush=True)
                continue

            if latest is None:
                from_str = yesterday
                days_gap = 1
            else:
                from_str = (datetime.combine(latest, datetime.min.time())
                            + timedelta(days=1)).strftime('%Y-%m-%d')
                days_gap = (yesterday_dt - latest).days

            try:
                days_scraped, _ = scrape_station_forward(station_id, from_str, yesterday,
                                                         live_prefix=prefix)
                if days_scraped > 0:
                    mark_station_complete(station_id)
                    updated.append(station_id)
                    if days_gap == 1:
                        print(f"{prefix}  ✓{PAD}", end='\r', flush=True)
                    else:
                        print(f"{prefix}  ✓  {days_scraped:,}/{days_gap:,} days{PAD}", end='\r', flush=True)
                elif latest is not None and days_gap >= EXTINCT_THRESHOLD:
                    print()  # end the \r line before the extinct message
                    mark_station_extinct(station_id)
                else:
                    if days_gap == 1:
                        print(f"{prefix}  — no data{PAD}", end='\r', flush=True)
                    else:
                        print(f"{prefix}  — no data ({days_gap:,} dates){PAD}", end='\r', flush=True)
            except KeyboardInterrupt:
                print(f"\n{label}  Interrupted — progress saved.")
                return
            except Exception as e:
                print(f"\n{prefix}  FAILED: {e}")

        elapsed = int(time.time() - cycle_start)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        dur_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
        print(f"{label}  Done: {len(updated):,} updated, {already_current:,} already current  ({dur_str})")


def main():
    """Main function to scrape historical data backwards for all configured stations."""
    print("="*60)
    print("Weather Underground Historical Data Scraper")
    print("WINDOWS VERSION - External Hard Drive Storage")
    print("="*60)

    # Verify external drive is accessible
    if not os.path.exists(EXTERNAL_DRIVE):
        print(f"\n{'='*60}")
        print("ERROR: EXTERNAL DRIVE NOT ACCESSIBLE")
        print(f"{'='*60}")
        print(f"The specified external drive path does not exist:")
        print(f"  {EXTERNAL_DRIVE}")
        print(f"\nPlease:")
        print(f"  1. Verify the external drive is connected")
        print(f"  2. Update the EXTERNAL_DRIVE variable at the top of this script")
        print(f"  3. Restart the script")
        print(f"{'='*60}\n")
        return

    print(f"\n✓ External drive accessible: {EXTERNAL_DRIVE}")
    print(f"✓ Station data directory: {OUTPUT_DIR}")
    print(f"✓ Metadata file: {METADATA_FILE}")

    # Show chart-maker status
    print("✓ Automatic chart generation: ENABLED")
    print()

    # # Sync checkpoint with actual station data at startup
    # # This will ERROR OUT if discrepancies found and checkpoint file is locked
    # checkpoint_sync()

    # Load WU station list from unified metadata file
    if not os.path.exists(METADATA_FILE):
        print(f"\nError: {METADATA_FILE} not found!")
        print("  Ensure station_metadata.csv exists in the metadata directory.")
        return

    try:
        metadata_df = pd.read_csv(METADATA_FILE)
        wu_df = metadata_df[
            (metadata_df['source'] == 'WU') &
            metadata_df['station_id'].notna()
        ]
        station_ids = wu_df['station_id'].unique().tolist()

        # Randomize the order to get better geographic coverage early on
        random.shuffle(station_ids)

        print(f"\nStart date: {START_DATE} (scraping backwards)")
        print(f"Stop condition: {NO_DATA_THRESHOLD} consecutive days with no data")
        print(f"Output directory: {OUTPUT_DIR}")
        print(f"Total stations: {len(station_ids):,}")

    except Exception as e:
        print(f"Error reading {METADATA_FILE}: {e}")
        return

    # Check which stations are already complete
    completed_stations = get_completed_stations()
    remaining_stations = [sid for sid in station_ids if sid not in completed_stations]

    # Check for interrupted stations (CSV exists but status is still 'pending')
    existing_csvs = set()
    if os.path.exists(OUTPUT_DIR):
        csv_files = [f for f in os.listdir(OUTPUT_DIR)
                     if f.endswith('.csv')]
        existing_csvs = {f.replace('.csv', '') for f in csv_files}

    # Split remaining into interrupted (have CSV) and not-yet-started (no CSV)
    interrupted_stations = [sid for sid in remaining_stations if sid in existing_csvs]
    not_started_stations = [sid for sid in remaining_stations if sid not in existing_csvs]

    # Concatenate: interrupted stations first, then not-yet-started
    remaining_stations = interrupted_stations + not_started_stations

    print(f"Completed stations: {len(completed_stations):,}")
    print(f"Remaining stations: {len(remaining_stations):,}")
    if len(interrupted_stations) > 0:
        print(f"  ⚡ {len(interrupted_stations):,} interrupted station(s) will be completed first")
    if len(not_started_stations) > 0:
        print(f"  📋 {len(not_started_stations):,} not-yet-started station(s)")
    print(f"Metadata file: {METADATA_FILE}")

    if not remaining_stations:
        print("\n✓ All stations already complete!")
        return


    print(f"\n{'='*60}\n")

    # Scrape each remaining station backwards
    total_days_scraped = 0
    stations_completed_this_run = 0

    for idx, station_id in enumerate(remaining_stations, 1):
        print(f"[{idx:,}/{len(remaining_stations):,}] Processing: {station_id}")
        print(f"{'='*60}")

        try:
            days_scraped, earliest_date, latest_date = scrape_station_backwards(
                station_id,
                START_DATE,
                delay=1
            )

            total_days_scraped += days_scraped

            # Mark this station as complete
            mark_station_complete(station_id)
            stations_completed_this_run += 1

            # Get and display metadata
            earliest, latest, last_scraped, total_days, total_obs = get_station_metadata(station_id)
            if earliest and latest:
                print(f"✓ Station {station_id} marked as complete")
                print(f"  Date range: {earliest} to {latest} ({total_days:,} days)")
                print(f"  Total observations: {total_obs:,}\n")
            else:
                print(f"✓ Station {station_id} marked as complete\n")


        except Exception as e:
            print(f"✗ Error processing {station_id}: {e}")
            print(f"  Continuing to next station...\n")
            continue

    # Final summary
    print("="*60)
    print("SCRAPING SESSION COMPLETE")
    print("="*60)
    print(f"Stations completed this run: {stations_completed_this_run:,}")
    print(f"Total days scraped: {total_days_scraped:,}")
    print(f"Total stations now complete: {len(completed_stations) + stations_completed_this_run:,}/{len(station_ids):,}")

    # Calculate summary statistics for all completed WU stations
    metadata_df = pd.read_csv(METADATA_FILE)
    completed_df = metadata_df[
        (metadata_df['source'] == 'WU') &
        (metadata_df['total_observations'].fillna(0) > 0)
    ]
    total_stations_complete = len(completed_df)
    total_observations = completed_df['total_observations'].sum()
    avg_days_per_station = completed_df['total_days'].mean() if total_stations_complete > 0 else 0

    print(f"\nStations Scraped: {total_stations_complete:,} | Total Observations: {total_observations:,.0f} | Average Days per Station: {avg_days_per_station:,.0f}")

    print(f"\nOutput directory: {OUTPUT_DIR}")
    print(f"Metadata file: {METADATA_FILE}")
    print(f"  (View this CSV for detailed metadata on each station)")
    print("="*60)


if __name__ == '__main__':
    if '--loop' in sys.argv:
        run_loop()
    else:
        main()
