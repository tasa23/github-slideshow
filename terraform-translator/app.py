"""Minimal Flask web app for translating plain language into Azure Terraform.

Run locally:
    export ANTHROPIC_API_KEY=sk-ant-...
    pip install -r requirements.txt
    python app.py
    # open http://127.0.0.1:5000
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from translator import TranslationError, translate

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/translate")
def api_translate():
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "")
    try:
        terraform = translate(prompt)
    except TranslationError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # surface unexpected SDK/network errors to the UI
        return jsonify(error=f"Unexpected error: {exc}"), 500
    return jsonify(terraform=terraform)


if __name__ == "__main__":
    app.run(debug=True)
