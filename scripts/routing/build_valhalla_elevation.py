#!/usr/bin/env python3
"""Build Valhalla's elevation cells from Mapterhorn terrain tiles.

Replaces the AWS terrain-tiles download (void-filled SRTM) as the source
of the 1-arcsec SRTM-format `.hgt` cells Valhalla's skadi reads. The AWS
data is not merely noisy in steep terrain — in the Gondo gorge it is off
by +200 to +450 m (it reports the gorge rim, not the floor), which
poisoned elevation profiles, ascent totals, displayed durations and
costing decisions. Mapterhorn is built from national lidar *terrain*
models across the whole routing bbox (CH 2 m, IT 10 m / Piemonte 5 m,
FR 5 m, DE/AT 1 m; 30 m Copernicus fallback), verified to match
swisstopo within metres exactly where the AWS data is wrong, and — being
DTMs — free of the bridges/buildings the SRTM-era surface data bakes in.
See mapterhorn-elevation-source.md.

Flow (one-off; re-run only on --force or when this script changes):
  1. `pmtiles extract` the routing bbox (whole covered degree cells plus
     margin) from Mapterhorn's z0-12 planet archive into a local,
     date-pinned PMTiles file — the published area-extract mechanism,
     mirror-backed and resumable.
  2. Serve that archive locally (`pmtiles serve`) and fetch the z12
     Terrarium WebP tiles covering the cells.
  3. Bilinear-resample onto the 3601x3601 1-arcsec grid of each 1°x1°
     cell and write `N{lat}/N{lat}E{lon}.hgt` (big-endian int16, the
     exact layout skadi expects) into --out.
  4. Stamp the set (`.mapterhorn_stamp`: date, source, zoom) — the
     bring-up script compares this stamp to decide syncing and tile
     wipes.

z12 at 512 px is ~19 m/px at 46° N — finer than the 1-arcsec (~30 m)
output grid, so resampling only ever averages down.

Dependencies: PyYAML, numpy, Pillow (pip --user), and the go-pmtiles
CLI — either a `pmtiles` binary on PATH (override with PMTILES_BIN) or
docker (falls back to the protomaps/go-pmtiles image).
"""

from __future__ import annotations

import argparse
import http.client
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np
import yaml
from PIL import Image
import io

ROOT = Path(__file__).resolve().parent.parent.parent
CFG_PATH = ROOT / "scripts" / "transit" / "config.yaml"

SOURCE_URL = "https://download.mapterhorn.com/planet.pmtiles"
ZOOM = 12
TILE_PX = 512
# Margin (degrees) past the whole-degree cell edges, so bilinear taps at
# the very edge of a cell never fall outside the extract.
EXTRACT_PAD_DEG = 0.05
STAMP_NAME = ".mapterhorn_stamp"

DEFAULT_OUT = ROOT / "data" / "elevation" / "mapterhorn_hgt"
DEFAULT_ARCHIVE = ROOT / "data" / "elevation" / "mapterhorn_z12.pmtiles"


def read_bbox() -> tuple[float, float, float, float]:
    cfg = yaml.safe_load(CFG_PATH.read_text())
    b = cfg["osm_bbox"]
    return b["min_lon"], b["min_lat"], b["max_lon"], b["max_lat"]


