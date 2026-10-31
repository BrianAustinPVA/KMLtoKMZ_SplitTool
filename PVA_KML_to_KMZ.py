#!/usr/bin/env python3
"""
PVA KML to KMZ Converter - Peak Valley Aerials

Turns site-boundary KML/KMZ files into DJI Area-route KMZs (wpmz/template.kml +
waylines.wpml) for FlightHub 2 / DJI Pilot 2, copying every flight setting from a
mission KMZ you exported (your approved template) and swapping in the new boundary.

For each file it:
  - pulls out every Polygon (including ones inside MultiGeometry/Folders)
  - drops altitudes, duplicate points and unclosed rings; clamps to ground
  - strips broken styles, folders, extended data and other Google Earth extras
  - writes one DJI KMZ per polygon, with a FlightHub-safe name:
    no underscores, slashes or other special characters

Only uses the Python standard library - no installs needed.
"""

import json
import os
import re
import subprocess
import sys
import time
import tkinter as tk
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "PVA KML to KMZ Converter"
VERSION = "v1.0"
IN_EXTS = {".kml", ".kmz"}
SETTINGS_FILE = Path.home() / ".pva_kml_to_kmz.json"
KML_NS = "http://www.opengis.net/kml/2.2"


# ----------------------------------------------------------------------------
# Conversion core
# ----------------------------------------------------------------------------

def safe_name(text, sep="-"):
    """FlightHub-safe name: letters, digits and single separators only."""
    text = re.sub(r"[^A-Za-z0-9]+", sep, text)   # _, /, spaces, punctuation -> sep
    text = re.sub(re.escape(sep) + "{2,}", sep, text)
    return text.strip(sep) or "boundary"


def read_kml_text(path):
    path = Path(path)
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as z:
            kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ValueError("no .kml inside the KMZ")
            main = "doc.kml" if "doc.kml" in kmls else kmls[0]
            return z.read(main)
    return path.read_bytes()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_coords(text):
    pts = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError(f"coordinate out of range: {tok}")
        pts.append((lon, lat))
    return pts


def _clean_ring(pts):
    out = []
    for p in pts:
        if not out or (abs(p[0] - out[-1][0]) > 1e-10 or abs(p[1] - out[-1][1]) > 1e-10):
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    if len(out) < 3:
        return None
    # outer rings counter-clockwise (KML convention)
    area = sum(out[i][0] * out[(i + 1) % len(out)][1] - out[(i + 1) % len(out)][0] * out[i][1]
               for i in range(len(out)))
    if area < 0:
        out.reverse()
    out.append(out[0])  # close ring
    return out


def extract_polygons(kml_bytes):
    """Return list of (placemark_name, outer_ring, [inner_rings])."""
    root = ET.fromstring(kml_bytes)
    polys = []
    for pm in root.iter():
        if _local(pm.tag) != "Placemark":
            continue
        pm_name = ""
        for ch in pm:
            if _local(ch.tag) == "name":
                pm_name = (ch.text or "").strip()
        for poly in pm.iter():
            if _local(poly.tag) != "Polygon":
                continue
            outer, inners = None, []
            for b in poly:
                kind = _local(b.tag)
                if kind not in ("outerBoundaryIs", "innerBoundaryIs"):
                    continue
                coords = next((e for e in b.iter() if _local(e.tag) == "coordinates"), None)
                ring = _clean_ring(_parse_coords(coords.text if coords is not None else ""))
                if ring is None:
                    continue
                if kind == "outerBoundaryIs":
                    outer = ring
                else:
                    inners.append(ring)
            if outer:
                polys.append((pm_name, outer, inners))
    return polys




class DjiTemplate:
    """A DJI mapping-area KMZ (exported from FlightHub 2 / Pilot 2) used as the mission template."""

    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            names = z.namelist()
            if "wpmz/template.kml" not in names or "wpmz/waylines.wpml" not in names:
                raise ValueError("not a DJI mission KMZ (needs wpmz/template.kml + wpmz/waylines.wpml)")
            self.template = z.read("wpmz/template.kml").decode("utf-8")
            self.waylines = z.read("wpmz/waylines.wpml").decode("utf-8")
            self.res = {n: z.read(n) for n in names
                        if n.startswith("wpmz/res/") and not n.endswith("/")}
        if "<wpml:templateType>mapping2d</wpml:templateType>" not in self.template:
            raise ValueError("template must be an Area (mapping2d) route")
        if not re.search(r"<Polygon>.*?<coordinates>.*?</coordinates>", self.template, re.S):
            raise ValueError("template has no boundary polygon")

    def describe(self):
        g = lambda tag: (re.search(rf"<wpml:{tag}>([^<]*)</wpml:{tag}>", self.template) or [None, "?"])[1]
        return (f"drone {g('droneEnumValue')}, payload {g('payloadEnumValue')}, "
                f"{g('globalShootHeight')} m, speed {g('autoFlightSpeed')} m/s, "
                f"overlap {g('orthoCameraOverlapH')}/{g('orthoCameraOverlapW')}, "
                f"terrain follow {'on' if g('surfaceFollowModeEnable') == '1' else 'off'}")

    @staticmethod
    def _dsm_tile_covers(dsm_name, ring):
        m = re.search(r"([NS])(\d{2})([EW])(\d{3})", dsm_name)
        if not m:
            return False
        lat0 = int(m[2]) * (1 if m[1] == "N" else -1)
        lon0 = int(m[4]) * (1 if m[3] == "E" else -1)
        return all(lat0 <= lat <= lat0 + 1 and lon0 <= lon <= lon0 + 1 for lon, lat in ring)

    def build(self, ring, out_path):
        """Write a DJI KMZ: template settings + new boundary + empty flight lines.
        Returns list of notes for the log."""
        notes = []
        pts = ring[:-1] if ring[0] == ring[-1] else list(ring)   # DJI: open ring
        area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
                   for i in range(len(pts)))
        if area > 0:                                              # DJI sample is clockwise
            pts.reverse()
        coord_txt = "\n" + "".join(f"                {lon:.13f},{lat:.13f},0\n" for lon, lat in pts) + "              "

        t = self.template
        t = re.sub(r"(<Polygon>.*?<coordinates>).*?(</coordinates>)",
                   lambda m: m[1] + coord_txt + m[2], t, count=1, flags=re.S)
        now = str(int(time.time() * 1000))
        t = re.sub(r"<wpml:(createTime|updateTime)>\d+<", lambda m: f"<wpml:{m[1]}>{now}<", t)

        # takeoff reference point -> centre of new boundary (keeps template's height)
        clat = sum(p[1] for p in pts) / len(pts)
        clon = sum(p[0] for p in pts) / len(pts)
        t = re.sub(r"<wpml:takeOffRefPoint>[^,<]+,[^,<]+,([^<]+)<",
                   lambda m: f"<wpml:takeOffRefPoint>{clat:.6f},{clon:.6f},{m[1]}<", t)

        # terrain-follow DSM: keep only if the template's tile still covers the new area
        res = dict(self.res)
        dsm = re.search(r"<wpml:dsmFile>([^<]*)</wpml:dsmFile>", t)
        if dsm:
            if dsm[1] in res and self._dsm_tile_covers(dsm[1], pts):
                notes.append("terrain-follow DSM kept")
            else:
                t = t.replace(dsm[0], "")
                t = t.replace("<wpml:surfaceFollowModeEnable>1<", "<wpml:surfaceFollowModeEnable>0<")
                res.pop(dsm[1], None)
                notes.append("TERRAIN FOLLOW OFF - area outside template DSM tile, re-enable in FlightHub")

        # flight lines: keep template's header + start actions, drop old waypoints
        w = self.waylines
        w = re.sub(r"\s*<Placemark>.*?</Placemark>", "", w, flags=re.S)
        w = re.sub(r"<wpml:distance>[^<]*<", "<wpml:distance>0<", w)
        w = re.sub(r"<wpml:duration>[^<]*<", "<wpml:duration>0<", w)

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("wpmz/waylines.wpml", w)
            for n, data in res.items():
                z.writestr(n, data)
            z.writestr("wpmz/template.kml", t)
        return notes


def ring_area_acres(ring):
    """Approximate area via local equirectangular projection."""
    import math
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    R = 6371008.8
    xy = [(math.radians(lon) * R * math.cos(lat0), math.radians(lat) * R) for lon, lat in ring]
    a = abs(sum(xy[i][0] * xy[i + 1][1] - xy[i + 1][0] * xy[i][1] for i in range(len(xy) - 1))) / 2
    return a / 4046.8564224


def split_narrow_strips(ring, width_m):
    """Cut out connecting strips narrower than width_m. Returns list of closed rings,
    west-to-east, plus acres removed. Original edges are kept exactly; only the
    strip pieces are subtracted. Needs shapely."""
    import math
    from shapely.geometry import Polygon
    from shapely.validation import make_valid
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    R = 6371008.8
    k = R * math.cos(lat0)
    P = make_valid(Polygon([(math.radians(lon) * k, math.radians(lat) * R) for lon, lat in ring]))
    d = width_m / 2.0
    opened = P.buffer(-d, join_style=2, mitre_limit=10).buffer(d, join_style=2, mitre_limit=10)
    lost = P.difference(opened)
    min_strip = width_m * width_m          # ignore hairline slivers along crooked edges
    strips = [g for g in getattr(lost, "geoms", [lost]) if g.area >= min_strip]
    if not strips:
        return [ring], 0.0
    result = P
    for g in strips:
        result = result.difference(g.buffer(0.01))
    parts = [g for g in getattr(result, "geoms", [result])
             if g.geom_type == "Polygon" and g.area >= min_strip]
    parts.sort(key=lambda g: g.centroid.x)
    removed = (P.area - sum(g.area for g in parts)) / 4046.8564224
    out = []
    for g in parts:
        g = g.simplify(0.05)               # drop collinear points left by the cut (5 cm)
        pts = [(math.degrees(x / k), math.degrees(y / R)) for x, y in g.exterior.coords]
        out.append(pts)
    return out, removed


