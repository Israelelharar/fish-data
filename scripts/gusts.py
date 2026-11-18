"""
Builds gusts.json: hourly max wind gust (km/h) for every beach, 7 days.

  PRIMARY  DWD ICON-EU (6.5 km, runs every 3 h, 120 h ahead), parameter VMAX_10M.
           https://opendata.dwd.de  - licence CC BY 4.0, credit "Deutscher Wetterdienst".
  BACKUP + EXTENSION  ECMWF IFS open data (0.25 deg, 144 h+), parameter 10fg.
           https://data.ecmwf.int (mirrors: AWS, Azure, Google) - CC BY 4.0, credit "ECMWF".

ICON-EU fills every hour it covers; ECMWF fills hours ICON lacks (after 120 h, or everything if
DWD is down). If one source fails the other still publishes. Exit code 1 only if both fail.

Run:  pip install eccodes ecmwf-opendata
      python scripts/gusts.py site/gusts.json [previous gusts.json URL]
"""
import bz2
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import eccodes

ROOT = Path(__file__).resolve().parent.parent
HOURS = 168
ICON_BASE = "https://opendata.dwd.de/weather/nwp/icon-eu/grib/{run:02d}/vmax_10m/"
ICON_FILE = "icon-eu_europe_regular-lat-lon_single-level_{stamp}_{step:03d}_VMAX_10M.grib2.bz2"
ICON_STEPS = list(range(1, 79)) + list(range(81, 121, 3))
UA = {"User-Agent": "fish-data/1.0 (+https://github.com/Israelelharar/fish-data)"}


def log(*a):
    print(*a, flush=True)


def get(url: str, tries: int = 4, timeout: int = 60) -> bytes:
    """GET with retries and exponential backoff (1s, 3s, 9s)."""
    for n in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if n == tries - 1:
                raise
            log(f"retry {n + 1} {url[-60:]}: {e}")
            time.sleep(3 ** n)
    raise RuntimeError("unreachable")


def exists(url: str) -> bool:
    try:
        req = urllib.request.Request(url, headers=UA, method="HEAD")
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def points():
    return json.loads((ROOT / "data/wind-points.json").read_text(encoding="utf8"))


_index_cache: dict[tuple, list[int]] = {}


def grid_index(gid, pts) -> list[int]:
    """Flat index of the nearest grid cell for every point, for any regular lat/lon GRIB grid.
    Computed once per grid geometry (much faster than eccodes' find_nearest per value)."""
    k = {n: eccodes.codes_get(gid, n) for n in (
        "Ni", "Nj", "latitudeOfFirstGridPointInDegrees", "longitudeOfFirstGridPointInDegrees",
        "iDirectionIncrementInDegrees", "jDirectionIncrementInDegrees", "iScansNegatively", "jScansPositively")}
    key = tuple(k.values())
    if key not in _index_cache:
        if eccodes.codes_get(gid, "gridType") != "regular_ll":
            raise RuntimeError("unexpected grid type")
        ni, nj = k["Ni"], k["Nj"]
        lat0, lon0 = k["latitudeOfFirstGridPointInDegrees"], k["longitudeOfFirstGridPointInDegrees"]
        di, dj = k["iDirectionIncrementInDegrees"], k["jDirectionIncrementInDegrees"]
        jsign = 1 if k["jScansPositively"] else -1
        isign = -1 if k["iScansNegatively"] else 1
        idx = []
        for p in pts:
            j = round((p["lat"] - lat0) * jsign / dj)
            i = round((((p["lon"] - lon0) * isign) % 360) / di)
            if not (0 <= j < nj and 0 <= i < ni):
                raise RuntimeError(f"{p['id']} outside the grid")
            idx.append(j * ni + i)
        _index_cache[key] = idx
    return _index_cache[key]


