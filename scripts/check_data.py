"""
Data quality checks for AI Flow Metrics.

Reads:
  output/data/context.json
  output/data/work_items.json
  output/data/work_item_history.json

Writes:
  output/data/data_quality_report.json
  output/data/excluded_items.json
  output/data/work_item_rework.json

Usage:
  python src/check.py
  python src/check.py --short-dwell-minutes 30
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Config ------------------------------------------------------------------

DEFAULT_SHORT_DWELL_MINUTES = 60
VIRTUAL_COLUMN_VALUES = {"Backlog"}  # ADO pseudo-values, not real board columns

# --- Helpers -----------------------------------------------------------------


def load(path):
    return json.loads(Path(path).read_text())


def parse_dt(s):
    if not s:
        return None
    s = s.rstrip("Z")
    # Handle both with and without fractional seconds
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def dwell_minutes(span):
    entered = parse_dt(span.get("entered"))
    left = parse_dt(span.get("left"))
    if not entered or not left:
        return None
    return (left - entered).total_seconds() / 60


# --- Checks ------------------------------------------------------------------


def check_missing_board_entry(history):
    ids = [h["id"] for h in history if not h.get("board_entry_date")]
    return {"count": len(ids), "item_ids": ids}


def check_empty_column_history(history):
    ids = [h["id"] for h in history if not h.get("column_history")]
    return {"count": len(ids), "item_ids": ids}


def check_snapshot_history_mismatch(work_items, history):
    history_by_id = {h["id"]: h for h in history}
    mismatches = []
    for item in work_items:
        item_id = item["id"]
        h = history_by_id.get(item_id)
        if not h:
            continue
        col_spans = [s for s in h.get("column_history", []) if s.get("left") is None]
        if not col_spans:
            continue
        last_history_col = col_spans[-1]["value"]
        snapshot_col = item.get("column")
        if snapshot_col and last_history_col and snapshot_col != last_history_col:
            mismatches.append({
                "id": item_id,
                "snapshot_column": snapshot_col,
                "last_history_column": last_history_col,
            })
    return {"count": len(mismatches), "items": mismatches}


def check_unknown_swimlanes(work_items, context):
    # null/None swimlane = item is in the default swimlane; always valid.
    known_lanes = {s["name"] for s in context.get("swimlanes", [])}
    unknown = []
    for item in work_items:
        lane = item.get("swimlane")
        if lane and lane not in known_lanes:
            unknown.append({"id": item["id"], "swimlane": lane})
    return {"count": len(unknown), "items": unknown}


def check_unknown_states_in_history(history, context):
    known_states = set()
    for mappings in context.get("state_mappings", {}).values():
        known_states.update(mappings.values())
    unknown = []
    for h in history:
        for span in h.get("state_history", []):
            val = span.get("value")
            if val and val not in known_states:
                unknown.append({"id": h["id"], "state": val, "entered": span["entered"]})
    return {
        "count": len(unknown),
        "items": unknown,
        "note": "Time-sensitive — re-evaluate once analysis timeframe is set.",
    }


def check_unknown_historical_columns(history, context):
    current_cols = {c["name"] for c in context.get("columns", [])} | VIRTUAL_COLUMN_VALUES
    found = {}
    for h in history:
        for span in h.get("column_history", []):
            val = span.get("value")
            if val and val not in current_cols:
                found.setdefault(val, []).append(h["id"])
    items = [{"column": col, "item_ids": ids} for col, ids in sorted(found.items())]
    return {
        "count": len(found),
        "unknown_columns": items,
        "note": "Time-sensitive — re-evaluate once analysis timeframe is set.",
    }


def check_short_dwell_spans(history, threshold_minutes):
    flagged = []
    for h in history:
        short_spans = []
        for span in h.get("column_history", []):
            minutes = dwell_minutes(span)
            if minutes is not None and minutes < threshold_minutes:
                short_spans.append({
                    "column": span["value"],
                    "entered": span["entered"],
                    "left": span["left"],
                    "dwell_minutes": round(minutes, 1),
                })
        if short_spans:
            flagged.append({"id": h["id"], "short_spans": short_spans})
    return {
        "threshold_minutes": threshold_minutes,
        "count": len(flagged),
        "items": flagged,
        "note": "Short dwell spans are likely accidental moves. Consider ignoring these when evaluating regressions.",
    }


def check_future_column_timestamps(history):
    """Flag items where any column_history 'entered' timestamp is in the future.

    A future 'entered' timestamp causes a negative days_in_current_column value,
    which corrupts WIP age metrics and can pollute AI analysis summaries.
    Root cause is typically a null or malformed timestamp stored by ADO.
    """
    now = datetime.now(timezone.utc)
    flagged = []
    for h in history:
        future_spans = []
        for span in h.get("column_history", []):
            entered = parse_dt(span.get("entered"))
            if entered and entered > now:
                future_spans.append({
                    "column": span.get("value"),
                    "entered": span.get("entered"),
                })
        if future_spans:
            flagged.append({"id": h["id"], "future_spans": future_spans})
    return {
        "count": len(flagged),
        "items": flagged,
        "note": "Items with future 'entered' timestamps produce negative days_in_current_column. "
                "Fix the source timestamp in ADO or exclude the affected column span.",
    }


def check_unassigned_in_progress(work_items, context):
    in_progress_cols = {
        c["name"] for c in context.get("columns", []) if c.get("column_type") == "inProgress"
    }
    unassigned = [
        {"id": item["id"], "column": item.get("column")}
        for item in work_items
        if item.get("column") in in_progress_cols and not item.get("assignee")
    ]
    return {"count": len(unassigned), "items": unassigned}


def check_broken_references(work_items):
    item_ids = {item["id"] for item in work_items}
    broken_parents, broken_children, broken_deps = [], [], []

    for item in work_items:
        item_id = item["id"]
        parent = item.get("parent_id")
        if parent and parent not in item_ids:
            broken_parents.append({"id": item_id, "parent_id": parent})
        for child in item.get("children_ids", []):
            if child not in item_ids:
                broken_children.append({"id": item_id, "child_id": child})
        for dep in item.get("dependency_ids", []):
            if dep not in item_ids:
                broken_deps.append({"id": item_id, "dependency_id": dep})

    return {
        "broken_parents": {"count": len(broken_parents), "items": broken_parents},
        "broken_children": {"count": len(broken_children), "items": broken_children},
        "broken_dependencies": {"count": len(broken_deps), "items": broken_deps},
    }


# --- Rework -----------------------------------------------------------------


def compute_rework(history, context):
    board_column_names = [c["name"] for c in context.get("columns", [])]
    board_col_index = {name: i for i, name in enumerate(board_column_names)}
    outgoing_cols = {c["name"] for c in context.get("columns", []) if c.get("column_type") == "outgoing"}

    results = []
    for h in history:
        col_history = h.get("column_history", [])
        item_id = h["id"]

        # backward_column_moves: regressions between real columns + moves to Backlog from a real column
        backward_moves = 0
        first_backward_at = None
        for i in range(1, len(col_history)):
            prev = col_history[i - 1]["value"]
            curr = col_history[i]["value"]
            is_backward = (
                (curr not in board_col_index and prev in board_col_index)
                or (prev in board_col_index and curr in board_col_index
                    and board_col_index[curr] < board_col_index[prev])
            )
            if is_backward:
                backward_moves += 1
                if first_backward_at is None:
                    first_backward_at = col_history[i]["entered"]

        # reopened_after_done: item entered outgoing column, then moved elsewhere
        reopened = False
        entered_done = False
        for span in col_history:
            if span["value"] in outgoing_cols:
                entered_done = True
            elif entered_done:
                reopened = True
                break

        # revisited_columns: any column visited more than once
        col_visit_count = {}
        for span in col_history:
            val = span["value"]
            col_visit_count[val] = col_visit_count.get(val, 0) + 1
        revisited = sorted(
            (col for col, count in col_visit_count.items() if count > 1 and col is not None),
            key=lambda x: x,
        )

        # time_after_first_backward_move_hours: from first backward move to outgoing entry (or now)
        time_after_first_backward = None
        if first_backward_at:
            start_dt = parse_dt(first_backward_at)
            # Use first outgoing column entry after the backward move, or now
            end_dt = datetime.now(timezone.utc)
            for span in col_history:
                if span["value"] in outgoing_cols and (parse_dt(span["entered"]) or datetime.min.replace(tzinfo=timezone.utc)) >= start_dt:
                    end_dt = parse_dt(span["entered"]) or end_dt
                    break
            if start_dt:
                time_after_first_backward = round((end_dt - start_dt).total_seconds() / 3600, 1)

        # time_in_revisited_columns_hours: sum of 2nd+ visit dwell times across all columns
        col_visit_counter = {}
        revisit_hours = 0.0
        for span in col_history:
            val = span["value"]
            col_visit_counter[val] = col_visit_counter.get(val, 0) + 1
            if col_visit_counter[val] >= 2:
                minutes = dwell_minutes(span)
                if minutes is not None:
                    revisit_hours += minutes / 60

        results.append({
            "work_item_id": item_id,
            "rework_summary": {
                "backward_column_moves": backward_moves,
                "reopened_after_done": reopened,
                "revisited_columns": revisited,
                "time_after_first_backward_move_hours": time_after_first_backward,
                "time_in_revisited_columns_hours": round(revisit_hours, 1),
            },
        })
    return results


# --- Main --------------------------------------------------------------------


def main():
    if Path("output/data/data_quality_report.json").exists():
        print("Skipping: output/data/data_quality_report.json already exists. Delete it to regenerate.")
        sys.exit(0)

    threshold = DEFAULT_SHORT_DWELL_MINUTES
    if "--short-dwell-minutes" in sys.argv:
        idx = sys.argv.index("--short-dwell-minutes")
        try:
            threshold = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("Error: --short-dwell-minutes requires an integer value.", file=sys.stderr)
            sys.exit(1)

    context = load("output/data/context.json")
    work_items = load("output/data/work_items.json")
    history = load("output/data/work_item_history.json")

    refs = check_broken_references(work_items)
    short_dwells = check_short_dwell_spans(history, threshold)
    missing_entry = check_missing_board_entry(history)
    empty_col = check_empty_column_history(history)
    mismatch = check_snapshot_history_mismatch(work_items, history)
    unknown_lanes = check_unknown_swimlanes(work_items, context)
    future_timestamps = check_future_column_timestamps(history)

    # Build exclusion list: items with data problems that would corrupt analysis
    excluded = {}
    for item_id in missing_entry["item_ids"]:
        excluded.setdefault(item_id, []).append("missing_board_entry_date")
    for item_id in empty_col["item_ids"]:
        excluded.setdefault(item_id, []).append("empty_column_history")
    for item in mismatch["items"]:
        excluded.setdefault(item["id"], []).append("snapshot_history_mismatch")
    for item in unknown_lanes["items"]:
        excluded.setdefault(item["id"], []).append("unknown_swimlane")

    excluded_list = [
        {"id": item_id, "reasons": reasons}
        for item_id, reasons in sorted(excluded.items())
    ]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_items": len(work_items),
        "excluded_item_ids": excluded_list,
        "checks": {
            "missing_board_entry_date": missing_entry,
            "empty_column_history": empty_col,
            "snapshot_history_mismatch": mismatch,
            "unknown_swimlanes": unknown_lanes,
            "unknown_states_in_history": check_unknown_states_in_history(history, context),
            "historical_unknown_columns": check_unknown_historical_columns(history, context),
            "short_dwell_spans": short_dwells,
            "future_column_timestamps": future_timestamps,
            "unassigned_in_progress": check_unassigned_in_progress(work_items, context),
            "broken_parent_references": refs["broken_parents"],
            "broken_child_references": refs["broken_children"],
            "broken_dependency_references": refs["broken_dependencies"],
        },
    }

    # Print summary to terminal
    print(f"Data quality report — {len(work_items)} items\n")
    checks = report["checks"]
    rows = [
        ("Missing board entry date",     checks["missing_board_entry_date"]["count"]),
        ("Empty column history",          checks["empty_column_history"]["count"]),
        ("Snapshot / history mismatch",   checks["snapshot_history_mismatch"]["count"]),
        ("Unknown swimlanes",             checks["unknown_swimlanes"]["count"]),
        ("Unknown states in history *",   checks["unknown_states_in_history"]["count"]),
        ("Historical unknown columns *",  checks["historical_unknown_columns"]["count"]),
        (f"Short dwell spans (<{threshold}m)", checks["short_dwell_spans"]["count"]),
        ("Future column entry timestamps !",  checks["future_column_timestamps"]["count"]),
        ("Unassigned in-progress items",      checks["unassigned_in_progress"]["count"]),
        ("Broken parent references",      checks["broken_parent_references"]["count"]),
        ("Broken child references",       checks["broken_child_references"]["count"]),
        ("Broken dependency references",  checks["broken_dependency_references"]["count"]),
    ]
    for label, count in rows:
        flag = "  !" if count else "   "
        print(f"  {flag}  {label:45}  {count}")

    print("\n  * Time-sensitive — re-evaluate once analysis timeframe is set.")
    print(f"\n  Items excluded from analysis: {len(excluded_list)}")
    for item in excluded_list:
        print(f"    id {item['id']:>10}  —  {', '.join(item['reasons'])}")

    out = Path("output/data/data_quality_report.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"\nReport saved to: {out}")
    # Write unified exclusion list — read by metrics.py and dashboard
    # Source: data quality checks (this file). metrics.py will append its own exclusions.
    excluded_items = [
        {
            "id": item["id"],
            "source": "data_quality",
            "reasons": item["reasons"],
        }
        for item in excluded_list
    ]
    excluded_path = Path("output/data/excluded_items.json")
    excluded_path.write_text(json.dumps(excluded_items, indent=2))
    print(f"Exclusions saved to: {excluded_path}  ({len(excluded_items)} items)")
    rework = compute_rework(history, context)
    rework_path = Path("output/data/work_item_rework.json")
    rework_path.write_text(json.dumps(rework, indent=2))
    items_with_rework = sum(1 for r in rework if r["rework_summary"]["backward_column_moves"] > 0)
    print(f"Rework saved to: {rework_path}  ({items_with_rework}/{len(rework)} items have backward moves)")


if __name__ == "__main__":
    main()