def subdivide_by_area(ring, max_acres, overlap_m=0.0):
    """Cut an area with straight lines (across its long side, at the equal-acreage
    position) until every piece is <= max_acres. Pieces can overlap their neighbours
    by overlap_m (clipped to the original boundary). Returns closed lon/lat rings."""
    import math
    from shapely.geometry import Polygon, box
    from shapely.validation import make_valid
    lat0 = math.radians(sum(p[1] for p in ring) / len(ring))
    R = 6371008.8
    k = R * math.cos(lat0)
    P = make_valid(Polygon([(math.radians(lon) * k, math.radians(lat) * R) for lon, lat in ring]))
    max_m2 = max_acres * 4046.8564224

    def polys(g):
        return [x for x in getattr(g, "geoms", [g]) if x.geom_type == "Polygon" and x.area > 1.0]

    def halve(g):
        minx, miny, maxx, maxy = g.bounds
        vertical = (maxx - minx) >= (maxy - miny)       # cut across the long side
        lo, hi = (minx, maxx) if vertical else (miny, maxy)
        target = g.area / 2
        for _ in range(50):                             # bisection for equal area
            mid = (lo + hi) / 2
            cut = box(minx - 1, miny - 1, mid, maxy + 1) if vertical else box(minx - 1, miny - 1, maxx + 1, mid)
            if g.intersection(cut).area < target:
                lo = mid
            else:
                hi = mid
        mid = (lo + hi) / 2
        a = box(minx - 1, miny - 1, mid, maxy + 1) if vertical else box(minx - 1, miny - 1, maxx + 1, mid)
        b = box(mid, miny - 1, maxx + 1, maxy + 1) if vertical else box(minx - 1, mid, maxx + 1, maxy + 1)
        return polys(g.intersection(a)) + polys(g.intersection(b))

    todo, done = polys(P), []
    while todo:
        g = todo.pop()
        if g.area <= max_m2 * 1.001 or len(done) + len(todo) > 500:
            done.append(g)
        else:
            todo += halve(g)
    if overlap_m > 0 and len(done) > 1:
        done = [polys(g.buffer(overlap_m, join_style=2).intersection(P))[0] for g in done]
    done.sort(key=lambda g: (round(g.centroid.x / 50), -g.centroid.y))
    out = []
    for g in done:
        g = g.simplify(0.05)
        out.append([(math.degrees(x / k), math.degrees(y / R)) for x, y in g.exterior.coords])
    return out


def add_seam_overlap(rings, band_m):
    """Grow each piece into the pieces it touches so neighbours share a band band_m wide
    along every seam. Pieces never grow outside the combined area of all pieces
    (so nothing spills into deleted areas or past the boundary)."""
    if band_m <= 0 or len(rings) < 2:
        return rings
    from shapely.ops import unary_union
    proj = LocalProj(rings)
    geos = [proj.to_poly(r) for r in rings]
    U = unary_union([g.buffer(0.01) for g in geos]).buffer(-0.01)
    out = []
    for g in geos:
        grown = _polys(g.buffer(band_m / 2.0, join_style=2).intersection(U))
        grown = max(grown, key=lambda x: x.area) if grown else g
        out.append(proj.to_ring(grown))
    return out


MANUAL = {}   # path -> list of rings saved in the Area Editor (set by the app)


def plan_outputs(path, sep, strip_m=0, max_ac=0, overlap_m=0, keep_whole=(), detail=False, use_manual=True):
    """Inspect a file: returns (polygons, output base names).
    keep_whole: indexes of areas (after strip removal) that must not be cut.
    detail=True also returns (parents, areas): which area each output came from,
    and the area rings themselves (for the preview map)."""
    manual = MANUAL.get(str(path)) if use_manual else None
    if manual:
        polys = [("manual", r, []) for r in add_seam_overlap(manual, overlap_m)]
        base = safe_name(Path(path).stem, sep)
        names = [base] if len(polys) <= 1 else [f"{base}{sep}{i}" for i in range(1, len(polys) + 1)]
        if detail:
            return polys, names, list(range(len(polys))), [r for _, r, _ in polys]
        return polys, names
    polys = extract_polygons(read_kml_text(path))
    if strip_m and strip_m > 0:
        split = []
        for pm_name, outer, inners in polys:
            rings, _ = split_narrow_strips(outer, strip_m)
            split += [(pm_name, r, inners if len(rings) == 1 else []) for r in rings]
        polys = split
    areas = [outer for _, outer, _ in polys]
    parents = list(range(len(polys)))
    if max_ac and max_ac > 0:
        cut, cut_par = [], []
        for i, (pm_name, outer, inners) in enumerate(polys):
            rings = [outer] if i in keep_whole else subdivide_by_area(outer, max_ac, 0)
            cut += [(pm_name, r, inners if len(rings) == 1 else []) for r in rings]
            cut_par += [i] * len(rings)
        polys, parents = cut, cut_par
    if overlap_m and overlap_m > 0 and len(polys) > 1:
        grown = add_seam_overlap([r for _, r, _ in polys], overlap_m)
        polys = [(pm, r, inn) for (pm, _, inn), r in zip(polys, grown)]
    base = safe_name(Path(path).stem, sep)
    names = [base] if len(polys) <= 1 else [f"{base}{sep}{i}" for i in range(1, len(polys) + 1)]
    if detail:
        return polys, names, parents, areas
    return polys, names


def convert_file(path, out_dir, sep, tmpl, keep_dates, overwrite, strip_m=0, max_ac=0, overlap_m=0, keep_whole=()):
    """Convert one KML/KMZ. Yields (status, message)."""
    polys, names = plan_outputs(path, sep, strip_m, max_ac, overlap_m, keep_whole)
    if not polys:
        yield "fail", f"'{Path(path).name}': no polygon found (boundary files need a closed area)"
        return
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_stat = Path(path).stat()
    for (pm_name, outer, inners), name in zip(polys, names):
        out = out_dir / f"{name}.kmz"
        if out.exists() and not overwrite:
            yield "skip", f"'{out.name}': Skipped (exists)"
            continue
        notes = tmpl.build(outer, out)
        if keep_dates:
            os.utime(out, (src_stat.st_atime, src_stat.st_mtime))
        acres = ring_area_acres(outer)
        if inners:
            notes.append(f"{len(inners)} hole(s) ignored - DJI area routes use the outer boundary")
        status = "warn" if any(n.isupper() or n.startswith("TERRAIN") for n in notes) else "ok"
        yield status, (f"'{out.name}': {len(outer) - 1} vertices, {acres:,.1f} ac, "
                       + "; ".join(notes))


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------

def list_inputs(folder):
    try:
        return sorted(f for f in Path(folder).iterdir()
                      if f.is_file() and f.suffix.lower() in IN_EXTS)
    except OSError:
        return []


PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
           "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]
SAT_TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"


class LocalProj:
    """Flat local metres <-> lon/lat (equirectangular around the site's mean latitude)."""

    def __init__(self, rings):
        import math
        pts = [p for r in rings for p in r]
        self.lat0 = math.radians(sum(p[1] for p in pts) / len(pts))
        self.R = 6371008.8
        self.k = self.R * math.cos(self.lat0)

    def fwd(self, lon, lat):
        import math
        return math.radians(lon) * self.k, math.radians(lat) * self.R

    def inv(self, x, y):
        import math
        return math.degrees(x / self.k), math.degrees(y / self.R)

    def to_poly(self, ring):
        from shapely.geometry import Polygon
        from shapely.validation import make_valid
        return make_valid(Polygon([self.fwd(*p) for p in ring]))

    def to_ring(self, g):
        return [self.inv(x, y) for x, y in g.simplify(0.05).exterior.coords]


def _polys(g, min_m2=1.0):
    return [x for x in getattr(g, "geoms", [g]) if x.geom_type == "Polygon" and x.area > min_m2]


def divide_equal(g, n, direction="auto"):
    """Split polygon g into n equal-area pieces with parallel straight cuts."""
    from shapely.geometry import box
    minx, miny, maxx, maxy = g.bounds
    if direction == "auto":
        vertical = (maxx - minx) >= (maxy - miny)
    else:
        vertical = direction.startswith("West")
    total = g.area
    cuts = []
    for i in range(1, n):
        lo, hi = (minx, maxx) if vertical else (miny, maxy)
        for _ in range(50):
            mid = (lo + hi) / 2
            part = box(minx - 1, miny - 1, mid, maxy + 1) if vertical else box(minx - 1, miny - 1, maxx + 1, mid)
            if g.intersection(part).area < total * i / n:
                lo = mid
            else:
                hi = mid
        cuts.append((lo + hi) / 2)
    edges = [minx - 1 if vertical else miny - 1] + cuts + [maxx + 1 if vertical else maxy + 1]
    out = []
    for a, b in zip(edges, edges[1:]):
        slab = box(a, miny - 1, b, maxy + 1) if vertical else box(minx - 1, a, maxx + 1, b)
        out += _polys(g.intersection(slab))
    return out


