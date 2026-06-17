#!/usr/bin/env bash
# Deploy the IaC Build app to Azure Container Apps from your workstation.
# Builds the image in ACR (server-side — no local Docker needed) and rolls the
# Container App to it, then prints revision health. Run from the repo root
# (the folder containing the Dockerfile):  ./deploy.sh
set -euo pipefail

RG=rg-iacb-lean
APP=ca-iacb-web
ACR=acriacblean
IMAGE=iacb-web
HEALTH_URL=https://iac-aistudio.com/healthz

cd "$(dirname "$0")"

# Sanity: must be run where the Dockerfile lives.
[ -f Dockerfile ] || { echo "ERROR: no Dockerfile here ($(pwd))."; exit 1; }

# Make sure you're logged in / on the right subscription.
az account show -o table >/dev/null 2>&1 || { echo "Run 'az login' first."; exit 1; }

# Capture the current image so rollback is one command.
PREV=$(az containerapp show -n "$APP" -g "$RG" \
  --query "properties.template.containers[0].image" -o tsv)
echo "Current image (rollback target): $PREV"

# Optional: ensure the base image exists in ACR (uncomment if a build ever
# fails with 'manifest unknown' for python:3.12-slim-bookworm):
# az acr import -n "$ACR" --source mirror.gcr.io/library/python:3.12-slim-bookworm \
#   --image python:3.12-slim-bookworm --force

TAG=$(date +%Y%m%d-%H%M)
echo "Building $ACR.azurecr.io/$IMAGE:$TAG ..."
az acr build --registry "$ACR" --image "$IMAGE:$TAG" .

echo "Rolling Container App to the new image ..."
az containerapp update -n "$APP" -g "$RG" \
  --image "$ACR.azurecr.io/$IMAGE:$TAG" -o none

echo "Active revisions:"
az containerapp revision list -n "$APP" -g "$RG" \
  --query "[?properties.active].{rev:name,running:properties.runningState,health:properties.healthState,traffic:properties.trafficWeight}" \
  -o table

echo "Health check:"
curl -fsS "$HEALTH_URL" && echo || echo "  (not ready yet — re-check revision health in a moment)"

echo
echo "Deployed tag: $TAG"
echo "Rollback if needed:  az containerapp update -n $APP -g $RG --image \"$PREV\""
