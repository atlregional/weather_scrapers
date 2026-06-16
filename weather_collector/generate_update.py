"""
Progress chart for all 4 data sources: AWN, WU, GDOT, and UGA.
WINDOWS VERSION - Configured for external hard drive storage.

Generates a single KPI summary and map showing all weather stations.
All stations share a single marker color.

Usage:
    python generate_update.py           # single run
    python generate_update.py --loop    # continuous, fires daily at LOOP_HOUR (2 AM)

Dependencies (in addition to standard scraper env):
    pip install geopandas contextily
"""
import sys
import time
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from datetime import datetime, timedelta
import os
import geopandas as gpd
from shapely.geometry import Point
import contextily as ctx
from PIL import Image
import io
from pyproj import Transformer

# ============================================================================
# PATH CONFIGURATION
# Derived automatically from the script's location — no manual edits needed.
# Script lives at <drive>/weather_collector/generate_update.py, so the drive
# root is always one directory up.
# ============================================================================
EXTERNAL_DRIVE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ============================================================================
# DIRECTORY STRUCTURE - All paths derive from EXTERNAL_DRIVE above
# ============================================================================
# D:\Weather\metadata\       — station_metadata.csv, input CSVs, GeoJSON, output PNG
# D:\Weather\station-data\   — raw station CSVs, split by source
METADATA_DIR = os.path.join(EXTERNAL_DRIVE, 'metadata')

# Per-source metadata files (each scraper owns its own to avoid write collisions)
WU_METADATA_FILE   = os.path.join(METADATA_DIR, 'station_metadata_wu.csv')
AWN_METADATA_FILE  = os.path.join(METADATA_DIR, 'station_metadata_awn.csv')
GDOT_METADATA_FILE = os.path.join(METADATA_DIR, 'station_metadata_gdot.csv')
UGA_METADATA_FILE  = os.path.join(METADATA_DIR, 'station_metadata_uga.csv')

# Station data directories (for filesize calculation)
AWN_STATIONS_DIR  = os.path.join(EXTERNAL_DRIVE, 'station-data', 'AWN')
WU_STATIONS_DIR   = os.path.join(EXTERNAL_DRIVE, 'station-data', 'WU')
GDOT_STATIONS_DIR = os.path.join(EXTERNAL_DRIVE, 'station-data', 'GDOT')
UGA_STATIONS_DIR  = os.path.join(EXTERNAL_DRIVE, 'station-data', 'UGA')

# Output
OUTPUT_FILE = os.path.join(METADATA_DIR, 'scraper-update.png')

# Map configuration
MAP_CENTER_LAT = 33.843943435216205
MAP_CENTER_LON = -84.39614750798709

# GeoJSON for county boundaries
GEOJSON_FILE = os.path.join(METADATA_DIR, 'metro_atlanta_counties.geojson')

# Custom fonts — registered once at import time
_FONT_BOLD_FILE   = os.path.join(METADATA_DIR, 'DINPro-Bold.otf')
_FONT_MEDIUM_FILE = os.path.join(METADATA_DIR, 'DINPro-Medium.otf')
font_manager.fontManager.addfont(_FONT_BOLD_FILE)
font_manager.fontManager.addfont(_FONT_MEDIUM_FILE)
_font_bold   = font_manager.FontProperties(fname=_FONT_BOLD_FILE)
_font_medium = font_manager.FontProperties(fname=_FONT_MEDIUM_FILE)


def make_gdf(df):
    """Convert a DataFrame with latitude/longitude columns to a Web Mercator GeoDataFrame."""
    if len(df) == 0:
        return gpd.GeoDataFrame(geometry=[], crs='EPSG:3857')
    geom = [Point(lon, lat) for lon, lat in zip(df['longitude'], df['latitude'])]
    return gpd.GeoDataFrame(df.copy(), geometry=geom, crs='EPSG:4326').to_crs('EPSG:3857')