class AreaEditor(tk.Toplevel):
    """Map preview + manual editor: cut with lines, delete, divide. Saves per file."""

    def __init__(self, app, path):
        super().__init__(app)
        self.app = app
        self.geometry("1200x820")
        self.mode = tk.StringVar(value="select")
        self.div_n = tk.IntVar(value=2)
        self.div_dir = tk.StringVar(value="auto")
        self.status = tk.StringVar()
        self.blank = tk.PhotoImage(width=1, height=1)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Return>", lambda e: self.finish_cut())
        self.bind("<Escape>", lambda e: (self.cancel_cut(), self.clear_measure()))
        self.bind("<Delete>", lambda e: self.delete_selected())
        self.bind("<Control-z>", lambda e: self.undo())
        self.set_path(path)

    # ---------- layout ----------
    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(6, 2))
        self.head = ttk.Label(bar, font=("Segoe UI", 10, "bold"))
        self.head.pack(side="left")
        self.badge = ttk.Label(bar, font=("Segoe UI", 9, "bold"))
        self.badge.pack(side="left", padx=10)

        tools = ttk.Frame(self)
        tools.pack(fill="x", padx=8, pady=4)
        ttk.Label(tools, text="Tool:").pack(side="left")
        for val, txt in (("select", "Select"), ("cut", "Cut line"), ("delete", "Delete"),
                         ("addback", "Add back"), ("draw", "Draw area"), ("measure", "Measure")):
            ttk.Radiobutton(tools, text=txt, value=val, variable=self.mode,
                            command=self._mode_changed).pack(side="left", padx=4)
        self.finish_btn = ttk.Button(tools, text="Finish (Enter)", command=self.finish_cut, state="disabled")
        self.finish_btn.pack(side="left", padx=4)
        ttk.Separator(tools, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(tools, text="Divide selected into").pack(side="left")
        ttk.Spinbox(tools, from_=2, to=20, width=3, textvariable=self.div_n).pack(side="left", padx=3)
        ttk.Label(tools, text="equal pieces, cuts running").pack(side="left")
        ttk.Combobox(tools, textvariable=self.div_dir, width=16, state="readonly",
                     values=["auto", "North-South", "West-East"]).pack(side="left", padx=3)
        ttk.Button(tools, text="Divide", command=self.divide_selected).pack(side="left", padx=3)
        ttk.Button(tools, text="Merge selected", command=self.merge_selected).pack(side="left", padx=3)
        ttk.Button(tools, text="Delete selected", command=self.delete_selected).pack(side="left", padx=3)

        tools2 = ttk.Frame(self)
        tools2.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Button(tools2, text="Undo (Ctrl+Z)", command=self.undo).pack(side="left", padx=3)
        ttk.Button(tools2, text="Reset to automatic", command=self.reset_auto).pack(side="left", padx=3)
        ttk.Button(tools2, text="Restore original boundary", command=self.reset_original).pack(side="left", padx=3)
        ttk.Button(tools2, text="Save edits for this file", command=self.save).pack(side="right", padx=3)
        ttk.Button(tools2, text="Find takeoff spots", command=self.find_takeoff).pack(side="left", padx=12)
        ttk.Button(tools2, text="Discard edits", command=self.discard).pack(side="right", padx=3)

        try:
            import tkintermapview
            self.map = tkintermapview.TkinterMapView(self, corner_radius=0)
            self.map.set_tile_server(SAT_TILES, max_zoom=19)
            self.map.add_left_click_map_command(self.on_click)
            self.map.pack(fill="both", expand=True, padx=8)
        except Exception:
            self.map = None
            ttk.Label(self, text="Map editor needs the 'tkintermapview' package:\n\n"
                                 "    python -m pip install tkintermapview",
                      font=("Consolas", 11)).pack(fill="both", expand=True, padx=20, pady=20)
        ttk.Label(self, textvariable=self.status, foreground="#555").pack(anchor="w", padx=10)
        self.measure_txt = tk.StringVar()
        ttk.Label(self, textvariable=self.measure_txt, foreground="#0050a0",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10)
        self.summary = tk.Text(self, height=8, bg="#0c0c0c", fg="#7CFC7C", font=("Consolas", 10),
                               relief="flat", state="disabled")
        self.summary.pack(fill="x", padx=8, pady=(2, 8))

    # ---------- data ----------
    def set_path(self, path):
        self.path = path
        self.title(f"Area Editor - {Path(path).name}")
        try:
            self.original = [o for _, o, _ in extract_polygons(read_kml_text(path))]
            if not self.original:
                raise ValueError("no polygon in file")
            self.proj = LocalProj(self.original)
            self.orig_geo = [self.proj.to_poly(r) for r in self.original]
        except Exception as e:
            self.head.config(text=f"Can't open: {e}")
            self.pieces = []
            return
        self.undo_stack, self.selected, self.cut_pts = [], set(), []
        saved = self.app.manual.get(path)
        if saved:
            self.pieces = [self.proj.to_poly(r) for r in saved]
            self.dirty = False
        else:
            self.pieces = self._auto_pieces()
            self.dirty = False
        self.redraw(fit=True)

    def _auto_pieces(self):
        app = self.app
        max_ac, ov = app._cut()
        polys, *_ = plan_outputs(self.path, app.sep.get() or "-", app._strip(), max_ac, 0,
                                 app.keep_whole.get(self.path, set()), detail=True, use_manual=False)
        return [self.proj.to_poly(r) for _, r, _ in polys]

    def _push(self):
        self.undo_stack.append([g for g in self.pieces])
        self.undo_stack = self.undo_stack[-50:]
        self.dirty = True

    def _sorted(self):
        self.pieces.sort(key=lambda g: (round(g.centroid.x / 50), -g.centroid.y))

    # ---------- interaction ----------
    def _mode_changed(self):
        self.cancel_cut()
        if self.mode.get() != "measure":
            self.clear_measure()
        tips = {"select": "Click pieces to select / unselect them (yellow outline). Drag to pan, scroll to zoom.",
                "cut": "Click points along your cut line, starting and ending OUTSIDE the area. "
                       "Press Enter (or Finish cut) to cut. Esc cancels.",
                "delete": "Click a piece to delete it. Ctrl+Z undoes.",
                "addback": "Click an EMPTY spot inside the red original boundary (e.g. a deleted corridor) "
                           "to bring that whole region back as its own piece.",
                "draw": "Click the corners of a new flight area, then press Enter (or Finish) to create it. "
                        "Esc cancels. It can be anywhere, even outside the red boundary.",
                "measure": "Click points to measure distance (each leg + total). 3+ points also shows the "
                           "enclosed area. Esc clears. Selecting a piece shows its length x width."}
        self.status.set(tips[self.mode.get()])

    def _hit(self, lat, lon):
        from shapely.geometry import Point
        pt = Point(*self.proj.fwd(lon, lat))
        for i, g in enumerate(self.pieces):
            if g.contains(pt):
                return i
        return None

    def on_click(self, coords):
        lat, lon = coords
        m = self.mode.get()
        if m == "measure":
            if not hasattr(self, "meas_pts"):
                self.meas_pts = []
            self.meas_pts.append((lon, lat))
            self._draw_measure()
            return
        if m == "addback":
            self.add_back(lat, lon)
            return
        if m in ("cut", "draw"):
            self.cut_pts.append((lon, lat))
            need = 2 if m == "cut" else 3
            self.finish_btn.config(state="normal" if len(self.cut_pts) >= need else "disabled")
            self._draw_cut()
            return
        i = self._hit(lat, lon)
        if i is None:
            return
        if m == "delete":
            self._push()
            del self.pieces[i]
            self.selected.clear()
            self.redraw()
        else:
            self.selected.symmetric_difference_update({i})
            self.redraw()

    def _draw_cut(self):
        if not self.map:
            return
        if getattr(self, "cut_path", None):
            self.cut_path.delete()
            self.cut_path = None
        for mk in getattr(self, "cut_marks", []):
            mk.delete()
        self.cut_marks = [self.map.set_marker(lat, lon, marker_color_circle="#ffff33",
                                              marker_color_outside="#c0a000")
                          for lon, lat in self.cut_pts]
        if len(self.cut_pts) >= 2:
            pts = list(self.cut_pts)
            if self.mode.get() == "draw" and len(pts) >= 3:
                pts.append(pts[0])
            self.cut_path = self.map.set_path([(lat, lon) for lon, lat in pts],
                                              color="#ffff33" if self.mode.get() == "cut" else "#ff66ff",
                                              width=3)

    @staticmethod
    def _fmt_len(m_):
        ft = m_ * 3.28084
        mi = f" / {m_ / 1609.344:.2f} mi" if m_ >= 400 else ""
        return f"{m_:,.0f} m / {ft:,.0f} ft{mi}"

    def _measure_values(self):
        import math
        xy = [self.proj.fwd(*p) for p in getattr(self, "meas_pts", [])]
        legs = [math.dist(a, b) for a, b in zip(xy, xy[1:])]
        area = 0.0
        if len(xy) >= 3:
            area = abs(sum(xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1]
                           for i in range(len(xy)))) / 2
        return legs, area

    def _draw_measure(self):
        legs, area = self._measure_values()
        pts = getattr(self, "meas_pts", [])
        if self.map:
            for obj in getattr(self, "meas_objs", []):
                obj.delete()
            self.meas_objs = []
            run = 0.0
            for i, (lon, lat) in enumerate(pts):
                run += legs[i - 1] if i else 0.0
                label = "start" if i == 0 else self._fmt_len(run)
                self.meas_objs.append(self.map.set_marker(lat, lon, text=label, text_color="#00e5ff",
                                                          marker_color_circle="#00e5ff",
                                                          marker_color_outside="#007a8a",
                                                          font=("Segoe UI", 10, "bold")))
            if len(pts) >= 2:
                self.meas_objs.append(self.map.set_path([(lat, lon) for lon, lat in pts],
                                                        color="#00e5ff", width=3))
        if not pts:
            self.measure_txt.set("")
            return
        txt = f"Total: {self._fmt_len(sum(legs))}"
        if len(legs) > 1:
            txt += "   |   legs: " + ", ".join(f"{l:,.0f} m" for l in legs)
        if area:
            txt += f"   |   enclosed area: {area / 4046.8564224:,.1f} ac"
        self.measure_txt.set(txt)

    def clear_measure(self):
        self.meas_pts = []
        self._draw_measure()

    def cancel_cut(self):
        self.cut_pts = []
        self.finish_btn.config(state="disabled")
        self._draw_cut()

    def add_back(self, lat, lon):
        from shapely.geometry import Point
        from shapely.ops import unary_union
        pt = Point(*self.proj.fwd(lon, lat))
        if self._hit(lat, lon) is not None:
            self.status.set("That spot is already covered by a piece - click an empty area inside the red line.")
            return
        orig = unary_union(self.orig_geo)
        if not orig.contains(pt):
            self.status.set("That spot is outside the original boundary - use Draw area for new areas.")
            return
        covered = unary_union([g.buffer(0.05) for g in self.pieces]) if self.pieces else None
        gaps = orig.difference(covered) if covered is not None else orig
        hit = [g for g in _polys(gaps, 25.0) if g.buffer(0.5).contains(pt)]
        if not hit:
            self.status.set("Nothing to add back there.")
            return
        self._push()
        self.pieces.append(hit[0])
        self.selected.clear()
        self.redraw()
        self.status.set(f"Added back {hit[0].area / 4046.8564224:,.1f} ac. Click another empty spot, or switch tools.")

    def finish_draw(self):
        from shapely.geometry import Polygon
        from shapely.validation import make_valid
        if len(self.cut_pts) < 3:
            return
        parts = _polys(make_valid(Polygon([self.proj.fwd(*p) for p in self.cut_pts])), 25.0)
        if not parts:
            messagebox.showwarning("Draw area", "That shape has no area - click at least 3 corners.", parent=self)
            return
        g = max(parts, key=lambda x: x.area)
        overlap = sum(g.intersection(o).area for o in self.pieces) / 4046.8564224
        self._push()
        self.pieces.append(g)
        self.selected.clear()
        self.cancel_cut()
        self.redraw()
        note = f" (overlaps other pieces by {overlap:,.1f} ac)" if overlap > 0.05 else ""
        self.status.set(f"Created a {g.area / 4046.8564224:,.1f} ac area{note}. Draw another, or switch tools.")

    def finish_cut(self):
        if self.mode.get() == "draw":
            return self.finish_draw()
        if self.mode.get() != "cut" or len(self.cut_pts) < 2:
            return
        from shapely.geometry import LineString
        from shapely.ops import split
        line = LineString([self.proj.fwd(*p) for p in self.cut_pts])
        new, n_cut = [], 0
        for g in self.pieces:
            if line.intersects(g):
                parts = _polys(split(g, line))
                if len(parts) > 1:
                    new += parts
                    n_cut += 1
                    continue
            new.append(g)
        if n_cut == 0:
            messagebox.showwarning("Cut", "That line didn't cut anything.\n\nStart and end the line "
                                          "outside the area so it crosses all the way through.", parent=self)
            return
        self._push()
        self.pieces = new
        self.selected.clear()
        self.cancel_cut()
        self.redraw()
        self.status.set(f"Cut {n_cut} piece(s). Draw another line, or switch tools.")

    def find_takeoff(self):
        if len(self.selected) == 1:
            i = next(iter(self.selected))
        elif len(self.pieces) == 1:
            i = 0
        else:
            messagebox.showinfo("Takeoff spots", "Select ONE piece (Select tool), then click Find takeoff spots.",
                                parent=self)
            return
        ring = self.proj.to_ring(self.pieces[i])
        if getattr(self, "takeoff_win", None) and self.takeoff_win.winfo_exists():
            self.takeoff_win.close()
        self.takeoff_win = TakeoffWindow(self, ring, f"piece {i + 1}")

    def divide_selected(self):
        if not self.selected:
            messagebox.showinfo("Divide", "Select one or more pieces first (Select tool).", parent=self)
            return
        n = max(2, int(self.div_n.get() or 2))
        d = self.div_dir.get()
        d = "auto" if d == "auto" else ("West" if d == "North-South" else "South")
        self._push()
        new = []
        for i, g in enumerate(self.pieces):
            new += divide_equal(g, n, d) if i in self.selected else [g]
        self.pieces = new
        self.selected.clear()
        self.redraw()

    def merge_selected(self):
        if len(self.selected) < 2:
            messagebox.showinfo("Merge", "Select two or more touching pieces to merge.", parent=self)
            return
        from shapely.ops import unary_union
        u = unary_union([self.pieces[i].buffer(0.05) for i in self.selected]).buffer(-0.05)
        parts = _polys(u)
        if len(parts) != 1:
            messagebox.showwarning("Merge", "Those pieces don't touch, so they can't become one area.",
                                   parent=self)
            return
        self._push()
        self.pieces = [g for i, g in enumerate(self.pieces) if i not in self.selected] + parts
        self.selected.clear()
        self.redraw()

    def delete_selected(self):
        if not self.selected:
            return
        self._push()
        self.pieces = [g for i, g in enumerate(self.pieces) if i not in self.selected]
        self.selected.clear()
        self.redraw()

    def undo(self):
        if self.undo_stack:
            self.pieces = self.undo_stack.pop()
            self.selected.clear()
            self.dirty = True
            self.redraw()

    def reset_auto(self):
        self._push()
        self.pieces = self._auto_pieces()
        self.selected.clear()
        self.redraw()

    def reset_original(self):
        self._push()
        self.pieces = [g for g in self.orig_geo]
        self.selected.clear()
        self.redraw()

    def save(self):
        self._sorted()
        self.app.manual[self.path] = [self.proj.to_ring(g) for g in self.pieces]
        self.dirty = False
        self.app._save_settings()
        self.app.scan_source(keep_checks=True)
        self.redraw()
        self.app.log(f"Saved manual areas for {Path(self.path).name}: {len(self.pieces)} piece(s).")

    def discard(self):
        if self.path in self.app.manual:
            del self.app.manual[self.path]
            self.app._save_settings()
            self.app.scan_source(keep_checks=True)
        self.pieces = self._auto_pieces()
        self.undo_stack, self.selected, self.dirty = [], set(), False
        self.redraw()

    def close(self):
        if self.dirty and messagebox.askyesno("Area Editor", "Save your edits for this file?", parent=self):
            self.save()
        if self.app.preview is self:
            self.app.preview = None
        self.destroy()

    def refresh_from_app(self):
        """Main-window options changed: follow them unless this file has manual edits."""
        if self.path not in self.app.manual and not self.dirty:
            self.pieces = self._auto_pieces()
            self.selected.clear()
            self.redraw()

    # ---------- drawing ----------
    def redraw(self, fit=False):
        self._sorted() if not self.selected else None
        manual = self.path in self.app.manual
        self.head.config(text=f"{Path(self.path).name}:  {len(self.pieces)} KMZ(s)")
        if self.dirty:
            self.badge.config(text="UNSAVED EDITS", foreground="#c62828")
        elif manual:
            self.badge.config(text="MANUAL (saved) - main-window split/cut options are ignored for this file",
                              foreground="#2e7d32")
        else:
            self.badge.config(text="AUTOMATIC (follows main-window options)", foreground="#555")
        if not self.status.get():
            self._mode_changed()
        base = safe_name(Path(self.path).stem, self.app.sep.get() or "-")
        sep = self.app.sep.get() or "-"
        names = [base] if len(self.pieces) == 1 else [f"{base}{sep}{i}" for i in range(1, len(self.pieces) + 1)]
        if self.map:
            m = self.map
            m.delete_all_polygon()
            m.delete_all_marker()
            m.delete_all_path()
            for ring in self.original:
                m.set_polygon([(lat, lon) for lon, lat in ring], outline_color="#ff3030",
                              fill_color=None, border_width=2)
            for i, g in enumerate(self.pieces):
                col = PALETTE[i % len(PALETTE)]
                ring = self.proj.to_ring(g)
                sel = i in self.selected
                m.set_polygon([(lat, lon) for lon, lat in ring], outline_color="#ffff33" if sel else col,
                              fill_color=col, border_width=5 if sel else 3)
                rp = g.representative_point()
                lon, lat = self.proj.inv(rp.x, rp.y)
                m.set_marker(lat, lon, text=f"{i + 1}\n{g.area / 4046.8564224:,.0f} ac", icon=self.blank,
                             text_color="#FFFF33", font=("Segoe UI", 11, "bold"))
            self._draw_cut()
            if getattr(self, "takeoff_win", None) and self.takeoff_win.winfo_exists():
                self.takeoff_win.draw_pins()
            self.meas_objs = []
            if getattr(self, "meas_pts", None):
                self._draw_measure()
            if fit:
                lons = [p[0] for r in self.original for p in r]
                lats = [p[1] for r in self.original for p in r]
                pad = 0.05 * max(max(lons) - min(lons), max(lats) - min(lats), 1e-4)
                m.fit_bounding_box((max(lats) + pad, min(lons) - pad), (min(lats) - pad, max(lons) + pad))
        ov = self.app._cut()[1]
        lines = [f"{len(self.pieces)} piece(s), {sum(g.area for g in self.pieces) / 4046.8564224:,.1f} ac total "
                 f"(original {sum(g.area for g in self.orig_geo) / 4046.8564224:,.1f} ac).  "
                 + (f"Seam overlap of {ov:g} m ({ov * 3.28084:,.0f} ft) is added when you Process."
                    if ov > 0 else "No seam overlap set (main window).")]
        for i, (g, n) in enumerate(zip(self.pieces, names)):
            mark = ""
            if i in self.selected:
                import math
                c = list(g.minimum_rotated_rectangle.exterior.coords)
                a_, b_ = sorted([math.dist(c[0], c[1]), math.dist(c[1], c[2])])
                mark = (f"  <- selected: about {b_:,.0f} x {a_:,.0f} m "
                        f"({b_ * 3.28084:,.0f} x {a_ * 3.28084:,.0f} ft)")
            lines.append(f"  {i + 1:>2}. {n}.kmz   {g.area / 4046.8564224:,.1f} ac{mark}")
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("end", "\n".join(lines))
        self.summary.configure(state="disabled")


OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"]


def _max_dist(pt, verts):
    import math
    return max(math.dist(pt, v) for v in verts)


def best_los_point(g):
    """Point INSIDE polygon g (metres) that minimises the farthest distance to the boundary
    (= best spot to keep the whole area in line of sight). Returns ((x, y), radius_m)."""
    from shapely.geometry import Point
    from shapely.prepared import prep
    verts = list(g.exterior.coords)
    inner = g.buffer(-15) if g.buffer(-15).area > 0 else g
    pg = prep(inner)
    minx, miny, maxx, maxy = g.bounds
    best, best_r = None, float("inf")
    step = max(maxx - minx, maxy - miny) / 40
    cx = cy = None
    for _ in range(4):
        xs = [minx + i * step for i in range(int((maxx - minx) / step) + 2)]
        ys = [miny + j * step for j in range(int((maxy - miny) / step) + 2)]
        for x in xs:
            for y in ys:
                if pg.contains(Point(x, y)):
                    r = _max_dist((x, y), verts)
                    if r < best_r:
                        best, best_r = (x, y), r
        if best is None:
            rp = g.representative_point()
            return (rp.x, rp.y), _max_dist((rp.x, rp.y), verts)
        cx, cy = best
        minx, maxx, miny, maxy = cx - 2 * step, cx + 2 * step, cy - 2 * step, cy + 2 * step
        step /= 5
    return best, best_r