def cells_for(bbox) -> list[tuple[int, int]]:
    """SW corners (lat, lon) of every 1°x1° cell the bbox touches."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return [(la, lo)
            for la in range(math.floor(min_lat), math.ceil(max_lat))
            for lo in range(math.floor(min_lon), math.ceil(max_lon))]


# ── pmtiles CLI (binary or docker fallback) ──────────────────────────────

def pmtiles_runner() -> tuple[list[str], bool]:
    """Returns (command prefix, is_docker)."""
    binary = os.environ.get("PMTILES_BIN") or shutil.which("pmtiles")
    if binary:
        return [binary], False
    if shutil.which("docker"):
        return ["docker"], True
    raise SystemExit(
        "need the go-pmtiles CLI: `brew install pmtiles` (or grab a "
        "release from github.com/protomaps/go-pmtiles, or install "
        "docker for the protomaps/go-pmtiles fallback)")


def pmtiles_extract(archive: Path, bbox_str: str) -> None:
    prefix, is_docker = pmtiles_runner()
    archive.parent.mkdir(parents=True, exist_ok=True)
    if is_docker:
        cmd = prefix + ["run", "--rm", "-v", f"{archive.parent}:/data",
                        "protomaps/go-pmtiles", "extract", SOURCE_URL,
                        f"/data/{archive.name}", f"--bbox={bbox_str}"]
    else:
        cmd = prefix + ["extract", SOURCE_URL, str(archive),
                        f"--bbox={bbox_str}"]
    print("  $", " ".join(cmd))
    subprocess.run(cmd, check=True)


class TileServer:
    """`pmtiles serve` on the archive's directory, on a local port."""

    PORT = 8123

    def __init__(self, archive: Path):
        self.archive = archive
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        prefix, is_docker = pmtiles_runner()
        if is_docker:
            cmd = prefix + ["run", "--rm", "-p",
                            f"127.0.0.1:{self.PORT}:8080",
                            "-v", f"{self.archive.parent}:/data",
                            "protomaps/go-pmtiles", "serve", "/data",
                            "--port", "8080"]
        else:
            cmd = prefix + ["serve", str(self.archive.parent),
                            "--port", str(self.PORT)]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        name = self.archive.stem
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if self.fetch(name, 0, 0, 0) is not None:
                    return self
            except OSError:
                pass
            if self.proc.poll() is not None:
                break
            time.sleep(0.5)
        self.__exit__(None, None, None)
        raise SystemExit("pmtiles serve did not come up")

    def __exit__(self, *_):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def fetch(self, name: str, z: int, x: int, y: int) -> bytes | None:
        conn = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=30)
        try:
            conn.request("GET", f"/{name}/{z}/{x}/{y}.webp")
            res = conn.getresponse()
            body = res.read()
            if res.status == 200:
                return body
            if res.status == 404:
                return None
            raise SystemExit(f"tile {z}/{x}/{y}: HTTP {res.status}")
        finally:
            conn.close()


# ── Mercator / Terrarium ─────────────────────────────────────────────────

SCALE = TILE_PX * (1 << ZOOM)  # global pixels at ZOOM


def lon_to_px(lon):
    return (lon + 180.0) / 360.0 * SCALE


def lat_to_px(lat):
    rad = np.radians(lat)
    return (1.0 - np.log(np.tan(rad) + 1.0 / np.cos(rad)) / math.pi) / 2.0 * SCALE


def decode_terrarium(webp: bytes) -> np.ndarray:
    rgb = np.asarray(Image.open(io.BytesIO(webp)).convert("RGB"), dtype=np.float32)
    return rgb[:, :, 0] * 256.0 + rgb[:, :, 1] + rgb[:, :, 2] / 256.0 - 32768.0


