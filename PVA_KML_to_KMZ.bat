@echo off
REM Double-click to launch the PVA KML to KMZ Converter.
REM First run installs shapely (split/cut), tkintermapview (map) and tkinterdnd2 (drag & drop).
python -c "import shapely, tkintermapview, tkinterdnd2" 2>nul || python -m pip install shapely tkintermapview tkinterdnd2
start "" pythonw "%~dp0PVA_KML_to_KMZ.py" %*