def get_source_stats(metadata_file, stations_dir, exclude_csvs=None):
    """
    Calculate statistics for a single data source.
    Reads the source-specific metadata file — no source filtering needed.

    Returns dict with: completed_count, total_observations,
                       total_filesize_bytes, total_days, completed_df, coords_df
    """
    stats = {
        'completed_count': 0,
        'total_observations': 0,
        'total_filesize_bytes': 0,
        'total_days': 0,
        'completed_df': pd.DataFrame(),
        'coords_df': pd.DataFrame(),
        'extinct_coords_df': pd.DataFrame(),
    }

    if not os.path.exists(metadata_file):
        print(f"  Warning: {metadata_file} not found")
        return stats

    metadata_df = pd.read_csv(metadata_file)
    completed = metadata_df[
        metadata_df['total_observations'] > 0
    ]
    stats['completed_count'] = len(completed)
    stats['completed_df'] = completed
    stats['total_days'] = completed['total_days'].sum()

    # Sum observations from metadata (no need to re-read every CSV)
    stats['total_observations'] = int(completed['total_observations'].sum())

    # Sum filesize from actual station CSV files (stat calls only, no file reading)
    if exclude_csvs is None:
        exclude_csvs = set()

    if os.path.exists(stations_dir):
        for f in os.listdir(stations_dir):
            if not f.endswith('.csv') or f in exclude_csvs:
                continue
            path = os.path.join(stations_dir, f)
            stats['total_filesize_bytes'] += os.path.getsize(path)

    # Coordinates come directly from the metadata file, split by extinct status
    coord_cols = [c for c in ['station_id', 'latitude', 'longitude'] if c in completed.columns]
    if len(coord_cols) == 3:
        has_coords = completed.dropna(subset=['latitude', 'longitude'])
        if 'extinct' in completed.columns:
            extinct_mask = has_coords['extinct'].apply(
                lambda v: str(v).strip().lower() == 'true'
            )
            stats['coords_df']         = has_coords[~extinct_mask][coord_cols].copy()
            stats['extinct_coords_df'] = has_coords[extinct_mask][coord_cols].copy()
        else:
            stats['coords_df']         = has_coords[coord_cols].copy()

    return stats


