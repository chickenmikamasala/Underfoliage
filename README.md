# Typeset — local LaTeX editor

A split-pane LaTeX editor that runs on your own computer. Source on the left,
the typeset PDF on the right, one **Compile** button (⌘↵ / Ctrl↵), a
**Download PDF** button that saves the PDF named after your `\title`, and an
**Ask AI** chat that edits the document for you using your own API key.

Everything runs locally: the editor is a small Python server on your machine,
your documents are plain `.tex` files in the `doc/` folder, and your API key
never leaves your computer except to go to the AI provider you chose.

---

## Set it up on your computer

You need three things: the code, Python, and a TeX engine.

### 1. Get the code

If you were given a GitHub link:

```bash
git clone <the GitHub link you were given>
cd latex-editor
```

If you were given a zip file, unzip it and open a terminal in the
`latex-editor` folder.

### 2. Install Python and the chat packages

You need Python 3.10 or newer (check with `python3 --version`; on Windows the
command is usually `python`). Get it from https://www.python.org/downloads/
if you don't have it.

Then install the packages the chat uses:

```bash
python3 -m pip install -r requirements.txt
```

The editor and compiler work without them; only **Ask AI** needs them.

### 3. Install a TeX engine

The easiest is **Tectonic**, a single program that downloads the LaTeX
packages it needs on first use. Download the file for your computer from
https://github.com/tectonic-typesetting/tectonic/releases (version 0.17.0):

| Your computer | File to download |
| --- | --- |
| Mac with Apple Silicon (M1 or newer) | `tectonic-0.17.0-aarch64-apple-darwin.tar.gz` |
| Mac with Intel | `tectonic-0.17.0-x86_64-apple-darwin.tar.gz` |
| Windows | `tectonic-0.17.0-x86_64-pc-windows-msvc.zip` |
| Linux (most PCs) | `tectonic-0.17.0-x86_64-unknown-linux-gnu.tar.gz` |

Extract it and put the single `tectonic` program (`tectonic.exe` on Windows)
in a folder called `bin` inside `latex-editor`. Download the program files
above, not "Source code".

On a Mac, the browser marks downloads as quarantined, so clear that once:

```bash
xattr -d com.apple.quarantine bin/tectonic
```

Already have a full LaTeX installation (MacTeX, TeX Live or MiKTeX)? Skip
Tectonic: the server finds `latexmk`, `pdflatex`, `xelatex` or `lualatex` on
your PATH and uses them instead.

### 4. Start it

```bash
python3 server.py
```

Then open http://127.0.0.1:8765 in your browser. Keep the terminal open
while you work; close it or press Ctrl+C to stop the editor. On a Mac you can
instead double-click `Open Typeset.command`.

The first compile can take a minute while Tectonic downloads packages. Later
compiles take about a second. A starter document is created in
`doc/main.tex` the first time you run it.

### Your documents

Click the document name next to **Typeset** in the top-left corner to open the
document menu. One document is open at a time.

- **New document**: type a name such as `chapter-2` and press **Create**. It
  is saved as `doc/chapter-2.tex` and starts from a small template.
- **Open**: pick any saved document from the list. Whatever you typed in the
  current one is saved first.

Your work saves itself about a second after you stop typing. To bring in
LaTeX you already have, copy the `.tex` file into the `doc/` folder and it
appears in the list. Put images and `.bib` files in `doc/` too, so
`\includegraphics` and `\bibliography` can find them. Names can use letters,
digits, `-` and `_`.

### 5. Add your API key

Click **Ask AI**, paste a key from your AI provider, and press **Save key**.
The key is checked with the provider first and then stored in `config.json`
in this folder, readable only by you.

| Provider | Where to get a key | Key usually starts with |
| --- | --- | --- |
| Google Gemini | Google AI Studio → API keys | `AQ.` (older keys: `AIza`) |
| Claude | https://console.anthropic.com → API Keys | `sk-ant-` |

The server guesses the provider from the key's first characters. If it
guesses wrong, or your key looks different, choose the provider from the menu
next to the key field. Using the chat is billed to your own account with that
provider.

To change model, add a line such as `"gemini_model": "gemini-4-carbon"` or
`"claude_model": "claude-sonnet-5-5"` to `config.json` and restart.
Environment variables work too: `GEMINI_API_KEY`, `GOOGLE_API_KEY` or
`ANTHROPIC_API_KEY`.

---

## Sharing it with someone else

Share the code, never your own copy of these:

- `config.json` — contains your API key.
- `doc/` — your documents.
- `bin/` — your TeX engine, which only runs on your type of computer.

All three are listed in `.gitignore`, so a Git repository or `git archive`
leaves them out. If you zip the folder by hand, delete them from the zip.
Each person installs their own TeX engine and uses their own API key.