def fetch_osm_access(ring, timeout=40):
    """Roads, tracks and parking inside the ring's bounding box from OpenStreetMap (Overpass)."""
    import json as _json
    import urllib.parse
    import urllib.request
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    bbox = f"{min(lats):.6f},{min(lons):.6f},{max(lats):.6f},{max(lons):.6f}"
    q = (f'[out:json][timeout:{timeout}];('
         f'way["highway"]({bbox});way["amenity"="parking"]({bbox});node["amenity"="parking"]({bbox});'
         f');out geom;')
    data = urllib.parse.urlencode({"data": q}).encode()
    last = None
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"User-Agent": "PVA-KML-to-KMZ/1.0 (takeoff spot finder)"})
            with urllib.request.urlopen(req, timeout=timeout + 10) as r:
                return _json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
    raise RuntimeError(f"OpenStreetMap lookup failed: {last}")


ACCESS_RANK = {"parking": 0, "track": 1, "service": 1, "construction": 1, "unclassified": 2,
               "residential": 2, "tertiary": 3, "secondary": 4, "primary": 5}


def takeoff_candidates(ring, osm=None, n=5, min_sep_m=250):
    """Rank takeoff spots INSIDE the ring. Returns list of dicts sorted best first.
    The first entry is always the geometric best line-of-sight point."""
    import math
    from shapely.geometry import LineString, Point
    proj = LocalProj([ring])
    g = proj.to_poly(ring)
    if g.geom_type != "Polygon":
        g = max(_polys(g), key=lambda x: x.area)
    verts = list(g.exterior.coords)
    inside = g.buffer(-10) if g.buffer(-10).area > 0 else g
    (bx, by), br = best_los_point(g)
    blon, blat = proj.inv(bx, by)
    spots = [{"kind": "Best line of sight", "name": "geometric best spot - check imagery for access",
              "lat": blat, "lon": blon, "los_m": br, "access": False}]
    cands = []
    for el in (osm or {}).get("elements", []):
        tags = el.get("tags", {})
        if tags.get("amenity") == "parking":
            kind, name = "Parking", tags.get("name", "parking lot")
        elif "highway" in tags:
            hw = tags["highway"]
            if hw in ("motorway", "motorway_link", "trunk", "trunk_link", "footway", "path", "cycleway",
                      "steps", "bridleway", "corridor", "proposed", "platform"):
                continue
            kind, name = f"Road ({hw})", tags.get("name") or tags.get("ref") or f"unnamed {hw}"
        else:
            continue
        rank = ACCESS_RANK.get("parking" if kind == "Parking" else tags.get("highway"), 3)
        if el.get("type") == "node":
            pts = [proj.fwd(el["lon"], el["lat"])]
        else:
            geom = [proj.fwd(p["lon"], p["lat"]) for p in el.get("geometry", []) if p]
            if len(geom) < 2:
                pts = geom
            elif kind == "Parking":
                pts = [LineString(geom).centroid.coords[0]]
            else:
                ln = LineString(geom)
                k = max(1, int(ln.length // 30))
                pts = [ln.interpolate(i / k, normalized=True).coords[0] for i in range(k + 1)]
        for x, y in pts:
            if inside.contains(Point(x, y)):
                cands.append((_max_dist((x, y), verts), rank, kind, name, x, y))
    cands.sort(key=lambda c: (round(c[0] / 50), c[1]))   # line of sight first, then easier access
    chosen = []
    for r, rank, kind, name, x, y in cands:
        if all(math.dist((x, y), (c[4], c[5])) >= min_sep_m for c in chosen):
            chosen.append((r, rank, kind, name, x, y))
        if len(chosen) >= n:
            break
    for r, rank, kind, name, x, y in chosen:
        lon, lat = proj.inv(x, y)
        spots.append({"kind": kind, "name": name, "lat": lat, "lon": lon, "los_m": r, "access": True,
                      "from_best_m": math.dist((x, y), (bx, by))})
    return spots


class TakeoffWindow(tk.Toplevel):
    """Lists takeoff spots for one area and pins them on the owner's map."""

    def __init__(self, owner, ring, title):
        super().__init__(owner)
        self.owner, self.ring = owner, ring
        self.title(f"Takeoff spots - {title}")
        self.geometry("860x330")
        self.spots = []
        self.blank = tk.PhotoImage(width=1, height=1)
        self.msg = tk.StringVar(value="Finding the best line-of-sight spot and nearby roads/parking "
                                      "(OpenStreetMap)...")
        ttk.Label(self, textvariable=self.msg, foreground="#555", wraplength=820).pack(anchor="w", padx=8, pady=6)
        cols = ("id", "kind", "name", "los", "coords")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=7, selectmode="browse")
        for c, t, w, a in [("id", "#", 34, "center"), ("kind", "Type", 140, "w"), ("name", "Where", 250, "w"),
                           ("los", "Farthest edge", 150, "e"), ("coords", "Coordinates", 190, "w")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c == "name"))
        self.tree.pack(fill="both", expand=True, padx=8)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.draw_pins())
        self.tree.bind("<Double-1>", lambda e: self.zoom())
        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=8, pady=6)
        for txt, fn in (("Copy coordinates", self.copy), ("Open in Google Maps", self.open_maps),
                        ("Directions", self.directions), ("Zoom to", self.zoom), ("Copy all", self.copy_all)):
            ttk.Button(bf, text=txt, command=fn).pack(side="left", padx=3)
        self.protocol("WM_DELETE_WINDOW", self.close)
        import queue as _q
        import threading
        self._result = _q.Queue()
        threading.Thread(target=self._work, daemon=True).start()
        self.after(200, self._poll)

    def _work(self):
        # runs in a background thread: NO Tk calls in here
        osm, err = None, None
        try:
            osm = fetch_osm_access(self.ring)
        except Exception as e:
            err = e
        try:
            spots = takeoff_candidates(self.ring, osm)
        except Exception as e:
            spots, err = [], e
        self._result.put((spots, err))

    def _poll(self):
        import queue as _q
        try:
            spots, err = self._result.get_nowait()
        except _q.Empty:
            if self.winfo_exists():
                self.after(200, self._poll)
            return
        self._done(spots, err)

    def _done(self, spots, err):
        if not self.winfo_exists():
            return
        self.spots = spots
        for i, s in enumerate(spots):
            los = f"{s['los_m']:,.0f} m / {s['los_m'] * 3.28084:,.0f} ft"
            self.tree.insert("", "end", iid=str(i), values=(
                "★" if i == 0 else f"T{i}", s["kind"], s["name"], los, f"{s['lat']:.6f}, {s['lon']:.6f}"))
        n_access = sum(1 for s in spots if s["access"])
        if err and not spots:
            self.msg.set(f"Couldn't compute spots: {err}")
        elif err:
            self.msg.set("Road/parking lookup failed (no internet?) - showing the best line-of-sight spot only. "
                         "'Farthest edge' = distance to the farthest corner of the area.")
        elif n_access == 0:
            self.msg.set("No mapped roads or parking inside this area - use ★ and check the satellite "
                         "imagery for haul roads / open dirt. 'Farthest edge' = distance to the farthest corner.")
        else:
            self.msg.set(f"★ = best line of sight (may not be drivable). T1-T{n_access} = mapped roads/parking "
                         "INSIDE the area, best line of sight first. Always confirm on the imagery.")
        if spots:
            self.tree.selection_set("0")
        self.draw_pins()

    def _sel(self):
        s = self.tree.selection()
        return self.spots[int(s[0])] if s and self.spots else None

    def draw_pins(self):
        m = getattr(self.owner, "map", None)
        if not m:
            return
        for p in getattr(self, "pins", []):
            p.delete()
        self.pins = []
        sel = self._sel()
        for i, s in enumerate(self.spots):
            hl = s is sel
            self.pins.append(m.set_marker(
                s["lat"], s["lon"], text="★ best LOS" if i == 0 else f"T{i}",
                text_color="#ffff33" if hl else "#ffffff",
                marker_color_circle="#ffff33" if hl else ("#ff9800" if i == 0 else "#00c853"),
                marker_color_outside="#c0a000" if hl else ("#b26a00" if i == 0 else "#007a33"),
                font=("Segoe UI", 11, "bold")))

    def copy(self):
        s = self._sel()
        if s:
            self.clipboard_clear()
            self.clipboard_append(f"{s['lat']:.6f}, {s['lon']:.6f}")
            self.msg.set(f"Copied {s['lat']:.6f}, {s['lon']:.6f} - paste it into Google Maps.")

    def copy_all(self):
        lines = [f"{'BEST LOS' if i == 0 else 'T' + str(i)}: {s['lat']:.6f}, {s['lon']:.6f}  "
                 f"({s['kind']} - {s['name']}, farthest edge {s['los_m']:,.0f} m)"
                 for i, s in enumerate(self.spots)]
        self.clipboard_clear()
        self.clipboard_append("\n".join(lines))
        self.msg.set(f"Copied {len(lines)} spots.")

    def open_maps(self):
        import webbrowser
        s = self._sel()
        if s:
            webbrowser.open(f"https://www.google.com/maps/search/?api=1&query={s['lat']:.6f},{s['lon']:.6f}")

    def directions(self):
        import webbrowser
        s = self._sel()
        if s:
            webbrowser.open(f"https://www.google.com/maps/dir/?api=1&destination={s['lat']:.6f},{s['lon']:.6f}")

    def zoom(self):
        s = self._sel()
        m = getattr(self.owner, "map", None)
        if s and m:
            m.set_position(s["lat"], s["lon"])
            m.set_zoom(17)

    def close(self):
        for p in getattr(self, "pins", []):
            p.delete()
        self.destroy()


