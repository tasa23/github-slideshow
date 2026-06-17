"""Offline tests for translator.py.

These run without an ANTHROPIC_API_KEY and without the `anthropic` package,
by injecting a fake client that mimics the SDK's streaming surface.

    python -m unittest discover -s tests        # or: pytest
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from translator import TranslationError, _strip_code_fences, translate


class _FakeStream:
    """Stands in for the context manager returned by client.messages.stream(...)."""

    def __init__(self, message):
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeClient:
    """Minimal fake exposing client.messages.stream(...)."""

    def __init__(self, text, stop_reason="end_turn"):
        message = SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type="text", text=text)],
        )
        self.messages = SimpleNamespace(stream=lambda **kwargs: _FakeStream(message))


class StripFenceTests(unittest.TestCase):
    def test_hcl_fence(self):
        self.assertEqual(_strip_code_fences('```hcl\nresource "x" "y" {}\n```'),
                         'resource "x" "y" {}')

    def test_bare_fence(self):
        self.assertEqual(_strip_code_fences("```\nfoo\n```"), "foo")

    def test_no_fence(self):
        self.assertEqual(_strip_code_fences("  plain  "), "plain")


class TranslateTests(unittest.TestCase):
    def test_returns_code_and_strips_fence(self):
        client = _FakeClient('```hcl\nprovider "azurerm" {\n  features {}\n}\n```')
        out = translate("a resource group", client=client)
        self.assertEqual(out, 'provider "azurerm" {\n  features {}\n}')

    def test_empty_prompt_raises(self):
        with self.assertRaises(TranslationError):
            translate("   ", client=_FakeClient("ignored"))

    def test_refusal_raises(self):
        client = _FakeClient("", stop_reason="refusal")
        with self.assertRaises(TranslationError):
            translate("something", client=client)


if __name__ == "__main__":
    unittest.main()
