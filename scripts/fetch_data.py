import json
import os
import re
import sys
from pathlib import Path

from util_ado import (
    ADOError,
    discover_board,
    fetch_parent_items,
    fetch_work_item_history,
    fetch_work_item_ids,
    fetch_work_item_type_styles,
    fetch_work_items,
    get_board_columns,
    get_board_rows,
    get_card_rule_settings,
    get_boards,
    get_team_area_paths,
    make_auth_header,
    parse_board_url,
)


def _path_exists(p: Path) -> bool:
    # Python 3.12+ raises PermissionError from exists(); treat as not-found
    try:
        return p.exists()
    except OSError:
        return False


def _humanise_filter(filter_str):
    m = re.match(r"\[System\.Tags\] contains '(.+)'", filter_str, re.IGNORECASE)
    if m:
        return f"Tag: {m.group(1)}"
    m = re.match(r"\[(.+?)\] ([><=!]+) '(.+)'", filter_str)
    if m:
        field = m.group(1).split(".")[-1]  # strip namespace
        return f"{field} {m.group(2)} {m.group(3)}"
    return filter_str


def get_pat():
    pat = os.environ.get("ADO_PAT")
    if not pat:
        print("Error: ADO_PAT environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return pat


def prompt_board_selection(candidates, context_label=""):
    """Ask the user to pick a board from a numbered list. Returns the chosen board dict."""
    if context_label:
        print(context_label)
    for i, board in enumerate(candidates, start=1):
        print(f"  {i}. {board['name']}")

    raw = input("\nChoose a board number: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(candidates)):
        print(
            f"Error: '{raw}' is not a valid choice. Enter a number between 1 and {len(candidates)}.",
            file=sys.stderr,
        )
        sys.exit(1)
    return candidates[int(raw) - 1]


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch ADO board data")
    parser.add_argument("url", help="ADO board URL")
    parser.add_argument("--context-only", action="store_true",
                        help="Only fetch board context (context.json)")
    args = parser.parse_args()
    url = args.url

    if args.context_only:
        if _path_exists(Path("output/data/context.json")):
            print("Skipping: context.json already exists. Delete it to re-fetch.")
            sys.exit(0)
    else:
        _data_outputs = [
            Path("output/data/context.json"),
            Path("output/data/work_items.json"),
            Path("output/data/work_item_history.json"),
        ]
        if all(_path_exists(p) for p in _data_outputs):
            print("Skipping: output data already exists. Delete output/data to re-fetch.")
            sys.exit(0)
    pat = get_pat()

    try:
        org, project, team, board_hint = parse_board_url(url)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Organisation : {org}")
    print(f"Project      : {project}")
    print(f"Team         : {team}")
    print()

    headers = make_auth_header(pat)

    try:
        boards = get_boards(org, project, team, headers)
        discovery = discover_board(boards, board_hint)
    except ADOError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if discovery["status"] == "no_boards_found":
        print("No boards found for this team.")
        sys.exit(0)

    if discovery["status"] == "matched":
        selected = discovery["matched_board"]
    else:
        candidates = discovery["candidates"]
        hint_context = f"No exact match for '{board_hint}'. " if board_hint else ""
        label = f"{hint_context}Found {len(candidates)} board(s):"
        selected = prompt_board_selection(candidates, label)

    print(f"Board  : {selected['name']}")
    print()

    try:
        columns = get_board_columns(selected["url"], headers)
        rows = get_board_rows(selected["url"], headers)
        card_rules = get_card_rule_settings(selected["url"], headers)
    except ADOError as e:
        print(f"Error fetching board details: {e}", file=sys.stderr)
        sys.exit(1)

    if not columns:
        print("No columns found for this board.")
        sys.exit(0)

    # Derive board-scoped work item types and state mappings from column data
    work_item_types = sorted({wit for c in columns for wit in c.get("stateMappings", {})})
    state_mappings = {
        wit: {c["name"]: c["stateMappings"][wit] for c in columns if wit in c.get("stateMappings", {})}
        for wit in work_item_types
    }

    print(f"Columns ({len(columns)}):\n")
    for col in columns:
        wip = col.get("itemLimit") or 0
        wip_label = f"WIP {wip}" if wip else "no WIP"
        print(f"  [{col.get('columnType', '?'):10}]  {col['name']:25}  {wip_label}")

    print(f"\nSwimlanes ({len(rows)}):\n")
    for row in rows:
        color = row.get("color") or "no colour"
        print(f"  {row['name']:35}  {color}")

    fill_rules = card_rules.get("fill", [])
    swimlane_rules = card_rules.get("swimlaneRule", [])
    print(f"\nCard colour rules ({len(fill_rules)}):\n")
    for rule in fill_rules:
        color = rule.get("settings", {}).get("background-color", "?")
        label = _humanise_filter(rule["filter"])
        print(f"  {rule['name']:20}  {label:35}  {color}")
    print(f"\nSwimlane rules ({len(swimlane_rules)}):\n")
    for rule in swimlane_rules:
        label = _humanise_filter(rule["filter"])
        print(f"  {rule['name']:35}  {label}")

    print(f"\nWork item types on this board ({len(work_item_types)}):\n")
    for wit in work_item_types:
        print(f"  - {wit}")

    print("\nFetching team area paths...")
    area_paths = get_team_area_paths(org, project, team, headers)
    for ap in area_paths:
        op = "UNDER" if ap.get("includeChildren", True) else "="
        print(f"  {op} {ap['value']}")

    print("\nFetching work items...")
    try:
        ids = fetch_work_item_ids(org, project, team, work_item_types, headers, closed_within_days=365, area_paths=area_paths)
        print(f"  {len(ids)} items found")
        work_items = fetch_work_items(org, project, ids, headers)
        print(f"  {len(work_items)} items fetched")
    except ADOError as e:
        print(f"Error fetching work items: {e}", file=sys.stderr)
        sys.exit(1)

    # Normalise null swimlane → the board's default swimlane name.
    # ADO returns System.BoardLane = null for items in the first/default swimlane
    # regardless of what that swimlane is called.
    _default_sl = next(
        (r["name"] for r in rows if r.get("id") == "00000000-0000-0000-0000-000000000000"),
        rows[0]["name"] if rows else None,
    )
    if _default_sl:
        for item in work_items:
            if item.get("swimlane") is None:
                item["swimlane"] = _default_sl

    # Enrich work items with parent title/type (features, epics, etc.)
    board_item_ids = {item["id"] for item in work_items}
    parent_ids = {
        item["parent_id"]
        for item in work_items
        if item.get("parent_id") and item["parent_id"] not in board_item_ids
    }
    parent_info = {}
    if parent_ids:
        print(f"\nFetching {len(parent_ids)} parent items (features/epics)...")
        try:
            parent_info = fetch_parent_items(org, project, parent_ids, headers)
            print(f"  {len(parent_info)} parent items resolved")
        except ADOError as e:
            print(f"  Warning: could not fetch parent items: {e}", file=sys.stderr)
    for item in work_items:
        pid = item.get("parent_id")
        info = parent_info.get(pid, {}) if pid else {}
        item["parent_title"] = info.get("title")
        item["parent_type"] = info.get("type")

    print("\nFetching work item type styles...")
    work_item_type_styles = fetch_work_item_type_styles(org, project, work_item_types, headers)
    if work_item_type_styles:
        for wit, style in work_item_type_styles.items():
            print(f"  {wit}: color={style['color']}")
    else:
        print("  (none fetched — will use fallback colours in metrics.py)")

    output = {
        "org": org,
        "project": project,
        "team": team,
        "board_url": url,
        "board": {
            "id": selected["id"],
            "name": selected["name"],
            "url": selected["url"],
        },
        "columns": [
            entry
            for c in columns
            for entry in (
                [
                    {
                        "id": c.get("id"),
                        "name": f"{c['name']} (Doing)",
                        "column_type": c.get("columnType"),
                        "wip_limit": c.get("itemLimit") or 0,
                        "is_split": True,
                    },
                    {
                        "id": c.get("id"),
                        "name": f"{c['name']} (Done)",
                        "column_type": c.get("columnType"),
                        "wip_limit": 0,
                        "is_split": True,
                    },
                ]
                if c.get("isSplit", False)
                else [
                    {
                        "id": c.get("id"),
                        "name": c["name"],
                        "column_type": c.get("columnType"),
                        "wip_limit": c.get("itemLimit") or 0,
                        "is_split": False,
                    }
                ]
            )
        ],
        "work_item_types": work_item_types,
        "work_item_type_styles": work_item_type_styles,
        "state_mappings": state_mappings,
        "swimlanes": [{"id": r["id"], "name": r["name"], "color": r.get("color")} for r in rows],
        "default_swimlane": next(
            (r["name"] for r in rows if r.get("id") == "00000000-0000-0000-0000-000000000000"),
            rows[0]["name"] if rows else None,
        ),
        "area_paths": area_paths,
        "card_rules": {
            "fill": [
                {
                    "name": rule["name"],
                    "filter": rule["filter"],
                    "background_color": rule.get("settings", {}).get("background-color"),
                }
                for rule in card_rules.get("fill", [])
            ],
            "swimlane_rules": [
                {
                    "name": rule["name"],
                    "filter": rule["filter"],
                }
                for rule in card_rules.get("swimlaneRule", [])
            ],
        },
    }

    output_path = Path("output/data/context.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\nContext saved to: {output_path}")

    if args.context_only:
        return

    items_path = Path("output/data/work_items.json")
    items_path.write_text(json.dumps(work_items, indent=2))
    print(f"Work items saved to: {items_path}")

    print(f"\nFetching history for {len(ids)} items (one request per item)...")
    # Build the expanded column name list used for board_entry_date detection and
    # regression tracking. Split columns appear as "Col (Doing)" then "Col (Done)".
    split_columns = {c["name"] for c in columns if c.get("isSplit", False)}
    board_column_names = []
    for c in columns:
        if c["name"] in split_columns:
            board_column_names.append(f"{c['name']} (Doing)")
            board_column_names.append(f"{c['name']} (Done)")
        else:
            board_column_names.append(c["name"])
    history = []
    for i, item_id in enumerate(ids, start=1):
        try:
            record = fetch_work_item_history(
                org, project, item_id, board_column_names, headers,
                split_columns=split_columns,
            )
            history.append(record)
        except ADOError as e:
            print(f"  Warning: could not fetch history for {item_id}: {e}", file=sys.stderr)
        if i % 25 == 0 or i == len(ids):
            print(f"  {i}/{len(ids)} done")

    history_path = Path("output/data/work_item_history.json")
    history_path.write_text(json.dumps(history, indent=2))
    print(f"History saved to: {history_path}")


if __name__ == "__main__":
    main()