def read_flight_file(path):
    """Read any KML/KMZ (plain or DJI mission). Returns dict with boundary rings,
    flight-line points (DJI waylines), and a type label."""
    path = Path(path)
    info = {"rings": [], "lines": [], "kind": "KML", "settings": ""}
    if path.suffix.lower() == ".kmz":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "wpmz/template.kml" in names:
                info["kind"] = "DJI KMZ"
                tk_ = z.read("wpmz/template.kml")
                info["rings"] = [o for _, o, _ in extract_polygons(tk_)]
                t = tk_.decode("utf-8", "replace")
                g = lambda tag: (re.search(rf"<wpml:{tag}>([^<]*)<", t) or [None, ""])[1]
                if g("templateType"):
                    info["settings"] = (f"{g('templateType')}, {g('globalShootHeight') or g('height')} m, "
                                        f"drone {g('droneEnumValue')}/payload {g('payloadEnumValue')}")
                if "wpmz/waylines.wpml" in names:
                    w = z.read("wpmz/waylines.wpml").decode("utf-8", "replace")
                    pts = re.findall(r"<Point>\s*<coordinates>\s*([-\d.]+),([-\d.]+)", w)
                    info["lines"] = [(float(a), float(b)) for a, b in pts]
                return info
            info["kind"] = "KMZ"
    info["rings"] = [o for _, o, _ in extract_polygons(read_kml_text(path))]
    return info


