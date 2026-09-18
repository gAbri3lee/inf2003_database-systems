# One-time PostgreSQL bootstrap for the prototype.
# Creates a *local, non-service* PostgreSQL cluster under .\pgdata on port 5433
# using the portable binaries in .\vendor\pgsql. No admin rights required.

$ErrorActionPreference = "Stop"
$root    = Split-Path -Parent $PSScriptRoot
$bin     = Join-Path $root "vendor\pgsql\bin"
$data    = Join-Path $root "pgdata"
$port    = 5433
$user    = "hdb"
$pass    = "hdb"
$dbname  = "hdb"

if (-not (Test-Path (Join-Path $bin "initdb.exe"))) {
    throw "Portable PostgreSQL not found at $bin. Extract vendor\pg-binaries.zip first."
}

if (Test-Path (Join-Path $data "PG_VERSION")) {
    Write-Host "Cluster already initialised at $data - skipping initdb." -ForegroundColor Yellow
} else {
    $pwfile = Join-Path $env:TEMP "pg_pw_$([guid]::NewGuid().ToString('N')).txt"
    Set-Content -Path $pwfile -Value $pass -NoNewline -Encoding ascii
    try {
        Write-Host "initdb -> $data"
        & (Join-Path $bin "initdb.exe") -D $data -U $user --pwfile=$pwfile `
            --encoding=UTF8 --auth=scram-sha-256 --auth-host=scram-sha-256 | Out-Host
    } finally {
        Remove-Item $pwfile -ErrorAction SilentlyContinue
    }
}

# make the port permanent for this cluster
$conf = Join-Path $data "postgresql.conf"
if (-not (Select-String -Path $conf -Pattern "^\s*port\s*=\s*$port" -Quiet)) {
    Add-Content -Path $conf -Value "`nport = $port`nlisten_addresses = 'localhost'`n"
}

Write-Host "Starting server on port $port ..."
& (Join-Path $bin "pg_ctl.exe") -D $data -l (Join-Path $data "server.log") -w start | Out-Host

# psql / createdb are non-interactive here - hand them the password via env
$env:PGPASSWORD = $pass

# create the application database if missing
$exists = & (Join-Path $bin "psql.exe") -U $user -h localhost -p $port -d postgres `
    -tAc "SELECT 1 FROM pg_database WHERE datname='$dbname'"
if (-not $exists) {
    & (Join-Path $bin "createdb.exe") -U $user -h localhost -p $port $dbname
    Write-Host "Created database '$dbname'." -ForegroundColor Green
} else {
    Write-Host "Database '$dbname' already exists." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "PostgreSQL ready:  host=localhost port=$port user=$user password=$pass db=$dbname" -ForegroundColor Green
Write-Host "Stop it later with:  scripts\pg_stop.ps1"
