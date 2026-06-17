"""Core translation logic: plain language -> Azure Terraform (HCL).

Uses the Anthropic Python SDK with Claude Opus 4.8. Streaming is used so that
long generations don't hit the SDK's HTTP timeout, and adaptive thinking lets
the model reason about the right Azure resources before emitting code.
"""

from __future__ import annotations

import re

from anthropic import Anthropic

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """\
You are an expert cloud infrastructure engineer specializing in Microsoft Azure \
and Terraform (HCL). Convert the user's plain-language infrastructure \
description into production-quality Terraform configuration using the official \
`azurerm` provider.

Guidelines:
- Output valid, well-formatted HCL.
- Include a `terraform` block that pins a recent `azurerm` provider version, and \
a `provider "azurerm"` block with `features {}`.
- Create a resource group unless the user explicitly says to use an existing one.
- Use descriptive, Azure-valid resource names and locations.
- Parameterize key values with `variable` blocks (with sensible defaults) and \
expose useful values (IDs, endpoints, connection info) via `output` blocks.
- Apply Azure + Terraform best practices: tags, secure defaults, least-privilege.
- Add concise `#` comments explaining non-obvious choices.
- If the request is ambiguous, pick reasonable defaults and note them in comments.

Return ONLY the Terraform code. Do not add any explanation before or after it."""


class TranslationError(RuntimeError):
    """Raised when the model cannot produce Terraform for the request."""


def _strip_code_fences(text: str) -> str:
    """Remove a surrounding ```hcl ... ``` (or ```) fence if the model added one."""
    fenced = re.match(r"^\s*```[a-zA-Z]*\n(.*?)\n```\s*$", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()


def translate(prompt: str, *, client: Anthropic | None = None) -> str:
    """Translate a plain-language description into Azure Terraform HCL.

    Args:
        prompt: Natural-language description of the desired Azure infrastructure.
        client: Optional pre-configured Anthropic client. If omitted, a default
            client is created (reads ANTHROPIC_API_KEY from the environment).

    Returns:
        The generated Terraform configuration as a string.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise TranslationError("Please describe the infrastructure you want.")

    client = client or Anthropic()

    with client.messages.stream(
        model=MODEL,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise TranslationError(
            "The request was declined by the model's safety system."
        )

    text = "".join(
        block.text for block in message.content if block.type == "text"
    )
    code = _strip_code_fences(text)
    if not code:
        raise TranslationError("The model did not return any Terraform code.")
    return code
