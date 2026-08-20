"""
Metrics calculation layer — slice 2: cycle time.

Reads:
  output/data/work_item_history.json
  output/data/config.json
  output/data/context.json
  output/data/excluded_items.json
  output/data/work_items.json

Writes:
  output/metrics/cycle_time.json

Usage:
  python src/cycle_time.py [--window 6m]

Cycle time: time from first entry into the clock_start column to first entry
into the clock_end column (as defined in config.json). Only items whose
clock_end falls within the analysis window are included.
"""

import argparse
import json
import re
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

OUTPUT_PATH = METRICS_DIR / "cycle_time.json"


def parse_window(window_str):
    s = window_str.strip().lower()
    if s.endswith("w"):
        return timedelta(weeks=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(days=int(s[:-1]) * 30)
    if s.endswith("y"):
        return timedelta(days=int(s[:-1]) * 365)
    raise ValueError(f"Unrecognised window: {window_str!r}. Use e.g. '2w', '1m', '3m', '6m', '1y'.")


def parse_dt(s):
    if s is None:
        return None
    # Strip Z or ±HH:MM timezone suffix before parsing
    s = re.sub(r'Z$|[+-]\d{2}:\d{2}$', '', s)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {s!r}")


def week_start_str(dt):
    """Return ISO date string (YYYY-MM-DD) for the Monday of the week containing dt."""
    d = dt.date()
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def percentile(sorted_values, p):
    """Return the p-th percentile (0-100) of a pre-sorted list."""
    if not sorted_values:
        return None
    idx = min(int(len(sorted_values) * p / 100), len(sorted_values) - 1)
    return sorted_values[idx]


def main():
    parser = argparse.ArgumentParser(description="Calculate cycle time metric.")
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

    item_meta = {
        item["id"]: {"title": item.get("title", ""), "type": item.get("type", "")}
        for item in work_items_raw
    }

    ado_url_base = f"https://dev.azure.com/{context['org']}/{context['project']}/_workitems/edit"
    present_types = sorted({m["type"] for m in item_meta.values() if m["type"]})
    fetched_styles = context.get("work_item_type_styles", {})
    work_item_type_styles = {
        t: fetched_styles.get(t) or {"color": "#718096", "abbr": t[:4]}
        for t in present_types
    }

    col_mapping = config.get("historical_column_mapping", {})
    ct_config = config.get("cycle_time", {})
    clock_start_cfg = ct_config.get("clock_start", {})
    clock_end_cfg = ct_config.get("clock_end", {})

    if clock_start_cfg.get("type") != "column" or clock_end_cfg.get("type") != "column":
        print(
            "Error: cycle_time clock_start and clock_end must both be of type 'column'.",
            file=sys.stderr,
        )
        sys.exit(1)

    clock_start_col = clock_start_cfg["value"]
    clock_end_col = clock_end_cfg["value"]

    print(f"Clock start: first entry into '{clock_start_col}'")
    print(f"Clock end:   first entry into '{clock_end_col}'")

    all_warnings = []
    items_output = []
    throughput_items = []  # all completions within window, regardless of started_at

    for item in history:
        item_id = item["id"]
        if item_id in excluded_ids:
            continue

        item_warnings = []
        started_at = None
        completed_at = None

        for visit in item.get("column_history", []):
            raw_col = visit.get("value")
            entered_str = visit.get("entered")
            if not raw_col or not entered_str:
                continue

            entered = parse_dt(entered_str)
            if entered is None:
                continue

            col_name = col_mapping.get(raw_col, raw_col)

            if col_name == clock_start_col and started_at is None:
                started_at = entered

            if col_name == clock_end_col and completed_at is None:
                completed_at = entered

        # Must have reached clock_end within the window
        if completed_at is None:
            continue
        if not (window_start <= completed_at <= window_end):
            continue

        # Count this item for throughput regardless of whether we have a start date
        meta = item_meta.get(item_id, {})
        throughput_items.append({
            "id": item_id,
            "type": meta.get("type", ""),
            "completed_at": completed_at.isoformat(),
        })

        if started_at is None:
            item_warnings.append(
                f"Reached '{clock_end_col}' but no '{clock_start_col}' visit found — "
                "clock start may predate available history; excluded"
            )
            for w in item_warnings:
                all_warnings.append(f"Item {item_id}: {w}")
            continue

        if started_at > completed_at:
            item_warnings.append(
                f"Clock start ({started_at.date()}) is after clock end "
                f"({completed_at.date()}) — data anomaly; excluded"
            )
            for w in item_warnings:
                all_warnings.append(f"Item {item_id}: {w}")
            continue

        cycle_time_days = (completed_at - started_at).total_seconds() / 86400
        meta = item_meta.get(item_id, {})
        items_output.append({
            "id": item_id,
            "title": meta.get("title", ""),
            "type": meta.get("type", ""),
            "cycle_time_days": round(cycle_time_days, 2),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        })

        for w in item_warnings:
            all_warnings.append(f"Item {item_id}: {w}")

    # --- Overall stats ---
    cycle_times = [item["cycle_time_days"] for item in items_output]
    if cycle_times:
        sorted_times = sorted(cycle_times)
        overall = {
            "n": len(cycle_times),
            "mean_days": round(statistics.mean(cycle_times), 1),
            "median_days": round(statistics.median(cycle_times), 1),
            "p85_days": round(percentile(sorted_times, 85), 1),
            "min_days": round(sorted_times[0], 1),
            "max_days": round(sorted_times[-1], 1),
        }
    else:
        overall = {"n": 0}

    # --- Weekly stats (grouped by Monday of completion week) ---
    weekly_buckets: dict = {}
    for item in items_output:
        w = week_start_str(parse_dt(item["completed_at"]))
        weekly_buckets.setdefault(w, []).append(item["cycle_time_days"])

    weekly_stats = []
    for w_key in sorted(weekly_buckets.keys()):
        times = weekly_buckets[w_key]
        weekly_stats.append({
            "week_start": w_key,
            "n": len(times),
            "mean_days": round(statistics.mean(times), 1),
            "median_days": round(statistics.median(times), 1),
        })

    output = {
        "metric": "cycle_time",
        "calculated_at": now.isoformat(),
        "window": {
            "parameter": args.window,
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "config_used": {
            "clock_start": clock_start_cfg,
            "clock_end": clock_end_cfg,
            "historical_column_mapping": col_mapping,
        },
        "ado_url_base": ado_url_base,
        "work_item_type_styles": work_item_type_styles,
        "excluded_item_ids": sorted(excluded_ids),
        "item_count": len(items_output),
        "warnings": all_warnings,
        "overall": overall,
        "weekly_stats": weekly_stats,
        "items": items_output,
        "throughput_items": throughput_items,
    }

    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    output["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    print(f"Written: {OUTPUT_PATH}")
    print(f"Completed items in window: {len(items_output)}")
    if overall.get("n", 0) > 0:
        print(
            f"Mean: {overall['mean_days']}d  "
            f"Median: {overall['median_days']}d  "
            f"P85: {overall['p85_days']}d"
        )
    if all_warnings:
        for w in all_warnings[:5]:
            print(f"  {w}")
        if len(all_warnings) > 5:
            print(f"  ... and {len(all_warnings) - 5} more (see output JSON)")


if __name__ == "__main__":
    main()