---

## Adding another AI provider

The chat can use any AI provider whose API supports **tool calling**, also
called function calling. All provider-specific code lives in one table,
`PROVIDERS`, near the bottom of `server.py`. Adding a provider means writing
two functions and adding one entry to that table. The key form's menu, the
config file and the environment variables all follow from the table.

### OpenAI, DeepSeek and other OpenAI-compatible APIs

`examples/openai_compatible.py` is a complete, tested example that adds
OpenAI and DeepSeek. The same code works for any provider that offers an
OpenAI-compatible API, such as Groq, Mistral, OpenRouter, Together or a local
Ollama server, by changing `base_url`.

1. Install the OpenAI package, which DeepSeek and the others also use:

   ```bash
   python3 -m pip install openai
   ```

2. Open `examples/openai_compatible.py`. Copy **PART 1** into `server.py`,
   directly above the line `PROVIDERS = {`.
3. Copy the `"openai"` and `"deepseek"` entries from **PART 2** into the
   `PROVIDERS = { ... }` table in `server.py`.
4. Restart `server.py`, open **Ask AI**, click **Change key**, choose
   OpenAI or DeepSeek in the menu, and paste your key.

To add another OpenAI-compatible provider, copy one of those lines and
entries with its own name and address. For example, for Groq:

```python
verify_groq, run_groq = make_openai_compatible("https://api.groq.com/openai/v1", label="Groq")
```

Then add a `"groq"` entry to `PROVIDERS` like the DeepSeek one, with Groq's
model name. Check your provider's documentation for its API address, its
current model names, and whether that model supports tool calling.

### Any other provider

For a provider with its own SDK, write two functions in `server.py` and add
an entry to `PROVIDERS`. `run_gemini` and `verify_gemini` are the closest
templates.

```python
PROVIDERS = {
    ...
    "myprovider": {
        "name": "My Provider",       # shown in the menu and the chat drawer
        "prefixes": ("mp-",),        # key prefixes used to guess the provider; () for none
        "env": ("MYPROVIDER_API_KEY",),
        "default_model": "model-name",  # users can override with "myprovider_model" in config.json
        "package": "myprovider-sdk",    # pip package name, shown if it is missing
        "available": lambda: myprovider_sdk is not None,
        "verify": verify_myprovider,
        "run": run_myprovider,
    },
}
```

**`verify_myprovider(key, model)`** makes one cheap call with the key, such as
looking up the model, and returns `(ok, message)`. `ok` is `True` if the key
works, `False` if the provider rejected it, or `None` if it couldn't be
checked.

**`run_myprovider(cfg, msgs, system, emit)`** runs one chat turn:

- `cfg["key"]` and `cfg["model"]` are the key and model to use.
- `msgs` is the conversation so far, a list of `{"role": "user" | "assistant", "content": text}`.
- `system` is the instructions plus the current document; send it as the system prompt.
- Offer the model the four tools described in `TOOL_SPECS` (JSON Schema).
- `make_tool_funcs(results)` returns the Python functions to call for each tool, by name.
- Loop: call the model; `emit({"type": "text", "text": ...})` for its text; for each
  tool call, `emit({"type": "tool", "name": ..., "summary": tool_summary(name, args)})`,
  run the function, call `flush_results(results, emit)`, and send the result
  back to the model. Stop when the model replies without tool calls.
- Report problems with `emit({"type": "error", "message": ...})`, adding `"auth": True`
  when the key is rejected. Don't emit `"done"`; `run_chat` does that.

Keys are saved as `"<id>_api_key"` in `config.json`, and the model override as
`"<id>_model"`.

---

## How it fits together

- `server.py` — standard-library Python HTTP server. It serves the page,
  saves and opens the documents in `doc/`, runs the TeX engine, serves the
  PDF, and runs the chat through the provider in `PROVIDERS`.
- `index.html` — the editor page. CodeMirror shows the source and pdf.js
  shows the PDF; both load from cdnjs.
- `examples/openai_compatible.py` — the OpenAI and DeepSeek provider example.
- `requirements.txt` — Python packages for the chat.
- `doc/` — your documents, one `.tex` file each. `build/<name>/` holds each
  document's compiler output and PDF, and `config.json` holds your API keys.
  None of these are shared.

The server only listens on your own computer (127.0.0.1), so nobody else on
your network can open your editor or use your key.

The engine is chosen in this order: `latexmk` with `pdflatex`, `tectonic`,
`pdflatex`, `xelatex`, `lualatex`. The server looks on your PATH, in
`/Library/TeX/texbin`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.cargo/bin`,
and in this folder's `bin/`.
