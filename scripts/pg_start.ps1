$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bin  = Join-Path $root "vendor\pgsql\bin"
$data = Join-Path $root "pgdata"
& (Join-Path $bin "pg_ctl.exe") -D $data -l (Join-Path $data "server.log") -w start
& (Join-Path $bin "pg_ctl.exe") -D $data status
