"""Example: add OpenAI, DeepSeek or any other OpenAI-compatible API to Underleaf's chat.

How to use it (details in README.md, "Adding another AI provider"):
  1. python3 -m pip install openai
  2. Copy PART 1 into server.py, just above the line "PROVIDERS = {".
  3. Copy the entries from PART 2 into the PROVIDERS table in server.py.
  4. Restart server.py, open Ask AI, choose the provider from the menu, paste the key.

Any provider that speaks the OpenAI Chat Completions API *with tool (function) calling* works
by changing base_url: Groq, Mistral, OpenRouter, Together, a local Ollama server, and so on.
Check that your provider and model support tool calling; the chat cannot edit without it.
"""

# ============================== PART 1: paste above "PROVIDERS = {" ==============================

try:
    import openai
except ImportError:  # this provider stays off until `python3 -m pip install openai`
    openai = None


def make_openai_compatible(base_url=None, label="The provider"):
    """Build (verify, run, models) for an OpenAI-compatible API. base_url=None means OpenAI itself."""

    def verify(key, model):
        client = openai.OpenAI(api_key=key, base_url=base_url)
        try:
            client.models.retrieve(model)
            return True, f"Key works. Using {model}."
        except openai.AuthenticationError:
            return False, f"{label} rejected this key."
        except openai.NotFoundError:
            return True, f"Key works, but the model {model!r} was not found. Set the model in config.json."

    def run(cfg, msgs, system, emit, max_steps=16):
        client = openai.OpenAI(api_key=cfg["key"], base_url=base_url)
        results = []
        funcs = make_tool_funcs(results)
        tools = [{"type": "function",
                  "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                 for t in TOOL_SPECS]
        messages = [{"role": "system", "content": system}] + msgs
        try:
            for _ in range(max_steps):
                resp = client.chat.completions.create(model=cfg["model"], messages=messages, tools=tools)
                msg = resp.choices[0].message
                if getattr(msg, "refusal", None):
                    emit({"type": "error", "message": f"{label} declined: {msg.refusal}"})
                    return
                if msg.content and msg.content.strip():
                    emit({"type": "text", "text": msg.content.strip()})
                calls = msg.tool_calls or []
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    **({"tool_calls": [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in calls]} if calls else {}),
                })
                if not calls:
                    return
                for call in calls:
                    name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except ValueError:
                        args = None
                    emit({"type": "tool", "name": name, "summary": tool_summary(name, args or {})})
                    fn = funcs.get(name)
                    if fn is None:
                        result = f"ERROR: unknown tool {name!r}."
                        results.append({"ok": False, "summary": f"Unknown tool {name}"})
                    elif not isinstance(args, dict):
                        result = "ERROR: the tool arguments were not valid JSON. Send them again."
                        results.append({"ok": False, "summary": f"{name}: invalid arguments"})
                    else:
                        try:
                            result = fn(**args)
                        except TypeError as e:
                            result = f"ERROR: bad arguments for {name}: {e}"
                            results.append({"ok": False, "summary": f"{name}: bad arguments"})
                    flush_results(results, emit)
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            emit({"type": "error", "message": f"Stopped after {max_steps} steps. Ask again to continue."})
        except openai.AuthenticationError:
            emit({"type": "error", "message": f"{label} rejected the API key. Check it and save it again.", "auth": True})
        except openai.RateLimitError:
            emit({"type": "error", "message": f"{label} rate limit or quota reached. Wait a moment and try again."})
        except openai.APIStatusError as e:
            emit({"type": "error", "message": f"{label} API error {e.status_code}: {getattr(e, 'message', str(e))}"})
        except openai.APIConnectionError:
            emit({"type": "error", "message": f"Could not reach {label}. Check your internet connection."})

    def models(key):
        client = openai.OpenAI(api_key=key, base_url=base_url)
        return sorted(({"id": m.id, "name": m.id} for m in client.models.list()), key=lambda d: d["id"])

    return verify, run, models


verify_openai, run_openai, models_openai = make_openai_compatible(label="OpenAI")
verify_deepseek, run_deepseek, models_deepseek = make_openai_compatible("https://api.deepseek.com", label="DeepSeek")


# ============================== PART 2: add inside PROVIDERS = { ... } ==============================

EXAMPLE_PROVIDER_ENTRIES = {
    "openai": {
        "name": "OpenAI",
        "prefixes": (),                         # no guessing: choose "OpenAI" in the menu when saving
        "env": ("OPENAI_API_KEY",),
        "default_model": "gpt-6-astra",         # check https://developers.openai.com/api/docs/models
        "package": "openai",
        "available": lambda: openai is not None,
        "verify": verify_openai,
        "run": run_openai,
        "models": models_openai,                # lists the models for the picker
    },
    "deepseek": {
        "name": "DeepSeek",
        "prefixes": (),
        "env": ("DEEPSEEK_API_KEY",),
        "default_model": "deepseek-v4-pro",     # check https://api-docs.deepseek.com/
        "package": "openai",                    # DeepSeek uses the OpenAI client with its own base_url
        "available": lambda: openai is not None,
        "verify": verify_deepseek,
        "run": run_deepseek,
        "models": models_deepseek,
    },
}
