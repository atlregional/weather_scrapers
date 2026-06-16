"""
UGA Georgia Weather Network — 15-minute current conditions scraper
===================================================================
Fetches current conditions for 6 Atlanta-area stations from
georgiaweather.net and appends one row per station to its respective
historical CSV.

Run every 15 minutes via Task Scheduler (--loop handles this internally).
The task should fire a few minutes AFTER the :00/:15/:30/:45 mark so the
site has time to publish the new reading.

Usage:
    python scraper_uga.py          # single run
    python scraper_uga.py --loop   # continuous, Ctrl+C to stop

Data appended per station (columns match existing historical CSVs):
    station_id, timestamp, Temperature (F), Humidity (%),
    Pressure (in), Precip. Rate (in/hr), Precip. Accum (in), Wet Bulb (F)

Notes on field mapping vs. historical data:
  - Precip. Accum (in): the site reports "Cumulative Rain Since 12:00 AM",
    which resets at midnight (vs. the historical CSVs' annual cumulative).
  - Precip. Rate (in/hr): estimated as the change in daily cumulative rain
    between consecutive 15-min scrapes x 4.  NaN on the first scrape of
    each day or when the previous row is >20 min old.
  - Wet Bulb (F): taken directly from the site (vs. Stull formula in
    historical data).
"""

