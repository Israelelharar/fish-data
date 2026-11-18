"""
Builds marine.json (waves, swell, sea temperature + its 24h/72h/7d change, bottom temperature, tide, currents, water stir; 7 days, hourly) for every sea beach
from Copernicus Marine forecasts. Free for commercial use with the credit
"E.U. Copernicus Marine Service Information".

Needs a free Copernicus Marine account:
  env COPERNICUSMARINE_SERVICE_USERNAME / COPERNICUSMARINE_SERVICE_PASSWORD
Run:  pip install copernicusmarine==2.* xarray numpy
      python scripts/copernicus_marine.py out/marine.json

⚠️ Dataset IDs below follow the Copernicus catalogue naming; verify them in the catalogue
   on first run (the script prints a clear error if one is wrong).
"""
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import copernicusmarine as cm
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DAYS = 7

WAVE_VARS = ["VHM0", "VMDR", "VTM10", "VHM0_SW1", "VTM01_SW1", "VMDR_SW1"]
# Each kind lists candidate dataset IDs, newest naming first. Copernicus renames datasets
# between catalogue releases (e.g. "phy-tem" -> "phy-temp" in 2026-11), so we try each in turn.
# IDs verified in the STAC catalogue on 2026-11-18. Surface-only (2D) datasets: no depth needed.
# "waves" and "sst" are essential; "tide" and "cur" are extras (a failure there only drops them).
TIDE = (["cmems_mod_glo_phy_anfc_merged-sl_PT1H-i"], ["ocean_tide"],
        "GLOBAL_ANALYSISFORECAST_PHY_001_024", r"cmems_mod_glo_phy_anfc_merged-sl_PT1H-i")
DATASETS = {
    # Mediterranean (4.2 km, hourly). Does NOT cover the Red Sea.
    "med": {
        "waves": (["cmems_mod_med_wav_anfc_4.2km_PT1H-i"], WAVE_VARS,
                  "MEDSEA_ANALYSISFORECAST_WAV_006_017", r"cmems_mod_med_wav_anfc_[\d.]+km_PT1H-i"),
        "sst": (["cmems_mod_med_phy-temp_anfc_4.2km-2D_PT1H-m", "cmems_mod_med_phy-tem_anfc_4.2km-2D_PT1H-m"], ["thetao"],
                "MEDSEA_ANALYSISFORECAST_PHY_006_013", r"cmems_mod_med_phy-tem\w*_anfc_[\d.]+km-2D_PT1H-m"),
        "tide": TIDE,
        "cur": (["cmems_mod_med_phy-cur_anfc_4.2km-2D_PT1H-m"], ["uo", "vo"],
                "MEDSEA_ANALYSISFORECAST_PHY_006_013", r"cmems_mod_med_phy-cur_anfc_[\d.]+km-2D_PT1H-m"),

        # Bottom potential temperature. Extra: failure only drops it.
        # (only the 3D hourly dataset carries bottomT; we request just that 2D variable). Verified in STAC 2026-11-18.
        "bott": (["cmems_mod_med_phy-temp_anfc_4.2km-3D_PT1H-m", "cmems_mod_med_phy-tem_anfc_4.2km-3D_PT1H-m"], ["bottomT"],
                 "MEDSEA_ANALYSISFORECAST_PHY_006_013", r"cmems_mod_med_phy-tem\w*_anfc_[\d.]+km-3D_PT1H-m"),
    },
    # Global (1/12 deg) for Eilat / Gulf of Aqaba.
    "eilat": {
        "waves": (["cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"], WAVE_VARS,
                  "GLOBAL_ANALYSISFORECAST_WAV_001_027", r"cmems_mod_glo_wav_anfc_[\d.]+deg_PT3H-i"),
        "sst": (["cmems_mod_glo_phy_anfc_0.083deg_PT1H-m"], ["thetao"],
                "GLOBAL_ANALYSISFORECAST_PHY_001_024", r"cmems_mod_glo_phy\w*_anfc_[\d.]+deg_PT1H-m"),
        "tide": TIDE,
        "cur": (["cmems_mod_glo_phy_anfc_merged-uv_PT1H-i"], ["utotal", "vtotal"],
                "GLOBAL_ANALYSISFORECAST_PHY_001_024", r"cmems_mod_glo_phy_anfc_merged-uv_PT1H-i"),

        # Global bottom temperature is daily (interpolated to hours). Extra.
        # Global bottom temperature is "tob" in the daily dataset. Verified in STAC 2026-11-18.
        "bott": (["cmems_mod_glo_phy_anfc_0.083deg_P1D-m"], ["tob"],
                 "GLOBAL_ANALYSISFORECAST_PHY_001_024", r"cmems_mod_glo_phy\w*_anfc_[\d.]+deg_P1D-m"),
    },
}
OUT_KEYS = {"VHM0": "wave", "VMDR": "waveDir", "VTM10": "wavePer", "VHM0_SW1": "swell", "VTM01_SW1": "swellPer",
            "VMDR_SW1": "swellDir", "thetao": "sst", "ocean_tide": "tide", "bottomT": "bott", "tob": "bott"}
