# Convenience launcher: make sure PostgreSQL is up, then start the GUI.
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$bin  = Join-Path $root "vendor\pgsql\bin"
$data = Join-Path $root "pgdata"

$running = $false
try { & (Join-Path $bin "pg_ctl.exe") -D $data status | Out-Null; $running = ($LASTEXITCODE -eq 0) } catch {}
if (-not $running) {
    Write-Host "Starting portable PostgreSQL ..." -ForegroundColor Cyan
    & (Join-Path $root "scripts\pg_start.ps1")
}

Write-Host "Launching Streamlit ..." -ForegroundColor Cyan
python -m streamlit run (Join-Path $root "app.py")
