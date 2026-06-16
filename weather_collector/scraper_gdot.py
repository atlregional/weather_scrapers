"""
GDOT Road Weather Station scraper
==================================
Queries the ArcGIS FeatureServer for all 57 GDOT road-weather stations
in a single request and appends one row per station to its own CSV.

Data refreshes every 30 minutes; this script fires at :15 and :45 past
each hour to give late-reporting stations (observed up to ~14 min after
the nominal update time) time to post.

Usage:
    python scraper_gdot.py          # single run
    python scraper_gdot.py --loop   # continuous, Ctrl+C to stop

Output:
    One CSV per station in STATIONS_DIR (see configuration below).
    Filenames use the NAME field (e.g. I75_HudsonBridgeRoad.csv).

Column schema per CSV:
    station_id, station_name, timestamp (Eastern, naive),
    Temperature (F), Dew Point (F), Wind Direction,
    Wind Speed (mph), Wind Gust, Visibility,
    Road Temperature (F), Subsurface Temperature (F),
    Road State, latitude, longitude
"""

import csv
import html
import logging
import math
import os
import sys
import time
from datetime import datetime, date

import pytz
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUERY_URL = (
    "https://services1.arcgis.com/2iUE8l8JKrP2tygQ/arcgis/rest/services"
    "/GDOT_Road_Weather_Stations/FeatureServer/0/query"
)
QUERY_PARAMS = {
    "where":             "1=1",
    "outFields":         "*",
    "resultRecordCount": 2000,
    "f":                 "json",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EASTERN    = pytz.timezone("US/Eastern")

# ---------------------------------------------------------------------------
# EXTERNAL HARD DRIVE CONFIGURATION
# ---------------------------------------------------------------------------
# UPDATE THIS to your external hard drive root (e.g. "E:\\", "F:\\", "D:\\")
# Must match scraper_wu.py, scraper_awn.py, scraper_uga.py, and generate_update.py
EXTERNAL_DRIVE = "D:\\Weather\\"  # Windows
# EXTERNAL_DRIVE = "/Volumes/Extreme Pro/Weather/" # Mac -> for testing

STATIONS_DIR  = os.path.join(EXTERNAL_DRIVE, 'station-data', 'GDOT')
METADATA_FILE = os.path.join(EXTERNAL_DRIVE, 'metadata', 'station_metadata_gdot.csv')
# ---------------------------------------------------------------------------

# 11-county metro Atlanta stations to keep (station IDs from ArcGIS ID field)
KEEP_IDS = {
    10878,  # GA400_PittsRoad
    10887,  # GA400_SR140_HolcombBridgeRoad
    10889,  # GA400_SR20
    11665,  # I20_Klondike_WestAve
    10870,  # I285_MorelandAve
    10872,  # I285_SR8_DL_HollowellPkwy
    10869,  # I285_WashingtonRoad
    12490,  # I575_SR20
    10891,  # I75_10thStreet
    10864,  # I75_HudsonBridgeRoad
    10892,  # I75_SR120_SouthMariettaPkwy
    10886,  # I75_SR92
    10893,  # I85_JimmyCarterBlvd
    10885,  # I85_OldPeachtreeRoad
    10863,  # I85_SR74_SenoiaRoad
    10881,  # US78_MountainIndustrialBlvd
}

CSV_COLUMNS = [
    "station_id",
    "timestamp",
    "Temperature (F)",
    "Humidity (%)",           # derived from T + Dew Point via Magnus formula
    "Pressure (in)",          # not available from GDOT — blank
    "Precip. Rate (in/hr)",   # not available from GDOT — blank
    "Precip. Accum (in)",     # not available from GDOT — blank
    "Wet Bulb (F)",           # derived via Stull (2011)
]

INTERVAL_MIN      = 30
OFFSET_MIN        = 15          # fire at :15 and :45
RUNS_PER_HOUR     = 60 // INTERVAL_MIN   # = 2
EXTINCT_THRESHOLD = 30          # days with no new data before marking extinct

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GDOT-scraper/1.0)"}
TIMEOUT = 30

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_stations() -> list[dict]:
    """Query the FeatureServer and return a list of attribute dicts."""
    resp = requests.get(QUERY_URL, params=QUERY_PARAMS, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return [f["attributes"] for f in data.get("features", [])]


def obstime_to_eastern(epoch_ms) -> str | None:
    """Convert ArcGIS epoch-milliseconds (UTC) to a naive Eastern datetime string."""
    if epoch_ms is None:
        return None
    utc_dt = datetime.utcfromtimestamp(epoch_ms / 1000).replace(tzinfo=pytz.utc)
    et_dt  = utc_dt.astimezone(EASTERN)
    return et_dt.strftime("%Y-%m-%d %H:%M:%S")


def rh_from_t_and_dp(t_f, dp_f):
    """
    Estimate relative humidity (%) from air temp and dew point (both °F).
    Uses the Magnus formula via Celsius conversion.  Returns None if inputs missing.
    """
    if t_f is None or dp_f is None:
        return None
    t_c  = (t_f  - 32) * 5 / 9
    dp_c = (dp_f - 32) * 5 / 9
    rh = 100 * math.exp(17.625 * dp_c / (243.04 + dp_c)) / \
               math.exp(17.625 * t_c  / (243.04 + t_c))
    return round(min(max(rh, 0), 100), 2)


def wet_bulb_f(t_f, rh):
    """
    Wet bulb temperature (°F) via Stull (2011), same formula as UGA pipeline.
    t_f: air temp in °F; rh: relative humidity in %.
    Returns None if inputs missing.
    """
    if t_f is None or rh is None:
        return None
    t_c = (t_f - 32) * 5 / 9
    wb_c = (
        t_c * math.atan(0.151977 * (rh + 8.313659) ** 0.5)
        + math.atan(t_c + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
        - 4.686035
    )
    return round(wb_c * 9 / 5 + 32, 4)


def clean_str(value) -> str:
    """Unescape HTML entities and strip whitespace from string fields."""
    if value is None:
        return ""
    return html.unescape(str(value)).strip()


def station_csv_path(name: str) -> str:
    return os.path.join(STATIONS_DIR, f"{name}.csv")


def last_timestamp(csv_path: str) -> str | None:
    """Return the timestamp of the last row in the CSV, or None."""
    if not os.path.exists(csv_path):
        return None
    with open(csv_path, newline="") as f:
        last = None
        for row in csv.DictReader(f):
            last = row.get("timestamp")
    return last


def append_row(csv_path: str, row: dict) -> None:
    """Append a row to the station CSV, writing the header if new."""
    write_header = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# Metadata update (once per calendar day)
# ---------------------------------------------------------------------------

def csv_line_count(csv_path: str) -> int:
    """Return number of data rows (excluding header) without loading into memory."""
    with open(csv_path, "rb") as f:
        return sum(1 for _ in f) - 1  # subtract header


def update_metadata_if_needed() -> None:
    """
    Update GDOT rows in station_metadata.csv with current stats.
    Runs only once per calendar day (keyed on last_scraped_date).
    No-op if the metadata file doesn't exist or has no GDOT rows.
    """
    if not os.path.exists(METADATA_FILE):
        log.warning("Metadata file not found, skipping update: %s", METADATA_FILE)
        return

    today_str = date.today().strftime("%m/%d/%y").lstrip("0").replace("/0", "/")

    rows = []
    with open(METADATA_FILE, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    # Ensure 'extinct' column exists in the file schema
    if 'extinct' not in fieldnames:
        fieldnames.append('extinct')
        for r in rows:
            r.setdefault('extinct', 'False')

    gdot_rows = [r for r in rows if r.get("source") == "GDOT"]
    if not gdot_rows:
        return  # no GDOT stations in metadata yet
    if all(r.get("last_scraped_date") == today_str for r in gdot_rows):
        return  # already updated today

    # Build station_id → csv_path by reading the first data row of each CSV.
    # This is robust regardless of how the name field is spelled in metadata.
    sid_to_csv: dict[str, str] = {}
    for fname in os.listdir(STATIONS_DIR):
        if not fname.endswith(".csv"):
            continue
        fpath = os.path.join(STATIONS_DIR, fname)
        try:
            with open(fpath, newline="") as _f:
                first = next(csv.DictReader(_f), None)
                if first and first.get("station_id"):
                    sid_to_csv[str(first["station_id"])] = fpath
        except Exception:
            pass

    for row in rows:
        if row.get("source") != "GDOT":
            continue
        csv_path = sid_to_csv.get(str(row.get("station_id", "")))
        if not csv_path:
            continue

        ts = last_timestamp(csv_path)
        if ts:
            try:
                latest_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                row["latest_date"] = latest_dt.strftime("%m/%d/%y").lstrip("0").replace("/0", "/")
            except ValueError:
                pass

        try:
            earliest = datetime.strptime(row["earliest_date"], "%m/%d/%y")
            latest   = datetime.strptime(row["latest_date"],   "%m/%d/%y")
            row["total_days"] = str((latest - earliest).days + 1)
        except (ValueError, KeyError):
            pass

        row["total_observations"] = str(csv_line_count(csv_path))
        row["last_scraped_date"]  = today_str

        # Extinction flag: set when data is stale, cleared when fresh data returns.
        # Since GDOT hits all stations in one API call, a revived station's new
        # rows already landed in the CSV above, so latest_date is fresh again.
        try:
            latest_date_val = row.get("latest_date", "")
            total_obs = int(row.get("total_observations", 0) or 0)
            if latest_date_val and total_obs > 0:
                latest_dt_check = datetime.strptime(latest_date_val, "%m/%d/%y")
                today_dt        = datetime.strptime(today_str, "%m/%d/%y")
                days_stale      = (today_dt - latest_dt_check).days
                was_extinct     = str(row.get("extinct", "")).strip().lower() == "true"

                if days_stale >= EXTINCT_THRESHOLD and not was_extinct:
                    row["extinct"] = "True"
                    log.warning(
                        "Station %s marked extinct (%d days with no data) — "
                        "scraper continues for all other stations",
                        row.get("station_id"), days_stale,
                    )
                    print(
                        f"  [EXTINCT] Station {row.get('station_id')} has had no new data "
                        f"for {days_stale} days and is now marked extinct. "
                        f"All other stations continue normally."
                    )
                elif days_stale < EXTINCT_THRESHOLD and was_extinct:
                    row["extinct"] = "False"
                    log.info(
                        "Station %s revived (fresh data after extinction) — "
                        "extinct flag cleared",
                        row.get("station_id"),
                    )
                    print(
                        f"  [REVIVED] Station {row.get('station_id')} is reporting "
                        f"again — extinct flag cleared."
                    )
        except (ValueError, KeyError, TypeError):
            pass

    with open(METADATA_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    log.info("Metadata updated for %d GDOT station(s)", len(gdot_rows))


# ---------------------------------------------------------------------------
# Per-run scrape logic
# ---------------------------------------------------------------------------

def scrape_all() -> dict:
    """
    Fetch all stations, write new rows. Returns {station_name: True/False}.
    """
    features = fetch_stations()
    results  = {}

    for attrs in features:
        if attrs.get("ID") not in KEEP_IDS:
            continue
        name = attrs.get("NAME", "UNKNOWN").strip()
        try:
            timestamp = obstime_to_eastern(attrs.get("OBSTIME"))
            csv_path  = station_csv_path(name)

            # Duplicate guard
            if last_timestamp(csv_path) == timestamp:
                log.debug("%s  duplicate %s — skipped", name, timestamp)
                results[name] = True   # not an error, data just hasn't updated
                continue

            t_f  = attrs.get("TMPF")
            if t_f is None:
                log.debug("%s  no temperature data for %s — skipped", name, timestamp)
                results[name] = True
                continue

            dp_f = attrs.get("DPTF")
            rh   = rh_from_t_and_dp(t_f, dp_f)

            row = {
                "station_id":           attrs.get("ID"),
                "timestamp":            timestamp,
                "Temperature (F)":      t_f,
                "Humidity (%)":         rh,
                "Pressure (in)":        None,
                "Precip. Rate (in/hr)": None,
                "Precip. Accum (in)":   None,
                "Wet Bulb (F)":         wet_bulb_f(t_f, rh),
            }

            append_row(csv_path, row)
            results[name] = True

        except Exception as exc:
            log.error("%s  FAILED: %s", name, exc)
            results[name] = False

    return results


# ---------------------------------------------------------------------------
# Main / loop
# ---------------------------------------------------------------------------

def run_once() -> dict:
    """Single scrape cycle. Returns {station_name: success bool}."""
    log.info("===== scrape run started =====")
    try:
        results = scrape_all()
    except Exception as exc:
        # Entire request failed — mark all stations as failed
        log.error("API request failed: %s", exc)
        try:
            results = {name: False for name in _known_stations()}
        except Exception:
            results = {}

    ok    = sum(v for v in results.values())
    total = len(results)
    errors = total - ok
    label  = datetime.now().strftime("[%a %H:%M]")
    if errors:
        failed = ", ".join(n for n, v in results.items() if not v)
        print(f"{label}  {ok:,}/{total:,} stations OK  (failed: {failed})")
    else:
        print(f"{label}  {ok:,}/{total:,} stations OK")
    log.info("===== scrape run complete: %d/%d stations OK =====", ok, total)
    try:
        update_metadata_if_needed()
    except Exception as exc:
        log.error("Metadata update failed: %s", exc)
    return results


def _known_stations() -> list[str]:
    """Return station names from existing CSVs (fallback when API is down)."""
    return [
        f[:-4] for f in os.listdir(STATIONS_DIR)
        if f.endswith(".csv") and not f.startswith("scrape_gdot") and not f.startswith("gdot_")
    ]


def seconds_until_next_fire() -> float:
    """Return seconds until the next :15 or :45 mark."""
    now = datetime.now()
    elapsed = (now.minute % INTERVAL_MIN) * 60 + now.second
    target  = OFFSET_MIN * 60
    if elapsed < target:
        return target - elapsed
    else:
        return INTERVAL_MIN * 60 - elapsed + target


def _print_hourly_summary(hourly: dict) -> None:
    """One-line hourly diagnostic to stdout (and log)."""
    # hourly: {station_name: [bool, bool]} — one entry per run this hour
    total_ok      = sum(sum(v) for v in hourly.values())
    total_attempts = sum(len(v) for v in hourly.values())
    failures = {n: v.count(False) for n, v in hourly.items() if False in v}
    label = datetime.now().strftime("[%a %H:%M]")

    if not failures:
        msg = (
            f"{label}  Hourly summary: {total_ok:,}/{total_attempts:,} pulls OK "
            f"— all {len(hourly):,} stations"
        )
    else:
        sample    = list(failures.items())[:5]
        fail_str  = ", ".join(f"{n} ({c:,}x)" for n, c in sample)
        remainder = len(failures) - len(sample)
        if remainder:
            fail_str += f" and {remainder:,} more"
        msg = (
            f"{label}  Hourly summary: {total_ok:,}/{total_attempts:,} pulls OK "
            f"-- missed: {fail_str}"
        )

    print(msg)
    log.info(msg)


def run_loop() -> None:
    """Run continuously at :15/:45, Ctrl+C to stop."""
    print(
        f"Loop mode active — firing at :{OFFSET_MIN:02d} and "
        f":{OFFSET_MIN + INTERVAL_MIN:02d} past each hour.  "
        f"Hourly summary every {RUNS_PER_HOUR} runs."
    )
    print("Press Ctrl+C to stop.\n")
    log.info("Loop mode started (interval=%d min, offset=%d min)", INTERVAL_MIN, OFFSET_MIN)

    hourly: dict = {}
    run_count = 0

    while True:
        wait      = seconds_until_next_fire()
        next_fire = datetime.fromtimestamp(time.time() + wait)
        _t = next_fire.strftime("%I:%M").lstrip("0") + next_fire.strftime("%p").lower()
        _d = next_fire.strftime("%m/%d/%y").lstrip("0").replace("/0", "/")
        print(f"  Waiting... next run at {_t} on {next_fire.strftime('%A')}, {_d}    ", end="\r", flush=True)
        time.sleep(wait)
        print("\r\033[2K", end="", flush=True)  # clear the waiting line before printing results

        try:
            results = run_once()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            log.error("Unexpected error in run cycle: %s", exc)
            results = {}
        run_count += 1

        for name, ok in results.items():
            hourly.setdefault(name, []).append(ok)

        if run_count % RUNS_PER_HOUR == 0:
            _print_hourly_summary(hourly)
            hourly = {}


def main() -> None:
    if "--loop" in sys.argv:
        run_loop()
    else:
        results = run_once()
        errors  = sum(1 for ok in results.values() if not ok)
        if errors:
            sys.exit(1)


if __name__ == "__main__":
    main()