DIRECTIONS = {"waveDir", "swellDir"}
# Waves are read from 48 h back: "stir" = the biggest wave of the last 36 h tells the app whether
# the water is still murky after a blow (water clarity).
HISTORY_H = 48
STIR_H = 36
# Sea temperature is read from 7 days back, for the change over 24 h / 72 h / 7 days ("sstD24" ...):
# a sharp drop or rise matters to fish more than the absolute value alone.
SST_HISTORY_H = 168
SST_LAGS = {"sstD24": 24, "sstD72": 72, "sstD168": 168}


def lagged_change(src_h, values, dst_h, lag_h):
    """value(t) - value(t - lag) on the output hours. None where either side is outside the data."""
    now = interp(src_h, values, dst_h)
    before = interp(src_h, values, dst_h - lag_h)
    return [None if a is None or b is None else clean(a - b) for a, b in zip(now, before)]


def beaches():
    """Sea points for every beach, exported from the app (data/points.json)."""
    return json.loads((ROOT / "data/points.json").read_text(encoding="utf8"))


def clean(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 2)


def nearest_sea(ds, var, lat, lon, max_deg=0.32):
    first = ds[var].isel(time=0).values
    if first.ndim > 2:
        first = first.reshape(first.shape[-2:])
    valid = ~np.isnan(first)
    if not valid.any():
        return None
    la = ds["latitude"].values[:, None]
    lo = ds["longitude"].values[None, :]
    d2 = (la - lat) ** 2 + ((lo - lon) * math.cos(math.radians(lat))) ** 2
    d2 = np.where(valid, d2, np.inf)
    i, j = np.unravel_index(np.argmin(d2), d2.shape)
    if not np.isfinite(d2[i, j]) or d2[i, j] > max_deg ** 2:
        return None
    return ds.isel(latitude=int(i), longitude=int(j))


STAC = "https://stac.marine.copernicus.eu/metadata/{product}/product.stac.json"


def discover(product, pattern):
    """If Copernicus renamed a dataset again, find its new ID in the public STAC catalogue."""
    try:
        with urllib.request.urlopen(STAC.format(product=product), timeout=30) as r:
            links = json.load(r).get("links", [])
    except Exception as e:  # noqa: BLE001
        print(f"catalogue lookup failed for {product}: {e}")
        return []
    found = {}
    for link in links:
        m = re.match(r"(.+)_(\d{6})/dataset\.stac\.json$", link.get("href", ""))
        if m and re.fullmatch(pattern, m.group(1)):
            found[m.group(1)] = max(found.get(m.group(1), ""), m.group(2))
    ids = sorted(found, key=lambda k: found[k], reverse=True)
    print(f"catalogue {product}: {ids}")
    return ids


def is_auth_error(e):
    return "password" in type(e).__name__.lower() or "credential" in type(e).__name__.lower()


def open_first(candidates, variables, box, product, pattern):
    """Open the first dataset ID that works: known IDs first, then whatever the catalogue lists now.
    Each ID gets 2 tries (transient outages). Wrong credentials stop everything at once."""
    tried = []
    for dataset_id in list(candidates) + ["*"]:
        ids = discover(product, pattern) if dataset_id == "*" else [dataset_id]
        for did in ids:
            if did in tried:
                continue
            tried.append(did)
            for attempt in range(2):
                try:
                    ds = cm.open_dataset(dataset_id=did, variables=variables, **box)
                    print(f"ok: {did}")
                    return ds
                except Exception as e:  # noqa: BLE001 - catalogue renames, outages
                    if is_auth_error(e):
                        raise
                    print(f"skip {did} (try {attempt + 1}): {type(e).__name__}: {str(e)[:200]}")
                    if "NotFound" in type(e).__name__:
                        break
                    time.sleep(20)
    return None


def to_hours(times):
    return (times.astype("datetime64[s]").astype(np.int64) / 3600.0)


