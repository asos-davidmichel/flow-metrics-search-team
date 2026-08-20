"""
Metrics calculation layer — slice 1: time in columns.

Reads:
  output/data/work_item_history.json
  output/data/config.json
  output/data/context.json
  output/data/excluded_items.json

Writes:
  output/metrics/time_in_columns.json

Usage:
  python src/metrics.py [--window 6m]

Window values: 1m, 3m, 6m, 1y  (rolling back from today)
"""

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path("output/data")
METRICS_DIR = Path("output/metrics")

CONTEXT_PATH = DATA_DIR / "context.json"
CONFIG_PATH = DATA_DIR / "config.json"
HISTORY_PATH = DATA_DIR / "work_item_history.json"
EXCLUDED_PATH = DATA_DIR / "excluded_items.json"
WORK_ITEMS_PATH = DATA_DIR / "work_items.json"

OUTPUT_PATH = METRICS_DIR / "time_in_columns.json"


def parse_window(window_str):
    """Parse a window string like '2w', '3m', '6m', '1y' into a timedelta."""
    s = window_str.strip().lower()
    if s.endswith("w"):
        return timedelta(weeks=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(days=int(s[:-1]) * 30)
    if s.endswith("y"):
        return timedelta(days=int(s[:-1]) * 365)
    raise ValueError(f"Unrecognised window: {window_str!r}. Use e.g. '2w', '1m', '3m', '6m', '1y'.")


def parse_dt(s):
    """Parse an ISO 8601 string to a UTC-aware datetime. Returns None if s is None."""
    if s is None:
        return None
    s = s.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {s!r}")


def main():
    parser = argparse.ArgumentParser(description="Calculate time-in-columns metric.")
    parser.add_argument(
        "--window", default="6m",
        help="Rolling analysis window, e.g. 1m, 3m, 6m, 1y (default: 6m)"
    )
    parser.add_argument(
        "--from", dest="date_from", default=None, metavar="YYYY-MM-DD",
        help="Custom start date (overrides --window)"
    )
    parser.add_argument(
        "--to", dest="date_to", default=None, metavar="YYYY-MM-DD",
        help="Custom end date (default: today, used with --from)"
    )
    args = parser.parse_args()

    if OUTPUT_PATH.exists():
        print(f"Skipping: {OUTPUT_PATH} already exists. Delete it to regenerate.")
        sys.exit(0)

    now = datetime.now(timezone.utc)
    if args.date_from:
        try:
            window_start = datetime.strptime(args.date_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Error: --from value {args.date_from!r} is not a valid YYYY-MM-DD date.", file=sys.stderr)
            sys.exit(1)
        if args.date_to:
            try:
                window_end = datetime.strptime(args.date_to, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc
                )
            except ValueError:
                print(f"Error: --to value {args.date_to!r} is not a valid YYYY-MM-DD date.", file=sys.stderr)
                sys.exit(1)
        else:
            window_end = now
        label = f"{window_start.date()} → {window_end.date()}"
    else:
        window_start = now - parse_window(args.window)
        window_end = now
        label = args.window

    print(f"Window: {window_start.date()} → {window_end.date()} ({label})")

    # --- Load inputs ---
    for path in (CONTEXT_PATH, CONFIG_PATH, HISTORY_PATH, EXCLUDED_PATH, WORK_ITEMS_PATH):
        if not path.exists():
            print(f"Error: required file not found: {path}", file=sys.stderr)
            sys.exit(1)

    context = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    excluded_raw = json.loads(EXCLUDED_PATH.read_text(encoding="utf-8"))
    excluded_ids = set(item["id"] if isinstance(item, dict) else item for item in excluded_raw) if isinstance(excluded_raw, list) else set()
    work_items_raw = json.loads(WORK_ITEMS_PATH.read_text(encoding="utf-8"))

    # Item metadata lookup: id → {title, type}
    item_meta = {
        item["id"]: {"title": item.get("title", ""), "type": item.get("type", "")}
        for item in work_items_raw
    }

    # ADO URL base and type styles for the dashboard.
    # Use styles fetched from ADO API (stored in context.json); fall back to
    # hardcoded colours for any type not covered.
    ado_url_base = f"https://dev.azure.com/{context['org']}/{context['project']}/_workitems/edit"
    present_types = sorted({m["type"] for m in item_meta.values() if m["type"]})
    fetched_styles = context.get("work_item_type_styles", {})
    work_item_type_styles = {
        t: fetched_styles.get(t) or {"color": "#718096", "abbr": t[:4]}
        for t in present_types
    }

    # Board column order, known names, and types from context
    board_columns = [col["name"] for col in context["columns"]]
    board_column_type = {col["name"]: col["column_type"] for col in context["columns"]}
    known_columns = set(board_columns)

    # Historical column mapping from config
    col_mapping = config.get("historical_column_mapping", {})

    # --- Process items ---
    all_warnings = []
    items_output = []

    for item in history:
        item_id = item["id"]
        if item_id in excluded_ids:
            continue

        item_warnings = []
        column_hours: dict = {}

        for visit in item.get("column_history", []):
            raw_col = visit.get("value")
            entered_str = visit.get("entered")
            left_str = visit.get("left")

            if not raw_col or not entered_str:
                item_warnings.append("Skipped visit with missing column name or entered date")
                continue

            entered = parse_dt(entered_str)
            if left_str is None:
                left = now
                item_warnings.append(
                    f"Open visit in '{raw_col}' (entered {entered.date()}): "
                    f"capped at now ({now.date()})"
                )
            else:
                left = parse_dt(left_str)

            # Clip visit to analysis window
            effective_start = max(entered, window_start)
            effective_end = min(left, window_end)
            if effective_start >= effective_end:
                continue  # Visit entirely outside window

            # Apply historical column mapping
            col_name = col_mapping.get(raw_col, raw_col)

            # Skip columns not on the current board (after mapping)
            if col_name not in known_columns:
                mapped_note = f" (mapped to '{col_name}')" if col_name != raw_col else ""
                item_warnings.append(
                    f"Column '{raw_col}'{mapped_note} not on current board — visit excluded"
                )
                continue

            hours = (effective_end - effective_start).total_seconds() / 3600
            column_hours[col_name] = column_hours.get(col_name, 0) + hours

        if column_hours:
            meta = item_meta.get(item_id, {})
            items_output.append({
                "id": item_id,
                "title": meta.get("title", ""),
                "type": meta.get("type", ""),
                "column_hours": {k: round(v, 2) for k, v in column_hours.items()},
                "warnings": item_warnings,
            })

        for w in item_warnings:
            all_warnings.append(f"Item {item_id}: {w}")

    # --- Aggregate per column ---
    col_accumulator: dict = {}
    col_item_accumulator: dict = {}
    for item in items_output:
        for col, hours in item["column_hours"].items():
            col_accumulator.setdefault(col, []).append(hours)
            col_item_accumulator.setdefault(col, []).append({
                "id": item["id"],
                "title": item["title"],
                "type": item["type"],
                "hours": hours,
            })

    # Order by board order
    ordered_cols = [c for c in board_columns if c in col_accumulator]

    columns_output = []
    for i, col_name in enumerate(ordered_cols):
        hours_list = col_accumulator[col_name]
        col_items = sorted(
            col_item_accumulator.get(col_name, []),
            key=lambda x: x["hours"], reverse=True
        )
        columns_output.append({
            "name": col_name,
            "column_type": board_column_type.get(col_name, "unknown"),
            "order": i,
            "n": len(hours_list),
            "total_hours": round(sum(hours_list), 2),
            "mean_hours": round(statistics.mean(hours_list), 2),
            "median_hours": round(statistics.median(hours_list), 2),
            "items": col_items,
        })

    output = {
        "metric": "time_in_columns",
        "calculated_at": now.isoformat(),
        "window": {
            "parameter": args.window,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "source_files": [
            str(HISTORY_PATH),
            str(CONFIG_PATH),
            str(CONTEXT_PATH),
            str(EXCLUDED_PATH),
            str(WORK_ITEMS_PATH),
        ],
        "config_used": {
            "historical_column_mapping": col_mapping,
        },
        "ado_url_base": ado_url_base,
        "work_item_type_styles": work_item_type_styles,
        "excluded_item_ids": sorted(excluded_ids),
        "item_count": len(items_output),
        "warnings": all_warnings,
        "columns": columns_output,
        "items": items_output,
    }

    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    output["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"Written: {OUTPUT_PATH}")
    print(f"Items with activity in window: {len(items_output)}")
    print(f"Warnings: {len(all_warnings)}")
    if all_warnings:
        for w in all_warnings[:5]:
            print(f"  {w}")
        if len(all_warnings) > 5:
            print(f"  ... and {len(all_warnings) - 5} more (see output JSON)")


if __name__ == "__main__":
    main()