class Viewer(tk.Toplevel):
    """Open any number of KML/KMZ files and see them on the satellite map."""

    def __init__(self, app, files=()):
        super().__init__(app)
        self.app = app
        self.title("KML / KMZ Viewer")
        self.geometry("1400x850")
        self.items = []          # dicts: path, info, color, visible, polys
        self.blank = tk.PhotoImage(width=1, height=1)
        self.show_lines = tk.BooleanVar(value=True)
        self.show_labels = tk.BooleanVar(value=True)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Open files...", command=self.open_files).pack(side="left", padx=3)
        ttk.Button(bar, text="Open folder...", command=self.open_folder).pack(side="left", padx=3)
        ttk.Button(bar, text="Show Target Folder", command=lambda: self.add_folder(app.dst_var.get())).pack(side="left", padx=3)
        ttk.Button(bar, text="Show Source Folder", command=lambda: self.add_folder(app.src_var.get())).pack(side="left", padx=3)
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="left", padx=3)
        ttk.Checkbutton(bar, text="Flight lines", variable=self.show_lines, command=self.redraw).pack(side="left", padx=10)
        ttk.Checkbutton(bar, text="Labels", variable=self.show_labels, command=self.redraw).pack(side="left")
        ttk.Button(bar, text="Zoom to all", command=self.zoom_all).pack(side="right", padx=3)
        ttk.Button(bar, text="Find takeoff spots", command=self.find_takeoff).pack(side="right", padx=3)

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(body)
        body.add(left, weight=1)
        cols = ("vis", "name", "kind", "ac", "lines")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        for c, t, w, a in [("vis", "", 30, "center"), ("name", "File", 300, "w"), ("kind", "Type", 70, "center"),
                           ("ac", "Acres", 70, "e"), ("lines", "Flight pts", 70, "e")]:
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c == "name"))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._tree_click)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.redraw())
        self.tree.bind("<Double-1>", lambda e: self.zoom_selected())
        self.detail = ttk.Label(left, foreground="#555", wraplength=480, justify="left")
        self.detail.pack(fill="x", pady=4)
        ttk.Label(left, text="Click the box to show/hide. Click a row (or a shape on the map) to highlight it.\n"
                             "Double-click a row to zoom to it.", foreground="#555").pack(anchor="w")
        right = ttk.Frame(body)
        body.add(right, weight=3)
        try:
            import tkintermapview
            self.map = tkintermapview.TkinterMapView(right, corner_radius=0)
            self.map.set_tile_server(SAT_TILES, max_zoom=19)
            self.map.pack(fill="both", expand=True)
        except Exception:
            self.map = None
            ttk.Label(right, text="Viewer needs 'tkintermapview':  python -m pip install tkintermapview").pack(pady=40)
        self.drop_hint = ttk.Label(left, foreground="#2e7d32", font=("Segoe UI", 10, "bold"))
        self.drop_hint.pack(anchor="w", pady=(6, 0))
        self._enable_drop()
        for f in files:
            self.add_path(f, refresh=False)
        self.refresh_list()
        self.zoom_all()

    def _enable_drop(self):
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
            TkinterDnD._require(self.app)
            targets = [self, self.tree] + ([self.map, self.map.canvas] if self.map else [])
            for w in targets:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)
            self.drop_hint.config(text="Drag & drop KML / KMZ files or folders anywhere in this window.")
        except Exception:
            self.drop_hint.config(text="(Drag & drop needs:  python -m pip install tkinterdnd2)",
                                  foreground="#b36b00")

    def _on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        n0 = len(self.items)
        for p_ in paths:
            self.add_path(p_, refresh=False)
        self.refresh_list()
        if len(self.items) > n0:
            self._fit([r for it in self.items[n0:] for r in it["info"]["rings"] + [it["info"]["lines"]] if r])
        return getattr(event, "action", "copy")

    def add_path(self, p_, refresh=True):
        """File or folder (folders: every KML/KMZ inside, one level of subfolders)."""
        p_ = Path(p_)
        if p_.is_dir():
            for f in list_inputs(p_) + [g for sub in sorted(x for x in p_.iterdir() if x.is_dir())
                                        for g in list_inputs(sub)]:
                self.add_file(f, refresh=False)
        elif p_.suffix.lower() in IN_EXTS:
            self.add_file(p_, refresh=False)
        if refresh:
            self.refresh_list()

    # ---------- loading ----------
    def open_files(self):
        fs = filedialog.askopenfilenames(parent=self, title="Open KML / KMZ files",
                                         filetypes=[("KML / KMZ", "*.kml *.kmz"), ("All files", "*.*")])
        for f in fs:
            self.add_file(f, refresh=False)
        self.refresh_list()
        self.zoom_all()

    def open_folder(self):
        d = filedialog.askdirectory(parent=self, title="Open a folder of KML / KMZ files")
        if d:
            self.add_folder(d)

    def add_folder(self, d):
        if not d or not Path(d).is_dir():
            messagebox.showinfo("Viewer", "That folder isn't set or doesn't exist.", parent=self)
            return
        for f in list_inputs(d):
            self.add_file(f, refresh=False)
        self.refresh_list()
        self.zoom_all()

    def add_file(self, f, refresh=True):
        f = str(Path(f))
        if any(it["path"] == f for it in self.items):
            return
        try:
            info = read_flight_file(f)
        except Exception as e:
            info = {"rings": [], "lines": [], "kind": "ERROR", "settings": str(e)}
        self.items.append({"path": f, "info": info, "visible": True,
                           "color": PALETTE[len(self.items) % len(PALETTE)]})
        if refresh:
            self.refresh_list()

    def clear(self):
        self.items = []
        self.refresh_list()

    # ---------- list ----------
    def _short_names(self):
        """Map labels: files in a numbered set (name-1, name-2 / 'Flight 4') show just their number;
        everything else shows its full name."""
        stems = [Path(it["path"]).stem for it in self.items]
        parts = [re.match(r"^(.*?)[-_ ]+(\d+)$", s) for s in stems]
        bases = [p_[1].lower() if p_ else None for p_ in parts]
        out = []
        for s_, p_, b_ in zip(stems, parts, bases):
            if p_ and bases.count(b_) > 1:
                out.append(p_[2])
            else:
                out.append(s_)
        return out

    def refresh_list(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, it in enumerate(self.items):
            inf = it["info"]
            ac = sum(ring_area_acres(r) for r in inf["rings"])
            tag = f"c{i}"
            self.tree.tag_configure(tag, foreground=it["color"])
            self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                "☑" if it["visible"] else "☐", Path(it["path"]).name, inf["kind"],
                f"{ac:,.1f}" if inf["rings"] else "-", len(inf["lines"]) or "-"))
        for s in sel:
            if self.tree.exists(s):
                self.tree.selection_set(s)
        self.redraw()

    def _tree_click(self, e):
        row = self.tree.identify_row(e.y)
        if row and self.tree.identify_column(e.x) == "#1":
            it = self.items[int(row)]
            it["visible"] = not it["visible"]
            vals = list(self.tree.item(row, "values"))
            vals[0] = "☑" if it["visible"] else "☐"
            self.tree.item(row, values=vals)
            self.redraw()
            return "break"

    def _selected(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def _select_from_map(self, i):
        self.tree.selection_set(str(i))
        self.tree.see(str(i))

    # ---------- map ----------
    def redraw(self):
        sel = self._selected()
        if sel is not None and sel < len(self.items):
            it = self.items[sel]
            inf = it["info"]
            ac = sum(ring_area_acres(r) for r in inf["rings"])
            self.detail.config(text=f"{Path(it['path']).name}\n{it['path']}\n{inf['kind']}: "
                                    f"{len(inf['rings'])} boundary, {ac:,.1f} ac"
                                    + (f"\n{inf['settings']}" if inf["settings"] else "")
                                    + ("\nFlight lines: none (FlightHub generates them)" if inf["kind"] == "DJI KMZ"
                                       and not inf["lines"] else ""))
        else:
            self.detail.config(text="")
        if not self.map:
            return
        m = self.map
        m.delete_all_polygon()
        m.delete_all_marker()
        m.delete_all_path()
        short = self._short_names()
        order = [i for i in range(len(self.items)) if i != sel] + ([sel] if sel is not None else [])
        for i in order:
            it = self.items[i]
            if not it["visible"]:
                continue
            hl = i == sel
            for ring in it["info"]["rings"]:
                m.set_polygon([(lat, lon) for lon, lat in ring], fill_color=it["color"],
                              outline_color="#ffff33" if hl else it["color"], border_width=5 if hl else 3,
                              command=lambda poly, i=i: self._select_from_map(i))
                if self.show_labels.get():
                    try:
                        from shapely.geometry import Polygon
                        rp = Polygon(ring).representative_point()
                        cx, cy = rp.x, rp.y
                    except Exception:
                        cx = sum(p[0] for p in ring) / len(ring)
                        cy = sum(p[1] for p in ring) / len(ring)
                    m.set_marker(cy, cx, text=short[i], icon=self.blank,
                                 text_color="#ffff33" if hl else "#ffffff", font=("Segoe UI", 12, "bold"))
            if self.show_lines.get() and len(it["info"]["lines"]) >= 2:
                m.set_path([(lat, lon) for lon, lat in it["info"]["lines"]],
                           color="#ffff33" if hl else it["color"], width=2)
        if getattr(self, "takeoff_win", None) and self.takeoff_win.winfo_exists():
            self.takeoff_win.draw_pins()

    def find_takeoff(self):
        s = self._selected()
        if s is None or not self.items[s]["info"]["rings"]:
            messagebox.showinfo("Takeoff spots", "Click a file in the list (or its shape on the map) first.",
                                parent=self)
            return
        it = self.items[s]
        ring = max(it["info"]["rings"], key=ring_area_acres)
        if getattr(self, "takeoff_win", None) and self.takeoff_win.winfo_exists():
            self.takeoff_win.close()
        self.takeoff_win = TakeoffWindow(self, ring, Path(it["path"]).name)

    def _fit(self, rings):
        pts = [p for r in rings for p in r]
        if not pts or not self.map:
            return
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        pad = 0.08 * max(max(lons) - min(lons), max(lats) - min(lats), 2e-4)
        self.map.fit_bounding_box((max(lats) + pad, min(lons) - pad), (min(lats) - pad, max(lons) + pad))

    def zoom_all(self):
        self._fit([r for it in self.items if it["visible"] for r in it["info"]["rings"] + [it["info"]["lines"]] if r])

    def zoom_selected(self):
        s = self._selected()
        if s is not None:
            it = self.items[s]
            self._fit([r for r in it["info"]["rings"] + [it["info"]["lines"]] if r])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {VERSION}")
        self.geometry("1100x940")
        self.tmpl = None
        self.minsize(900, 650)
        self.checked = set()
        self.rows = {}
        self.keep_whole = {}
        self.preview = None
        self.manual = MANUAL
        MANUAL.clear()
        MANUAL.update(self._load_settings().get("manual", {}))

        s = self._load_settings()
        self.src_var = tk.StringVar(value=s.get("source", ""))
        self.dst_var = tk.StringVar(value=s.get("target", ""))
        self.keep_dates = tk.BooleanVar(value=s.get("keep_dates", True))
        self.overwrite = tk.BooleanVar(value=s.get("overwrite", False))
        self.tmpl_var = tk.StringVar(value=s.get("template", ""))
        self.include_sub = tk.BooleanVar(value=s.get("include_sub", True))
        self.sep = tk.StringVar(value=s.get("sep", "-"))
        self.split_on = tk.BooleanVar(value=s.get("split_on", False))
        self.strip_m = tk.DoubleVar(value=s.get("strip_m", 100.0))
        self.cut_on = tk.BooleanVar(value=s.get("cut_on", False))
        self.max_ac = tk.DoubleVar(value=s.get("max_ac", 300.0))
        self.overlap_m = tk.DoubleVar(value=s.get("overlap_m", 100.0))
        self.progress_txt = tk.StringVar(value="Progress: 0% (Idle)")

        self._build()
        self.log(f"{APP_NAME} {VERSION} started.")
        self.load_template()
        if self.src_var.get():
            self.scan_source()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Bold.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", rowheight=24)
        pad = dict(padx=10, pady=4)

        loc = ttk.LabelFrame(self, text="Locations")
        loc.pack(fill="x", **pad)
        loc.columnconfigure(1, weight=1)
        ttk.Label(loc, text="Source Folder:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(loc, textvariable=self.src_var).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Button(loc, text="Browse...", command=self.browse_src).grid(row=0, column=2, padx=8)
        ttk.Label(loc, text="Target Folder:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(loc, textvariable=self.dst_var).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(loc, text="Browse...", command=self.browse_dst).grid(row=1, column=2, padx=8)
        ttk.Label(loc, text="Mission Template:").grid(row=2, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(loc, textvariable=self.tmpl_var).grid(row=2, column=1, sticky="ew", pady=6)
        ttk.Button(loc, text="Browse...", command=self.browse_tmpl).grid(row=2, column=2, padx=8)
        self.tmpl_lbl = ttk.Label(loc, foreground="#555")
        self.tmpl_lbl.grid(row=3, column=1, columnspan=2, sticky="w", pady=(0, 6))

        opt = ttk.LabelFrame(self, text="Processing Options")
        opt.pack(fill="x", **pad)
        ttk.Checkbutton(opt, text="Keep original file date (Uncheck for today's date)",
                        variable=self.keep_dates).grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=3)
        ttk.Checkbutton(opt, text="Overwrite existing KMZs",
                        variable=self.overwrite).grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=3)
        ttk.Checkbutton(opt, text="Include KMLs in subfolders",
                        variable=self.include_sub, command=lambda: self.scan_source(keep_checks=True)).grid(
            row=2, column=0, columnspan=4, sticky="w", padx=8, pady=3)
        ttk.Label(opt, text="Replace _ / spaces / symbols in names with:").grid(
            row=4, column=0, sticky="w", padx=8, pady=6)
        sep_box = ttk.Combobox(opt, textvariable=self.sep, values=["-", " "], width=4, state="readonly")
        sep_box.grid(row=4, column=1, sticky="w")
        sep_box.bind("<<ComboboxSelected>>", lambda e: self.scan_source(keep_checks=True))
        ttk.Label(opt, text='("-" = dash, " " = space)', foreground="#555").grid(row=4, column=2, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="Split areas: remove connecting strips narrower than",
                        variable=self.split_on, command=lambda: self.scan_source(keep_checks=True)).grid(
            row=5, column=0, sticky="w", padx=8, pady=6)
        sp = ttk.Spinbox(opt, from_=10, to=1000, increment=10, width=6, textvariable=self.strip_m,
                         command=lambda: self.scan_source(keep_checks=True))
        sp.grid(row=5, column=1, sticky="w")
        sp.bind("<Return>", lambda e: self.scan_source(keep_checks=True))
        ttk.Label(opt, text="m  (each remaining area becomes its own KMZ: -1, -2... west to east)",
                  foreground="#555").grid(row=5, column=2, sticky="w", padx=6)
        ttk.Checkbutton(opt, text="Cut big areas: max size per area",
                        variable=self.cut_on, command=lambda: self.scan_source(keep_checks=True)).grid(
            row=6, column=0, sticky="w", padx=8, pady=6)
        sp2 = ttk.Spinbox(opt, from_=10, to=10000, increment=25, width=6, textvariable=self.max_ac,
                          command=lambda: self.scan_source(keep_checks=True))
        sp2.grid(row=6, column=1, sticky="w")
        sp2.bind("<Return>", lambda e: self.scan_source(keep_checks=True))
        ttk.Label(opt, text="acres  (halves areas with straight cuts until each fits)",
                  foreground="#555").grid(row=6, column=2, sticky="w", padx=6)
        ttk.Label(opt, text="Overlap shared along seams between touching pieces:").grid(
            row=7, column=0, sticky="w", padx=8, pady=6)
        sp3 = ttk.Spinbox(opt, from_=0, to=1000, increment=10, width=6, textvariable=self.overlap_m,
                          command=lambda: self.scan_source(keep_checks=True))
        sp3.grid(row=7, column=1, sticky="w")
        sp3.bind("<Return>", lambda e: self.scan_source(keep_checks=True))
        self.ov_lbl = ttk.Label(opt, foreground="#555")
        self.ov_lbl.grid(row=7, column=2, sticky="w", padx=6)
        def _ov_text(*_):
            try:
                m_ = float(self.overlap_m.get())
                self.ov_lbl.config(text=f"m total  (= {m_ * 3.28084:,.0f} ft; applies to automatic AND manual pieces; "
                                        "~100-150 m recommended at 120 m AGL)")
            except Exception:
                pass
        self.overlap_m.trace_add("write", _ov_text)
        _ov_text()

        info = ttk.LabelFrame(self, text="Output")
        info.pack(fill="x", **pad)
        self.info_lbl = ttk.Label(info, foreground="#555")
        self.info_lbl.pack(anchor="w", padx=8, pady=6)
        self.dst_var.trace_add("write", lambda *_: self._update_info())
        self._update_info()

        hdr = ttk.Frame(self)
        hdr.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(hdr, text="KML/KMZ Files at Source (Select to Process):",
                  style="Bold.TLabel").pack(side="left")
        ttk.Button(hdr, text="Refresh", command=self.scan_source).pack(side="right")
        ttk.Button(hdr, text="Select None", command=lambda: self._select_all(False)).pack(side="right", padx=4)
        ttk.Button(hdr, text="Select All", command=lambda: self._select_all(True)).pack(side="right")

        tf = ttk.Frame(self)
        tf.pack(fill="both", expand=True, padx=10, pady=4)
        cols = ("sel", "name", "date", "polys", "verts", "out")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=8)
        for c, text, w, anchor, stretch in [
                ("sel", "", 36, "center", False), ("name", "File Name", 340, "w", True),
                ("date", "Modified Date", 110, "center", False), ("polys", "Polygons", 75, "e", False),
                ("verts", "Vertices", 75, "e", False), ("out", "Output KMZ Name", 340, "w", True)]:
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda e: self.open_preview(self.tree.identify_row(e.y)))

        self.pbar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=12, pady=(8, 2))
        ttk.Label(self, textvariable=self.progress_txt).pack(anchor="w", padx=12)

        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=6)
        bf.columnconfigure((0, 1, 2, 3), weight=1, uniform="b")
        self.process_btn = ttk.Button(bf, text="Process (0) Files", command=self.start)
        self.process_btn.grid(row=0, column=0, sticky="ew", padx=4, ipady=6)
        ttk.Button(bf, text="Preview / Edit Areas on Map", command=self.open_preview).grid(
            row=0, column=1, sticky="ew", padx=4, ipady=6)
        ttk.Button(bf, text="KML / KMZ Viewer", command=self.open_viewer).grid(
            row=0, column=2, sticky="ew", padx=4, ipady=6)
        ttk.Button(bf, text="Open Target Folder", command=self.open_target).grid(
            row=0, column=3, sticky="ew", padx=4, ipady=6)

        ttk.Label(self, text="Processing Log:", style="Bold.TLabel").pack(anchor="w", padx=10)
        lf = ttk.Frame(self)
        lf.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        self.logbox = tk.Text(lf, height=10, bg="#0c0c0c", fg="#7CFC7C",
                              font=("Consolas", 10), wrap="none", state="disabled", relief="flat")
        self.logbox.tag_configure("warn", foreground="#FFD75E")
        self.logbox.tag_configure("fail", foreground="#FF6B6B")
        lsb = ttk.Scrollbar(lf, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=lsb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    def _update_info(self):
        dst = self.dst_var.get() or "<Target Folder>"
        self.info_lbl.config(text=f"KMZs will be saved to:  {dst}   "
                                  f"(DJI area route per boundary: template settings + new boundary, no underscores)")

    def log(self, msg, tag=None):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{datetime.now():%H:%M:%S} - {msg}\n", tag or ())
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def browse_src(self):
        d = filedialog.askdirectory(initialdir=self.src_var.get() or str(Path.home()))
        if d:
            self.src_var.set(d)
            self.scan_source()

    def _strip(self):
        if not self.split_on.get():
            return 0
        try:
            import shapely  # noqa: F401
        except ImportError:
            self.split_on.set(False)
            messagebox.showerror(APP_NAME, "Splitting needs the 'shapely' package.\n\n"
                                           "Close this, then run:  python -m pip install shapely")
            return 0
        try:
            return max(1.0, float(self.strip_m.get()))
        except Exception:
            return 100.0

    def open_viewer(self, files=()):
        if getattr(self, "viewer", None) and self.viewer.winfo_exists():
            for f in files:
                self.viewer.add_path(f, refresh=False)
            self.viewer.refresh_list()
            self.viewer.zoom_all()
            self.viewer.lift()
        else:
            self.viewer = Viewer(self, files)

    def open_preview(self, path=None):
        if not path:
            sel = self.tree.selection() or [i for i in self.tree.get_children() if i in self.checked] \
                  or self.tree.get_children()
            if not sel:
                messagebox.showinfo(APP_NAME, "No KML files listed - set a Source Folder first.")
                return
            path = sel[0]
        if self.preview and self.preview.winfo_exists():
            self.preview.set_path(path)
            self.preview.lift()
        else:
            self.preview = AreaEditor(self, path)

    def _cut(self):
        """(max_acres, overlap_m). max_acres is 0 when cutting is off; overlap always applies."""
        try:
            ov = max(0.0, float(self.overlap_m.get()))
        except Exception:
            ov = 0.0
        if not self.cut_on.get():
            return 0, ov
        try:
            import shapely  # noqa: F401
        except ImportError:
            self.cut_on.set(False)
            messagebox.showerror(APP_NAME, "Cutting needs the 'shapely' package.\n\n"
                                           "Close this, then run:  python -m pip install shapely")
            return 0, 0
        try:
            return max(1.0, float(self.max_ac.get())), ov
        except Exception:
            return 300.0, ov

    def browse_tmpl(self):
        f = filedialog.askopenfilename(title="Pick a DJI Area-route KMZ to use as the template",
                                       filetypes=[("DJI mission KMZ", "*.kmz")],
                                       initialdir=str(Path(self.tmpl_var.get()).parent) if self.tmpl_var.get() else str(Path.home()))
        if f:
            self.tmpl_var.set(f)
            self.load_template()

    def load_template(self):
        self.tmpl = None
        p = self.tmpl_var.get().strip()
        if not p:
            self.tmpl_lbl.config(text="Pick an Area route KMZ exported from FlightHub 2 / Pilot 2 (your approved settings).",
                                 foreground="#b36b00")
            return
        try:
            self.tmpl = DjiTemplate(p)
            d = self.tmpl.describe()
            self.tmpl_lbl.config(text=f"Template OK: {d}", foreground="#2e7d32")
            self.log(f"Template loaded: {Path(p).name} ({d})")
        except Exception as e:
            self.tmpl_lbl.config(text=f"Template problem: {e}", foreground="#c62828")
            self.log(f"Template problem: {e}", "fail")

    def browse_dst(self):
        d = filedialog.askdirectory(initialdir=self.dst_var.get() or str(Path.home()))
        if d:
            self.dst_var.set(d)

    def scan_source(self, keep_checks=False):
        prev_checked = set(self.checked)        # option changes keep your ticks
        prev_sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self.rows.clear()
        src = Path(self.src_var.get())
        if not src.is_dir():
            self._refresh_count()
            return
        files = list_inputs(src)
        if self.include_sub.get():
            for sub in sorted(p for p in src.iterdir() if p.is_dir()):
                files += list_inputs(sub)
        sep = self.sep.get() or "-"
        for f in files:
            label = f.name if f.parent == src else f"{f.parent.name}\\{f.name}"
            date = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
            try:
                polys, names = plan_outputs(f, sep, self._strip(), *self._cut(),
                                            self.keep_whole.get(str(f), set()))
                n_poly = len(polys)
                verts = sum(len(p[1]) - 1 for p in polys)
                out = ", ".join(n + ".kmz" for n in names) if polys else "(no polygon found)"
                if str(f) in self.manual:
                    out = "[MANUAL] " + out
            except Exception as e:
                n_poly, verts, out = 0, 0, f"(unreadable: {e})"
            self.tree.insert("", "end", iid=str(f), values=("☐", label, date, n_poly, verts, out))
            if str(f) in prev_checked:
                self._toggle(str(f), True)
        sel = [i for i in prev_sel if self.tree.exists(i)]
        if sel:
            self.tree.selection_set(sel)
        if not keep_checks:
            self.log(f"Scanned source: {len(files)} KML/KMZ file(s).")
        self._refresh_count()
        if self.preview and self.preview.winfo_exists():
            if self.tree.exists(self.preview.path):
                self.preview.refresh_from_app()
            else:
                self.preview.close()

    def _on_tree_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_region(event.x, event.y) == "cell":
            self._toggle(row)

    def _toggle(self, iid, state=None):
        on = (iid not in self.checked) if state is None else state
        (self.checked.add if on else self.checked.discard)(iid)
        vals = list(self.tree.item(iid, "values"))
        vals[0] = "☑" if on else "☐"
        self.tree.item(iid, values=vals)
        self._refresh_count()

    def _select_all(self, on):
        for iid in self.tree.get_children():
            self._toggle(iid, on)

    def _refresh_count(self):
        self.process_btn.config(text=f"Process ({len(self.checked)}) Files",
                                state="normal" if self.checked else "disabled")

    def open_target(self):
        d = self.dst_var.get()
        if d and Path(d).is_dir():
            if os.name == "nt":
                os.startfile(d)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])

    def start(self):
        dst = self.dst_var.get().strip()
        if not dst:
            messagebox.showerror(APP_NAME, "Set a Target Folder first.")
            return
        if not self.tmpl:
            self.load_template()
            if not self.tmpl:
                messagebox.showerror(APP_NAME, "Pick a valid Mission Template KMZ first.")
                return
        files = [iid for iid in self.tree.get_children() if iid in self.checked]
        if not files:
            return
        self._save_settings()
        self.process_btn.config(state="disabled", text="Processing...")
        self.pbar["value"] = 0
        counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
        self.log(f"--- Starting: {len(files)} file(s) ---")
        for i, f in enumerate(files, 1):
            try:
                for status, msg in convert_file(f, dst, self.sep.get() or "-", self.tmpl,
                                                self.keep_dates.get(), self.overwrite.get(),
                                                self._strip(), *self._cut(),
                                                self.keep_whole.get(f, set())):
                    counts[status] += 1
                    self.log(f"  [{Path(f).name}] {msg}", {"warn": "warn", "fail": "fail"}.get(status))
            except Exception as e:
                counts["fail"] += 1
                self.log(f"  [{Path(f).name}] FAILED - {e}", "fail")
            pct = i / len(files) * 100
            self.pbar["value"] = pct
            self.progress_txt.set(f"Progress: {pct:.0f}% ({i}/{len(files)})")
            self.update_idletasks()
        self.progress_txt.set("Progress: 100% (Complete)")
        self.log(f"--- Workflow Complete --- {counts['ok']} ok, {counts['warn']} warnings, "
                 f"{counts['fail']} failed, {counts['skip']} skipped")
        if counts["skip"] and counts["ok"] + counts["warn"]:
            self.log("WARNING: some KMZs were SKIPPED because files with those names already existed. "
                     "The target folder now MIXES old and new pieces - numbers may not match the editor. "
                     "Tick 'Overwrite existing KMZs' and Process again, or use a new Target Folder.", "fail")
            messagebox.showwarning(APP_NAME, f"{counts['skip']} KMZ(s) were skipped because they already existed.\n\n"
                                   "The target folder now mixes OLD and NEW pieces, so the numbers may not "
                                   "match what you see in the editor.\n\nTick 'Overwrite existing KMZs' and "
                                   "Process again (or pick an empty Target Folder).")
        self._refresh_count()

    def _load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return {}

    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(json.dumps({
                "source": self.src_var.get(), "target": self.dst_var.get(),
                "keep_dates": self.keep_dates.get(), "overwrite": self.overwrite.get(),
                "template": self.tmpl_var.get(), "include_sub": self.include_sub.get(),
                "sep": self.sep.get(),
                "split_on": self.split_on.get(), "strip_m": self._strip() or float(self.strip_m.get() or 100),
                "manual": {k: v for k, v in self.manual.items() if Path(k).exists()},
                "cut_on": self.cut_on.get(), "max_ac": float(self.max_ac.get() or 300),
                "overlap_m": float(self.overlap_m.get() or 0)}, indent=2))
        except Exception:
            pass

    def on_close(self):
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    app = App()
    if args or "--viewer" in sys.argv:
        app.after(300, lambda: app.open_viewer(args))
    app.mainloop()
