"""
AI interpretation — computes flow metrics summary and builds prompts for chart insights
and a holistic leadership overview.

Reads:
  output/metrics/cycle_time.json
  output/metrics/lead_time.json
  output/metrics/time_in_columns.json
  output/data/context.json
  output/data/config.json
  output/data/work_items.json
  output/data/work_item_history.json

Writes:
  output/data/ai_interpret_metrics.prompt.md  open in VS Code chat or paste into any AI

Usage:
  python src/ai_interpret_metrics.py
  python src/ai_interpret_metrics.py --dump-summary
"""

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR    = Path("output/data")
METRICS_DIR = Path("output/metrics")
PROMPTS_DIR = Path(__file__).parent / "prompts"

CT_PATH      = METRICS_DIR / "cycle_time.json"
LT_PATH      = METRICS_DIR / "lead_time.json"
TIC_PATH     = METRICS_DIR / "time_in_columns.json"
CTX_PATH     = DATA_DIR / "context.json"
CFG_PATH     = DATA_DIR / "config.json"
WI_PATH      = DATA_DIR / "work_items.json"
WIH_PATH     = DATA_DIR / "work_item_history.json"

PROMPT_MD_PATH  = DATA_DIR / "ai_interpret_metrics.prompt.md"
INSIGHTS_PATH   = DATA_DIR / "insights.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load(path, required=True):
    p = Path(path)
    if not p.exists():
        if required:
            print(f"Error: required file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _parse_dt(s):
    if not s:
        return None
    s = s.rstrip("Z").split("+")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _week_start(dt):
    """ISO Monday of the week containing dt."""
    d = dt - __import__("datetime").timedelta(days=dt.weekday())
    return d.strftime("%Y-%m-%d")


def _linreg_slope(pairs):
    """Slope of linear regression on [(x, y), ...]. x can be float."""
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    num = sum((x - mx) * (y - my) for x, y in pairs)
    den = sum((x - mx) ** 2 for x, _ in pairs)
    return num / den if den else 0.0


def _round(v, d=1):
    return round(v, d) if v is not None else None


def _load_signals(config):
    """Return list of {tags (lowercase list), label, color} from blocked_time.signals.
    Last entry = highest priority (matches ADO card rule evaluation order).
    Supports both 'tags' (list) and legacy 'tag' (single string).
    Falls back to Blocked/On Hold if config has no tag signals."""
    raw = (config or {}).get("blocked_time", {}).get("signals", [])
    result = []
    for s in raw:
        if s.get("mechanism") != "tag":
            continue
        raw_tags = s.get("tags") or ([s["tag"]] if s.get("tag") else [])
        if not raw_tags:
            continue
        result.append({
            "tags":  [t.lower() for t in raw_tags],
            "label": s.get("label", raw_tags[0]),
            "color": s.get("color", "#fc8181"),
        })
    return result or [
        {"tags": ["blocked"], "label": "Blocked",  "color": "#fc8181"},
        {"tags": ["hold"],    "label": "On Hold",   "color": "#63b3ed"},
    ]


def _item_signal(signals, tags):
    """Return label of highest-priority matching signal for these tags, or None."""
    tags_lower = {t.lower() for t in (tags or [])}
    match = None
    for sig in signals:
        if any(t in tags_lower for t in sig["tags"]):
            match = sig["label"]
    return match


def _type_breakdown(items, field="cycle_time_days"):
    by_type = defaultdict(list)
    for i in items:
        by_type[i.get("type", "Unknown")].append(i.get(field, 0) or 0)
    return {
        t: {"n": len(vs), "mean": _round(sum(vs)/len(vs)), "median": _round(sorted(vs)[len(vs)//2])}
        for t, vs in sorted(by_type.items(), key=lambda x: -len(x[1]))
    }


# ---------------------------------------------------------------------------
# Summary builders (one per chart / section)
# ---------------------------------------------------------------------------

def summarise_cycle_time(ct):
    if not ct:
        return None
    items = ct.get("items", [])
    overall = ct.get("overall", {})
    weekly = ct.get("weekly_stats", [])

    # Trend: slope of weekly mean_days over time (days per week)
    trend_pts = [
        (i, w["mean_days"])
        for i, w in enumerate(weekly)
        if w.get("n", 0) > 0 and w.get("mean_days") is not None
    ]
    slope = _linreg_slope(trend_pts)  # days per weekly bucket
    slope_per_week = _round(slope, 2) if slope is not None else None

    # Items above P85
    p85 = overall.get("p85_days", 0)
    outlier_count = sum(1 for i in items if (i.get("cycle_time_days") or 0) > p85)

    return {
        "chart": "cycle_time_histogram",
        "window_days": ct.get("window", {}).get("parameter"),
        "item_count": overall.get("n"),
        "mean_days": _round(overall.get("mean_days")),
        "median_days": _round(overall.get("median_days")),
        "p85_days": _round(overall.get("p85_days")),
        "min_days": _round(overall.get("min_days")),
        "max_days": _round(overall.get("max_days")),
        "weekly_trend_slope_days_per_week": slope_per_week,
        "trend_direction": (
            "improving" if slope_per_week and slope_per_week < -0.1
            else "worsening" if slope_per_week and slope_per_week > 0.1
            else "stable"
        ),
        "items_above_p85": outlier_count,
        "by_type": _type_breakdown(items, "cycle_time_days"),
    }


def summarise_lead_time(lt):
    if not lt:
        return None
    items = lt.get("items", [])
    overall = lt.get("overall", {})
    weekly = lt.get("weekly_stats", [])

    trend_pts = [
        (i, w["mean_days"])
        for i, w in enumerate(weekly)
        if w.get("n", 0) > 0 and w.get("mean_days") is not None
    ]
    slope = _linreg_slope(trend_pts)
    slope_per_week = _round(slope, 2) if slope is not None else None

    return {
        "chart": "lead_time_histogram",
        "item_count": overall.get("n"),
        "mean_days": _round(overall.get("mean_days")),
        "median_days": _round(overall.get("median_days")),
        "p85_days": _round(overall.get("p85_days")),
        "min_days": _round(overall.get("min_days")),
        "max_days": _round(overall.get("max_days")),
        "weekly_trend_slope_days_per_week": slope_per_week,
        "trend_direction": (
            "improving" if slope_per_week and slope_per_week < -0.1
            else "worsening" if slope_per_week and slope_per_week > 0.1
            else "stable"
        ),
        "by_type": _type_breakdown(items, "lead_time_days"),
    }


def summarise_throughput(ct):
    if not ct:
        return None
    items = ct.get("items", [])
    window = ct.get("window", {})

    # Weekly counts
    by_week = defaultdict(int)
    for i in items:
        dt = _parse_dt(i.get("completed_at"))
        if dt:
            by_week[_week_start(dt)] += 1

    weeks_sorted = sorted(by_week)
    counts = [by_week[w] for w in weeks_sorted]

    # Window total weeks
    w_start = _parse_dt(window.get("start"))
    w_end   = _parse_dt(window.get("end"))
    total_weeks = max(1, round((w_end - w_start).days / 7)) if w_start and w_end else len(counts) or 1
    avg_per_week = _round(len(items) / total_weeks)

    trend_pts = [(i, c) for i, c in enumerate(counts)]
    slope = _linreg_slope(trend_pts)
    slope_per_week = _round(slope, 2) if slope is not None else None

    # Weeks with zero throughput
    zero_weeks = total_weeks - len(weeks_sorted)

    return {
        "chart": "throughput",
        "total_items": len(items),
        "total_weeks": total_weeks,
        "avg_per_week": avg_per_week,
        "weeks_with_zero_completions": zero_weeks,
        "weekly_trend_slope": slope_per_week,
        "trend_direction": (
            "improving" if slope_per_week and slope_per_week > 0.05
            else "worsening" if slope_per_week and slope_per_week < -0.05
            else "stable"
        ),
        "by_type": {
            t: sum(1 for i in items if i.get("type") == t)
            for t in sorted(set(i.get("type", "Unknown") for i in items))
        },
    }


def summarise_time_in_columns(tic):
    if not tic:
        return None
    cols = tic.get("columns", [])
    in_progress = [c for c in cols if c.get("column_type") == "inProgress"]
    if not in_progress:
        return None

    avg_total = sum(c["mean_hours"] for c in in_progress) / len(in_progress)
    col_stats = []
    for c in in_progress:
        ratio = _round(c["mean_hours"] / avg_total, 2) if avg_total else None
        col_stats.append({
            "column": c["name"],
            "mean_hours": _round(c["mean_hours"]),
            "median_hours": _round(c["median_hours"]),
            "items_through": c.get("n"),
            "ratio_vs_avg": ratio,
        })

    bottleneck = max(in_progress, key=lambda c: c["mean_hours"])
    return {
        "chart": "time_in_columns",
        "columns": col_stats,
        "bottleneck_column": bottleneck["name"],
        "bottleneck_mean_hours": _round(bottleneck["mean_hours"]),
        "bottleneck_ratio_vs_avg": _round(bottleneck["mean_hours"] / avg_total, 2) if avg_total else None,
    }


def summarise_flow_efficiency(tic, cfg):
    if not tic or not cfg:
        return None
    fe_cfg = cfg.get("flow_efficiency", {})
    active_cols  = set(fe_cfg.get("active_columns", []))
    waiting_cols = set(fe_cfg.get("waiting_columns", []))
    if not active_cols and not waiting_cols:
        return None

    tic_items = tic.get("items", [])

    # Auto-include any split "(Done)" sub-columns as waiting if not explicitly classified
    for item in tic_items:
        for col in item.get("column_hours", {}):
            if col.endswith(" (Done)") and col not in active_cols and col not in waiting_cols:
                waiting_cols.add(col)
    effs = []
    for ti in tic_items:
        ch = ti.get("column_hours", {})
        active_h  = sum(ch.get(c, 0) for c in active_cols)
        waiting_h = sum(ch.get(c, 0) for c in waiting_cols)
        total_h   = active_h + waiting_h
        if total_h > 0:
            effs.append(active_h / total_h * 100)

    if not effs:
        return None
    effs_sorted = sorted(effs)
    mean_eff  = _round(sum(effs) / len(effs))
    median_eff = _round(effs_sorted[len(effs_sorted) // 2])
    p85_eff   = _round(effs_sorted[int(len(effs_sorted) * 0.85)])

    return {
        "chart": "flow_efficiency",
        "item_count": len(effs),
        "active_columns": sorted(active_cols),
        "waiting_columns": sorted(waiting_cols),
        "mean_pct": mean_eff,
        "median_pct": median_eff,
        "p85_pct": p85_eff,
        "items_below_20pct": sum(1 for e in effs if e < 20),
        "items_above_40pct": sum(1 for e in effs if e >= 40),
    }


def summarise_work_start_efficiency(ct, lt):
    if not ct or not lt:
        return None
    lt_map = {i["id"]: i for i in lt.get("items", [])}
    pairs = []
    for ci in ct.get("items", []):
        li = lt_map.get(ci["id"])
        if li and li.get("lead_time_days", 0) > 0:
            eff = ci["cycle_time_days"] / li["lead_time_days"] * 100
            if 0 <= eff <= 100:
                wait = li["lead_time_days"] - ci["cycle_time_days"]
                pairs.append({"eff": eff, "wait_days": wait})

    if not pairs:
        return None
    effs   = sorted(p["eff"]  for p in pairs)
    waits  = sorted(p["wait_days"] for p in pairs)
    mean_eff  = _round(sum(effs)  / len(effs))
    mean_wait = _round(sum(waits) / len(waits))

    return {
        "chart": "work_start_efficiency",
        "item_count": len(pairs),
        "mean_efficiency_pct": mean_eff,
        "median_efficiency_pct": _round(effs[len(effs) // 2]),
        "mean_wait_before_dev_days": mean_wait,
        "median_wait_before_dev_days": _round(waits[len(waits) // 2]),
        "items_below_50pct": sum(1 for e in effs if e < 50),
    }


def summarise_wip(wi, ctx, ct):
    if not wi or not ctx:
        return None
    in_prog_cols = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "inProgress"}
    out_cols     = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "outgoing"}

    current_wip = [i for i in wi if i.get("column") in in_prog_cols]
    by_col = defaultdict(int)
    for i in current_wip:
        by_col[i["column"]] += 1

    # WIP limit violations
    col_limits = {c["name"]: c.get("wip_limit") for c in ctx.get("columns", [])}
    violations = {col: cnt for col, cnt in by_col.items()
                  if col_limits.get(col) and cnt > col_limits[col]}

    return {
        "chart": "wip",
        "current_wip": len(current_wip),
        "by_column": dict(by_col),
        "wip_limit_violations": violations,
        "by_type": {
            t: sum(1 for i in current_wip if i.get("type") == t)
            for t in sorted(set(i.get("type", "Unknown") for i in current_wip))
        },
    }


def summarise_blockers(wi, wih, ctx, ct, cfg=None):
    if not wi or not ctx:
        return None

    signals      = _load_signals(cfg)
    all_sig_tags = {t for s in signals for t in s["tags"]}

    win_start_ms = None
    win_end_ms   = None
    if ct:
        ws = _parse_dt(ct.get("window", {}).get("start"))
        we = _parse_dt(ct.get("window", {}).get("end"))
        if ws: win_start_ms = ws.timestamp() * 1000
        if we: win_end_ms   = we.timestamp() * 1000

    out_cols  = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "outgoing"}

    def parse_tags(s):
        return [t.strip().lower() for t in (s or "").split(";") if t.strip()]

    def has_sig(s): return any(t in all_sig_tags for t in parse_tags(s))

    tag_hist_map = {}
    if wih:
        for h in wih:
            tag_hist_map[h["id"]] = [
                e for e in h.get("tag_history", [])
                if e.get("field") == "System.Tags"
            ]

    def get_intervals(item_id, current_tags):
        tl     = [t.lower() for t in (current_tags or [])]
        is_any = any(t in all_sig_tags for t in tl)
        history = sorted(tag_hist_map.get(item_id, []), key=lambda e: e.get("changed_at", ""))
        start = None
        ivs   = []
        for ev in history:
            ms = _parse_dt(ev.get("changed_at"))
            if not ms: continue
            ms = ms.timestamp() * 1000
            had, now_ = has_sig(ev.get("old_value")), has_sig(ev.get("new_value"))
            if not had and now_: start = ms
            if had and not now_ and start: ivs.append((start, ms)); start = None
        if start and is_any and win_end_ms: ivs.append((start, win_end_ms))

        def clip(a, b):
            s = max(a, win_start_ms or a)
            e = min(b, win_end_ms   or b)
            return (s, e) if e > s else None

        return [r for r in (clip(*iv) for iv in ivs) if r]

    def sum_days(ivs):
        return sum((e - s) / 86400000 for s, e in ivs)

    def merge(ivs):
        sorted_ivs = sorted(ivs)
        merged = []
        for iv in sorted_ivs:
            if merged and iv[0] <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], iv[1]))
            else:
                merged.append(list(iv))
        return merged

    active_blocked = [i for i in wi if i.get("column") not in out_cols
                      and any(t.lower() in all_sig_tags for t in (i.get("tags") or []))]

    total_days_lost = 0
    by_col = defaultdict(int)
    max_days = 0
    for item in active_blocked:
        merged = merge(get_intervals(item["id"], item.get("tags")))
        days = sum_days(merged)
        total_days_lost += days
        by_col[item.get("column", "Unknown")] += 1
        if days > max_days:
            max_days = days

    # Count all items (any column/state) carrying each signal tag
    def item_has_sig(item, sig):
        item_tags = [t.strip().lower() for t in (item.get("tags") or []) if t.strip()]
        return any(t in item_tags for t in sig["tags"])

    by_signal = {sig["label"]: sum(1 for i in wi if item_has_sig(i, sig)) for sig in signals}

    return {
        "chart": "blockers",
        "currently_blocked_count": len(active_blocked),
        "total_days_lost_to_blocking": _round(total_days_lost),
        "longest_single_block_days": _round(max_days),
        "blocked_by_column": dict(by_col),
        "blocked_by_signal": by_signal,
    }


def summarise_net_flow(ct, ctx, wih):
    if not ct or not ctx:
        return None
    cols = ctx.get("columns", [])
    first_in_prog = next((c["name"] for c in cols if c.get("column_type") == "inProgress"), None)
    if not first_in_prog:
        return None

    window  = ct.get("window", {})
    w_start = _parse_dt(window.get("start"))
    w_end   = _parse_dt(window.get("end"))
    if not w_start or not w_end:
        return None

    ws_ms = w_start.timestamp() * 1000
    we_ms = w_end.timestamp() * 1000
    total_weeks = max(1, round((w_end - w_start).days / 7))

    # Completions per week
    completions_by_week = defaultdict(int)
    for item in ct.get("throughput_items", ct.get("items", [])):
        dt = _parse_dt(item.get("completed_at"))
        if dt and ws_ms <= dt.timestamp() * 1000 <= we_ms:
            completions_by_week[_week_start(dt)] += 1

    # Arrivals per week: entries into the first in-progress column
    arrivals_by_week = defaultdict(int)
    if wih:
        for h in wih:
            for seg in h.get("column_history", []):
                if seg.get("value") == first_in_prog:
                    entered = _parse_dt(seg.get("entered"))
                    if entered and ws_ms <= entered.timestamp() * 1000 <= we_ms:
                        arrivals_by_week[_week_start(entered)] += 1

    all_weeks = sorted(set(list(completions_by_week.keys()) + list(arrivals_by_week.keys())))
    weekly = [
        {
            "week_start": w,
            "arrived":    arrivals_by_week.get(w, 0),
            "completed":  completions_by_week.get(w, 0),
            "net_flow":   completions_by_week.get(w, 0) - arrivals_by_week.get(w, 0),
        }
        for w in all_weeks
    ]

    total_arrived  = sum(arrivals_by_week.values())
    total_completed = sum(completions_by_week.values())
    arr_rate = _round(total_arrived / total_weeks)
    dep_rate = _round(total_completed / total_weeks)

    return {
        "chart": "net_flow",
        "total_weeks": total_weeks,
        "arrived_items": total_arrived,
        "completed_items": total_completed,
        "arrival_rate_per_week": arr_rate,
        "departure_rate_per_week": dep_rate,
        "weekly": weekly,
    }


def summarise_ad_ratio(ct, ctx, wih):
    """Arrival/Departure ratio per in-progress column."""
    if not ct or not ctx or not wih:
        return None
    window = ct.get("window", {})
    win_start_ms = _parse_dt(window.get("start"))
    win_end_ms   = _parse_dt(window.get("end"))
    if not win_start_ms or not win_end_ms:
        return None
    ws_ms = win_start_ms.timestamp() * 1000
    we_ms = win_end_ms.timestamp() * 1000

    col_names = {c["name"] for c in ctx.get("columns", [])}
    in_prog   = [c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "inProgress"]

    arr = defaultdict(int)
    dep = defaultdict(int)
    for h in wih:
        for seg in h.get("column_history", []):
            col = seg.get("value")
            if col not in col_names: continue
            ent_ms = _parse_dt(seg.get("entered"))
            if ent_ms and ws_ms <= ent_ms.timestamp() * 1000 <= we_ms:
                arr[col] += 1
            if seg.get("left"):
                left_ms = _parse_dt(seg.get("left"))
                if left_ms and ws_ms <= left_ms.timestamp() * 1000 <= we_ms:
                    dep[col] += 1

    ratios = []
    for col in in_prog:
        if dep[col] > 0:
            ratios.append({
                "column": col,
                "arrivals": arr[col],
                "departures": dep[col],
                "ratio": _round(arr[col] / dep[col], 2),
                "status": (
                    "accumulating" if arr[col] / dep[col] > 1.1
                    else "draining"  if arr[col] / dep[col] < 0.9
                    else "balanced"
                ),
            })
    return {
        "chart": "arrival_departure_ratio",
        "columns": ratios,
        "accumulating_columns": [r["column"] for r in ratios if r["status"] == "accumulating"],
        "draining_columns":     [r["column"] for r in ratios if r["status"] == "draining"],
    }


def summarise_cfd(ct, ctx, wih):
    """Snapshot CFD summary: all items on the board positioned at their furthest column
    at each weekly snapshot. Mirrors the dashboard JS model."""
    from datetime import timedelta
    if not ct or not ctx or not wih:
        return None
    window = ct.get("window", {})
    win_start = _parse_dt(window.get("start"))
    win_end   = _parse_dt(window.get("end"))
    if not win_start or not win_end:
        return None

    all_cols = [c["name"] for c in ctx.get("columns", [])]
    if len(all_cols) < 2:
        return None
    col_order = {col: i for i, col in enumerate(all_cols)}

    # Weekly buckets aligned to Monday of the window start week
    def _mon(dt):
        return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    cur = _mon(win_start)
    weeks = []
    while cur <= win_end:
        weeks.append(cur)
        cur += timedelta(weeks=1)
    if not weeks:
        return None

    week_end_ts  = [(w + timedelta(weeks=1) - timedelta(microseconds=1)).timestamp() for w in weeks]

    # Build item progressions for all items on the board (mirrors JS itemProgressions)
    progressions = []
    for entry in wih:
        events = []
        for seg in entry.get("column_history", []):
            idx = col_order.get(seg.get("value"))
            if idx is None:
                continue
            ent = _parse_dt(seg.get("entered"))
            if ent:
                events.append((ent.timestamp(), idx))
        if not events:
            continue
        events.sort()
        sorted_ts, max_idx_at, run_max = [], [], -1
        for ts, idx in events:
            run_max = max(run_max, idx)
            sorted_ts.append(ts)
            max_idx_at.append(run_max)
        progressions.append((sorted_ts, max_idx_at))

    if not progressions:
        return None

    def _max_idx_at_snap(prog, snap_ts):
        ts_list, idx_list = prog
        lo, hi, res = 0, len(ts_list) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if ts_list[mid] <= snap_ts:
                res = mid; lo = mid + 1
            else:
                hi = mid - 1
        return idx_list[res] if res >= 0 else -1

    # cumAtOrBeyond[col_i][week_i] = cohort items at or beyond col_i by end of week
    n_cols  = len(all_cols)
    n_weeks = len(weeks)
    cum = [[0] * n_weeks for _ in range(n_cols)]
    for wi_idx in range(n_weeks):
        snap_ts = week_end_ts[wi_idx]
        for prog in progressions:
            mi = _max_idx_at_snap(prog, snap_ts)
            if mi >= 0:
                for col_i in range(mi + 1):
                    cum[col_i][wi_idx] += 1

    def _slope(values):
        n = len(values)
        return _round((values[-1] - values[0]) / (n - 1), 1) if n >= 2 else 0

    arrival_rate   = _slope(cum[0])       # slope of first col = new arrivals per week during window
    departure_rate = _slope(cum[-1])      # slope of last col  = completions per week during window

    # Average band width per column (items currently in that column per week)
    band_avgs = {}
    for col_i, col in enumerate(all_cols):
        if col_i < n_cols - 1:
            vals = [cum[col_i][w] - cum[col_i + 1][w] for w in range(n_weeks)]
        else:
            vals = cum[col_i]
        band_avgs[col] = _round(sum(vals) / len(vals), 1) if vals else 0

    # Widening bands: second-half average > first-half by >30 %
    mid = max(1, n_weeks // 2)
    widening = []
    for col_i, col in enumerate(all_cols):
        if col_i < n_cols - 1:
            vals = [cum[col_i][w] - cum[col_i + 1][w] for w in range(n_weeks)]
        else:
            vals = cum[col_i]
        if len(vals) >= 4:
            fh = sum(vals[:mid]) / mid
            sh = sum(vals[mid:]) / (n_weeks - mid)
            if sh > fh * 1.3:
                widening.append(col)

    acc = arrival_rate or 0
    dep = departure_rate or 0
    return {
        "chart": "cfd",
        "weeks_in_window": n_weeks,
        "total_items_tracked": len(progressions),
        "arrival_rate_per_week": arrival_rate,
        "departure_rate_per_week": departure_rate,
        "accumulation_signal": (
            "growing"   if acc > dep * 1.1
            else "draining" if dep > acc * 1.1
            else "balanced"
        ),
        "avg_items_per_column": band_avgs,
        "widening_bands": widening,
    }


def summarise_ageing_wip(wi, wih, ctx, cfg=None):
    """Current in-progress items with age, time in current column, and blocked status."""
    if not wi or not ctx:
        return None

    now          = datetime.now(timezone.utc)
    in_prog_cols = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "inProgress"}
    signals      = _load_signals(cfg)
    wih_map      = {h["id"]: h for h in (wih or [])}

    items_out = []
    for item in wi:
        if item.get("column") not in in_prog_cols:
            continue
        item_id = item["id"]
        h = wih_map.get(item_id, {})

        # Age: days since board_entry_date, falling back to created_date
        board_entry = _parse_dt(h.get("board_entry_date") or item.get("created_date"))
        age_days    = _round((now - board_entry).total_seconds() / 86400, 1) if board_entry else None

        # Days in current column: latest column_history entry with left=None for this column
        days_in_col = None
        for seg in reversed(h.get("column_history", [])):
            if seg.get("value") == item.get("column") and seg.get("left") is None:
                entered = _parse_dt(seg.get("entered"))
                if entered:
                    days_in_col = _round((now - entered).total_seconds() / 86400, 1)
                    # Guard: a future 'entered' timestamp produces a negative value — treat as unknown
                    if days_in_col is not None and days_in_col < 0:
                        days_in_col = None
                break

        # Days since last update from changed_date
        changed           = _parse_dt(item.get("changed_date"))
        days_since_update = _round((now - changed).total_seconds() / 86400, 1) if changed else None

        # Blocked status from current tags (highest-priority matching signal)
        blocked_status = _item_signal(signals, item.get("tags") or [])

        items_out.append({
            "id":                    item_id,
            "type":                  item.get("type"),
            "current_column":        item.get("column"),
            "age_days":              age_days,
            "days_in_current_column": days_in_col,
            "days_since_last_update": days_since_update,
            "blocked_status":        blocked_status,
        })

    if not items_out:
        return None

    ages = sorted(i["age_days"] for i in items_out if i["age_days"] is not None)

    # Age band distribution
    age_bands = {
        "0_to_7_days":   sum(1 for a in ages if a <= 7),
        "8_to_14_days":  sum(1 for a in ages if 8 <= a <= 14),
        "15_to_30_days": sum(1 for a in ages if 15 <= a <= 30),
        "over_30_days":  sum(1 for a in ages if a > 30),
    }

    # Per-column age percentiles (for wip_age_by_column chart)
    by_col_ages = defaultdict(list)
    for item in items_out:
        if item["age_days"] is not None:
            by_col_ages[item["current_column"]].append(item["age_days"])
    per_column_age = {}
    for col, col_ages in sorted(by_col_ages.items()):
        col_ages_sorted = sorted(col_ages)
        per_column_age[col] = {
            "n":          len(col_ages_sorted),
            "median_days": _round(col_ages_sorted[len(col_ages_sorted) // 2]),
            "p85_days":   _round(col_ages_sorted[int(len(col_ages_sorted) * 0.85)]),
            "max_days":   _round(max(col_ages_sorted)),
        }

    return {
        "total_wip":        len(items_out),
        "median_age_days":  _round(ages[len(ages) // 2]) if ages else None,
        "average_age_days": _round(sum(ages) / len(ages), 1) if ages else None,
        "p85_age_days":     _round(ages[int(len(ages) * 0.85)]) if ages else None,
        "age_bands":        age_bands,
        "per_column_age":   per_column_age,
        "current_items":    sorted(items_out, key=lambda x: -(x.get("age_days") or 0)),
    }


def summarise_stale_work(wi, ctx, threshold_days=30):
    """Items not in done columns with no update for threshold_days or more."""
    if not wi or not ctx:
        return None

    now      = datetime.now(timezone.utc)
    out_cols = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "outgoing"}

    stale = []
    for item in wi:
        if item.get("column") in out_cols:
            continue
        changed = _parse_dt(item.get("changed_date"))
        if not changed:
            continue
        days_since = (now - changed).total_seconds() / 86400
        if days_since >= threshold_days:
            stale.append({
                "id":                    item["id"],
                "type":                  item.get("type"),
                "current_column":        item.get("column"),
                "days_since_last_update": _round(days_since, 1),
                "state":                 item.get("state"),
            })

    if not stale:
        return None

    stale.sort(key=lambda x: -(x["days_since_last_update"] or 0))
    return {
        "threshold_days": threshold_days,
        "count":          len(stale),
        "items":          stale,
    }


def summarise_blocked_items_detail(wi, wih, ctx, ct, cfg=None):
    """Currently blocked/on-hold items with computed days-blocked and last-update age."""
    if not wi or not ctx:
        return None

    signals      = _load_signals(cfg)
    all_sig_tags = {t for s in signals for t in s["tags"]}

    now      = datetime.now(timezone.utc)
    out_cols = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "outgoing"}

    tag_hist_map = {}
    if wih:
        for h in wih:
            tag_hist_map[h["id"]] = [
                e for e in h.get("tag_history", []) if e.get("field") == "System.Tags"
            ]

    win_end  = _parse_dt(ct.get("window", {}).get("end")) if ct else None
    ref_date = win_end or now
    ref_ms   = ref_date.timestamp() * 1000

    def _ptags(s):
        return [t.strip().lower() for t in (s or "").split(";") if t.strip()]

    def _has_sig(s): return any(t in all_sig_tags for t in _ptags(s))

    result = []
    for item in wi:
        if item.get("column") in out_cols:
            continue
        signal_label = _item_signal(signals, item.get("tags") or [])
        if not signal_label:
            continue

        # Compute total blocked time from tag history (all signals combined)
        history  = sorted(tag_hist_map.get(item["id"], []), key=lambda e: e.get("changed_at", ""))
        start    = None
        total_ms = 0
        for ev in history:
            ev_dt = _parse_dt(ev.get("changed_at"))
            if not ev_dt:
                continue
            ms      = ev_dt.timestamp() * 1000
            had, now_ = _has_sig(ev.get("old_value")), _has_sig(ev.get("new_value"))
            if not had and now_:      start = ms
            if had and not now_ and start: total_ms += ms - start; start = None

        if start:  total_ms += ref_ms - start

        days_blocked = _round(total_ms / 86400000, 1) if total_ms > 0 else None
        changed      = _parse_dt(item.get("changed_date"))
        days_since_update = _round((now - changed).total_seconds() / 86400, 1) if changed else None

        result.append({
            "id":                    item["id"],
            "type":                  item.get("type"),
            "current_column":        item.get("column"),
            "blocked_status":        signal_label,
            "days_blocked":          days_blocked,
            "days_since_last_update": days_since_update,
        })

    if not result:
        return None

    result.sort(key=lambda x: -(x.get("days_blocked") or 0))
    return result


def summarise_bugs(wi, ctx, ct):
    """Bug-specific metrics: open count, WIP share, weekly completions."""
    if not wi or not ctx:
        return None

    in_prog_cols = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "inProgress"}
    out_cols     = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "outgoing"}

    all_bugs = [i for i in wi if i.get("type") == "Bug"]
    if not all_bugs:
        return None

    open_bugs = [i for i in all_bugs if i.get("column") not in out_cols]
    by_col    = defaultdict(int)
    for b in open_bugs:
        by_col[b.get("column", "Unknown")] += 1

    total_wip    = [i for i in wi if i.get("column") in in_prog_cols]
    wip_bug_count = sum(1 for i in total_wip if i.get("type") == "Bug")
    bug_pct_wip  = _round(wip_bug_count / len(total_wip) * 100) if total_wip else None

    # Weekly bug completions from throughput_items (includes items without clock_start)
    bug_by_week = defaultdict(int)
    if ct:
        for item in ct.get("throughput_items", ct.get("items", [])):
            if item.get("type") == "Bug":
                dt = _parse_dt(item.get("completed_at"))
                if dt:
                    bug_by_week[_week_start(dt)] += 1

    # Weekly bug creations from created_date on all_bugs within the analysis window
    bug_created_by_week = defaultdict(int)
    if ct:
        window = ct.get("window", {})
        w_start_dt = _parse_dt(window.get("start"))
        w_end_dt   = _parse_dt(window.get("end"))
        if w_start_dt and w_end_dt:
            for item in all_bugs:
                created = _parse_dt(item.get("created_date"))
                if created and w_start_dt <= created <= w_end_dt:
                    bug_created_by_week[_week_start(created)] += 1

    return {
        "open_bug_count":                  len(open_bugs),
        "total_bug_count":                 len(all_bugs),
        "bug_share_of_wip_pct":            bug_pct_wip,
        "open_bug_distribution_by_column": dict(by_col),
        "bug_completions_by_week":         dict(sorted(bug_by_week.items())),
        "bug_creations_by_week":           dict(sorted(bug_created_by_week.items())),
    }


def summarise_throughput_weekly(ct, wih, ctx):
    """Weekly breakdown of completions, starts (first in-progress entry), and net flow."""
    if not ct:
        return None

    window  = ct.get("window", {})
    w_start = _parse_dt(window.get("start"))
    w_end   = _parse_dt(window.get("end"))
    if not w_start or not w_end:
        return None

    ws_ms = w_start.timestamp() * 1000
    we_ms = w_end.timestamp() * 1000

    # Completions per week (all items that completed in the window)
    completions_by_week = defaultdict(int)
    for item in ct.get("throughput_items", ct.get("items", [])):
        dt = _parse_dt(item.get("completed_at"))
        if dt and ws_ms <= dt.timestamp() * 1000 <= we_ms:
            completions_by_week[_week_start(dt)] += 1

    # Starts per week: entries into the first in-progress column within the window
    starts_by_week = defaultdict(int)
    if wih and ctx:
        first_in_prog = next(
            (c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "inProgress"),
            None,
        )
        if first_in_prog:
            for h in wih:
                for seg in h.get("column_history", []):
                    if seg.get("value") == first_in_prog:
                        entered = _parse_dt(seg.get("entered"))
                        if entered and ws_ms <= entered.timestamp() * 1000 <= we_ms:
                            starts_by_week[_week_start(entered)] += 1

    all_weeks = sorted(set(list(completions_by_week.keys()) + list(starts_by_week.keys())))
    return [
        {
            "week_start": w,
            "completed":  completions_by_week.get(w, 0),
            "started":    starts_by_week.get(w, 0),
            "net_flow":   completions_by_week.get(w, 0) - starts_by_week.get(w, 0),
        }
        for w in all_weeks
    ]


# ---------------------------------------------------------------------------
# Assemble all summaries
# ---------------------------------------------------------------------------

def summarise_blocker_timeline(wi, wih, ctx, ct, cfg=None):
    """Weekly count of items that were blocked or on-hold during each week of the analysis window.

    Mirrors the JS logic in blockerTimelineChart: an item is counted in a week
    if its blocking interval overlaps any part of that week.
    """
    if not wi or not wih or not ctx or not ct:
        return None

    signals      = _load_signals(cfg)
    all_sig_tags = {t for s in signals for t in s["tags"]}

    window    = ct.get("window", {})
    win_start = _parse_dt(window.get("start"))
    win_end   = _parse_dt(window.get("end"))
    if not win_start or not win_end:
        return None

    ws_ms    = win_start.timestamp() * 1000
    we_ms    = win_end.timestamp() * 1000
    week_ms  = 7 * 24 * 3600 * 1000
    out_cols = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "outgoing"}

    # Build weeks list (Monday-aligned)
    weeks_ms = []
    w = ws_ms
    while w < we_ms:
        weeks_ms.append(w)
        w += week_ms

    # Tag history index
    tag_hist_map = {}
    for h in wih:
        tag_hist_map[h["id"]] = [
            e for e in h.get("tag_history", []) if e.get("field") == "System.Tags"
        ]

    def _ptags(s):
        return [t.strip().lower() for t in (s or "").split(";") if t.strip()]

    def _has_sig(s): return any(t in all_sig_tags for t in _ptags(s))

    # Per-signal weekly counts (keyed by signal label)
    per_sig_per_week = {s["label"]: defaultdict(int) for s in signals}

    def _clip(s, e):
        cs = max(s, ws_ms)
        ce = min(e, we_ms)
        return (cs, ce) if ce > cs else None

    for item in wi:
        if item.get("column") in out_cols:
            continue

        item_id    = item["id"]
        tags_lower = {t.lower() for t in (item.get("tags") or [])}

        history = sorted(tag_hist_map.get(item_id, []), key=lambda e: e.get("changed_at", ""))

        for sig in signals:
            is_active = any(t in tags_lower for t in sig["tags"])
            sig_start = None
            sig_ivs   = []
            for ev in history:
                ev_dt = _parse_dt(ev.get("changed_at"))
                if not ev_dt:
                    continue
                ms     = ev_dt.timestamp() * 1000
                had    = any(t in _ptags(ev.get("old_value")) for t in sig["tags"])
                has_   = any(t in _ptags(ev.get("new_value")) for t in sig["tags"])
                if not had and has_:             sig_start = ms
                if had and not has_ and sig_start: sig_ivs.append((sig_start, ms)); sig_start = None
            if sig_start and is_active: sig_ivs.append((sig_start, we_ms))
            sig_ivs = [r for r in (_clip(*iv) for iv in sig_ivs) if r]
            for wk_ms in weeks_ms:
                wk_end_ms = wk_ms + week_ms
                wk_str    = datetime.utcfromtimestamp(wk_ms / 1000).strftime("%Y-%m-%d")
                if any(s < wk_end_ms and e > wk_ms for s, e in sig_ivs):
                    per_sig_per_week[sig["label"]][wk_str] += 1

    all_weeks = sorted(set(w for pw in per_sig_per_week.values() for w in pw))
    if not all_weeks:
        return None

    # Combined total for trend calculation
    total_vals = [sum(per_sig_per_week[s["label"]].get(w, 0) for s in signals) for w in all_weeks]
    t_slope = _linreg_slope(list(enumerate(total_vals)))

    return {
        "per_signal_per_week": {
            label: {w: pw.get(w, 0) for w in all_weeks}
            for label, pw in per_sig_per_week.items()
        },
        "peak_any_in_one_week":          max(total_vals, default=0),
        "weeks_with_any_blocked":        sum(1 for v in total_vals if v > 0),
        "total_blocked_trend_direction": (
            "increasing" if t_slope and t_slope > 0.1
            else "decreasing" if t_slope and t_slope < -0.1
            else "stable"
        ),
    }


def summarise_wip_over_time(wih, ctx, ct):
    """Weekly WIP snapshot count for trend analysis (feeds wip_over_time chart insight)."""
    if not wih or not ctx or not ct:
        return None

    window    = ct.get("window", {})
    win_start = _parse_dt(window.get("start"))
    win_end   = _parse_dt(window.get("end"))
    if not win_start or not win_end:
        return None

    in_prog_cols = {c["name"] for c in ctx.get("columns", []) if c.get("column_type") == "inProgress"}
    ws_ms   = win_start.timestamp() * 1000
    we_ms   = win_end.timestamp() * 1000
    week_ms = 7 * 24 * 3600 * 1000

    weeks_ms = []
    w = ws_ms
    while w <= we_ms:
        weeks_ms.append(w)
        w += week_ms

    weekly_wip = []
    for wk_ms in weeks_ms:
        wk_str    = datetime.utcfromtimestamp(wk_ms / 1000).strftime("%Y-%m-%d")
        wip_count = 0
        for h in wih:
            for seg in h.get("column_history", []):
                if seg.get("value") not in in_prog_cols:
                    continue
                entered  = _parse_dt(seg.get("entered"))
                left     = _parse_dt(seg.get("left"))
                ent_ms   = entered.timestamp() * 1000 if entered else 0
                left_ms  = left.timestamp() * 1000 if left else float("inf")
                if ent_ms <= wk_ms < left_ms:
                    wip_count += 1
                    break  # item counted once per snapshot
        weekly_wip.append({"week_start": wk_str, "wip": wip_count})

    if not weekly_wip:
        return None

    wip_values = [entry["wip"] for entry in weekly_wip]
    slope      = _linreg_slope(list(enumerate(wip_values)))

    return {
        "weekly_snapshots":      weekly_wip,
        "mean_wip":              _round(sum(wip_values) / len(wip_values), 1),
        "min_wip":               min(wip_values),
        "max_wip":               max(wip_values),
        "trend_slope_per_week":  _round(slope, 2) if slope is not None else None,
        "trend_direction":       (
            "increasing" if slope and slope > 0.1
            else "decreasing" if slope and slope < -0.1
            else "stable"
        ),
    }


def summarise_wip_level_distribution(wih, ctx, ct):
    """Per-column distribution of daily WIP levels (feeds wip_level_distribution chart insight)."""
    if not wih or not ctx or not ct:
        return None

    window    = ct.get("window", {})
    win_start = _parse_dt(window.get("start"))
    win_end   = _parse_dt(window.get("end"))
    if not win_start or not win_end:
        return None

    in_prog_cols = [c for c in ctx.get("columns", []) if c.get("column_type") == "inProgress"]
    if not in_prog_cols:
        return None

    ws_ms  = win_start.timestamp() * 1000
    we_ms  = win_end.timestamp()  * 1000
    ms_day = 86_400_000
    day_count = math.ceil((we_ms - ws_ms) / ms_day) + 1

    # Build per-column daily WIP arrays (noon-snap, matching the chart logic)
    col_day_counts = {c["name"]: [0] * day_count for c in in_prog_cols}
    col_names_set  = {c["name"] for c in in_prog_cols}

    for entry in wih:
        for seg in entry.get("column_history", []):
            col = seg.get("value")
            if col not in col_names_set:
                continue
            ent = _parse_dt(seg.get("entered"))
            lft = _parse_dt(seg.get("left"))
            ent_ms = ent.timestamp() * 1000 if ent else 0
            lft_ms = lft.timestamp() * 1000 if lft else we_ms + ms_day
            half_day = 12 * 3600 * 1000
            start_di = max(0, math.ceil((ent_ms - ws_ms - half_day) / ms_day))
            end_di   = min(day_count - 1, int((lft_ms - ws_ms - half_day) / ms_day))
            for di in range(start_di, end_di + 1):
                col_day_counts[col][di] += 1

    PCT_THRESHOLD = 1.0  # mirror chart's filter
    result = []
    for col_cfg in in_prog_cols:
        col       = col_cfg["name"]
        wip_limit = col_cfg.get("wip_limit") or 0
        counts    = col_day_counts[col]

        freq = {}
        for v in counts:
            freq[v] = freq.get(v, 0) + 1

        dist = {}
        for level, cnt in freq.items():
            pct = (cnt / day_count) * 100
            if pct >= PCT_THRESHOLD:
                dist[level] = round(pct, 1)

        if not dist:
            continue

        modal_level  = max(dist, key=lambda l: dist[l])
        pct_zero     = dist.get(0, 0.0)
        pct_at_limit = sum(v for l, v in dist.items() if wip_limit > 0 and l >= wip_limit)
        pct_over     = sum(v for l, v in dist.items() if wip_limit > 0 and l >  wip_limit)

        result.append({
            "column":          col,
            "wip_limit":       wip_limit,
            "modal_level":     modal_level,
            "pct_days_empty":  round(pct_zero, 1),
            "pct_days_at_or_above_limit": round(pct_at_limit, 1) if wip_limit > 0 else None,
            "pct_days_over_limit":        round(pct_over, 1)     if wip_limit > 0 else None,
            "level_distribution": dict(sorted((str(l), v) for l, v in dist.items())),
        })

    return result if result else None


def build_summary():
    ct  = load(CT_PATH,  required=False)
    lt  = load(LT_PATH,  required=False)
    tic = load(TIC_PATH, required=True)
    ctx = load(CTX_PATH, required=False)
    cfg = load(CFG_PATH, required=False)
    wi  = load(WI_PATH,  required=False)
    wih = load(WIH_PATH, required=False)

    return {
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "analysis_window":       ct.get("window") if ct else None,
        "board_context":         {"team": ctx.get("team"), "project": ctx.get("project")} if ctx else None,
        "cycle_time":            summarise_cycle_time(ct),
        "lead_time":             summarise_lead_time(lt),
        "throughput":            summarise_throughput(ct),
        "throughput_weekly":     summarise_throughput_weekly(ct, wih, ctx),
        "time_in_columns":       summarise_time_in_columns(tic),
        "flow_efficiency":       summarise_flow_efficiency(tic, cfg),
        "work_start_efficiency": summarise_work_start_efficiency(ct, lt),
        "wip":                   summarise_wip(wi, ctx, ct),
        "wip_over_time":         summarise_wip_over_time(wih, ctx, ct),
        "ageing_wip":            summarise_ageing_wip(wi, wih, ctx, cfg),
        "stale_work":            summarise_stale_work(wi, ctx),
        "blockers":              summarise_blockers(wi, wih, ctx, ct, cfg),
        "blocker_timeline":      summarise_blocker_timeline(wi, wih, ctx, ct, cfg),
        "current_blocked_items": summarise_blocked_items_detail(wi, wih, ctx, ct, cfg),
        "bugs":                  summarise_bugs(wi, ctx, ct),
        "net_flow":              summarise_net_flow(ct, ctx, wih),
        "arrival_departure":     summarise_ad_ratio(ct, ctx, wih),
        "cfd":                   summarise_cfd(ct, ctx, wih),
        "wip_level_distribution": summarise_wip_level_distribution(wih, ctx, ct),
    }


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "ai_interpret_metrics.prompt.md"

# Legacy in-code constants kept only so call_openai can still reference CHART_PROMPTS keys.
# All prompt text is now in src/prompts/ai_interpret_metrics.prompt.md.
CHART_PROMPTS = {
    "cycle_time": {
        "title": "Cycle Time",
        "instruction": """\
Given the cycle time statistics below, write a 2-3 sentence insight.
Focus on: what the spread between median and P85 reveals about predictability, \
whether the trend is cause for concern or encouragement, and what the \
variation across work item types suggests about how the team processes work.
Do NOT describe the numbers. Interpret what they mean for the team and their stakeholders.
""",
    },
    "lead_time": {
        "title": "Lead Time",
        "instruction": """\
Given the lead time statistics below, write a 2-3 sentence insight.
Focus on: how far ahead stakeholders can reliably plan based on this data, \
whether lead time is driven by wait time or active work time, and what the \
gap between lead time and cycle time implies about how work enters the system.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "throughput": {
        "title": "Throughput",
        "instruction": """\
Given the throughput statistics below, write a 2-3 sentence insight.
Focus on: whether the delivery rate is stable enough for meaningful forecasting, \
what weeks with zero completions suggest about batch delivery vs. steady flow, \
and whether the trend points toward acceleration or deceleration of delivery.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "time_in_columns": {
        "title": "Time in Columns",
        "instruction": """\
Given the time-in-column statistics below, write a 2-3 sentence insight.
Focus on: where work accumulates and why that column is the likely constraint, \
whether the pattern suggests a capacity problem, a handoff delay, or work arriving \
faster than it can be processed, and what addressing this bottleneck could mean \
for overall cycle time.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "flow_efficiency": {
        "title": "Flow Efficiency",
        "instruction": """\
Given the flow efficiency statistics below, write a 2-3 sentence insight.
Flow efficiency = active time / (active + waiting time) across the cycle.
Industry typical range is 15-40%.
Focus on: what this efficiency level implies about how much of cycle time \
is actually productive, what the likely drivers of low efficiency are in this \
type of team, and what a meaningful improvement would require.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "work_start_efficiency": {
        "title": "Work Start Efficiency",
        "instruction": """\
Given the work start efficiency statistics below, write a 2-3 sentence insight.
Work start efficiency = cycle time / lead time — it measures how quickly \
work moves from intake to active development.
Focus on: what a long average wait before development starts implies about \
prioritisation, queue management, or batch-intake practices, and what the team \
could do to start work sooner after committing to it.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "wip": {
        "title": "WIP (Work in Progress)",
        "instruction": """\
Given the current WIP snapshot below, write a 2-3 sentence insight.
Focus on: whether the WIP level is likely to be causing multitasking and \
context-switching overhead, which columns hold the most inventory and what \
that suggests about flow, and what reducing WIP might do to cycle time \
based on Little's Law.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "blockers": {
        "title": "Blocked Items",
        "instruction": """\
Given the blocker statistics below, write a 2-3 sentence insight.
Focus on: the systemic cost of blocking (days lost vs. value delivered), \
whether blockers are concentrated in specific columns (suggesting handoff or \
dependency problems), and what a persistent blocking pattern implies about \
how the team manages dependencies and escalations.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "net_flow": {
        "title": "Net Flow",
        "instruction": """\
Given the net flow statistics below, write a 2-3 sentence insight.
Net flow = items finished minus items started each week. \
Positive = more finishing than starting (backlog shrinking). \
Negative = more starting than finishing (backlog growing).
Focus on: whether the current pattern is sustainable, what it implies \
about team capacity vs. demand, and what the trend suggests about future \
delivery risk.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
    "arrival_departure": {
        "title": "Arrival / Departure Ratio by Column",
        "instruction": """\
Given the arrival/departure ratio data below, write a 2-3 sentence insight.
A ratio > 1 means work arrives into a column faster than it leaves (accumulating). \
A ratio < 1 means it drains faster (clearing). Ratio = 1 is balanced.
Focus on: which columns are the system's current pressure points, \
what that pattern suggests about where to focus improvement effort, \
and whether the accumulation is likely temporary or structural.
Do NOT describe the numbers. Interpret what they mean.
""",
    },
}

OVERVIEW_PROMPT = """\
## Task: Holistic flow analysis

You are analysing flow metrics for a software delivery team. \
All statistics are anonymised — no item titles or individual names are included.

Using ALL the metric summaries below, produce TWO sections:

### Section 1 — What is happening (leadership narrative)
Write 3-5 sentences of plain English narrative suitable for a non-technical \
leadership audience. No jargon. No metric names. Describe the situation as a story: \
what is the team's current state of flow, what patterns stand out, \
and what is the most likely underlying cause.

### Section 2 — Suggested actions
Provide a numbered list of 5-8 specific actions. Each action should be one sentence. \
Mix two types:
- Generic flow improvement practices that apply to this pattern
- Specific actions grounded in this team's data (e.g. "investigate why X column \
  has a 2x higher wait time than the others")

Do NOT repeat the metrics back. Focus entirely on what to do and why.
"""


def build_prompt_text(summary):
    template = PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace("{{SUMMARY_JSON}}", json.dumps(summary, indent=2))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def write_prompt(summary):
    import subprocess
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    header = """\
---
agent: agent
description: "Flow metrics — generate chart insights and leadership overview"
---

When you have produced the JSON object, save it to `output/data/insights.json`
using the create_file or replace_string_in_file tool. Do not display the raw JSON
in chat — just confirm the file has been written and summarise the key findings
in 3-5 bullet points.

"""
    content = header + build_prompt_text(summary)
    PROMPT_MD_PATH.write_text(content, encoding="utf-8")
    print(f"Written: {PROMPT_MD_PATH}")
    try:
        subprocess.Popen(["code", str(PROMPT_MD_PATH)])
        print(f"Opened: {PROMPT_MD_PATH}")
    except FileNotFoundError:
        pass
    print("Open the prompt file in any AI assistant and run it.")
    print("The agent will save output/data/insights.json when done.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_insights(content: str) -> dict:
    """Parse JSON from an AI response, stripping accidental markdown fences."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = "\n".join(stripped.splitlines()[1:])
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")]
    return json.loads(stripped.strip())


def _write_insights(content: str):
    insights = _parse_insights(content)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INSIGHTS_PATH.write_text(json.dumps(insights, indent=2), encoding="utf-8")
    print(f"Written: {INSIGHTS_PATH}")


async def _copilot_call(prompt_text: str, github_token: str) -> str:
    import asyncio
    from copilot import CopilotClient
    from copilot.session import PermissionHandler
    from copilot.session_events import AssistantMessageData, AssistantMessageDeltaData, SessionIdleData

    done = asyncio.Event()
    deltas: list[str] = []
    final_content: list[str] = []

    # session_idle_timeout_seconds=0 disables the server idle timeout so long
    # inference runs don't get cut off before the response arrives.
    async with CopilotClient(github_token=github_token, session_idle_timeout_seconds=0) as client:
        async with await client.create_session(
            on_permission_request=PermissionHandler.approve_all,
            model="auto",
            streaming=True,
        ) as session:
            def on_event(event):
                match event.data:
                    case AssistantMessageDeltaData() as data:
                        delta = getattr(data, 'delta_content', '') or ''
                        if isinstance(delta, str):
                            deltas.append(delta)
                    case AssistantMessageData() as data:
                        val = getattr(data, 'content', '')
                        if isinstance(val, str) and val:
                            final_content.append(val)
                    case SessionIdleData():
                        done.set()

            session.on(on_event)
            await session.send(prompt_text)
            await done.wait()

    assembled = ''.join(deltas)
    return assembled if assembled else (final_content[-1] if final_content else '')


def auto_interpret(summary):
    """Write insights.json directly — CI/automation mode.

    Tries GitHub Copilot SDK first (GITHUB_TOKEN + copilot-requests:write).
    Falls back to an OpenAI-compatible HTTP API if AI_API_KEY is set.
    """
    import os
    import urllib.request
    import urllib.error

    prompt_text = build_prompt_text(summary)

    github_token = os.environ.get('GITHUB_TOKEN')
    if github_token:
        try:
            import asyncio
            print("Calling GitHub Copilot SDK…")
            content = asyncio.run(_copilot_call(prompt_text, github_token))
            if content:
                _write_insights(content)
                return
            print("Copilot SDK returned empty response, falling back to AI_API_KEY.", file=sys.stderr)
        except ImportError:
            print("github-copilot-sdk not installed, falling back to AI_API_KEY.")
        except Exception as exc:
            print(f"Copilot SDK error ({exc}), falling back to AI_API_KEY.", file=sys.stderr)

    # Fallback: any OpenAI-compatible API
    token = os.environ.get('AI_API_KEY')
    endpoint = os.environ.get('AI_API_ENDPOINT') or 'https://api.openai.com/v1'
    model = os.environ.get('AI_MODEL') or 'gpt-4o-mini'

    if not token:
        print("Skipping AI interpretation: set AI_API_KEY as a repository secret to enable automated insights.")
        return

    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.2,
    }).encode()

    print(f"Calling AI API: {endpoint}  model={model}")
    req = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"AI API error {exc.code}: {body}", file=sys.stderr)
        sys.exit(1)

    _write_insights(data["choices"][0]["message"]["content"])


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate an AI prompt to produce chart insights."
    )
    parser.add_argument(
        "--dump-summary", action="store_true",
        help="Write the computed summary JSON to stdout and exit (for inspection).",
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Call the AI API directly and write insights.json (CI mode). "
             "Uses GITHUB_TOKEN (GitHub Models) or AI_API_KEY + AI_API_ENDPOINT.",
    )
    args = parser.parse_args()

    summary = build_summary()

    if args.dump_summary:
        print(json.dumps(summary, indent=2))
        return

    if args.auto:
        auto_interpret(summary)
        return

    write_prompt(summary)


if __name__ == "__main__":
    main()
