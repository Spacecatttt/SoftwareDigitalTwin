"""Queries to the twin graph in Eclipse Ditto (HTTP API + RQL search).

  python ditto_query.py state kpi.dt:printer_01.hotend                      # current state of a twin
  python ditto_query.py state kpi.dt:printer_01 --fields features/status    # ... selected fields only
  python ditto_query.py search 'gt(features/heater/properties/temperature,212)' --fields features/heater/properties/temperature
  python ditto_query.py count 'eq(attributes/kind,"Hotend")'
  python ditto_query.py parts kpi.dt:printer_01                             # direct parts (inverse isPartOf)
  python ditto_query.py relations kpi.dt:printer_01.psu                     # outgoing + incoming edges
  python ditto_query.py tree                                                # traversal of the graph from the farm
  python ditto_query.py tree --mermaid                                      # ... as a Mermaid diagram
  python ditto_query.py impact kpi.dt:printer_01.psu                        # what fails if the twin fails
  python ditto_query.py td kpi.dt:printer_01.hotend                         # WoT Thing Description
  python ditto_query.py history kpi.dt:printer_02.hotend --ago 5m           # state 5 minutes ago (Ditto history)
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

from twin.config import DITTO_URL
from twin.ditto_client import DittoClient, DittoError, rql_str
from twin.ditto_graph import DEPENDENCIES, FARM_ID, RELATIONS

# the live value shown next to a node in the tree, by kind
HEADLINE = {
    "PrintFarm": ["features/summary/properties/printersOnline", "features/summary/properties/printersInAlarm"],
    "Printer": ["features/status/properties/operatingMode", "features/status/properties/healthIndex",
                "features/alarms/properties/level"],
    "Controller": ["features/connectivity/properties/status"],
    "Hotend": ["features/heater/properties/temperature", "features/heater/properties/thermalState"],
    "HeatedBed": ["features/temperature/properties/temperature"],
    "Enclosure": ["features/temperature/properties/temperature"],
    "Extruder": ["features/drive/properties/feedRate"],
    "StepperAxis": ["features/motor/properties/current", "features/motor/properties/loadState"],
    "Fan": ["features/fan/properties/speedPercent"],
    "PowerSupply": ["attributes/ratedPowerW"],
}
NODE_FIELDS = "thingId,attributes/kind,attributes/name,attributes/relations," + ",".join(
    sorted({f for fs in HEADLINE.values() for f in fs}))


def pointer(obj: dict, path: str):
    for part in path.split("/"):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def flatten(obj, prefix=""):
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}/{k}" if prefix else k)
    else:
        yield prefix, obj


def headline(thing: dict) -> str:
    vals = []
    for path in HEADLINE.get(pointer(thing, "attributes/kind"), []):
        v = pointer(thing, path)
        if v is not None:
            vals.append(f"{path.rsplit('/', 1)[-1]}={v}")
    return ", ".join(vals)


def table(rows: list[dict], fields: list[str]):
    cols = ["thingId"] + fields
    data = [[str(r.get("thingId"))] + [_cell(pointer(r, f)) for f in fields] for r in rows]
    widths = [max(len(c), *(len(d[i]) for d in data)) if data else len(c) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for d in data:
        print("  ".join(c.ljust(w) for c, w in zip(d, widths)))
    print(f"({len(rows)} twins)")


def _cell(v) -> str:
    return "-" if v is None else json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def incoming_rql(thing_id: str, relations=RELATIONS) -> str:
    return "or(" + ",".join(f"eq(attributes/relations/{r},{rql_str(thing_id)})" for r in relations) + ")"


# --- commands ------------------------------------------------------------------------------------------
def cmd_state(d: DittoClient, a):
    t = d.get_thing(a.thing, fields=a.fields)
    if t is None:
        sys.exit(f"{a.thing}: not found")
    print(json.dumps(t, ensure_ascii=False, indent=2))


def cmd_search(d: DittoClient, a):
    fields = [f for f in (a.fields or "").split(",") if f]
    rows = d.search(a.rql, fields=",".join(["thingId"] + fields), limit=a.limit)
    table(rows, fields)


def cmd_count(d: DittoClient, a):
    print(d.count(a.rql))


def cmd_parts(d: DittoClient, a):
    rows = d.search(f"eq(attributes/relations/isPartOf,{rql_str(a.thing)})", fields=NODE_FIELDS)
    for r in rows:
        print(f"{r['thingId']:<34} {pointer(r, 'attributes/kind'):<12} {headline(r)}")
    print(f"({len(rows)} parts of {a.thing})")


def cmd_relations(d: DittoClient, a):
    t = d.get_thing(a.thing, fields="thingId,attributes/kind,attributes/relations")
    if t is None:
        sys.exit(f"{a.thing}: not found")
    print(f"{a.thing} ({pointer(t, 'attributes/kind')})")
    print("  outgoing (attributes/relations of the twin):")
    for rel, target in (pointer(t, "attributes/relations") or {}).items():
        print(f"    --{rel}--> {target}")
    print(f"  incoming (search {incoming_rql(a.thing)[:60]}...):")
    for r in d.search(incoming_rql(a.thing), fields="thingId,attributes/kind,attributes/relations"):
        for rel, target in (pointer(r, "attributes/relations") or {}).items():
            if target == a.thing:
                print(f"    {r['thingId']} ({pointer(r, 'attributes/kind')}) --{rel}-->")


def load_graph(d: DittoClient, root: str) -> tuple[dict, dict]:
    """Breadth-first traversal over isPartOf: one search per level of the composition tree."""
    nodes, children = {}, {}
    root_thing = d.get_thing(root, fields=NODE_FIELDS)
    if root_thing is None:
        sys.exit(f"{root}: not found")
    nodes[root] = root_thing
    level = [root]
    while level:
        rql = "in(attributes/relations/isPartOf," + ",".join(rql_str(t) for t in level) + ")"
        found = d.search(rql, fields=NODE_FIELDS)
        level = []
        for t in found:
            nodes[t["thingId"]] = t
            children.setdefault(pointer(t, "attributes/relations/isPartOf"), []).append(t["thingId"])
            level.append(t["thingId"])
    return nodes, children


def cmd_tree(d: DittoClient, a):
    nodes, children = load_graph(d, a.root)
    if a.mermaid:
        print("graph TD")
        for tid, t in nodes.items():
            label = f"{tid.split(':', 1)[1]}<br/>{pointer(t, 'attributes/kind')}"
            print(f'  {_mid(tid)}["{label}"]')
        for tid, t in nodes.items():
            for rel, target in (pointer(t, "attributes/relations") or {}).items():
                if target in nodes:
                    arrow = "-->" if rel == "isPartOf" else "-.->"
                    print(f"  {_mid(tid)} {arrow}|{rel}| {_mid(target)}")
        return

    def show(tid, prefix="", last=True, root=True):
        t = nodes[tid]
        branch = "" if root else ("└── " if last else "├── ")
        edges = [f"{rel}->{target.split(':', 1)[1]}" for rel, target in (pointer(t, "attributes/relations") or {}).items()
                 if rel != "isPartOf"]
        print(f"{prefix}{branch}{tid}  [{pointer(t, 'attributes/kind')}]  {headline(t)}"
              + (f"   ({', '.join(edges)})" if edges else ""))
        kids = sorted(children.get(tid, []))
        for i, k in enumerate(kids):
            show(k, prefix + ("" if root else ("    " if last else "│   ")), i == len(kids) - 1, False)

    show(a.root)
    edges = sum(len(pointer(t, "attributes/relations") or {}) for t in nodes.values())
    print(f"({len(nodes)} twins, {edges} relations)")


def _mid(thing_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", thing_id.split(":", 1)[1])


def cmd_impact(d: DittoClient, a):
    """Twins affected by a failure of the given twin: everything that depends on it (controlledBy, poweredBy,
    cooledBy, fedBy - transitively) and the wholes it is a part of (isPartOf upwards)."""
    start = d.get_thing(a.thing, fields="thingId,attributes/kind,attributes/relations")
    if start is None:
        sys.exit(f"{a.thing}: not found")
    affected = {a.thing: (None, None)}
    queue = [a.thing]
    while queue:
        cur = queue.pop(0)
        for r in d.search(incoming_rql(cur, DEPENDENCIES), fields="thingId,attributes/relations"):
            rel = next(k for k, v in pointer(r, "attributes/relations").items() if v == cur and k in DEPENDENCIES)
            if r["thingId"] not in affected:
                affected[r["thingId"]] = (rel, cur)
                queue.append(r["thingId"])
        parent = pointer(d.get_thing(cur, fields="attributes/relations") or {}, "attributes/relations/isPartOf")
        if parent and parent not in affected:
            affected[parent] = ("hasPart", cur)
            queue.append(parent)
    print(f"failure of {a.thing} affects {len(affected) - 1} twins:")
    for tid, (rel, via) in list(affected.items())[1:]:
        reason = f"{tid} --{rel}--> {via}" if rel != "hasPart" else f"{via} is a part of {tid}"
        print(f"  {tid:<34} because {reason}")


def cmd_td(d: DittoClient, a):
    td = d.thing_description(a.thing, a.feature)
    if a.json:
        print(json.dumps(td, ensure_ascii=False, indent=2))
        return
    print(f"{td.get('title')}  ({td.get('id')})\n  {td.get('description', '')}")
    print(f"  base: {td.get('base')}")
    for kind in ("properties", "actions", "events"):
        for name, aff in (td.get(kind) or {}).items():
            rw = "" if kind != "properties" else (" read-only" if aff.get("readOnly") else " read/write")
            unit = f" [{aff['unit']}]" if aff.get("unit") else ""
            hrefs = sorted({f.get("href", "") for f in aff.get("forms", [])})
            print(f"  {kind.rstrip('s').replace('propertie', 'property'):<8} {name:<20} {aff.get('type', 'object'):<8}{unit}{rw}  {hrefs[0] if hrefs else ''}")
    for link in td.get("links", []):
        print(f"  link     {link.get('rel'):<20} {link.get('href')}")


def cmd_history(d: DittoClient, a):
    if a.revision is not None:
        headers = {"at-historical-revision": str(a.revision)}
    else:
        at = a.at or (datetime.now(timezone.utc) - parse_ago(a.ago)).isoformat(timespec="seconds").replace("+00:00", "Z")
        headers = {"at-historical-timestamp": at}
    try:
        r = d.request("GET", f"/things/{a.thing}", params={"fields": a.fields} if a.fields else None, headers=headers)
    except DittoError as e:
        sys.exit(str(e))
    print(f"{a.thing} at {headers}:")
    hist = r.headers.get("historical-headers")
    if hist:
        print(f"  changed by: {hist}")
    for path, v in flatten(r.json()):
        print(f"  {path:<55} {v}")


def parse_ago(s: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([smhd])", s or "")
    if not m:
        raise SystemExit("--ago expects e.g. 30s, 5m, 1h")
    return timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}[m[2]]: int(m[1])})


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ditto-url", default=DITTO_URL)
    p.add_argument("--user", default="ditto")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state"); s.add_argument("thing"); s.add_argument("--fields")
    s = sub.add_parser("search"); s.add_argument("rql"); s.add_argument("--fields"); s.add_argument("--limit", type=int, default=200)
    s = sub.add_parser("count"); s.add_argument("rql", nargs="?")
    s = sub.add_parser("parts"); s.add_argument("thing")
    s = sub.add_parser("relations"); s.add_argument("thing")
    s = sub.add_parser("tree"); s.add_argument("root", nargs="?", default=FARM_ID); s.add_argument("--mermaid", action="store_true")
    s = sub.add_parser("impact"); s.add_argument("thing")
    s = sub.add_parser("td"); s.add_argument("thing"); s.add_argument("--feature"); s.add_argument("--json", action="store_true")
    s = sub.add_parser("history"); s.add_argument("thing"); s.add_argument("--fields")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--revision", type=int); g.add_argument("--at", help="ISO 8601, UTC"); g.add_argument("--ago", help="e.g. 5m")
    a = p.parse_args()
    d = DittoClient(a.user, url=a.ditto_url)
    globals()[f"cmd_{a.cmd}"](d, a)


if __name__ == "__main__":
    main()