def create_combined_chart():
    """Create a progress chart combining all 4 data sources."""

    print("=" * 60)
    print("Scraping Progress Chart (AWN + WU + GDOT + UGA)")
    print("=" * 60)

    # Gather stats from all four sources
    print("\nReading AWN data...")
    awn = get_source_stats(AWN_METADATA_FILE, AWN_STATIONS_DIR)
    print(f"  AWN:  {awn['completed_count']:,} stations, {awn['total_observations']:,} observations")

    print("\nReading WU data...")
    wu = get_source_stats(WU_METADATA_FILE, WU_STATIONS_DIR)
    print(f"  WU:   {wu['completed_count']:,} stations, {wu['total_observations']:,} observations")

    print("\nReading GDOT data...")
    gdot = get_source_stats(GDOT_METADATA_FILE, GDOT_STATIONS_DIR)
    print(f"  GDOT: {gdot['completed_count']:,} stations, {gdot['total_observations']:,} observations")

    print("\nReading UGA data...")
    uga = get_source_stats(UGA_METADATA_FILE, UGA_STATIONS_DIR)
    print(f"  UGA:  {uga['completed_count']:,} stations, {uga['total_observations']:,} observations")

    # Aggregated KPIs across all sources
    total_stations    = awn['completed_count'] + wu['completed_count'] + gdot['completed_count'] + uga['completed_count']
    total_observations = awn['total_observations'] + wu['total_observations'] + gdot['total_observations'] + uga['total_observations']
    total_filesize_bytes = awn['total_filesize_bytes'] + wu['total_filesize_bytes'] + gdot['total_filesize_bytes'] + uga['total_filesize_bytes']

    all_stations_df = pd.concat(
        [awn['completed_df'], wu['completed_df'], gdot['completed_df'], uga['completed_df']],
        ignore_index=True
    )
    avg_days  = all_stations_df['total_days'].mean() if len(all_stations_df) > 0 else 0
    avg_years = avg_days / 365.25

    total_obs_millions = total_observations / 1_000_000
    total_filesize_gb  = total_filesize_bytes / (1024 ** 3)

    # Windows-compatible date format (%-d is Linux-only; use .day instead)
    now = datetime.now()
    current_date_str = now.strftime(f'%B {now.day}, %Y')

    print(f"\nAggregated KPIs:")
    print(f"  Total Stations:     {total_stations:,}")
    print(f"  Total Observations: {total_obs_millions:.1f}M")
    print(f"  Avg Years/Station:  {avg_years:.1f}")
    print(f"  Total Filesize:     {total_filesize_gb:.1f}GB")

    # ===== KPI HEADER =====
    fig_header, ax_header = plt.subplots(figsize=(11.33, 2.0))
    ax_header.axis('off')

    ax_header.text(0.5, 0.75,
                   f'Scraping Progress Through {current_date_str}',
                   ha='center', va='center', fontproperties=_font_bold, fontsize=25)

    kpi_line1 = f'Total Stations: {total_stations:,} | Total Observations: {total_obs_millions:.1f}M'
    ax_header.text(0.5, 0.45, kpi_line1, ha='center', va='center', fontproperties=_font_medium, fontsize=19)

    kpi_line2 = f'Average Years per Station: {avg_years:.1f} | Total Filesize Scraped: {total_filesize_gb:.1f}GB'
    ax_header.text(0.5, 0.20, kpi_line2, ha='center', va='center', fontproperties=_font_medium, fontsize=19)

    plt.tight_layout()

    buf_header = io.BytesIO()
    fig_header.savefig(buf_header, format='png', dpi=300, bbox_inches='tight', facecolor='white')
    buf_header.seek(0)
    img_header = Image.open(buf_header)
    plt.close(fig_header)

    # ===== MAP =====
    print("\nGenerating map...")

    all_coords = pd.concat(
        [awn['coords_df'], wu['coords_df'], gdot['coords_df'], uga['coords_df']],
        ignore_index=True
    )
    all_gdf = make_gdf(all_coords)

    all_extinct_coords = pd.concat(
        [awn['extinct_coords_df'], wu['extinct_coords_df'],
         gdot['extinct_coords_df'], uga['extinct_coords_df']],
        ignore_index=True
    )
    all_extinct_gdf = make_gdf(all_extinct_coords)

    fig_map, ax_map = plt.subplots(figsize=(16, 10))

    # County boundaries from GeoJSON
    if os.path.exists(GEOJSON_FILE):
        try:
            counties = gpd.read_file(GEOJSON_FILE).to_crs('EPSG:3857')
            counties.plot(ax=ax_map, facecolor='none', edgecolor='white', linewidth=1, zorder=2)
            print(f"  Added GeoJSON layer ({len(counties):,} features)")
        except Exception as e:
            print(f"  Warning: Could not read GeoJSON: {e}")

    # Colors for active and extinct markers
    marker_color  = '#00FFFF'
    extinct_color = '#ee575d'

    # Active stations
    if len(all_gdf) > 0:
        all_gdf.plot(ax=ax_map, color=marker_color, markersize=15, alpha=0.95, zorder=3, edgecolor='none')
        print(f"  Added {len(all_gdf):,} active stations")

    # Extinct stations (smaller, muted)
    if len(all_extinct_gdf) > 0:
        all_extinct_gdf.plot(ax=ax_map, color=extinct_color, markersize=10, alpha=0.7, zorder=3, edgecolor='none')
        print(f"  Added {len(all_extinct_gdf):,} extinct stations")

    transformer = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)
    center_x, center_y = transformer.transform(MAP_CENTER_LON, MAP_CENTER_LAT)

    half_width  = 120000   # meters east/west from center
    half_height = 80000    # meters north/south from center

    ax_map.set_xlim(center_x - half_width, center_x + half_width)
    ax_map.set_ylim(center_y - half_height, center_y + half_height)

    ctx.add_basemap(ax_map, source=ctx.providers.CartoDB.DarkMatter)

    # Legend (extinct entry added only when there are extinct stations)
    legend_elements = [
        Line2D([0], [0], marker='o', color='w', markerfacecolor=marker_color,
               markersize=10, label='Active Station', linestyle='None', alpha=0.8, markeredgewidth=0),
    ]
    if len(all_extinct_gdf) > 0:
        legend_elements.append(
            Line2D([0], [0], marker='o', color='w', markerfacecolor=extinct_color,
                   markersize=10, label='Extinct Station', linestyle='None', alpha=0.7, markeredgewidth=0)
        )
    ax_map.legend(handles=legend_elements, loc='lower right',
                  facecolor='black', labelcolor='white', framealpha=0,
                  prop=font_manager.FontProperties(fname=_FONT_MEDIUM_FILE, size=18))

    ax_map.set_axis_off()
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

    buf_map = io.BytesIO()
    fig_map.savefig(buf_map, format='png', dpi=300, facecolor='white')
    buf_map.seek(0)
    img_map = Image.open(buf_map)
    plt.close(fig_map)

    # ===== COMBINE IMAGES =====
    total_height = img_header.height + img_map.height + 80
    max_width    = max(img_header.width, img_map.width)

    combined = Image.new('RGB', (max_width, total_height), 'white')

    header_x = (max_width - img_header.width) // 2
    combined.paste(img_header, (header_x, 0))

    map_x = (max_width - img_map.width) // 2
    combined.paste(img_map, (map_x, img_header.height))

    # Timestamp footer
    fig_ts = plt.figure(figsize=(max_width / 300, 0.5))
    fig_ts.text(0.99, 0.5, f'Generated: {now.strftime("%Y-%m-%d %H:%M:%S")}',
                ha='center', va='center', fontproperties=_font_medium, fontsize=20, style='italic', alpha=0.6)
    fig_ts.patch.set_visible(False)
    plt.axis('off')

    buf_ts = io.BytesIO()
    fig_ts.savefig(buf_ts, format='png', dpi=100, bbox_inches='tight', pad_inches=0, facecolor='white')
    buf_ts.seek(0)
    img_ts = Image.open(buf_ts)
    plt.close(fig_ts)

    combined.paste(img_ts, (0, img_header.height + img_map.height))

    combined.save(OUTPUT_FILE, dpi=(300, 300))
    print(f"\nChart saved to: {OUTPUT_FILE}")
    print("=" * 60)


