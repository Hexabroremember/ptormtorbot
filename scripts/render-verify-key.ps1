# Verifies RENDER_API_KEY from environment (never commit keys).
$ErrorActionPreference = "Stop"
if (-not $env:RENDER_API_KEY) {
    Write-Host "Set RENDER_API_KEY first, e.g. `$env:RENDER_API_KEY='rnd_...'"
    exit 1
}
$h = @{ Authorization = "Bearer $env:RENDER_API_KEY" }
$r = Invoke-RestMethod -Uri "https://api.render.com/v1/owners?limit=5" -Headers $h
# API returns either { value: [ { owner: ... } ] } or a single { owner: ... } depending on version
$items = @()
if ($r.value) { $items = $r.value }
elseif ($r.owner) { $items = @(@{ owner = $r.owner }) }
Write-Host "API key OK. Workspaces:"
foreach ($x in $items) {
    $o = $x.owner
    Write-Host ("  - " + $o.name + " [" + $o.type + "] " + $o.id + " <" + $o.email + ">")
}
