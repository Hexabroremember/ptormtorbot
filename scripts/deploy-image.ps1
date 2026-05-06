# Build Docker image for Render (no Git). Run from project root in PowerShell.
# After build: push to Docker Hub, then Render Dashboard -> New -> Web Service -> Existing Image
# (Creating an image-only service via Render's REST API often fails for public image refs; use the Dashboard.)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Install Docker Desktop, then run this script again."
    exit 1
}

$image = "ptormtorbot-api:local"
Write-Host "Building $image (linux/amd64 for Render) ..."
docker build --platform linux/amd64 -t $image .

Write-Host ""
Write-Host "Build OK. Next (replace YOUR_USER with your Docker Hub username):"
Write-Host "  docker tag $image YOUR_USER/ptormtorbot-api:latest"
Write-Host "  docker login"
Write-Host "  docker push YOUR_USER/ptormtorbot-api:latest"
Write-Host ""
Write-Host "Render Dashboard (NO GitHub):"
Write-Host "  New -> Web Service -> Deploy an existing image from a registry"
Write-Host "  Image URL: docker.io/YOUR_USER/ptormtorbot-api:latest"
Write-Host ""
Write-Host "Environment variables:"
Write-Host "  TELEGRAM_BOT_TOKEN = <your bot token>"
Write-Host "  WEB_APP_URL = https://<your-service-name>.onrender.com"
Write-Host ""
Write-Host "Optional: verify API key (use env var only — do not commit keys):"
Write-Host '  $env:RENDER_API_KEY = "rnd_..." ; .\scripts\render-verify-key.ps1'
