"""Import a Garmin Connect GDPR export into the coach database.

Run with `python -I` (the export is untrusted), passing paths as arguments:

    uv run --no-project --python 3.12 --with fitdecode \
        python -I scripts/import_garmin_export.py --export /path/to/DI_CONNECT --db data/coach.sqlite
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# -I drops the script directory from sys.path; add the project root explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.ingest.garmin_export import run_import, table_counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--export", type=Path, required=True, help="path to the DI_CONNECT folder")
    parser.add_argument("--db", type=Path, required=True, help="SQLite database file")
    parser.add_argument("--streams-months", type=int, default=18, help="FIT streams for rides this recent (0 = none)")
    args = parser.parse_args()

    if not (args.export / "DI-Connect-Fitness").is_dir():
        parser.error(f"{args.export} does not look like a DI_CONNECT folder")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    since = datetime.now(UTC) - timedelta(days=round(args.streams_months * 30.44)) if args.streams_months else None

    stats = run_import(args.export, args.db, since)
    print(f"activities upserted: {stats.activities} (skipped {stats.activities_skipped})")
    print(f"wellness days per source: {stats.wellness_days}")
    print(f"weights upserted: {stats.weights}")
    print(f"vo2max estimates upserted: {stats.vo2max}")
    if since:
        print(
            f"streams since {since:%Y-%m-%d}: {stats.rides_with_streams} rides imported, "
            f"{stats.rides_streams_already_present} already present, "
            f"{stats.ride_fits_unmatched} activity FITs unmatched, {stats.fit_files_scanned} FIT files scanned"
        )
    for table, count in table_counts(args.db).items():
        print(f"  {table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
