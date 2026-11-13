@echo off
setlocal
cd /d "%~dp0"
where go >nul 2>nul
if errorlevel 1 (
  echo Install Go 1.24 or newer before building the client.
  pause
  exit /b 1
)
set "GOOS=windows"
set "GOARCH=amd64"
set "CGO_ENABLED=0"
go build -buildvcs=false -trimpath -ldflags "-s -w -H windowsgui" -o Blue3-ScoreEntry.exe .
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)
echo Created Blue3-ScoreEntry.exe
pause
