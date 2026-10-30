@echo off
REM Drag KML / KMZ files (or folders) onto this icon to view them on the map,
REM or double-click it to open an empty viewer you can drop files into.
python -c "import shapely, tkintermapview, tkinterdnd2" 2>nul || python -m pip install shapely tkintermapview tkinterdnd2
start "" pythonw "%~dp0PVA_KML_to_KMZ.py" --viewer %*
