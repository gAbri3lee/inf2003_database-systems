$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin  = Join-Path $root "vendor\pgsql\bin"
$data = Join-Path $root "pgdata"
& (Join-Path $bin "pg_ctl.exe") -D $data -m fast stop