def cell_tile_range(cell: tuple[int, int]) -> tuple[range, range]:
    """z12 tile x/y ranges needed for a cell, with a 2 px sampling margin."""
    lat, lon = cell
    x0 = int((lon_to_px(lon) - 2) // TILE_PX)
    x1 = int((lon_to_px(lon + 1) + 2) // TILE_PX)
    y0 = int((lat_to_px(lat + 1) - 2) // TILE_PX)  # north edge = smaller y
    y1 = int((lat_to_px(lat) + 2) // TILE_PX)
    return range(x0, x1 + 1), range(y0, y1 + 1)


def build_cell(cell: tuple[int, int], tiles: dict) -> np.ndarray:
    """Resample the mosaic onto the cell's 3601x3601 1-arcsec grid."""
    lat, lon = cell
    xr, yr = cell_tile_range(cell)
    mosaic = np.empty((len(yr) * TILE_PX, len(xr) * TILE_PX), dtype=np.float32)
    for j, ty in enumerate(yr):
        for i, tx in enumerate(xr):
            mosaic[j * TILE_PX:(j + 1) * TILE_PX,
                   i * TILE_PX:(i + 1) * TILE_PX] = tiles[(tx, ty)]
    off_x = xr.start * TILE_PX
    off_y = yr.start * TILE_PX

    n = 3601
    lons = lon + np.arange(n) / 3600.0
    # Sample positions in mosaic pixel-center coordinates.
    sx = lon_to_px(lons) - 0.5 - off_x
    ix = np.clip(np.floor(sx).astype(np.int64), 0, mosaic.shape[1] - 2)
    fx = sx - ix

    out = np.empty((n, n), dtype=np.float32)
    for row in range(n):
        cell_lat = lat + 1 - row / 3600.0  # row 0 = north edge
        sy = float(lat_to_px(cell_lat)) - 0.5 - off_y
        iy = min(max(int(math.floor(sy)), 0), mosaic.shape[0] - 2)
        fy = sy - iy
        top = mosaic[iy, ix] * (1 - fx) + mosaic[iy, ix + 1] * fx
        bot = mosaic[iy + 1, ix] * (1 - fx) + mosaic[iy + 1, ix + 1] * fx
        out[row] = top * (1 - fy) + bot * fy
    return out


def write_hgt(out_dir: Path, cell: tuple[int, int], grid: np.ndarray) -> Path:
    lat, lon = cell
    band = out_dir / f"N{lat:02d}"
    band.mkdir(parents=True, exist_ok=True)
    path = band / f"N{lat:02d}E{lon:03d}.hgt"
    data = np.clip(np.rint(grid), -32768, 32767).astype(">i2").tobytes()
    tmp = path.with_suffix(".hgt.tmp")
    tmp.write_bytes(data)
    tmp.rename(path)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help=f"output cell tree (default {DEFAULT_OUT})")
    ap.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE,
                    help="local PMTiles extract (kept as the pinned source)")
    ap.add_argument("--force", action="store_true",
                    help="regenerate cells even when the stamp exists")
    ap.add_argument("--force-download", action="store_true",
                    help="re-extract the PMTiles archive from upstream too")
    args = ap.parse_args()

    stamp = args.out / STAMP_NAME
    if stamp.exists() and not args.force:
        print(f"{stamp} exists — nothing to do (--force to regenerate)")
        return

    bbox = read_bbox()
    cells = cells_for(bbox)
    print(f"bbox {bbox} → {len(cells)} cells: "
          + ", ".join(f"N{la:02d}E{lo:03d}" for la, lo in cells))

    if args.archive.exists() and not args.force_download:
        print(f"reusing extract {args.archive}")
    else:
        pad = EXTRACT_PAD_DEG
        ex = (f"{math.floor(bbox[0]) - pad},{math.floor(bbox[1]) - pad},"
              f"{math.ceil(bbox[2]) + pad},{math.ceil(bbox[3]) + pad}")
        print(f"extracting Mapterhorn z0-{ZOOM} for bbox {ex} …")
        pmtiles_extract(args.archive, ex)

    needed: set[tuple[int, int]] = set()
    for cell in cells:
        xr, yr = cell_tile_range(cell)
        needed.update((x, y) for x in xr for y in yr)
    print(f"fetching {len(needed)} z{ZOOM} tiles from the extract …")

    tiles: dict[tuple[int, int], np.ndarray] = {}
    with TileServer(args.archive) as srv:
        name = args.archive.stem

        def grab(xy):
            x, y = xy
            body = srv.fetch(name, ZOOM, x, y)
            if body is None:
                raise SystemExit(
                    f"tile {ZOOM}/{x}/{y} missing from the extract — "
                    "re-run with --force-download")
            return xy, decode_terrarium(body)

        with ThreadPoolExecutor(max_workers=16) as pool:
            for xy, arr in pool.map(grab, sorted(needed)):
                tiles[xy] = arr

    for cell in cells:
        path = write_hgt(args.out, cell, build_cell(cell, tiles))
        print(f"  → {path.relative_to(ROOT)}")

    stamp.write_text(
        f"generated={date.today().isoformat()}\n"
        f"source={SOURCE_URL}\nzoom={ZOOM}\n")
    print(f"stamped {stamp.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
