$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$pythonExe = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe = (Get-Command python -ErrorAction Stop).Source }
& $pythonExe (Join-Path $root 'scripts\launch.py')
if ($LASTEXITCODE -ne 0) { throw "Application exited with code $LASTEXITCODE" }
