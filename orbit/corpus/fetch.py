"""Build the Earth-observation frame corpus from NASA GIBS (public NASA imagery).

    uv run python -m orbit.corpus.fetch [--out corpus] [--scenes N] [--dates N]

Fixed scenes x fixed dates, each a 128x128 MODIS Terra true-colour tile converted to
8-bit grayscale. The only network code in the repo is fetch_live() below; the demo
itself loads the saved corpus offline via orbit.corpus.load().
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SOURCE = "NASA GIBS WMS (https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi)"
LAYER = "MODIS_Terra_CorrectedReflectance_TrueColor"
# Verbatim from the GIBS API page, "Data Use Guidance and Acknowledgements" (fetched 2026-09-14).
LICENCE = ("NASA supports full and open sharing of data. We acknowledge the use of imagery provided "
           "by services from NASA's Global Imagery Browse Services (GIBS), part of NASA's Earth "
           "Science Data and Information System (ESDIS).")
SIZE = 128
MIN_GRAY_LEVELS = 8       # GIBS returns flat black/blank for no-data; real terrain has far more levels
MAX_BLACK = 0.5           # no-data is opaque pure black (alpha stays 255); swath gaps show as black wedges
MIN_INTERVAL = 0.25       # <= 4 requests/s

# name -> (lat0, lon0, lat1, lon1), 0.6 deg boxes, mid-latitude/tropical only (no polar night).
SCENES: dict[str, tuple[float, float, float, float]] = {
    "tokyo":       (35.4, 139.4, 36.0, 140.0),
    "cairo":       (29.8, 31.0, 30.4, 31.6),
    "sahara":      (23.0, 10.0, 23.6, 10.6),
    "atacama":     (-24.0, -69.6, -23.4, -69.0),
    "pacific":     (5.0, -160.0, 5.6, -159.4),
    "amazon":      (-3.6, -62.0, -3.0, -61.4),
    "iowa":        (41.8, -93.6, 42.4, -93.0),
    "netherlands": (52.6, 4.4, 53.2, 5.0),
    "alps":        (45.8, 7.4, 46.4, 8.0),
    "himalaya":    (27.8, 86.6, 28.4, 87.2),
    "greenland":   (66.8, -50.4, 67.4, -49.8),
    "ganges":      (21.8, 88.6, 22.4, 89.2),
    "great_lakes": (43.6, -87.0, 44.2, -86.4),
    "hawaii":      (19.2, -155.8, 19.8, -155.2),
}
# 16 dates over 2024, roughly every 3 weeks Feb-Nov.
DATES = ["2024-02-01", "2024-02-20", "2024-03-10", "2024-03-30", "2024-04-18", "2024-05-08",
         "2024-05-27", "2024-06-15", "2024-07-05", "2024-07-24", "2024-08-13", "2024-09-01",
         "2024-09-21", "2024-10-10", "2024-10-30", "2024-11-18"]


def url_for(bbox: tuple[float, float, float, float], date: str) -> str:
    lat0, lon0, lat1, lon1 = bbox
    return ("https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?SERVICE=WMS&REQUEST=GetMap"
            f"&VERSION=1.3.0&LAYERS={LAYER}&CRS=EPSG:4326&BBOX={lat0},{lon0},{lat1},{lon1}"
            f"&WIDTH={SIZE}&HEIGHT={SIZE}&FORMAT=image/png&TIME={date}")


def fetch_live(url: str) -> bytes:
    """The one place the repo touches the network."""
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def fetch_retry(url: str, tries: int = 3) -> bytes:
    for i in range(tries):
        try:
            return fetch_live(url)
        except Exception as e:      # noqa: BLE001 - any transport error, retry with backoff
            err = e
            time.sleep(2 ** i)
    raise RuntimeError(f"{type(err).__name__}: {err}")


def to_gray(png: bytes) -> np.ndarray:
    from PIL import Image
    g = np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=np.uint8)
    assert g.shape == (SIZE, SIZE), g.shape
    return g


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="corpus")
    ap.add_argument("--scenes", type=int, default=len(SCENES))
    ap.add_argument("--dates", type=int, default=len(DATES))
    a = ap.parse_args(argv)
    from PIL import Image

    out = Path(a.out)
    (out / "png").mkdir(parents=True, exist_ok=True)
    frames, meta, scenes, skipped = [], [], {}, []
    last = 0.0
    for name, bbox in list(SCENES.items())[: a.scenes]:
        for date in DATES[: a.dates]:
            url = url_for(bbox, date)
            time.sleep(max(0.0, last + MIN_INTERVAL - time.monotonic()))
            last = time.monotonic()
            try:
                g = to_gray(fetch_retry(url))
            except Exception as e:  # noqa: BLE001
                skipped.append({"scene": name, "date": date, "url": url, "reason": str(e)})
                print(f"skip {name} {date}: {e}", file=sys.stderr)
                continue
            reason = ("empty tile" if len(np.unique(g)) < MIN_GRAY_LEVELS else
                      "no-data gap" if (g == 0).mean() > MAX_BLACK else None)
            if reason:
                skipped.append({"scene": name, "date": date, "url": url, "reason": reason})
                print(f"skip {name} {date}: {reason}", file=sys.stderr)
                continue
            fid = len(frames)
            frames.append(g)
            meta.append({"id": fid, "scene": name, "lat0": bbox[0], "lon0": bbox[1], "lat1": bbox[2],
                         "lon1": bbox[3], "date": date, "url": url,
                         "sha256": hashlib.sha256(g.tobytes()).hexdigest()})
            scenes.setdefault(name, {"bbox": list(bbox), "reference_id": fid})   # earliest date wins
            Image.fromarray(g).save(out / "png" / f"{fid:04d}.png", optimize=True)
            print(f"{fid:4d} {name:12s} {date} levels={len(np.unique(g))}")

    np.savez_compressed(out / "frames.npz", frames=np.stack(frames), ids=np.arange(len(frames), dtype=np.uint16))
    manifest = {"source": SOURCE, "layer": LAYER, "licence": LICENCE,
                "fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "frames": meta, "scenes": scenes, "skipped": skipped}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"{len(frames)} frames, {len(skipped)} skipped -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
