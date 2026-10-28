@echo off
REM Double-click to launch the PVA KML to KMZ Converter.
REM First run installs shapely (splitting/cutting) and tkintermapview (map preview).
python -c "import shapely, tkintermapview" 2>nul || python -m pip install shapely tkintermapview
start "" pythonw "%~dp0PVA_KML_to_KMZ.py"
