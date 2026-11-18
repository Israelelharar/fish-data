"""
Builds marine.json (waves, swell, sea temperature, 7 days, hourly) for every sea beach
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import copernicusmarine as cm
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DAYS = 7

DATASETS = {
    # Mediterranean (4.2 km, hourly). Does NOT cover the Red Sea.
    "med": {
        "waves": ("cmems_mod_med_wav_anfc_4.2km_PT1H-i", ["VHM0", "VMDR", "VTM10", "VHM0_SW1", "VTM01_SW1"]),
        "sst": ("cmems_mod_med_phy-tem_anfc_4.2km_PT1H-m", ["thetao"]),
    },
    # Global (1/12°) for Eilat / Gulf of Aqaba.
    "eilat": {
        "waves": ("cmems_mod_glo_wav_anfc_0.083deg_PT3H-i", ["VHM0", "VMDR", "VTM10", "VHM0_SW1", "VTM01_SW1"]),
        "sst": ("cmems_mod_glo_phy-thetao_anfc_0.083deg_PT1H-m", ["thetao"]),
    },
}
OUT_KEYS = {"VHM0": "wave", "VMDR": "waveDir", "VTM10": "wavePer", "VHM0_SW1": "swell", "VTM01_SW1": "swellPer", "thetao": "sst"}


def beaches():
    """Sea points for every beach, exported from the app (data/points.json)."""
    return json.loads((ROOT / "data/points.json").read_text(encoding="utf8"))


def clean(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), 2)


def main(out_path: str):
    pts = beaches()
    start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(days=DAYS)
    result = {"generatedAt": start.isoformat(), "source": "E.U. Copernicus Marine Service Information", "points": {}}

    for region, sets in DATASETS.items():
        group = [p for p in pts if p["water"] == region]
        if not group:
            continue
        lats = [p["lat"] for p in group]
        lons = [p["lon"] for p in group]
        box = dict(minimum_latitude=min(lats) - 0.1, maximum_latitude=max(lats) + 0.1,
                   minimum_longitude=min(lons) - 0.1, maximum_longitude=max(lons) + 0.1,
                   start_datetime=start.isoformat(), end_datetime=end.isoformat())
        for kind, (dataset_id, variables) in sets.items():
            extra = {"minimum_depth": 0, "maximum_depth": 1.1} if kind == "sst" else {}
            ds = cm.open_dataset(dataset_id=dataset_id, variables=variables, **box, **extra)
            if "depth" in ds.dims:
                ds = ds.isel(depth=0)
            times = [np.datetime_as_string(t, unit="m") + "Z" for t in ds["time"].values]
            for p in group:
                # Nearest *sea* cell: try the point, then widen until a non-NaN value appears.
                cell = None
                for tol in (0.05, 0.1, 0.2):
                    near = ds.sel(latitude=p["lat"], longitude=p["lon"], method="nearest", tolerance=tol)
                    if not np.isnan(near[variables[0]].values).all():
                        cell = near
                        break
                if cell is None:
                    continue
                entry = result["points"].setdefault(p["id"], {})
                entry[f"t_{kind}"] = times
                for v in variables:
                    entry[OUT_KEYS[v]] = [clean(x) for x in cell[v].values]

    # Waves and SST may have different time steps: keep the wave times as the main axis and
    # put SST on its own axis only if it differs (the app resamples anyway).
    for e in result["points"].values():
        e["t"] = e.pop("t_waves", e.get("t_sst", []))
        t_sst = e.pop("t_sst", None)
        if t_sst and t_sst != e["t"] and "sst" in e:
            lookup = dict(zip(t_sst, e["sst"]))
            e["sst"] = [lookup.get(t) for t in e["t"]]

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(result, separators=(",", ":")), encoding="utf8")
    print(f"wrote {out_path}: {len(result['points'])} beaches")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "out/marine.json")
