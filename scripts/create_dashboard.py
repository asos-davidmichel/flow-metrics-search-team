"""
Dashboard generator.

Reads:  output/metrics/time_in_columns.json
        output/metrics/cycle_time.json        (optional — skipped if not present)
        output/metrics/lead_time.json         (optional — skipped if not present)
        src/templates/dashboard.html
Writes: output/dashboard.html

Usage:
  python src/create_dashboard.py
"""

import json
import sys
from pathlib import Path

TIC_PATH      = Path("output/metrics/time_in_columns.json")
CT_PATH       = Path("output/metrics/cycle_time.json")
LT_PATH       = Path("output/metrics/lead_time.json")
CTX_PATH      = Path("output/data/context.json")
CFG_PATH      = Path("output/data/config.json")
WI_PATH       = Path("output/data/work_items.json")
INSIGHTS_PATH = Path("output/data/insights.json")
DQ_PATH       = Path("output/data/data_quality_report.json")
EXCL_PATH     = Path("output/data/excluded_items.json")
TEMPLATE_PATH = Path(__file__).parent / "templates" / "dashboard.html"
OUTPUT_PATH   = Path("output/dashboard.html")


def main():
    force = "--force" in sys.argv
    if OUTPUT_PATH.exists() and not force:
        print(f"Skipping: {OUTPUT_PATH} already exists. Delete it or use --force to regenerate.")
        sys.exit(0)

    for path in (TIC_PATH, TEMPLATE_PATH):
        if not path.exists():
            print(f"Error: {path} not found.", file=sys.stderr)
            sys.exit(1)

    dashboard = {
        "time_in_columns": json.loads(TIC_PATH.read_text(encoding="utf-8")),
        "cycle_time": json.loads(CT_PATH.read_text(encoding="utf-8")) if CT_PATH.exists() else None,
        "lead_time":  json.loads(LT_PATH.read_text(encoding="utf-8")) if LT_PATH.exists() else None,
        "context":    json.loads(CTX_PATH.read_text(encoding="utf-8")) if CTX_PATH.exists() else None,
        "config":     json.loads(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.exists() else None,
        "work_items": json.loads(WI_PATH.read_text(encoding="utf-8")) if WI_PATH.exists() else None,
        "insights":   json.loads(INSIGHTS_PATH.read_text(encoding="utf-8")) if INSIGHTS_PATH.exists() else None,
        "data_quality_report": json.loads(DQ_PATH.read_text(encoding="utf-8")) if DQ_PATH.exists() else None,
        "excluded_items": json.loads(EXCL_PATH.read_text(encoding="utf-8")) if EXCL_PATH.exists() else None,
        "work_item_history": [
            {"id": h["id"], "column_history": h["column_history"], "tag_history": h.get("tag_history", [])}
            for h in json.loads(Path("output/data/work_item_history.json").read_text(encoding="utf-8"))
        ] if Path("output/data/work_item_history.json").exists() else None,
    }

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    html = template.replace("/*DASHBOARD_DATA_PLACEHOLDER*/", json.dumps(dashboard))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Written: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
