# KML to KMZ Split Tool — PVA KML to KMZ Converter

Desktop tool (Windows, Python 3.9+) that turns site-boundary KMLs into DJI **Area route** KMZs
(`wpmz/template.kml` + `wpmz/waylines.wpml`) that import into **FlightHub 2 / DJI Pilot 2**.

Flight settings come from a mission KMZ you exported from FlightHub/Pilot 2 (the "Mission Template"):
drone/payload, altitude, overlaps, speed, terrain follow, calibration flights are all copied;
only the boundary is swapped in.

## Features
- FlightHub-safe output names (no underscores / slashes / symbols)
- **Split areas:** remove narrow connecting strips (e.g. a road corridor joining two sites)
- **Cut big areas:** halve automatically until every piece is under a max acreage
- **Area Editor (map):** satellite preview; cut with lines, delete, divide into N equal pieces,
  merge, undo; edits saved per file and used when processing ("[MANUAL]")
- **Add back:** click an empty spot inside the original boundary (e.g. a deleted corridor) to restore it as a piece
- **Draw area:** click corners to create any new flight area
- **Measure tool:** click points for distance (m / ft / mi) and enclosed area; selected pieces show length x width
- **Seam overlap** between touching pieces so separate flights stitch (~100–150 m at 120 m AGL)
- Batch processing with progress and log

## Run
Double-click `PVA_KML_to_KMZ.bat` (first run installs `shapely` and `tkintermapview`).

## Notes
- Flight lines in the output are left empty; FlightHub regenerates them for the new boundary.
  **Always open the route and check the lines before flying.**
- Terrain follow is kept only if the new area lies inside the template's DSM tile; otherwise it is
  switched off and flagged in the log.
- Settings and manual edits are saved in `%USERPROFILE%\.pva_kml_to_kmz.json`.
- Site boundaries and mission templates are **not** stored in this repo (see `.gitignore`).
