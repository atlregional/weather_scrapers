# Metro Atlanta Weather Data Pipeline

A four-scraper system that continuously collects historical and real-time weather observations from ~2,600+ stations across the 11-county Metro Atlanta region and publishes daily diagnostics to a separate GitHub account.

---

## Table of Contents

1. [Overview](#overview)
2. [Repository Contents](#repository-contents)
3. [Architecture](#architecture)
4. [Data Sources & Scrapers](#data-sources--scrapers)
5. [Unified CSV Schema](#unified-csv-schema)
6. [Metadata Files](#metadata-files)
7. [Setup & Prerequisites](#setup--prerequisites)
8. [Path Configuration](#path-configuration)
9. [Running the Scrapers](#running-the-scrapers)
10. [Picking Up Where the Previous Operator Left Off](#picking-up-where-the-previous-operator-left-off)
11. [GitHub Diagnostic Push](#github-diagnostic-push)
12. [Station Discovery Scripts](#station-discovery-scripts)
13. [Directory Layout on Disk](#directory-layout-on-disk)

---

## Overview

This pipeline scrapes weather data from four independent sources and stores it in a unified per-station CSV format on an external hard drive. The historical scrapers (WU and AWN) operate in a nightly catch-up loop, while the real-time scrapers (GDOT and UGA) fire on regular sub-hourly intervals. A daily diagnostic push regenerates a progress map and stats report and commits them to a separate GitHub repository ([`atlregional/weather_metadata`](https://github.com/atlregional/weather_metadata)).

**Scale (as of mid-2026):**

- ~1,438 Weather Underground stations
- ~1,176 Ambient Weather Network stations
- 16 GDOT road-weather stations
- 6 UGA Georgia Weather Network stations
- Hundreds of millions of individual observations stored as CSVs

---

## Repository Contents

```
repo-materials/
├── README.md                          ← this file
├── weather_collector/
│   ├── scraper_wu.py                  ← Weather Underground historical scraper
│   ├── scraper_awn.py                 ← Ambient Weather Network historical scraper
│   ├── scraper_gdot.py                ← GDOT road-weather real-time scraper
│   ├── scraper_uga.py                 ← UGA Georgia Weather Network real-time scraper
│   ├── generate_update.py             ← Generates daily diagnostic map (PNG)
│   └── github_update.py              ← Commits & pushes daily diagnostics to GitHub
├── station_discovery/
│   ├── discover_wu.py                 ← WU station discovery (Playwright grid scan)
│   ├── discover_awn.py                ← AWN station discovery (API bounding-box grid)
│   └── wu_probe_pending.py            ← Battleship-probe for WU stations with 0 obs
└── metadata/
    ├── station_metadata_wu.csv        ← WU station registry & scraping progress
    ├── station_metadata_awn.csv       ← AWN station registry & scraping progress
    ├── station_metadata_gdot.csv      ← GDOT station registry
    ├── station_metadata_uga.csv       ← UGA station registry
    ├── metro_atlanta_counties.geojson ← 11-county boundary used for the map layer
    ├── DINPro-Bold.otf                ← Custom font for generate_update.py
    ├── DINPro-Medium.otf              ← Custom font for generate_update.py
    ├── github_stats_snapshot.json     ← Persists yesterday's extinct counts for delta
    ├── probe_pending_checkpoint.csv   ← Checkpoint for wu_probe_pending.py
    └── discovered_awn_2026-05-26.csv  ← Discovery log from an AWN scan run
```

**Not included here** (too large for GitHub): the raw station-data CSVs (~hundreds of GB). Those live on the external hard drive under `station-data/WU/`, `station-data/AWN/`, `station-data/GDOT/`, and `station-data/UGA/`.

---

## Architecture

### Data Flow

```
Weather Sources → Scrapers → Per-station CSVs on external drive
                                      │
                          ┌───────────┴────────────┐
                          │   Per-source metadata   │
                          │  station_metadata_*.csv │
                          └───────────┬────────────┘
                                      │
                            generate_update.py
                                      │
                              scraper-update.png
                                      │
                             github_update.py
                                      │
                         ┌────────────┴────────────┐
                         │    diagnostic_repo/      │
                         │  scraper-update.png      │
                         │  pipeline_stats.txt      │
                         │  stations.geojson        │
                         └──────── git push ────────┘
```

### Key Design Decisions

- **Each scraper owns exactly one metadata CSV.** `scraper_wu.py` writes only to `station_metadata_wu.csv`; `scraper_awn.py` writes only to `station_metadata_awn.csv`, and so on. This eliminates concurrent-write collisions when all four scrapers run simultaneously.
- **Checkpoint/resume via `latest_date`.** Historical scrapers use the `latest_date` field in metadata as their resume point. Killing a scraper mid-run is safe — on restart it picks up from the last fully-written date.
- **Extinction flagging.** When a station has had no data for `EXTINCT_THRESHOLD` (30) consecutive days it is flagged `extinct = True` in its metadata row. Extinct stations are skipped during normal runs but re-probed every Sunday (the "Lazarus probe") to detect stations that have come back online.
- **`generate_update.py` and `github_update.py` auto-derive their paths** from their own `__file__` location, so they need no manual path edits regardless of drive letter or OS.

---

## Data Sources & Scrapers

### Weather Underground (`scraper_wu.py`)

| Property      | Value                                   |
| ------------- | --------------------------------------- |
| Source        | `wunderground.com` dashboard HTML       |
| Method        | HTTP GET → BeautifulSoup HTML parse     |
| Station count | ~1,438 stations                         |
| Scrape type   | Historical catch-up (nightly loop)      |
| Loop fires    | Daily at **00:00**                      |
| Rate limit    | 1.0–1.2 s between requests (randomized) |
| Output dir    | `station-data/WU/`                      |
| Metadata file | `metadata/station_metadata_wu.csv`      |

**Loop behavior:** On each nightly run the scraper reads all active (non-extinct) WU stations from the metadata file, sorts them by `latest_date` ascending (most-behind first), and forward-scrapes every missing date from `latest_date + 1` through yesterday. Once all stations are current the nightly cycle completes in minutes.

**Extinction & revival:** A station that returns no data for ≥30 consecutive days is flagged `extinct = True`. Every Sunday the Lazarus probe fires against all extinct stations; if yesterday's fetch returns data, the `extinct` flag is cleared and the station re-enters the normal rotation.

**Wet-bulb temperature** is computed via `psychrolib.GetTWetBulbFromRelHum()`. Pressure must be in PSI (`inHg × 0.491154`).

---

### Ambient Weather Network (`scraper_awn.py`)

| Property      | Value                                         |
| ------------- | --------------------------------------------- |
| Source        | `lightning.ambientweather.net` JSON API       |
| Method        | HTTP GET with MAC address + date range params |
| Station count | ~1,176 stations                               |
| Scrape type   | Historical catch-up (nightly loop)            |
| Loop fires    | Daily at **00:00**                            |
| Rate limit    | 1.2 s between requests                        |
| Output dir    | `station-data/AWN/`                           |
| Metadata file | `metadata/station_metadata_awn.csv`           |

**Key difference from WU:** AWN stations are identified by a **MAC address** (hardware ID) in addition to a station ID. The MAC address is stored in the `mac_address` column of `station_metadata_awn.csv` and is required for every API call.

**Loop behavior:** Same catch-up pattern as WU — most-behind stations processed first, forward-scrapes from `latest_date + 1` through yesterday. Extinction threshold and Lazarus probe are identical to WU.

---

### GDOT Road Weather (`scraper_gdot.py`)

| Property      | Value                                             |
| ------------- | ------------------------------------------------- |
| Source        | ArcGIS FeatureServer (GDOT Road Weather Stations) |
| Method        | Single HTTP GET returns all 57 GDOT stations      |
| Station count | 16 (metro Atlanta subset of 57 statewide)         |
| Scrape type   | Real-time (continuous loop)                       |
| Loop fires    | At **:15** and **:45** past each hour             |
| Output dir    | `station-data/GDOT/`                              |
| Metadata file | `metadata/station_metadata_gdot.csv`              |

**Metro Atlanta filter:** The `KEEP_IDS` set in the script filters the 57 statewide stations down to 16 within the 11-county metro. Station IDs are ArcGIS integer IDs (e.g., `10878` for GA400_PittsRoad).

**Derived fields:** GDOT does not report relative humidity or pressure directly. Humidity is derived from temperature + dew point via the Magnus formula. Wet-bulb temperature is computed via the Stull (2011) formula rather than psychrolib (pressure not available).

**Metadata update:** `update_metadata_if_needed()` runs once per calendar day to refresh `latest_date`, `total_observations`, and extinction flags in `station_metadata_gdot.csv`.

---

### UGA Georgia Weather Network (`scraper_uga.py`)

| Property      | Value                                                     |
| ------------- | --------------------------------------------------------- |
| Source        | `georgiaweather.net` HTML (current-conditions page)       |
| Method        | HTTP GET → BeautifulSoup HTML parse                       |
| Station count | 6 (ALPHARET, BALLGND, DULUTH, DUNWOODY, JONESB, KENNESAW) |
| Scrape type   | Real-time (continuous loop)                               |
| Loop fires    | At **:03 / :18 / :33 / :48** past each hour               |
| Output dir    | `station-data/UGA/`                                       |
| Metadata file | `metadata/station_metadata_uga.csv`                       |

**Precipitation handling:** The site reports "Cumulative Rain Since 12:00 AM," which resets at midnight. `Precip. Rate (in/hr)` is estimated as the change in daily cumulative rain between consecutive 15-minute scrapes × 4. The rate is `NaN` on the first scrape of each day or when the previous row is more than 20 minutes old.

**Wet-bulb temperature** for UGA is taken directly from the georgiaweather.net page rather than being computed (unlike the other three scrapers, which calculate it).

---

## Unified CSV Schema

Every station file from all four sources shares the same eight columns:

| Column                 | Type     | Notes                                                                |
| ---------------------- | -------- | -------------------------------------------------------------------- |
| `station_id`           | string   | Source-specific ID (e.g., `KGAATLAN4`, MAC-based ID, `10878`, `375`) |
| `timestamp`            | datetime | `YYYY-MM-DD HH:MM:SS`, Eastern time (naive)                          |
| `Temperature (F)`      | float    | Dry-bulb temperature in °F                                           |
| `Humidity (%)`         | float    | Relative humidity 0–100                                              |
| `Pressure (in)`        | float    | Station pressure in inches of mercury; blank for GDOT                |
| `Precip. Rate (in/hr)` | float    | Precipitation rate; blank for GDOT                                   |
| `Precip. Accum (in)`   | float    | Precipitation accumulation; blank for GDOT                           |
| `Wet Bulb (F)`         | float    | Wet-bulb temperature in °F                                           |

---

## Metadata Files

Each scraper maintains its own metadata CSV. All four share the same column schema:

| Column               | Description                                                |
| -------------------- | ---------------------------------------------------------- |
| `source`             | `WU`, `AWN`, `GDOT`, or `UGA`                              |
| `station_id`         | Unique station identifier                                  |
| `name`               | Human-readable station name                                |
| `mac_address`        | AWN only — hardware MAC address required for API calls     |
| `earliest_date`      | Earliest date with scraped data                            |
| `latest_date`        | Most recent date with scraped data                         |
| `last_scraped_date`  | Date metadata was last updated                             |
| `total_days`         | Number of distinct calendar days with data                 |
| `total_observations` | Total row count across all dates                           |
| `latitude`           | Station latitude (WGS84)                                   |
| `longitude`          | Station longitude (WGS84)                                  |
| `elevation_ft`       | Elevation in feet (WU and AWN)                             |
| `extinct`            | `True` if station has had no data for 30+ consecutive days |

**`total_observations = 0`** means the station has been registered but not yet scraped (it's in the queue).

---

## Setup & Prerequisites

### Conda Environment

Create a conda environment with any name you prefer (e.g. `weather`, `scraper`, `pipeline`). The package list is the same on Windows and Mac:

```bash
conda create -n <your-env-name> python=3.11
conda activate <your-env-name>
pip install requests beautifulsoup4 lxml pandas psychrolib pytz geopandas contextily shapely Pillow pyproj matplotlib playwright
playwright install chromium   # needed only for station discovery scripts
```

`geopandas` and `contextily` are included above and are required by `generate_update.py`.

### Git & GitHub Credentials

You need Git installed and push credentials configured before cloning this repo or the diagnostic repo. Without credentials, `git push` will fail interactively and `github_update.py` will be unable to push non-interactively.

**Recommended: SSH key**

1. Generate a key if you don't have one: `ssh-keygen -t ed25519 -C "your@email.com"`
2. Add `~/.ssh/id_ed25519.pub` to your GitHub account under **Settings → SSH and GPG keys**.
3. Test: `ssh -T git@github.com` — should greet you by username.

**Alternative: HTTPS with credential manager**
Run `gh auth login` (GitHub CLI) or use Git Credential Manager, which caches credentials so `git push` works without prompting.

**Git user identity (required to commit):**

```bash
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
```

This applies to both this repo (`repo-materials`) and the diagnostic repo cloned into `metadata/diagnostic_repo/`.

---

### External Hard Drive

All scrapers write data to an external hard drive. The four historical scrapers (`scraper_wu.py`, `scraper_awn.py`) and two utility scripts (`generate_update.py` path is auto-derived) require the `EXTERNAL_DRIVE` constant at the top of each file to match your drive path. See [Path Configuration](#path-configuration) below.

---

## Path Configuration

**Files that require manual path edits:**

The four scraper scripts contain a hardcoded `EXTERNAL_DRIVE` constant near the top:

```python
EXTERNAL_DRIVE = "D:\\Weather\\"  # Windows
# EXTERNAL_DRIVE = "/Volumes/Extreme Pro/Weather/" # Mac -> for testing
```

Update this in:

- `weather_collector/scraper_wu.py`
- `weather_collector/scraper_awn.py`
- `weather_collector/scraper_gdot.py`
- `weather_collector/scraper_uga.py`

**Files that do NOT require path edits:**

- `weather_collector/generate_update.py` — derives its drive root from `os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`. As long as the script lives at `<drive>/weather_collector/generate_update.py`, it finds everything automatically.
- `weather_collector/github_update.py` — same auto-derivation strategy.

**Expected directory structure on the external drive:**

```
<EXTERNAL_DRIVE>/            (e.g. D:\Weather\)
├── weather_collector/       ← scripts live here
├── metadata/                ← all 4 metadata CSVs, fonts, GeoJSON, snapshot JSON
│   └── diagnostic_repo/     ← cloned GitHub repo for daily push (see below)
└── station-data/
    ├── WU/                  ← one CSV per WU station (~1,438 files)
    ├── AWN/                 ← one CSV per AWN station (~1,176 files)
    ├── GDOT/                ← one CSV per GDOT station (16 files)
    └── UGA/                 ← one CSV per UGA station (6 files)
```

---

## Running the Scrapers

All four scrapers support a `--loop` flag that puts them into continuous operation. In production all four run simultaneously in separate terminal sessions (or background processes).

> **Note:** Replace `<env-name>` in the commands below with whatever you named your conda environment.

```bash
# Historical catch-up scrapers (fire nightly at 00:00, run immediately on launch)
conda run -n <env-name> python weather_collector/scraper_wu.py --loop
conda run -n <env-name> python weather_collector/scraper_awn.py --loop

# Real-time scrapers
conda run -n <env-name> python weather_collector/scraper_gdot.py --loop   # :15 and :45 past each hour
conda run -n <env-name> python weather_collector/scraper_uga.py --loop    # :03/:18/:33/:48 past each hour

# Progress chart (optional; also called internally by github_update.py)
conda run -n <env-name> python weather_collector/generate_update.py --loop  # fires daily at 18:00

# Daily GitHub diagnostic push
conda run -n <env-name> python weather_collector/github_update.py --loop   # fires daily at 07:00
```

Single-run mode (no `--loop`) fires once and exits:

```bash
python weather_collector/scraper_gdot.py    # one scrape cycle
python weather_collector/generate_update.py # one chart generation
```

**Stopping a scraper:** `Ctrl+C`. All scrapers write progress to disk before each sleep cycle, so interrupting mid-run is safe — restart picks up exactly where it left off.

---

## Picking Up Where the Previous Operator Left Off

The metadata CSVs included in this repo (`metadata/station_metadata_*.csv`) represent the current state of the pipeline as of the date this bundle was created. When you clone and set up on a new machine:

### Step 1 — Set up the directory structure

Create the expected directory layout on your external drive and place these repo contents inside it. Use the path style for your OS:

```
<EXTERNAL_DRIVE>/          (e.g. D:\Weather\ on Windows, /Volumes/MyDrive/Weather/ on Mac)
├── weather_collector/     ← copy from weather_collector/
├── metadata/              ← copy from metadata/
└── station-data/
    ├── WU/
    ├── AWN/
    ├── GDOT/
    └── UGA/
```

### Step 2 — Update `EXTERNAL_DRIVE` in all 4 scrapers

Edit the constant at the top of `scraper_wu.py`, `scraper_awn.py`, `scraper_gdot.py`, and `scraper_uga.py` to match your drive path.

### Step 3 — Populate station data from the previous operator

The metadata CSVs tell the scrapers the `latest_date` scraped for each station. Without the actual station data CSVs from the previous operator's hard drive, the historical scrapers will treat every station as starting from scratch (they will forward-scrape from whatever `latest_date` is in the metadata, but cannot verify what's in the target CSV).

**Ideal handoff:** Copy the entire `station-data/` directory from the previous machine's external drive to your external drive. The historical scrapers will then pick up from the correct `latest_date` for each station with no duplicate work.

**If `station-data/` is unavailable:** The scrapers will still work — they will simply re-scrape from `latest_date + 1` forward, which is correct. Historical data prior to that date will be absent from your local drive but the scrapers will continue collecting new data going forward.

### Step 4 — Launch all four scrapers in `--loop` mode

```bash
python weather_collector/scraper_wu.py --loop
python weather_collector/scraper_awn.py --loop
python weather_collector/scraper_gdot.py --loop
python weather_collector/scraper_uga.py --loop
```

The historical scrapers (WU, AWN) fire immediately on launch and begin their catch-up run. The real-time scrapers wait for their next scheduled time slot. Progress is printed to the terminal; the scrapers update metadata after each station.

---

## GitHub Diagnostic Push

`github_update.py` runs daily at **07:00** and pushes two files to [`atlregional/weather_metadata`](https://github.com/atlregional/weather_metadata):

1. **`scraper-update.png`** — a diagnostic map showing all station locations, color-coded active (cyan) vs. extinct (red), with a KPI header (total stations, observations, avg years/station, total GB).
2. **`pipeline_stats.txt`** — a plain-text report with per-source extinct counts, day-over-day deltas, and average years of data per station.
3. **`stations.geojson`** — a GeoJSON FeatureCollection of all scraped stations with metadata properties, suitable for a web map.

### Setting Up the Diagnostic Repo

The push target is a separate git repository cloned into `metadata/diagnostic_repo/`. You must set this up manually before `github_update.py` will work:

```bash
cd metadata/
git clone git@github.com:atlregional/weather_metadata.git diagnostic_repo
```

The repo must have **push credentials configured** (SSH key or credential helper) so that `git push` works non-interactively. Test with:

```bash
cd metadata/diagnostic_repo
git push
```

If this prompts for a password, set up an SSH key or a credential helper before running `github_update.py --loop`.

### Running the daily push

```bash
python weather_collector/github_update.py --loop
```

On each fire it:

1. Calls `generate_update.py` as a subprocess to regenerate `scraper-update.png` from current metadata.
2. Copies the PNG from `metadata/` into `metadata/diagnostic_repo/`.
3. Builds `stations.geojson` from all four metadata CSVs.
4. Writes `pipeline_stats.txt` with extinct counts and averages.
5. Mirrors DINPro fonts and the county GeoJSON into the repo (one-time, no-op once present).
6. `git add` → `git commit` → `git push` with retry logic (30 s / 120 s / 300 s delays on transient failures).
7. Saves `metadata/github_stats_snapshot.json` for tomorrow's delta calculation.

**`generate_update.py` dependencies:** The map layer requires `geopandas`, `contextily`, `shapely`, `pyproj`, and `Pillow`. It also requires the DINPro font files in `metadata/` and an internet connection to fetch the CartoDB DarkMatter basemap tiles.

---

## Station Discovery Scripts

These scripts are run **manually and periodically** (quarterly or as needed) to discover new stations that have come online since the last scan. They do **not** run in loop mode.

### `discover_wu.py`

Drives a Playwright browser across a coordinate grid of the 11-county metro area, navigating to known WU station pages and extracting the embedded station-selector JSON. Finds all stations visible in WU's map regardless of city-name prefix.

```bash
python station_discovery/discover_wu.py
```

New stations are appended to `station_metadata_wu.csv` with `total_observations = 0`. Run `scraper_wu.py --loop` afterward — it will pick them up automatically.

### `discover_awn.py`

Queries the `lightning.ambientweather.net` bounding-box API across a coordinate grid and collects all station records. New stations are appended to `station_metadata_awn.csv`.

```bash
python station_discovery/discover_awn.py
```

### `wu_probe_pending.py`

After running `discover_wu.py` you will have a batch of newly registered WU stations with `total_observations = 0`. The nightly loop (`scraper_wu.py --loop`) will eventually reach them, but this script accelerates the process using a "battleship algorithm": it samples stratified random dates spread across the last 9 years to locate any date with data (the anchor), then expands backward and forward from that anchor using the same 14-day gap threshold as the main scraper.

```bash
python station_discovery/wu_probe_pending.py
```

Once it finishes, `scraper_wu.py --loop` automatically includes any newly populated stations in its normal nightly forward-scrape. Progress is tracked in `metadata/probe_pending_checkpoint.csv` so the script can be safely interrupted and resumed.

---

## Directory Layout on Disk

```
<EXTERNAL_DRIVE>/
├── weather_collector/
│   ├── scraper_wu.py
│   ├── scraper_awn.py
│   ├── scraper_gdot.py
│   ├── scraper_uga.py
│   ├── generate_update.py
│   └── github_update.py
├── metadata/
│   ├── station_metadata_wu.csv
│   ├── station_metadata_awn.csv
│   ├── station_metadata_gdot.csv
│   ├── station_metadata_uga.csv
│   ├── metro_atlanta_counties.geojson
│   ├── DINPro-Bold.otf
│   ├── DINPro-Medium.otf
│   ├── github_stats_snapshot.json
│   ├── scraper-update.png             ← generated daily by generate_update.py
│   └── diagnostic_repo/               ← separate git repo (clone separately)
│       ├── .git/
│       ├── scraper-update.png
│       ├── pipeline_stats.txt
│       ├── stations.geojson
│       ├── index.html
│       ├── metro_atlanta_counties.geojson
│       └── fonts/
│           ├── DINPro-Bold.otf
│           └── DINPro-Medium.otf
└── station-data/
    ├── WU/
    │   └── KGAATLAN4.csv   (one file per station, ~1,438 files)
    ├── AWN/
    │   └── <station_id>.csv (one file per station, ~1,176 files)
    ├── GDOT/
    │   ├── GA400_PittsRoad.csv
    │   ├── I75_HudsonBridgeRoad.csv
    │   └── ... (16 files total)
    └── UGA/
        ├── ALPHARET.csv
        ├── BALLGND.csv
        ├── DULUTH.csv
        ├── DUNWOODY.csv
        ├── JONESB.csv
        └── KENNESAW.csv
```

---

## Quick-Start Checklist

- [ ] Install Git and configure user identity (`git config --global user.name / user.email`)
- [ ] Add an SSH key to your GitHub account (or configure a credential helper) so `git push` works without prompting
- [ ] Create a conda environment (any name) with all dependencies
- [ ] Place scripts in `<drive>/weather_collector/`
- [ ] Place metadata files in `<drive>/metadata/`
- [ ] Update `EXTERNAL_DRIVE` in the 4 scraper files
- [ ] (Optional) Copy `station-data/` from previous operator's drive
- [ ] Clone diagnostic repo into `<drive>/metadata/diagnostic_repo/` and verify `git push` works without a password prompt
- [ ] Launch all four scrapers in `--loop` mode in separate terminals
- [ ] Launch `github_update.py --loop` in a fifth terminal