def sample(grib: bytes, pts) -> tuple[datetime, list[float | None]]:
    """Value at the nearest grid point for every beach + the validity time of the message."""
    gid = eccodes.codes_new_from_message(grib)
    try:
        d, t = eccodes.codes_get(gid, "validityDate"), eccodes.codes_get(gid, "validityTime")
        valid = datetime.strptime(f"{d}{t:04d}", "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        values = eccodes.codes_get_values(gid)
        missing = eccodes.codes_get(gid, "missingValue")
        vals = []
        for n in grid_index(gid, pts):
            v = float(values[n])
            vals.append(None if v == missing or v != v or v > 1e6 else round(v * 3.6, 1))  # m/s -> km/h
        return valid, vals
    finally:
        eccodes.codes_release(gid)


def icon(pts):
    """Newest complete ICON-EU run (complete = its last step is published)."""
    now = datetime.now(timezone.utc)
    for back in range(0, 16, 3):
        base = (now - timedelta(hours=back)).replace(minute=0, second=0, microsecond=0)
        base -= timedelta(hours=base.hour % 3)
        stamp = base.strftime("%Y%m%d%H")
        folder = ICON_BASE.format(run=base.hour)
        if not exists(folder + ICON_FILE.format(stamp=stamp, step=ICON_STEPS[-1])):
            continue
        log(f"ICON-EU run {stamp}")

        def one(step):
            raw = bz2.decompress(get(folder + ICON_FILE.format(stamp=stamp, step=step)))
            return sample(raw, pts)

        with ThreadPoolExecutor(8) as ex:
            rows = list(ex.map(one, ICON_STEPS))
        return stamp, rows
    raise RuntimeError("no complete ICON-EU run in the last 15 hours")


def ecmwf(pts):
    """ECMWF open data, trying the main portal and then its cloud mirrors."""
    from ecmwf.opendata import Client

    steps = list(range(3, 91, 3)) + list(range(96, 175, 6))  # 10fg: 3-hourly to 90 h, then 6-hourly
    last = None
    for source in ("ecmwf", "aws", "azure", "google"):
        try:
            target = Path(tempfile.mkdtemp()) / "ecmwf-10fg.grib2"
            res = Client(source=source).retrieve(type="fc", param="10fg", step=steps, target=str(target))
            data = target.read_bytes()
            rows = []
            # Split the multi-message file into single GRIB messages ("GRIB" ... "7777").
            pos = 0
            while (start := data.find(b"GRIB", pos)) != -1:
                length = int.from_bytes(data[start + 8:start + 16], "big")  # GRIB2 total length
                rows.append(sample(data[start:start + length], pts))
                pos = start + length
            shutil.rmtree(target.parent, ignore_errors=True)
            log(f"ECMWF run {res.datetime} via {source}: {len(rows)} steps")
            return res.datetime.strftime("%Y%m%d%H"), rows
        except Exception as e:  # noqa: BLE001
            last = e
            log(f"ECMWF via {source} failed: {e}")
    raise RuntimeError(f"ECMWF failed on every mirror: {last}")


def main(out_path: str):
    pts = points()
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)
    end = start + timedelta(hours=HOURS)
    series: dict[datetime, list] = {}
    used = {}

    try:
        run, rows = icon(pts)
        used["icon"] = run
        for valid, vals in rows:
            if start <= valid <= end:
                series[valid] = vals
    except Exception as e:  # noqa: BLE001
        log(f"ICON-EU failed: {e}")

    try:
        run, rows = ecmwf(pts)
        icon_end = max(series) if series else start
        added = 0
        for valid, vals in rows:
            if valid > icon_end and valid <= end:
                series[valid] = vals
                added += 1
        if added:
            used["ecmwf"] = run
        log(f"ECMWF added {added} steps")
    except Exception as e:  # noqa: BLE001
        log(f"ECMWF failed: {e}")

    if not series:
        sys.exit("both gust sources failed")
    times = sorted(series)
    out = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "unit": "km/h",
        "sources": used,
        "credit": " · ".join(c for k, c in (("icon", "Deutscher Wetterdienst (ICON-EU)"), ("ecmwf", "ECMWF")) if k in used),
        "t": [t.strftime("%Y-%m-%dT%H:%MZ") for t in times],
        "points": {p["id"]: [series[t][i] for t in times] for i, p in enumerate(pts)},
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(out, separators=(",", ":")), encoding="utf8")
    log(f"wrote {out_path}: {len(pts)} points, {len(times)} steps, sources {used}")


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "site/gusts.json")
    except BaseException as e:  # record why, for status.json / the alert issue
        Path("tmp").mkdir(exist_ok=True)
        Path("tmp/gusts.err").write_text(f"{type(e).__name__}: {e}", encoding="utf8")
        raise
