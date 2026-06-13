#!/usr/bin/env python3
"""Smoke test: can we drive the model through the local Anthropic proxy?"""
import os, sys
import anthropic

base = os.environ.get("ANTHROPIC_BASE_URL")
model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4.8")
token = os.environ.get("ANTHROPIC_AUTH_TOKEN")

# The proxy uses ANTHROPIC_AUTH_TOKEN as bearer; pass as api_key.
client = anthropic.Anthropic(api_key=token, base_url=base)

try:
    resp = client.messages.create(
        model=model,
        max_tokens=128,
        messages=[{"role": "user", "content": "Reply with exactly: PONG. Then nothing else."}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    print("OK text=", repr(text.strip()))
    print("usage:", resp.usage)
except Exception as e:
    print("ERR", type(e).__name__, str(e)[:400])
    sys.exit(1)
