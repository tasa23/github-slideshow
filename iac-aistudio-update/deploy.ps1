# deploy.ps1 — deploy IaC Build to Azure Container Apps from Windows / VS Code.
# Builds the image in ACR (server-side — no local Docker needed) and rolls the
# Container App, then prints revision health. Run from the repo root (the folder
# with the Dockerfile):   .\deploy.ps1
$ErrorActionPreference = "Stop"

$RG     = "rg-iacb-lean"
$APP    = "ca-iacb-web"
$ACR    = "acriacblean"
$IMAGE  = "iacb-web"
$HEALTH = "https://iac-aistudio.com/healthz"

Set-Location -Path $PSScriptRoot
if (-not (Test-Path ".\Dockerfile")) { throw "No Dockerfile in $PWD - run this from the repo root." }

# Make sure you're logged in.
az account show -o table 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Not logged in. Run 'az login' first." }

# Capture current image for one-command rollback.
$prev = az containerapp show -n $APP -g $RG --query "properties.template.containers[0].image" -o tsv
Write-Host "Current image (rollback target): $prev" -ForegroundColor Cyan

# Optional: if a build ever fails with 'manifest unknown' for the base image,
# uncomment and run once to re-import it into ACR:
# az acr import -n $ACR --source mirror.gcr.io/library/python:3.12-slim-bookworm --image python:3.12-slim-bookworm --force

$tag  = Get-Date -Format "yyyyMMdd-HHmm"
$full = "$ACR.azurecr.io/${IMAGE}:$tag"

Write-Host "Building $full ..." -ForegroundColor Cyan
az acr build --registry $ACR --image "${IMAGE}:$tag" .
if ($LASTEXITCODE -ne 0) { throw "ACR build failed." }

Write-Host "Rolling Container App to the new image ..." -ForegroundColor Cyan
az containerapp update -n $APP -g $RG --image $full -o none
if ($LASTEXITCODE -ne 0) { throw "Container App update failed." }

Write-Host "Active revisions:" -ForegroundColor Cyan
az containerapp revision list -n $APP -g $RG `
  --query "[?properties.active].{rev:name,running:properties.runningState,health:properties.healthState,traffic:properties.trafficWeight}" -o table

Write-Host "Health check:" -ForegroundColor Cyan
try { (Invoke-WebRequest -UseBasicParsing $HEALTH).Content }
catch { Write-Host "  (not ready yet - re-check revision health in a moment)" -ForegroundColor Yellow }

Write-Host ""
Write-Host "Deployed tag: $tag" -ForegroundColor Green
Write-Host "Rollback:  az containerapp update -n $APP -g $RG --image `"$prev`""