import csv
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, date

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = (
    "http://www.georgiaweather.net/mindex.php"
    "?content=calculator&variable=CC&site={site}"
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# EXTERNAL HARD DRIVE CONFIGURATION
# ---------------------------------------------------------------------------
# UPDATE THIS to your external hard drive root (e.g. "E:\\", "F:\\", "D:\\")
# Must match scraper_wu.py, scraper_awn.py, scraper_gdot.py, and generate_update.py
EXTERNAL_DRIVE = "D:\\Weather\\"  # Windows
# EXTERNAL_DRIVE = "/Volumes/Extreme Pro/Weather/" # Mac -> for testing

STATIONS_DIR  = os.path.join(EXTERNAL_DRIVE, 'station-data', 'UGA')
METADATA_FILE = os.path.join(EXTERNAL_DRIVE, 'metadata', 'station_metadata_uga.csv')
# ---------------------------------------------------------------------------

# site_code → (station_id, csv_filename)
STATIONS = {
    "ALPHARET": (375, "ALPHARET.csv"),
    "BALLGND":  (368, "BALLGND.csv"),
    "DULUTH":   (270, "DULUTH.csv"),
    "DUNWOODY": (370, "DUNWOODY.csv"),
    "JONESB":   (380, "JONESB.csv"),
    "KENNESAW": (373, "KENNESAW.csv"),
}

CSV_COLUMNS = [
    "station_id",
    "timestamp",
    "Temperature (F)",
    "Humidity (%)",
    "Pressure (in)",
    "Precip. Rate (in/hr)",
    "Precip. Accum (in)",
    "Wet Bulb (F)",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; UGA-scraper/1.0)"}
TIMEOUT = 30  # seconds per request

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_html(site_code: str) -> str:
    url = BASE_URL.format(site=site_code)
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.text


def parse_numeric(text: str):
    """Return the first float found in *text*, or None."""
    m = re.search(r"[-+]?\d+\.?\d*", text.replace(",", ""))
    return round(float(m.group()), 4) if m else None


def parse_page(html: str) -> tuple[str, dict]:
    """
    Parse the current-conditions page.

    Returns
    -------
    timestamp : str   "YYYY-MM-DD HH:MM:SS"  (naive, as displayed on page)
    data      : dict  label -> raw value string
    """
    soup = BeautifulSoup(html, "html.parser")

    # ---- timestamp -------------------------------------------------------
    # Appears as: <b>Conditions at 1:30 PM EDT on March 19, 2026</b>
    ts_tag = next(
        (b for b in soup.find_all("b") if "Conditions at" in b.get_text()),
        None,
    )
    if ts_tag is None:
        raise ValueError("Timestamp element not found on page")

    ts_raw = ts_tag.get_text()
    m = re.search(r"Conditions at (.+)", ts_raw)
    if not m:
        raise ValueError(f"Cannot parse timestamp from: {ts_raw!r}")

    time_str = m.group(1).strip()
    # Strip timezone abbreviation (EDT, EST, CDT, etc.) — 3-letter codes only,
    # so AM/PM (2 letters) are preserved.
    time_str = re.sub(r"\b[A-Z]{3,4}\b", "", time_str).strip()
    time_str = re.sub(r"\s{2,}", " ", time_str)          # collapse spaces
    dt = datetime.strptime(time_str, "%I:%M %p on %B %d, %Y")
    timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")

    # ---- data table rows -------------------------------------------------
    # Each row: <tr class="TableRow2"><td class="tdClass">label</td>
    #                                 <td class="tdClass">value</td></tr>
    #
    # The page uses malformed &nbsp (no trailing semicolon) which BS4's
    # html.parser passes through as the literal string "&nbsp" rather than
    # converting to \xa0.  Strip both forms from labels and values.
    def clean(s: str) -> str:
        return (
            s.replace("&nbsp;", "")
             .replace("&nbsp", "")
             .replace("\xa0", "")
             .replace("&deg;", "")
             .replace("&deg", "")
             .strip()
        )

    data: dict[str, str] = {}
    for row in soup.find_all("tr", class_="TableRow2"):
        cells = row.find_all("td", class_="tdClass")
        if len(cells) == 2:
            label = clean(cells[0].get_text(separator=" ", strip=True))
            value = clean(cells[1].get_text(separator=" ", strip=True))
            data[label] = value

    return timestamp, data


def last_csv_row(csv_path: str) -> dict | None:
    """Return the last data row of the CSV as a dict, or None."""
    if not os.path.exists(csv_path):
        return None
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        last = None
        for last in reader:
            pass
    return last


def precip_rate(new_accum, new_ts: str, prev_row: dict | None) -> float | None:
    """
    Estimate Precip. Rate (in/hr) from the change in daily cumulative rain.
    Returns None when not computable (start of day, gap > 20 min, etc.).
    """
    if prev_row is None or new_accum is None:
        return None
    try:
        prev_accum = float(prev_row.get("Precip. Accum (in)") or 0)
        prev_dt = datetime.strptime(prev_row["timestamp"], "%Y-%m-%d %H:%M:%S")
        new_dt  = datetime.strptime(new_ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, KeyError, TypeError):
        return None

    # Only valid within the same calendar day and ~15-min spacing
    if prev_dt.date() != new_dt.date():
        return None
    delta_min = (new_dt - prev_dt).total_seconds() / 60
    if not (10 <= delta_min <= 20):
        return None

    diff = new_accum - prev_accum
    if diff < 0:
        return None  # shouldn't happen within same day
    return round(diff * 4, 4)


# ---------------------------------------------------------------------------
# Per-station scrape + append
# ---------------------------------------------------------------------------

def scrape_and_append(site_code: str, station_id: int, csv_path: str) -> None:
    html = fetch_html(site_code)
    timestamp, data = parse_page(html)

    temp     = parse_numeric(data.get("Temperature", ""))
    humidity = parse_numeric(data.get("Relative Humidity", ""))
    pressure = parse_numeric(data.get("Atmospheric Pressure", ""))
    accum    = parse_numeric(data.get("Cumulative Rain Since 12:00 AM", ""))
    wet_bulb = parse_numeric(data.get("Wet Bulb", ""))

    prev = last_csv_row(csv_path)

    # Skip if this timestamp is already the last row (duplicate run)
    if prev and prev.get("timestamp") == timestamp:
        log.info("%s  duplicate timestamp %s — skipped", site_code, timestamp)
        return

    rate = precip_rate(accum, timestamp, prev)

    row = {
        "station_id":           station_id,
        "timestamp":            timestamp,
        "Temperature (F)":      temp,
        "Humidity (%)":         humidity,
        "Pressure (in)":        pressure,
        "Precip. Rate (in/hr)": rate,
        "Precip. Accum (in)":   accum,
        "Wet Bulb (F)":         wet_bulb,
    }

    write_header = not os.path.exists(csv_path)
    with open(csv_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    log.info(
        "%s  %s  T=%.1f°F  RH=%.1f%%  P=%.2fin  Rain=%.4fin  WB=%.1f°F",
        site_code, timestamp,
        temp or 0, humidity or 0, pressure or 0, accum or 0, wet_bulb or 0,
    )


# ---------------------------------------------------------------------------
# Metadata update (once per calendar day)
# ---------------------------------------------------------------------------

def csv_line_count(csv_path: str) -> int:
    """Return number of data rows (excluding header) without loading into memory."""
    with open(csv_path, "rb") as f:
        return sum(1 for _ in f) - 1  # subtract header


def update_metadata_if_needed() -> None:
    """
    Update the UGA rows in station_metadata.csv with current stats.
    Runs only once per calendar day (keyed on last_scraped_date).
    """
    if not os.path.exists(METADATA_FILE):
        log.warning("Metadata file not found, skipping update: %s", METADATA_FILE)
        return

    today_str = date.today().strftime("%m/%d/%y").lstrip("0").replace("/0", "/")  # cross-platform, no leading zeros

    # Read full metadata, check if any UGA row already has today as last_scraped_date
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

    uga_rows = [r for r in rows if r.get("source") == "UGA"]
    if uga_rows and all(r.get("last_scraped_date") == today_str for r in uga_rows):
        return  # already updated today

    # Build a lookup: station_id -> csv_path
    sid_to_csv = {
        str(station_id): os.path.join(STATIONS_DIR, csv_file)
        for _, (station_id, csv_file) in STATIONS.items()
    }

    for row in rows:
        if row.get("source") != "UGA":
            continue
        sid = row.get("station_id", "")
        csv_path = sid_to_csv.get(sid)
        if not csv_path or not os.path.exists(csv_path):
            continue

        # Latest date from last line of the station CSV
        last = last_csv_row(csv_path)
        if last and last.get("timestamp"):
            try:
                latest_dt = datetime.strptime(last["timestamp"], "%Y-%m-%d %H:%M:%S")
                # Don't report Jan 1 of next year as the latest date
                # (Time=2400 boundary rows); back off to Dec 31 if it's Jan 1 00:00
                if latest_dt.month == 1 and latest_dt.day == 1 and latest_dt.hour == 0:
                    from datetime import timedelta
                    latest_dt = latest_dt - timedelta(days=1)
                row["latest_date"] = latest_dt.strftime("%m/%d/%y").lstrip("0").replace("/0", "/")
            except ValueError:
                pass

        # Recompute total_days from earliest -> latest
        try:
            earliest = datetime.strptime(row["earliest_date"], "%m/%d/%y")
            latest   = datetime.strptime(row["latest_date"],   "%m/%d/%y")
            row["total_days"] = str((latest - earliest).days + 1)
        except (ValueError, KeyError):
            pass

        row["total_observations"] = str(csv_line_count(csv_path))
        row["last_scraped_date"]  = today_str

        # Extinction flag: set when data is stale, cleared when fresh data returns.
        # Since UGA scrapes all 6 stations on every fire, a revived station's new
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
                    log.warning("Station %s marked extinct (%d+ days with no data)",
                                row.get("station_id"), EXTINCT_THRESHOLD)
                elif days_stale < EXTINCT_THRESHOLD and was_extinct:
                    row["extinct"] = "False"
                    log.info("Station %s revived — extinct flag cleared",
                             row.get("station_id"))
        except (ValueError, KeyError, TypeError):
            pass

    with open(METADATA_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    log.info("Metadata updated for %d UGA station(s)", len(uga_rows))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

INTERVAL_MIN      = 15           # publishing cadence
OFFSET_MIN        = 3            # fire this many minutes after each boundary
RUNS_PER_HOUR     = 60 // INTERVAL_MIN   # = 4
EXTINCT_THRESHOLD = 30           # days with no new data before marking extinct


def run_once() -> dict:
    """Run one scrape cycle. Returns {site_code: True/False} per station."""
    log.info("===== scrape run started =====")
    results = {}
    for site_code, (station_id, csv_file) in STATIONS.items():
        csv_path = os.path.join(STATIONS_DIR, csv_file)
        try:
            scrape_and_append(site_code, station_id, csv_path)
            results[site_code] = True
        except Exception as exc:
            log.error("%s  FAILED: %s", site_code, exc)
            results[site_code] = False
    errors    = sum(1 for ok in results.values() if not ok)
    ok_count  = len(results) - errors
    label     = datetime.now().strftime("[%a %H:%M]")
    if errors:
        failed = ", ".join(k for k, v in results.items() if not v)
        print(f"{label}  {ok_count:,}/{len(results):,} stations OK  (failed: {failed})")
    else:
        print(f"{label}  {ok_count:,}/{len(results):,} stations OK")
    log.info("===== scrape run complete (%d error(s)) =====", errors)
    try:
        update_metadata_if_needed()
    except Exception as exc:
        log.error("Metadata update failed: %s", exc)
    return results


def seconds_until_next_fire() -> float:
    """
    Return seconds to sleep until the next :03/:18/:33/:48 mark.
    Aligns to INTERVAL_MIN-minute boundaries offset by OFFSET_MIN minutes.
    """
    now = datetime.now()
    elapsed_in_interval = (now.minute % INTERVAL_MIN) * 60 + now.second
    target_offset_sec   = OFFSET_MIN * 60
    if elapsed_in_interval < target_offset_sec:
        return target_offset_sec - elapsed_in_interval
    else:
        return INTERVAL_MIN * 60 - elapsed_in_interval + target_offset_sec


def _print_hourly_summary(hourly: dict) -> None:
    """Print a one-line hourly diagnostic to stdout (and mirror to log)."""
    total_ok      = sum(sum(v) for v in hourly.values())
    total_attempts = sum(len(v) for v in hourly.values())
    failures = {code: v.count(False) for code, v in hourly.items() if False in v}
    label = datetime.now().strftime("[%a %H:%M]")

    if not failures:
        msg = f"{label}  Hourly summary: {total_ok:,}/{total_attempts:,} pulls OK — all 6 stations"
    else:
        fail_detail = ", ".join(f"{code} ({n:,}x)" for code, n in failures.items())
        msg = (
            f"{label}  Hourly summary: {total_ok:,}/{total_attempts:,} pulls OK "
            f"-- missed: {fail_detail}"
        )

    print(msg)
    log.info(msg)


def run_loop() -> None:
    """Run continuously, firing ~3 min after each 15-min clock boundary."""
    print(
        f"Loop mode active — firing at "
        f":{OFFSET_MIN:02d}/:{OFFSET_MIN+15:02d}/:{OFFSET_MIN+30:02d}/:{OFFSET_MIN+45:02d} "
        f"past each hour.  Hourly summary printed every {RUNS_PER_HOUR} runs."
    )
    print("Press Ctrl+C to stop.\n")
    log.info("Loop mode started (interval=%d min, offset=%d min)", INTERVAL_MIN, OFFSET_MIN)

    hourly: dict = {code: [] for code in STATIONS}
    run_count = 0

    while True:
        wait = seconds_until_next_fire()
        next_fire = datetime.fromtimestamp(time.time() + wait)
        # Overwrite the same terminal line while waiting so the screen stays tidy
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
            label = datetime.now().strftime("[%a %H:%M]")
            print(f"\n{label}  ERROR in run cycle: {exc}")
            results = {code: False for code in STATIONS}
        run_count += 1
        for code, ok in results.items():
            hourly[code].append(ok)

        if run_count % RUNS_PER_HOUR == 0:
            _print_hourly_summary(hourly)
            hourly = {code: [] for code in STATIONS}


def main() -> None:
    if "--loop" in sys.argv:
        run_loop()
    else:
        errors = sum(1 for ok in run_once().values() if not ok)
        if errors:
            sys.exit(1)


if __name__ == "__main__":
    main()
