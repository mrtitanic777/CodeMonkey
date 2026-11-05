"""Minimal client for an OpenAI-compatible server (LM Studio by default).

MSHMOE_LLM_URL sets the address. A server that needs a key gets one from MSHMOE_LLM_KEY, or from the
key CodeMonkey saved for one of its providers (MSHMOE_LLM_PROVIDER=<provider name>, read through
CodeMonkey's keystore); a key is never written anywhere by this module.
A reply asked for in a JSON shape is checked to be JSON: servers that ignore the shape request get
the shape in the prompt instead, and the JSON is taken out of the reply.
"""
import json
import os
import re
import sys
import urllib.request

BASE_URL = os.environ.get("MSHMOE_LLM_URL", "http://localhost:1234/v1")


def _key():
    key = os.environ.get("MSHMOE_LLM_KEY", "").strip()
    provider = os.environ.get("MSHMOE_LLM_PROVIDER", "").strip()
    if key or not provider:
        return key
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    import keystore   # CodeMonkey's: Windows Credential Manager
    return keystore.get(provider) or ""


def _json_in(text, schema=None):
    """The JSON object in a reply (it may come wrapped in prose or a code fence). Where the shape asked
    for is one list of strings, a reply written as plain lines is taken as that list."""
    text = (text or "").strip()
    try:
        json.loads(text)
        return text
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        json.loads(m.group(0))
        return m.group(0)
    props = (schema or {}).get("properties", {})
    if len(props) == 1:
        (name, spec), = props.items()
        if spec.get("type") == "array" and spec.get("items", {}).get("type") == "string" and text:
            lines = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", ln).strip().strip("`") for ln in text.splitlines()]
            return json.dumps({name: [ln for ln in lines if ln and not ln.startswith("```")]})
    raise ValueError("the reply holds no JSON object: " + text[:300])


def chat(model, messages, schema=None, temperature=0.0, max_tokens=800, timeout=900):
    key = _key()
    if key:   # a remote model may think before it answers: leave it room (MSHMOE_LLM_MIN_TOKENS)
        max_tokens = max(max_tokens, int(os.environ.get("MSHMOE_LLM_MIN_TOKENS", "4000")))
    body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "output", "strict": True, "schema": schema}}
        # said in words too, for a server that does not honour response_format
        body["messages"] = list(messages) + [{"role": "system", "content":
                                              "Reply with only a JSON object matching this schema: " + json.dumps(schema)}]
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(BASE_URL + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        content = json.loads(resp.read())["choices"][0]["message"]["content"]
    return _json_in(content, schema) if schema is not None else content
