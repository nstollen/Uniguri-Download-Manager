@echo off
rem Builds dist\UDM-Setup.exe: udm-downloader.exe and udm-host.exe with PyInstaller, then the installer
rem with Inno Setup 6. Run it again after every change. Needs (only for building, not to run UDM):
rem   PyInstaller:   python -m pip install pyinstaller
rem   truststore:    python -m pip install truststore   (https checks with Windows' certificates, built into the exe)
rem   Inno Setup 6:  https://jrsoftware.org/isdl.php
rem Another Python or Inno Setup:  set PYTHON=C:\path\python.exe  /  set ISCC=C:\path\ISCC.exe  before build.bat
setlocal
cd /d "%~dp0"
if not defined PYTHON set "PYTHON=python"
if not defined ISCC set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"

rem ---- input: the tools and files the build needs ----
"%PYTHON%" -m PyInstaller --version >nul 2>&1 || (echo PyInstaller not found. Run: %PYTHON% -m pip install pyinstaller & exit /b 1)
"%PYTHON%" -c "import truststore" >nul 2>&1 || (echo truststore not found. Run: %PYTHON% -m pip install truststore & exit /b 1)
if not exist "%ISCC%" (echo Inno Setup 6 not found. Get it from https://jrsoftware.org/isdl.php & exit /b 1)
if not exist downloader\ffmpeg.exe (echo downloader\ffmpeg.exe is missing. & exit /b 1)
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Content extension\manifest.json -Raw | ConvertFrom-Json).version"`) do set "VERSION=%%v"

rem ---- process: the two exes (folders with the exe and its _internal folder) in build\dist ----
echo [1/3] udm-downloader.exe (no console window)
"%PYTHON%" -m PyInstaller --noconfirm --clean --log-level WARN --windowed --name udm-downloader ^
  --icon "%~dp0downloader\udm.ico" --hidden-import tkinter --hidden-import tkinter.filedialog --hidden-import truststore ^
  --distpath build\dist --workpath build\work --specpath build downloader\downloader.py || goto failed
echo [2/3] udm-host.exe
"%PYTHON%" -m PyInstaller --noconfirm --clean --log-level WARN --console --name udm-host ^
  --distpath build\dist --workpath build\work --specpath build host\host.py || goto failed

rem ---- output: dist\UDM-Setup.exe ----
echo [3/3] UDM-Setup.exe (version %VERSION%)
"%ISCC%" /Q /DAppVersion=%VERSION% installer\udm.iss || goto failed
echo.
echo Done: %~dp0dist\UDM-Setup.exe
exit /b 0

:failed
echo.
echo Build failed, see the messages above.
exit /b 1