# Hour of day (24h) to fire the daily chart update in --loop mode
LOOP_HOUR = 18


def seconds_until_next_fire() -> float:
    """Return seconds until the next LOOP_HOUR:00."""
    now = datetime.now()
    next_fire = now.replace(hour=LOOP_HOUR, minute=0, second=0, microsecond=0)
    if now >= next_fire:
        next_fire += timedelta(days=1)
    return (next_fire - now).total_seconds()


def run_loop() -> None:
    """
    Run create_combined_chart() once per day at LOOP_HOUR.
    Reads all four source metadata CSVs fresh on each run so it always
    reflects the latest scraper output.  Press Ctrl+C to stop.
    """
    print(f"Loop mode active — chart updates daily at {LOOP_HOUR:02d}:00.  Press Ctrl+C to stop.\n")

    while True:
        wait      = seconds_until_next_fire()
        next_fire = datetime.fromtimestamp(time.time() + wait)
        _d = next_fire.strftime("%m/%d/%y").lstrip("0").replace("/0", "/")
        print(f"  Waiting... next run at {LOOP_HOUR:02d}:00 on {next_fire.strftime('%A')}, {_d}    ",
              end="\r", flush=True)
        time.sleep(wait)

        label = datetime.now().strftime("[%a %H:%M]")
        print(f"\n{label}  Generating chart...")
        try:
            create_combined_chart()
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f"{label}  ERROR: {e}")


if __name__ == '__main__':
    if '--loop' in sys.argv:
        run_loop()
    else:
        create_combined_chart()