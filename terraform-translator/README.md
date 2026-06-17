# Plain Language → Azure Terraform

A small AI-powered app that turns a plain-English description of Azure
infrastructure into [Terraform](https://www.terraform.io/) (HCL) using the
official `azurerm` provider. It is powered by Anthropic's Claude (Opus 4.8).

You can use it two ways:

- **Web app** — a single-page UI (Flask)
- **CLI** — pipe a description in and get `main.tf` on stdout

> ⚠️ The generated Terraform is a starting point. Always review it (and run
> `terraform validate` / `terraform plan`) before applying to a real subscription.

## Setup

```bash
cd terraform-translator
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # your Anthropic API key
```

## Web app

```bash
python app.py
# open http://127.0.0.1:5000
```

Type something like:

> A Linux web app on a B1 plan with a PostgreSQL flexible server, both in West Europe

…and click **Generate Terraform**. Use the **Copy** button to grab the HCL.

## CLI

```bash
python cli.py "a storage account with a private blob container"

# or read the description from stdin
echo "an AKS cluster with 3 nodes and a container registry" | python cli.py
```

## Testing

There are two levels.

**Offline tests (no API key, no network).** These inject a fake client that
mimics the SDK, so they verify the parsing/error logic anywhere:

```bash
python -m unittest discover -s tests -v   # or: pytest
```

**Real end-to-end check (needs `ANTHROPIC_API_KEY`).** After `pip install -r
requirements.txt` and setting the key:

```bash
# CLI
python cli.py "a resource group named demo-rg in West Europe"

# Web app — start it, then send a request
python app.py &
curl -s localhost:5000/api/translate \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a storage account with a private blob container"}'
```

Then run `terraform fmt -check` / `terraform validate` on the output to confirm
it's syntactically valid HCL.

## How it works

`translator.py` sends your description to Claude with a system prompt that makes
it act as an Azure + Terraform expert. It uses:

- **Model:** `claude-opus-4-8`
- **Streaming** via `client.messages.stream(...)` so long outputs don't hit the
  SDK's HTTP timeout (the final message is read with `get_final_message()`).
- **Adaptive thinking** (`thinking={"type": "adaptive"}`) so the model can reason
  about which Azure resources to use before emitting code.
- **`effort: "high"`** for better code quality.

The model is instructed to return only HCL; any surrounding Markdown code fence
is stripped before display.

## Files

| File                   | Purpose                                          |
| ---------------------- | ------------------------------------------------ |
| `translator.py`        | Core logic — calls the Anthropic API             |
| `app.py`               | Flask web server (`/` and `/api/translate`)      |
| `cli.py`               | Command-line entry point                         |
| `templates/index.html` | Web UI                                            |
| `requirements.txt`     | Python dependencies                              |
