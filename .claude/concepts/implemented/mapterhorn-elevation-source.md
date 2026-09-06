# Mapterhorn as Valhalla's elevation source

## Problem

Valhalla's routing tiles are built against the elevation set its container
downloads itself: AWS terrain tiles, which are void-filled SRTM. In steep
narrow valleys that data is not noisy but *wrong* — measured +200 to
+450 m against swisstopo along the Gondo gorge, where the fill reports
the gorge rim instead of the floor. The consequences ride on every
query: elevation profiles show mountains that do not exist, ascent /
descent totals are fantasy (1 642 m "up" on a 5 km descent), displayed
durations inherit the fake grades (47 min for a 5–7 min ride), and the
costing buys detours around phantom climbs. Being a surface-era product,
it also sees structures a road passes under, which is what forced the
underpass `tunnel=yes` marking and the through-road grade cap.

Mapterhorn — the terrain source the map itself already uses for
hillshade and contours — is built from national lidar terrain models
across the whole routing bbox (CH 2 m, Italy 10 m with 5 m in Piemonte,
France 5 m, Austria and Germany 1 m; 30 m Copernicus fallback) and was
verified to match swisstopo within metres exactly where the current data
is hundreds of metres off. Terrain models also strip bridges, buildings
and vegetation, so the structure-above artifact class largely disappears
at the source.

## Requirements

- **New artifact — `valhalla/data/elevation_data/` filled from
  Mapterhorn.** The same 1-arcsec SRTM-format `.hgt` cell files skadi
  reads today, covering every 1°×1° cell the routing bbox touches,
  generated from Mapterhorn data instead of downloaded from AWS. Same
  grid, same format, same size — nothing downstream changes shape.
- **New generator — `scripts/routing/build_valhalla_elevation.py`.**
  Produces those cells from a Mapterhorn PMTiles area extract of the
  routing bbox (the bbox already defined for the transit pipeline).
  Source zoom must be at least 12 (finer than 1-arcsec at this
  latitude, so the resampling only ever averages down). Downloads are
  the published area-extract mechanism, so the fetch is a one-off of a
  few hundred MB, resumable and mirror-backed.
- **One-off, not a pipeline stage.** The generator runs when its output
  is missing, when explicitly forced, or when the generator itself
  changed — never as part of a routine rebuild. `elevation_data/`
  already survives tile wipes; that property is load-bearing and stays.
- **Bring-up integration.** The routing bring-up (setup step 3 family)
  gains the same freshness treatment the admin-bounds sidecar got: a
  regenerated elevation set wipes the Valhalla tiles so the next step
  rebuilds them against it. The container must find `elevation_data/`
  non-empty and therefore skip its own AWS download (existing scripted
  behaviour — it downloads only into an empty directory).
- **Rebuild pairing.** A new elevation set means a full Valhalla tile
  rebuild and, per the existing doctrine, a footpath-matrix rebuild in
  the same cycle, so tiles and matrix keep describing the same surface.
- **Attribution.** The app already credits Mapterhorn for the map's
  terrain; confirm the credit covers its use in routing elevation too,
  and extend it if not.
- **Acceptance.** The Gondo benchmark segment
  (Zwischbergen → Paglino, ~5 km): profile descends monotonically from
  ~1 130 m to ~800 m, ascent total under ~200 m, duration in the
  10–15 min band instead of 47. The Weissenstein-class underpass
  artifact no longer appears in lidar-covered areas.

## Constraints

- **No runtime cost.** Elevation is consumed only at tile build, at a
  fixed sampling interval independent of source resolution; graph tiles
  must not grow and query performance must not change.
- **Existing guards stay.** The through-road grade cap and the underpass
  `tunnel=yes` marking remain as fallbacks — they still protect
  Copernicus-fallback areas and unmapped structures. Removing them is a
  separate decision after the new data has proven itself.
- **Vertical datum differences are ignored** — sub-metre at this use.
- The PMTiles extraction tool becomes a dependency of the build
  machines (Mac and Kranich), not of the server or the container.
- Mapterhorn's tiles update over time; the generated cells are pinned
  by their generation date and are not expected to track upstream. A
  refresh is an explicit, forced regeneration.
