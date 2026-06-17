# Move the IaC Build app to a git repo + VS Code (deploy without Cloud Shell)

Today the app is deployed from a **zip bundle**. This sets it up as a proper git
repo you edit in VS Code and deploy from the VS Code integrated terminal.

## Prerequisites (install once on your workstation)

- **Azure CLI** (`az`) — https://learn.microsoft.com/cli/azure/install-azure-cli
- **Git**
- **VS Code** + extensions: Claude Code (you have it), and optionally
  *Azure Container Apps*, *Docker*, and *Python*.
- You do **not** need Docker Desktop — `az acr build` builds in Azure.

## 1. Make the app a git repo

Use the bundle you already unzipped (it has the hardened `app.py`):

```bash
cd ~/iacb/code                      # the folder with Dockerfile, app.py, app.html, templates/

cp /path/to/iac-aistudio-update/deploy.sh ./deploy.sh && chmod +x deploy.sh
cp /path/to/iac-aistudio-update/app.gitignore ./.gitignore

git init -b main
git add .
git commit -m "IaC Build app (hardened) — initial repo"
```

> Keep the repo **private** — it contains your application logic (no secrets:
> those stay in Key Vault). Do not commit `.env` files or local databases
> (the `.gitignore` handles this).

## 2. Push to your git host (pick one)

**GitLab** (you mentioned this):
```bash
git remote add origin https://gitlab.com/<you>/iac-build.git
git push -u origin main
```

**GitHub:**
```bash
git remote add origin https://github.com/<you>/iac-build.git
git push -u origin main
```

## 3. Open in VS Code

```bash
code ~/iacb/code
```
Or in VS Code: **File ▸ Open Folder…** → select the repo. Use the Claude Code
extension in that window to make changes.

## 4. Deploy from VS Code (replaces Cloud Shell)

In the VS Code integrated terminal (**Ctrl+`**):

```bash
az login            # once per session/workstation
az account set --subscription 35b00a54-aaaf-46cb-9ec3-783ab739b084
./deploy.sh
```

`deploy.sh` builds in ACR, rolls the Container App, prints revision health, and
shows the exact rollback command. That's your whole deploy now — edit in VS
Code, commit, run `./deploy.sh`.

## 5. Day-to-day loop

1. Edit code in VS Code (Claude Code can make the changes).
2. `git commit` (and `git push` to back it up).
3. `./deploy.sh` in the terminal.
4. Verify: the script's health line, plus a click-through on https://iac-aistudio.com.

Rollback any time: `az containerapp update -n ca-iacb-web -g rg-iacb-lean --image "<previous-tag>"`.

## Notes

- The base image `python:3.12-slim-bookworm` now lives in your ACR. If a build
  ever fails with `manifest unknown`, uncomment the `az acr import` line in
  `deploy.sh` and run it once.
- For automated checks on every push, wire up the security tests
  (`tests/test_security.py` + `.gitlab-ci.yml`) from this update bundle.
