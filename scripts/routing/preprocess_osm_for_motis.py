#!/usr/bin/env python3
"""Rewrite an OSM PBF so MOTIS / Valhalla accept walkable Alp/forest roads.

MOTIS's OSR foot profile (and Valhalla's pedestrian costing) treat
`access=agricultural` and `access=forestry` as pedestrian-blacklisted —
a defensible global default that is wrong for CH, where those tags only
forbid unauthorised motor vehicles (walking is always legal on
Alpstrassen and Forststrassen).

This step adds `foot=yes` to any way tagged `access=agricultural` or
`access=forestry` that doesn't already carry a `foot=*` override, so the
per-profile access_override picks it up as a foot-whitelist.

For the Valhalla output it additionally subtracts bus/PSV lanes from
the `lanes` tags (`lanes`, `lanes:forward`, `lanes:backward`) — Valhalla
bakes only the plain lane count into its tiles, the bicycle costing
scales its crossing penalty per lane of the road being crossed, and a
bus lane does not make a crossing harder (bicycle-costing-fork.md
§ crossing penalty) — and tags under-passing highways
(`layer<0` or `cutting=*`, no tunnel) as `tunnel=yes`. Valhalla samples elevation from
a ~30 m DEM, which sees the structure a road passes UNDER — underpasses
come out as fake 10-15 % climbs (canonical case: Schwarzenburgstrasse
under the rail line at Weissenstein, a level ride that priced like a
mountain). Tunnel and bridge edges are exempt from DEM sampling —
Valhalla interpolates their elevation endpoint to endpoint — so marking
under-passing ways as tunnels buys exactly that treatment. Cuttings
count as under-passing: the canonical Weissenstein case carries only
`cutting=yes` — the rail bridge above holds the layer tag, the road
below has none. Genuine hillside cuttings on mountain roads lose
nothing: their endpoints stay real DEM heights, only the interior is
linearized per way. The tag is a
routing-graph fiction that never reaches the map or MOTIS; its only
other Valhalla effect is the (unused) exclude_tunnels request option.
See bicycle-costing-fork.md § grade cap for the query-time fallback that
guards the same artifact until tiles are rebuilt.

The Valhalla output also DROPS level-2/4 administrative boundary
relations: the bbox cut has clipped them into unclosed rings that
valhalla_build_admins cannot polygonize (which left the admin DB
covering only CH+FL, and everything abroad defaulted to drive-on-left —
flipping the bicycle costing's with-traffic turn exemption). The
complete relations reach the container via the `admin_bounds.osm.pbf`
sidecar built by setup_routing.sh step 3; dropping the clipped copies
here keeps each admin polygon from being built twice.

With `--overlay` it also merges the synthetic station walk network
(`build_station_walk_network.py`) into the output: platform walk lines,
their level-checked welds into the real pedestrian graph, and lift hubs.
Valhalla cannot route on areas, so without this a platform has no
routable geometry and walks end on whatever edge is nearest in plan view
— at a stacked station regularly a deck two levels up. See
`.claude/concepts/station-walk-network.md`.

Uses pyosmium's `FileProcessor` iterator so nodes, ways, and relations
all round-trip correctly (the older `SimpleHandler` + `SimpleWriter`
combo silently drops ways past ~37M nodes on this dataset).

Idempotent: re-running the script produces the same output.

Two default invocations:
  * no args → CH-only PBF for MOTIS's own OSR (as before):
      switzerland-latest → switzerland-motis
  * `--valhalla` → wide-bbox PBF for the Valhalla pedestrian router:
      ch_pfaedle → ch_pfaedle_walkable
    (`ch_pfaedle.osm.pbf` is produced by the pipeline step 03; it
    already covers CH + DE + FR + IT + AT + LI within our bbox, so
    Valhalla can snap cross-border GTFS stops that a CH-only extract
    misses — Basel Bad, Konstanz, Domodossola, Weil am Rhein, etc.)

Manual paths can be passed explicitly with --input / --output.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import osmium

ROOT = Path(__file__).resolve().parent.parent.parent
OSM_DIR = ROOT / "data" / "osm"

# `access=` values that block foot in the OSR / Valhalla pedestrian
# defaults but are walkable in CH by legal convention. `no`,
# `emergency`, `delivery`, `private` are intentionally NOT overridden —
# those genuinely mean no.
OVERRIDE_ACCESS = {"agricultural", "forestry"}

OVERLAY_PBF = OSM_DIR / "station_walk_network.osm.pbf"


def _read_overlay(path: Path):
    """Load the synthetic overlay into memory (it is a few MB at most)."""
    nodes, ways = [], []
    for obj in osmium.FileProcessor(str(path)):
        if obj.is_node():
            nodes.append((obj.id, obj.location.lon, obj.location.lat,
                          dict(obj.tags)))
        elif obj.is_way():
            ways.append((obj.id, [n.ref for n in obj.nodes], dict(obj.tags)))
    return nodes, ways


def _underpass_fix(tags: dict) -> bool:
    """True when the way needs tunnel=yes for sane elevation: a highway
    below ground level (layer<0 or a cutting) that is neither tunnel nor
    bridge. An explicit tunnel=no still qualifies — the tag speaks to
    semantics, but the DEM is poisoned either way. cutting=no does not
    qualify (explicitly not below ground)."""
    if "highway" not in tags:
        return False
    if tags.get("tunnel", "no") != "no" or "bridge" in tags:
        return False
    if tags.get("cutting", "no") != "no":
        return True
    try:
        return int(tags.get("layer", "0")) < 0
    except ValueError:
        return False


def _is_admin_boundary(tags) -> bool:
    """Level-2/4 administrative boundary relation — the kind Valhalla's
    admin DB is built from. The bbox cut clips these into unclosed rings
    (members outside the bbox are gone), so the Valhalla output drops
    them entirely: the complete relations arrive via the
    `admin_bounds.osm.pbf` sidecar that setup_routing.sh step 3 extracts
    from the full country PBFs, and keeping the clipped copies here
    would make valhalla_build_admins build every admin polygon twice
    (its relation list is a plain vector, no dedup)."""
    return (tags.get("boundary") == "administrative"
            and tags.get("admin_level") in ("2", "4"))


def _int_tag(tags: dict, key: str) -> int:
    try:
        return int(tags.get(key, "0"))
    except ValueError:
        return 0


def _bus_lane_fix(tags: dict) -> list[tuple[str, str]] | None:
    """Rewritten lane tags with bus/PSV lanes subtracted, or None when
    nothing changes. Each directional count is guarded to stay ≥ 1 (a
    way whose only lane is the bus lane still has one physical lane to
    cross); the total is recomputed from the parts when both directions
    are tagged, else reduced directly."""
    if "highway" not in tags or "lanes" not in tags:
        return None
    bus_total = sum(_int_tag(tags, k) for k in ("lanes:bus", "lanes:psv"))
    bus_fwd = sum(_int_tag(tags, k) for k in ("lanes:bus:forward", "lanes:psv:forward"))
    bus_bwd = sum(_int_tag(tags, k) for k in ("lanes:bus:backward", "lanes:psv:backward"))
    if bus_total + bus_fwd + bus_bwd == 0:
        return None
    lanes = _int_tag(tags, "lanes")
    if lanes <= 0:
        return None
    out: list[tuple[str, str]] = []
    fwd = _int_tag(tags, "lanes:forward")
    bwd = _int_tag(tags, "lanes:backward")
    if fwd > 0:
        fwd = max(1, fwd - bus_fwd - bus_total)
        out.append(("lanes:forward", str(fwd)))
    if bwd > 0:
        bwd = max(1, bwd - bus_bwd - bus_total)
        out.append(("lanes:backward", str(bwd)))
    if fwd > 0 and bwd > 0:
        new_lanes = fwd + bwd
    else:
        new_lanes = max(1, lanes - bus_total - bus_fwd - bus_bwd)
    out.append(("lanes", str(new_lanes)))
    return out


def patch(in_path: Path, out_path: Path, overlay: Path | None = None,
          underpass_elevation_fix: bool = False,
          drop_admin_relations: bool = False) -> None:
    if not in_path.exists():
        raise SystemExit(f"input PBF not found: {in_path}")

    ov_nodes, ov_ways = ([], [])
    if overlay is not None:
        if not overlay.exists():
            raise SystemExit(
                f"overlay not found: {overlay} — run "
                "scripts/routing/build_station_walk_network.py first")
        ov_nodes, ov_ways = _read_overlay(overlay)

    # Synthetic ids sit far above every live OSM id, so appending each
    # synthetic block after the corresponding real block keeps the output
    # sorted by (type, id) the way every osmium consumer expects.
    n_ways = n_patched = n_underpass = n_buslanes = n_admin_dropped = 0
    wrote_nodes = wrote_ways = False
    with osmium.SimpleWriter(str(out_path), overwrite=True) as writer:
        def flush_nodes():
            nonlocal wrote_nodes
            if wrote_nodes:
                return
            wrote_nodes = True
            for nid, lon, lat, tags in ov_nodes:
                writer.add_node(osmium.osm.mutable.Node(
                    id=nid, location=(lon, lat), tags=tags))

        def flush_ways():
            nonlocal wrote_ways
            if wrote_ways:
                return
            wrote_ways = True
            for wid, refs, tags in ov_ways:
                writer.add_way(osmium.osm.mutable.Way(
                    id=wid, nodes=refs, tags=tags))

        for obj in osmium.FileProcessor(str(in_path)):
            if obj.is_node():
                writer.add_node(obj)
            elif obj.is_way():
                flush_nodes()
                n_ways += 1
                tags = dict(obj.tags)
                extra = []
                if (tags.get("access") in OVERRIDE_ACCESS
                        and "foot" not in tags):
                    extra.append(("foot", "yes"))
                    n_patched += 1
                if underpass_elevation_fix and _underpass_fix(tags):
                    extra.append(("tunnel", "yes"))
                    n_underpass += 1
                if underpass_elevation_fix:
                    lane_fix = _bus_lane_fix(tags)
                    if lane_fix:
                        extra.extend(lane_fix)
                        n_buslanes += 1
                if extra:
                    replaced = {k for k, _ in extra}
                    new_tags = [(k, v) for k, v in obj.tags if k not in replaced]
                    writer.add_way(obj.replace(tags=new_tags + extra))
                else:
                    writer.add_way(obj)
            elif obj.is_relation():
                flush_nodes()
                flush_ways()
                if drop_admin_relations and _is_admin_boundary(obj.tags):
                    n_admin_dropped += 1
                    continue
                writer.add_relation(obj)
        flush_nodes()
        flush_ways()

    print(f"ways: {n_ways:,}  patched (added foot=yes): {n_patched:,}")
    if underpass_elevation_fix:
        print(f"underpasses marked tunnel=yes for elevation: {n_underpass:,}")
        print(f"ways with bus/PSV lanes subtracted: {n_buslanes:,}")
    if drop_admin_relations:
        print(f"clipped admin boundary relations dropped: {n_admin_dropped:,}")
    if overlay is not None:
        print(f"overlay merged: {len(ov_nodes):,} nodes, {len(ov_ways):,} ways")
    print(f"→ {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--valhalla", action="store_true",
                    help="Preset for Valhalla: patch data/osm/ch_pfaedle.osm.pbf "
                         "→ data/osm/ch_pfaedle_walkable.osm.pbf.")
    ap.add_argument("--input", type=Path, default=None,
                    help="Explicit input PBF (overrides the preset).")
    ap.add_argument("--output", type=Path, default=None,
                    help="Explicit output PBF (overrides the preset).")
    ap.add_argument("--overlay", type=Path, nargs="?", default=None,
                    const=OVERLAY_PBF,
                    help="Merge the station walk network overlay "
                         f"(default {OVERLAY_PBF.name}). Implied by --valhalla.")
    ap.add_argument("--no-overlay", action="store_true",
                    help="Skip the overlay even for --valhalla.")
    args = ap.parse_args()

    overlay = args.overlay
    if args.valhalla and overlay is None:
        overlay = OVERLAY_PBF
    if args.no_overlay:
        overlay = None

    if args.input and args.output:
        patch(args.input, args.output, overlay,
              underpass_elevation_fix=args.valhalla,
              drop_admin_relations=args.valhalla)
        return

    if args.valhalla:
        patch(OSM_DIR / "ch_pfaedle.osm.pbf",
              OSM_DIR / "ch_pfaedle_walkable.osm.pbf", overlay,
              underpass_elevation_fix=True,
              drop_admin_relations=True)
    else:
        # MOTIS's own OSR graph is not a walking authority (Valhalla is),
        # so the overlay is deliberately not merged here.
        patch(OSM_DIR / "switzerland-latest.osm.pbf",
              OSM_DIR / "switzerland-motis.osm.pbf")


if __name__ == "__main__":
    main()
