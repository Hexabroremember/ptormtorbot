# Build Docker image locally (optional — Railway usually builds from Git + Dockerfile).
# After build: push to a registry, then attach the image in Railway or deploy via Dockerfile from repo.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "Install Docker Desktop, then run this script again."
    exit 1
}

$image = "ptormtorbot-api:local"
Write-Host "Building $image (linux/amd64) ..."
docker build --platform linux/amd64 -t $image .

Write-Host ""
Write-Host "Build OK. Next (replace YOUR_USER with your registry username):"
Write-Host "  docker tag $image YOUR_USER/ptormtorbot-api:latest"
Write-Host "  docker login"
Write-Host "  docker push YOUR_USER/ptormtorbot-api:latest"
Write-Host ""
Write-Host "Railway: connect the GitHub repo (recommended) or set variables for a container from your registry."
Write-Host ""
Write-Host "Environment variables (see .env.example):"
Write-Host "  TELEGRAM_BOT_TOKEN = <your bot token>"
Write-Host "  WEB_APP_URL = https://<your-app>.up.railway.app   (or set in dashboard; RAILWAY_PUBLIC_DOMAIN is injected)"
Write-Host ""
Write-Host "Optional: legacy Render API check script (not used for Railway):"
Write-Host '  .\scripts\render-verify-key.ps1   # only if you use Render API'