def interp(src_h, values, dst_h, direction=False):
    """Values on the source times -> hourly output times. Never extrapolates (outside -> None)."""
    v = np.asarray(values, dtype=float)
    ok = ~np.isnan(v)
    if ok.sum() < 2:
        return [None] * len(dst_h)
    x, v = src_h[ok], v[ok]
    inside = (dst_h >= x[0]) & (dst_h <= x[-1])
    if direction:  # 350 -> 10 must not swing through 180
        r = np.radians(v)
        out = (np.degrees(np.arctan2(np.interp(dst_h, x, np.sin(r)), np.interp(dst_h, x, np.cos(r)))) + 360) % 360
        return [int(round(o)) if i else None for o, i in zip(out, inside)]
    out = np.interp(dst_h, x, v)
    return [clean(o) if i else None for o, i in zip(out, inside)]


def main(out_path: str):
    pts = beaches()
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(days=DAYS)
    hours = [start + timedelta(hours=i) for i in range(DAYS * 24 + 1)]
    dst_h = np.array([h.timestamp() / 3600.0 for h in hours])
    result = {"generatedAt": start.isoformat(), "source": "E.U. Copernicus Marine Service Information",
              "t": [h.strftime("%Y-%m-%dT%H:%MZ") for h in hours], "points": {}}
    got = set()

    for region, sets in DATASETS.items():
        group = [p for p in pts if p["water"] == region]
        if not group:
            continue
        lats = [p["lat"] for p in group]
        lons = [p["lon"] for p in group]
        for kind, (candidates, variables, product, pattern) in sets.items():
            back = {"waves": HISTORY_H, "sst": SST_HISTORY_H, "bott": 24}.get(kind, 0)
            t0 = start - timedelta(hours=back)
            box = dict(minimum_latitude=min(lats) - 0.3, maximum_latitude=max(lats) + 0.3,
                       minimum_longitude=min(lons) - 0.3, maximum_longitude=max(lons) + 0.3,
                       start_datetime=t0.isoformat(), end_datetime=end.isoformat())
            try:
                ds = open_first(candidates, variables, box, product, pattern)
            except Exception as e:  # noqa: BLE001
                if is_auth_error(e) or kind in ("waves", "sst"):
                    raise
                print(f"{region}/{kind} skipped: {type(e).__name__}: {str(e)[:200]}")
                continue
            if ds is None:
                print(f"{region}/{kind}: no dataset available")
                continue
            if "depth" in ds.dims:
                ds = ds.isel(depth=0)
            src_h = to_hours(ds["time"].values)
            for p in group:
                # Nearest *sea* cell (coastal points often fall on land-masked cells, e.g. the
                # narrow tip of the Gulf of Aqaba): search all cells that have data, within ~35 km.
                cell = nearest_sea(ds, variables[0], p["lat"], p["lon"])
                if cell is None:
                    print(f"no sea cell near {p['id']} ({kind})")
                    continue
                entry = result["points"].setdefault(p["id"], {})
                got.add(kind)
                if kind == "cur":
                    u, v = (cell[variables[0]].values, cell[variables[1]].values)
                    entry["cur"] = interp(src_h, np.hypot(u, v), dst_h)
                    # Direction the water flows TOWARDS (oceanographic convention).
                    entry["curDir"] = interp(src_h, (np.degrees(np.arctan2(u, v)) + 360) % 360, dst_h, direction=True)
                    continue
                for var in variables:
                    key = OUT_KEYS[var]
                    entry[key] = interp(src_h, cell[var].values, dst_h, direction=key in DIRECTIONS)
                if kind == "sst":
                    for key, lag in SST_LAGS.items():
                        entry[key] = lagged_change(src_h, cell["thetao"].values, dst_h, lag)
                if kind == "waves":
                    w = np.asarray(cell["VHM0"].values, dtype=float)
                    stir = []
                    for h in dst_h:
                        m = (src_h <= h) & (src_h > h - STIR_H) & ~np.isnan(w)
                        stir.append(clean(w[m].max()) if m.any() else None)
                    entry["stir"] = stir

    if not result["points"] or "waves" not in got:
        sys.exit("no marine data retrieved")
    print(f"kinds retrieved: {sorted(got)}")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(result, separators=(",", ":")), encoding="utf8")
    print(f"wrote {out_path}: {len(result['points'])} beaches")


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "out/marine.json")
    except BaseException as e:  # record why, for status.json / the alert issue
        Path("tmp").mkdir(exist_ok=True)
        Path("tmp/marine.err").write_text(f"{type(e).__name__}: {e}", encoding="utf8")
        raise
