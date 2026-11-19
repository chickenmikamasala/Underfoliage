#!/usr/bin/env python3
"""Local LaTeX editor backend.

Serves index.html, keeps your documents as .tex files in doc/, compiles the
open one with whatever TeX engine is installed (latexmk, tectonic, pdflatex,
xelatex, lualatex) and serves the resulting PDF. Standard library only, plus
optional AI provider packages for the chat.

    python3 server.py            # http://127.0.0.1:8765
    python3 server.py 9000       # another port
"""
import contextvars
import json
import os
import re
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    import anthropic
    from anthropic import beta_tool
except ImportError:  # Claude chat stays off until `python3 -m pip install anthropic`
    anthropic = None

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types as genai_types
except ImportError:  # Gemini chat stays off until `python3 -m pip install google-genai`
    genai = None

ROOT = os.path.dirname(os.path.abspath(__file__))
DOC_DIR = os.path.join(ROOT, "doc")
BUILD_DIR = os.path.join(ROOT, "build")
STATE = os.path.join(DOC_DIR, ".typeset-state.json")   # which document is open
IMAGE_DIR = os.path.join(DOC_DIR, "images")             # where "Insert image" saves pictures
TEX_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".pdf": "application/pdf"}
MAX_IMAGE_BYTES = 40_000_000
DEFAULT_DOC = "main.tex"
DOC_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}\.tex$")
INDEX = os.path.join(ROOT, "index.html")
ASSETS_DIR = os.path.join(ROOT, "assets")
ASSET_TYPES = {".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml"}
CONFIG = os.path.join(ROOT, "config.json")   # holds the API key; never commit it

CHAT_SYSTEM = """You are the editing assistant inside Underfoliage, a LaTeX editor. The user's open document
is named below, followed by its complete current content. The user sees it in an editor beside
this chat, and the PDF compiled from it.

Make changes with the tools, never by pasting LaTeX into your reply:
- replace_text for targeted edits. old_text must match the document exactly once; include enough
  surrounding lines to make it unique. Prefer several small replace_text calls over one rewrite.
- write_document only when most of the file must change.
- After editing, call compile. If it fails, read the errors, fix the document, and compile again.
- read_document returns the current text with line numbers when you need to re-check it.

Follow the document's existing conventions. Add \\usepackage lines in the preamble when a change
needs a package. If the request is a question or needs no change, answer it without editing.
Keep replies short, one to three sentences: say what you changed or found. Do not repeat the
document or large fragments of it in the reply."""

EXTRA_PATHS = [os.path.join(ROOT, "bin"), "/Library/TeX/texbin", "/opt/homebrew/bin", "/usr/local/bin",
               os.path.expanduser("~/.cargo/bin")]

STARTER = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage{amsmath,amssymb}
\usepackage[margin=1in]{geometry}

\title{A First Document}
\author{Written in the editor}
\date{\today}

\begin{document}
\maketitle

\section{Introduction}
This document is compiled on your own machine. Edit the source on the left,
press \textbf{Compile}, and the PDF appears here. Your AI chatbot can read
and edit this same file.

\section{Some mathematics}
The quadratic formula gives the roots of $ax^2 + bx + c = 0$:
\begin{equation}
  x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}.
\end{equation}

\begin{itemize}
  \item Inline math like $e^{i\pi} + 1 = 0$ works as usual.
  \item So do environments such as \texttt{align}, \texttt{itemize} and \texttt{table}.
\end{itemize}

\end{document}
"""


def which(name):
    """Find an executable on PATH or in EXTRA_PATHS (handles .exe on Windows)."""
    names = [name + ext for ext in (os.environ.get("PATHEXT", ".EXE").split(";") if os.name == "nt" else [""])]
    for d in os.environ.get("PATH", "").split(os.pathsep) + EXTRA_PATHS:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def detect_engine():
    """Return (label, argv builder) for the best installed engine, or None."""
    latexmk, pdflatex = which("latexmk"), which("pdflatex")
    if latexmk and pdflatex:
        return ("latexmk + pdflatex", lambda tex, out: [latexmk, "-pdf", "-interaction=nonstopmode",
                                                         "-file-line-error", "-halt-on-error",
                                                         "-output-directory=" + out, tex])
    tectonic = which("tectonic")
    if tectonic:
        return ("tectonic", lambda tex, out: [tectonic, "--outdir", out, "--keep-logs",
                                              "--keep-intermediates", tex])
    for name in ("pdflatex", "xelatex", "lualatex"):
        p = which(name)
        if p:
            return (name, lambda tex, out, p=p: [p, "-interaction=nonstopmode", "-file-line-error",
                                                 "-halt-on-error", "-output-directory=" + out, tex])
    return None


ERROR_RE = re.compile(r"^(?:\./)?([^:\n]+?\.tex):(\d+):\s*(.*)$")
BANG_RE = re.compile(r"^! (.*)$")
LINE_RE = re.compile(r"^l\.(\d+)\b")
TECTONIC_RE = re.compile(r"^(error|warning): (?:([^:\s]+\.tex):(\d+): )?(.*)$")


def parse_log(text, tex_name=DEFAULT_DOC):
    """Pull (line, message) pairs out of a TeX log / console output.

    Line numbers are kept only for errors in tex_name; errors in \\input files name the file."""
    def located(fname, line, msg):
        if os.path.basename(fname) == tex_name:
            return {"line": int(line), "message": msg.strip()}
        return {"line": None, "message": f"{os.path.basename(fname)}:{line}: {msg.strip()}"}

    errors, warnings = [], []
    lines = text.splitlines()
    pending = None
    for ln in lines:
        m = ERROR_RE.match(ln)
        if m:
            errors.append(located(m.group(1), m.group(2), m.group(3)))
            pending = None
            continue
        m = TECTONIC_RE.match(ln)
        if m:
            entry = (located(m.group(2), m.group(3), m.group(4)) if m.group(2)
                     else {"line": None, "message": m.group(4).strip()})
            (errors if m.group(1) == "error" else warnings).append(entry)
            continue
        m = BANG_RE.match(ln)
        if m:
            pending = {"line": None, "message": m.group(1).strip()}
            errors.append(pending)
            continue
        if pending is not None:
            m = LINE_RE.match(ln)
            if m:
                pending["line"] = int(m.group(1))
                pending = None
        if "LaTeX Warning:" in ln or ln.startswith("Overfull") or ln.startswith("Underfull"):
            warnings.append({"line": None, "message": ln.strip()})
    # de-duplicate while keeping order
    seen, out = set(), []
    for e in errors:
        key = (e["line"], e["message"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    seen_w, warns = set(), []
    for w in warnings:
        if w["message"] not in seen_w:
            seen_w.add(w["message"])
            warns.append(w)
    return out, warns[:50]


def compile_doc(name=None):
    name = name or current_name()
    engine = detect_engine()
    if not engine:
        return {"ok": False, "engine": None, "log": "", "errors": [{
            "line": None,
            "message": "No TeX engine found. Install one (for example `brew install tectonic`, "
                       "or MacTeX / BasicTeX) and reload."}],
            "warnings": [], "duration": 0, "pdf_mtime": None}
    label, argv = engine
    out_dir = build_dir(name)
    pdf = pdf_file(name)
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    try:
        proc = subprocess.run(argv(name, out_dir), cwd=DOC_DIR, capture_output=True, text=True,
                              timeout=420, errors="replace")
        output = proc.stdout + "\n" + proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        output, rc = "Compilation timed out after 420 seconds.", 1
    log_path = log_file(name)
    log_text = ""
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                log_text = f.read()
        except OSError:
            pass
    errors, warnings = parse_log(output + "\n" + log_text, name)
    pdf_ok = os.path.exists(pdf) and os.path.getmtime(pdf) >= t0 - 1
    ok = rc == 0 and pdf_ok
    if not ok and not errors:
        errors = [{"line": None, "message": f"{label} exited with status {rc}; see the log."}]
    return {"ok": ok, "engine": label, "name": name, "log": (output + "\n" + log_text)[-60000:],
            "errors": errors, "warnings": warnings, "duration": round(time.time() - t0, 2),
            "pdf_mtime": os.path.getmtime(pdf) if os.path.exists(pdf) else None}


def pdf_download_name(name=None):
    """File name for a downloaded PDF, taken from the document's \\title{...} when it has one."""
    name = name or current_name()
    try:
        with open(doc_file(name), encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        src = ""
    m = re.search(r"^[^%\n]*\\title\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}", src, re.M)
    title = m.group(1) if m else ""
    title = re.sub(r"\\\\|\\newline\b|\\thanks\{[^{}]*\}", " ", title)   # line breaks, footnotes
    title = re.sub(r"\\[a-zA-Z]+\*?\s*", "", title)                          # other commands
    title = re.sub(r"[{}$~^_\\]", "", title)
    title = re.sub(r'[<>:"/|?*\x00-\x1f]', "", title)                         # not allowed in file names
    title = re.sub(r"\s+", " ", title).strip(" .")[:80]
    return (title or name[:-4]) + ".pdf"


def clean_doc_name(raw):
    """Turn user input or an imported file name into a safe document name like "thesis-ch1.tex".

    Returns None when nothing usable is left. Only letters, digits, - and _ are kept, so names
    never contain paths or characters that trip up TeX."""
    name = os.path.basename((raw or "").strip())
    if name.lower().endswith(".tex"):
        name = name[:-4]
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^A-Za-z0-9_-]", "", name).lstrip("-_")[:80]
    return (name + ".tex") if name else None


def doc_file(name):
    if not DOC_NAME_RE.match(name or ""):
        raise ValueError(f"not a document name: {name!r}")
    return os.path.join(DOC_DIR, name)


def build_dir(name):
    return os.path.join(BUILD_DIR, name[:-4])


def pdf_file(name):
    return os.path.join(build_dir(name), name[:-4] + ".pdf")


def log_file(name):
    return os.path.join(build_dir(name), name[:-4] + ".log")


def doc_title(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            head = f.read(20000)
    except OSError:
        return ""
    m = re.search(r"^[^%\n]*\\title\s*(?:\[[^\]]*\])?\s*\{((?:[^{}]|\{[^{}]*\})*)\}", head, re.M)
    if not m:
        return ""
    t = re.sub(r"\\\\|\\newline\b|\\thanks\{[^{}]*\}", " ", m.group(1))
    t = re.sub(r"\\[a-zA-Z]+\*?\s*", "", t)
    return re.sub(r"\s+", " ", re.sub(r"[{}$~^_\\]", "", t)).strip()[:120]


def list_docs():
    """Every .tex file directly inside doc/, most recently changed first."""
    os.makedirs(DOC_DIR, exist_ok=True)
    docs = []
    for fn in os.listdir(DOC_DIR):
        p = os.path.join(DOC_DIR, fn)
        if DOC_NAME_RE.match(fn) and os.path.isfile(p):
            st = os.stat(p)
            docs.append({"name": fn, "title": doc_title(p), "modified": st.st_mtime})
    docs.sort(key=lambda d: d["modified"], reverse=True)
    return docs


def starter_for(name):
    title = name[:-4].replace("-", " ").replace("_", " ").strip().capitalize() or "Untitled"
    return STARTER.replace(r"\title{A First Document}", "\\title{" + title + "}")


def current_name():
    """The open document. Falls back to main.tex, then the newest .tex file, then a new main.tex."""
    try:
        with open(STATE, encoding="utf-8") as f:
            name = json.load(f).get("current")
        if name and DOC_NAME_RE.match(name) and os.path.isfile(os.path.join(DOC_DIR, name)):
            return name
    except (OSError, ValueError, AttributeError):
        pass
    if os.path.isfile(os.path.join(DOC_DIR, DEFAULT_DOC)):
        return DEFAULT_DOC
    docs = list_docs()
    if docs:
        return docs[0]["name"]
    write_doc(STARTER, DEFAULT_DOC)
    return DEFAULT_DOC


def set_current(name):
    doc_file(name)  # validates
    os.makedirs(DOC_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"current": name}, f)
    os.replace(tmp, STATE)


def read_doc(name=None):
    name = name or current_name()
    path = doc_file(name)
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    return {"name": name, "source": src, "mtime": os.path.getmtime(path), "path": path}


def write_doc(source, name=None):
    name = name or current_name()
    path = doc_file(name)
    os.makedirs(DOC_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(source)
    os.replace(tmp, path)
    return os.path.getmtime(path)


def create_doc(raw_name):
    """Create doc/<name>.tex from the starter template. Returns (name, error)."""
    name = clean_doc_name(raw_name)
    if not name:
        return None, "Give the document a name using letters, digits, - or _."
    if os.path.exists(doc_file(name)):
        return None, f"{name} already exists. Choose another name, or open it from the list."
    write_doc(starter_for(name), name)
    return name, None


def clean_image_stem(raw):
    """A file-name stem LaTeX is happy with: letters, digits, - and _ only (no spaces or extra dots)."""
    stem = os.path.splitext(os.path.basename(raw or ""))[0]
    stem = re.sub(r"\s+", "-", stem)
    stem = re.sub(r"[^A-Za-z0-9_-]", "", stem).strip("-_")[:60]
    return stem or "image"


def unique_image_path(stem, ext):
    os.makedirs(IMAGE_DIR, exist_ok=True)
    path, i = os.path.join(IMAGE_DIR, stem + ext), 2
    while os.path.exists(path):
        path, i = os.path.join(IMAGE_DIR, f"{stem}-{i}{ext}"), i + 1
    return path


def looks_like(ext, data):
    """Check the file really is what its extension says, so the TeX engine never chokes on it."""
    if ext == ".png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if ext in (".jpg", ".jpeg"):
        return data[:3] == b"\xff\xd8\xff"
    if ext == ".pdf":
        return data[:5] == b"%PDF-"
    return False


def convert_image(data, raw_name):
    """Convert a picture LaTeX can't read (HEIC, TIFF, BMP, GIF, WebP...) to PNG or JPG bytes.

    Returns (bytes, ext) or raises ValueError with a message for the user. Uses Pillow when it is
    installed (plus pillow-heif for HEIC), and macOS's built-in `sips` tool as a fallback."""
    errors = []
    try:
        from PIL import Image
        try:
            import pillow_heif                     # optional: HEIC/HEIF photos from iPhones
            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        import io
        im = Image.open(io.BytesIO(data))
        im.seek(0)                                  # first frame of an animated GIF/WebP
        im.info.pop("icc_profile", None)            # the TeX engine ignores it and only warns about it
        photo = im.format in ("HEIF", "HEIC", "AVIF", "TIFF", "MPO") and "A" not in im.getbands()
        out = io.BytesIO()
        if photo:
            im.convert("RGB").save(out, "JPEG", quality=92)
            return out.getvalue(), ".jpg"
        im.convert("RGBA" if "A" in im.getbands() or im.mode == "P" else "RGB").save(out, "PNG", optimize=True)
        return out.getvalue(), ".png"
    except ImportError:
        errors.append("Pillow is not installed")
    except Exception as e:
        errors.append(f"Pillow could not read it ({type(e).__name__})")
    if sys.platform == "darwin" and shutil.which("sips"):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in" + (os.path.splitext(raw_name)[1].lower() or ".img"))
            dst = os.path.join(tmp, "out.png")
            with open(src, "wb") as f:
                f.write(data)
            r = subprocess.run(["sips", "-s", "format", "png", src, "--out", dst], capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(dst):
                with open(dst, "rb") as f:
                    return f.read(), ".png"
            errors.append("macOS could not convert it")
    raise ValueError("This image format could not be converted (" + "; ".join(errors) + "). "
                     "Save it as PNG or JPG and add it again, or run: python3 -m pip install pillow pillow-heif")


def save_image(data, raw_name):
    """Store an uploaded image in doc/images/. Returns its path relative to doc/, e.g. images/plot.png."""
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"The image is larger than {MAX_IMAGE_BYTES // 1_000_000} MB.")
    ext = os.path.splitext(raw_name or "")[1].lower()
    if ext not in TEX_IMAGE_TYPES or not looks_like(ext, data):
        data, ext = convert_image(data, raw_name or "image")
    if ext == ".jpeg":
        ext = ".jpg"
    path = unique_image_path(clean_image_stem(raw_name), ext)
    with open(path, "wb") as f:
        f.write(data)
    return os.path.relpath(path, DOC_DIR).replace(os.sep, "/")


def list_images():
    """Images the documents can use: everything in doc/images/ plus any PNG/JPG/PDF directly in doc/."""
    found = []
    for folder in (IMAGE_DIR, DOC_DIR):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for fn in names:
            p = os.path.join(folder, fn)
            if os.path.splitext(fn)[1].lower() in TEX_IMAGE_TYPES and not fn.startswith(".") and os.path.isfile(p):
                st = os.stat(p)
                found.append({"path": os.path.relpath(p, DOC_DIR).replace(os.sep, "/"),
                              "name": fn, "size": st.st_size, "modified": st.st_mtime,
                              "kind": "pdf" if fn.lower().endswith(".pdf") else "image"})
    found.sort(key=lambda d: d["modified"], reverse=True)
    return found


def doc_image_file(rel):
    """Resolve an image path from the page (e.g. images/plot.png) to a file inside doc/, or None."""
    rel = (rel or "").replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/") or any(part.startswith(".") for part in rel.split("/")):
        return None
    path = os.path.realpath(os.path.join(DOC_DIR, rel))
    if not path.startswith(os.path.realpath(DOC_DIR) + os.sep):
        return None
    if os.path.splitext(path)[1].lower() not in TEX_IMAGE_TYPES or not os.path.isfile(path):
        return None
    return path


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def clean_key(raw):
    """Tidy a pasted key: drop whitespace, surrounding quotes and a leading NAME= or export NAME=."""
    key = (raw or "").strip()
    key = re.sub(r"^(export\s+)?[A-Z_]*(API_KEY|AUTH_TOKEN)\s*[:=]\s*", "", key)
    key = key.strip().strip("'\"`").strip()
    return re.sub(r"\s+", "", key)


def detect_key_provider(key):
    """Guess which provider a pasted key belongs to, from PROVIDERS[...]["prefixes"].

    Returns (provider_id, recognised). Prefixes are only a hint: key formats change (Google moved
    from AIza to AQ. keys in 2026), so an unfamiliar key goes to FALLBACK_PROVIDER and the
    provider's own check on save decides whether it works."""
    candidates = [(len(pre), pid) for pid, prov in PROVIDERS.items() for pre in prov["prefixes"] if key.startswith(pre)]
    if candidates:
        return max(candidates)[1], True       # longest matching prefix wins (sk-ant- beats sk-)
    return FALLBACK_PROVIDER, False


def verify_key(provider, key, model):
    """Check a key with its provider before saving. Returns (ok, message).

    ok is True (works), False (rejected) or None (could not check, e.g. offline or package missing)."""
    prov = PROVIDERS[provider]
    if not prov["available"]():
        return None, f"Saved. Install {prov['package']} to use it: python3 -m pip install {prov['package']}"
    try:
        return prov["verify"](key, model)
    except Exception as e:
        return None, f"Saved, but the key could not be checked right now ({type(e).__name__})."


def saved_key(cfg, provider):
    key = cfg.get(f"{provider}_api_key") or ""
    if not key and provider == "claude":
        key = cfg.get("anthropic_api_key") or ""     # name used by earlier versions
    return key.strip()


def chat_settings():
    """Return dict(provider, source, key, model). provider is None when nothing is configured.

    A key saved in config.json wins over the environment. When several keys are saved,
    config.json's "provider" picks one (the most recently saved key by default)."""
    cfg = load_config()
    order = [cfg["provider"]] if cfg.get("provider") in PROVIDERS else []
    order += [p for p in PROVIDERS if p not in order]
    for p in order:
        key = saved_key(cfg, p)
        if key:
            return {"provider": p, "source": "config.json", "key": key, "model": model_for(p, cfg)}
    for p in order:
        for var in PROVIDERS[p]["env"]:
            if os.environ.get(var):
                return {"provider": p, "source": "environment", "key": os.environ[var], "model": model_for(p, cfg)}
    return {"provider": None, "source": None, "key": None, "model": None}


def model_for(provider, cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return (cfg.get(f"{provider}_model") or PROVIDERS[provider]["default_model"]).strip()


def sdk_available(provider):
    return PROVIDERS[provider]["available"]()


# Tool schemas, shared by every provider (Claude builds its own from the function signatures).
TOOL_SPECS = [
    {"name": "read_document",
     "description": 'Return the current content of the document with line numbers (format: "12| text").',
     "parameters": {"type": "object", "properties": {}}},
    {"name": "replace_text",
     "description": "Replace one exact occurrence of old_text in the document with new_text.",
     "parameters": {"type": "object", "properties": {
         "old_text": {"type": "string", "description": "Text that occurs exactly once in the document, copied verbatim (keep its indentation and line breaks)."},
         "new_text": {"type": "string", "description": "The replacement text. Use an empty string to delete old_text."}},
         "required": ["old_text", "new_text"]}},
    {"name": "write_document",
     "description": "Replace the entire content of the document.",
     "parameters": {"type": "object", "properties": {
         "content": {"type": "string", "description": "The complete new LaTeX source, from \\documentclass to \\end{document}."}},
         "required": ["content"]}},
    {"name": "compile",
     "description": "Compile the document with the installed TeX engine and report errors and warnings.",
     "parameters": {"type": "object", "properties": {}}},
]


CHAT_DOC = contextvars.ContextVar("chat_doc", default=None)   # document the current chat turn edits


def make_tool_funcs(events, name=None):
    """Plain editing functions for one chat turn. Each call appends a result summary to `events`.

    They act on `name`, or on the document that was open when the chat message was sent."""
    name = name or CHAT_DOC.get() or current_name()

    def note(ok, summary):
        events.append({"ok": ok, "summary": summary})

    def read_document() -> str:
        """Return the current content of the document with line numbers (format: "12| text")."""
        src = read_doc(name)["source"]
        note(True, "Read the document")
        return "\n".join(f"{i + 1}| {ln}" for i, ln in enumerate(src.splitlines()))

    def replace_text(old_text: str, new_text: str) -> str:
        """Replace one exact occurrence of old_text in the document with new_text.

        Args:
            old_text: Text that occurs exactly once in the document, copied verbatim (keep its indentation and line breaks).
            new_text: The replacement text. Use an empty string to delete old_text.
        """
        src = read_doc(name)["source"]
        n = src.count(old_text) if old_text else 0
        if n == 0:
            note(False, "replace_text: old_text not found")
            return "ERROR: old_text was not found in the document. Call read_document and copy the text exactly."
        if n > 1:
            note(False, f"replace_text: old_text occurs {n} times")
            return f"ERROR: old_text occurs {n} times; include more surrounding text so it is unique."
        new_src = src.replace(old_text, new_text, 1)
        write_doc(new_src, name)
        first = next((ln.strip() for ln in new_text.splitlines() if ln.strip()), "") or "(deleted)"
        note(True, f"Edited {name}: " + (first[:60] + ("…" if len(first) > 60 else "")))
        return f"OK. Replaced {len(old_text)} characters with {len(new_text)}. The document is now {len(new_src.splitlines())} lines."

    def write_document(content: str) -> str:
        """Replace the entire content of the document.

        Args:
            content: The complete new LaTeX source, from \\documentclass to \\end{document}.
        """
        write_doc(content, name)
        note(True, f"Rewrote {name} ({len(content.splitlines())} lines)")
        return f"OK. Wrote {len(content.splitlines())} lines."

    def compile() -> str:
        """Compile the document with the installed TeX engine and report errors and warnings."""
        r = compile_doc(name)
        if r["ok"]:
            note(True, f"Compiled OK in {r['duration']}s")
            warns = r.get("warnings") or []
            return "Compiled successfully." + (f" {len(warns)} warnings: " + "; ".join(w["message"] for w in warns[:5]) if warns else "")
        errs = r.get("errors") or []
        first = errs[0]["message"] if errs else "unknown error"
        note(False, "Compile failed: " + first[:70])
        lines = [f"line {e['line']}: {e['message']}" if e.get("line") else e["message"] for e in errs[:10]]
        return "Compilation FAILED.\n" + "\n".join(lines) + "\n\nLog tail:\n" + (r.get("log") or "")[-2500:]

    return {"read_document": read_document, "replace_text": replace_text,
            "write_document": write_document, "compile": compile}


def clean_history(history):
    return [{"role": m["role"], "content": m["content"]} for m in history
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()]


def run_chat(history, emit, name=None):
    """Run one chat turn with whichever provider is configured. `emit(event)` streams to the page."""
    token = None
    try:
        name = name if name and DOC_NAME_RE.match(name) and os.path.isfile(os.path.join(DOC_DIR, name)) else current_name()
        token = CHAT_DOC.set(name)
        cfg = chat_settings()
        provider = cfg["provider"]
        if provider is None:
            emit({"type": "error", "message": "No API key configured yet."})
            return
        if not sdk_available(provider):
            pkg = PROVIDERS[provider]["package"]
            emit({"type": "error", "message": f"The {pkg} package is not installed. Run: python3 -m pip install {pkg}, then restart server.py."})
            return
        msgs = clean_history(history)
        if not msgs or msgs[-1]["role"] != "user":
            emit({"type": "error", "message": "The last message must come from you."})
            return
        imgs = [i["path"] for i in list_images()][:50]
        img_note = ("\n\nImage files the document can include with \\includegraphics (paths relative to it): "
                    + ", ".join(imgs) + ". Add \\usepackage{graphicx} to the preamble if it is missing.") if imgs else ""
        system = CHAT_SYSTEM + img_note + f"\n\nDocument: {name}\n\nCurrent content:\n\n" + read_doc(name)["source"]
        PROVIDERS[provider]["run"](cfg, msgs, system, emit)
    except Exception as e:  # keep the stream well-formed whatever happens
        emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        if token is not None:
            CHAT_DOC.reset(token)
        emit({"type": "done"})


def run_claude(cfg, msgs, system, emit):
    client = anthropic.Anthropic(api_key=cfg["key"]) if cfg["key"] else anthropic.Anthropic()
    results = []
    tools = [beta_tool(fn) for fn in make_tool_funcs(results).values()]
    # Newer models take an effort level and server-side refusal fallbacks; some older ones reject
    # them. If the first request is refused because of them, try once more without.
    extras = {"output_config": {"effort": "medium"},
              "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        for attempt in range(2):
            got_reply = False
            try:
                runner = client.beta.messages.tool_runner(
                    model=cfg["model"],
                    max_tokens=16000,
                    system=system,
                    tools=tools,
                    messages=msgs,
                    max_iterations=16,
                    **extras,
                )
                for message in runner:
                    got_reply = True
                    flush_results(results, emit)
                    if message.stop_reason == "refusal":
                        emit({"type": "error", "message": "Claude declined this request."})
                    for block in message.content:
                        if block.type == "text" and block.text.strip():
                            emit({"type": "text", "text": block.text.strip()})
                        elif block.type == "tool_use":
                            emit({"type": "tool", "name": block.name, "summary": tool_summary(block.name, block.input)})
                flush_results(results, emit)
                return
            except anthropic.BadRequestError as e:
                text = str(getattr(e, "message", e)).lower()
                if attempt == 0 and not got_reply and extras and any(w in text for w in ("effort", "output_config", "fallback", "beta")):
                    extras = {}
                    continue
                raise
    except anthropic.AuthenticationError:
        emit({"type": "error", "message": "The Claude API key was rejected. Check it and save it again.", "auth": True})
    except anthropic.RateLimitError:
        emit({"type": "error", "message": "Rate limited by the Claude API. Wait a moment and try again."})
    except anthropic.APIStatusError as e:
        emit({"type": "error", "message": f"Claude API error {e.status_code}: {getattr(e, 'message', str(e))}"})
    except anthropic.APIConnectionError:
        emit({"type": "error", "message": "Could not reach the Claude API. Check your internet connection."})


def run_gemini(cfg, msgs, system, emit, max_steps=16):
    client = genai.Client(api_key=cfg["key"])
    results = []
    funcs = make_tool_funcs(results)
    config = genai_types.GenerateContentConfig(
        system_instruction=system,
        tools=[genai_types.Tool(function_declarations=[
            genai_types.FunctionDeclaration(name=t["name"], description=t["description"],
                                            parameters_json_schema=t["parameters"])
            for t in TOOL_SPECS])],
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
        max_output_tokens=16000,
    )
    contents = [genai_types.Content(role="user" if m["role"] == "user" else "model",
                                    parts=[genai_types.Part.from_text(text=m["content"])])
                for m in msgs]
    try:
        for _ in range(max_steps):
            resp = client.models.generate_content(model=cfg["model"], contents=contents, config=config)
            cand = resp.candidates[0] if resp.candidates else None
            if cand is None or cand.content is None:
                fb = getattr(resp, "prompt_feedback", None)
                reason = getattr(fb, "block_reason", None) or getattr(cand, "finish_reason", None) or "no response"
                emit({"type": "error", "message": f"Gemini returned no answer ({reason})."})
                return
            for part in cand.content.parts or []:
                if part.text and not getattr(part, "thought", False) and part.text.strip():
                    emit({"type": "text", "text": part.text.strip()})
            calls = resp.function_calls or []
            # Keep the model turn exactly as returned: it carries the thought signatures
            # Gemini needs to continue a tool-calling turn.
            contents.append(cand.content)
            if not calls:
                return
            reply_parts = []
            for fc in calls:
                args = dict(fc.args or {})
                emit({"type": "tool", "name": fc.name, "summary": tool_summary(fc.name, args)})
                fn = funcs.get(fc.name)
                if fn is None:
                    result = f"ERROR: unknown tool {fc.name!r}."
                    results.append({"ok": False, "summary": f"Unknown tool {fc.name}"})
                else:
                    try:
                        result = fn(**args)
                    except TypeError as e:
                        result = f"ERROR: bad arguments for {fc.name}: {e}"
                        results.append({"ok": False, "summary": f"{fc.name}: bad arguments"})
                flush_results(results, emit)
                reply_parts.append(genai_types.Part(function_response=genai_types.FunctionResponse(
                    id=fc.id, name=fc.name, response={"result": result})))
            contents.append(genai_types.Content(role="user", parts=reply_parts))
        emit({"type": "error", "message": f"Stopped after {max_steps} steps. Ask again to continue."})
    except genai_errors.ClientError as e:
        msg = (getattr(e, "message", None) or str(e)).strip()
        code = getattr(e, "code", None)
        if code in (401, 403) or "API key" in msg or "API_KEY" in msg:
            emit({"type": "error", "message": "The Gemini API key was rejected. Check it and save it again.", "auth": True})
        elif code == 429:
            emit({"type": "error", "message": "Gemini rate limit or quota reached. Wait a moment, or check your quota in Google AI Studio."})
        elif code == 404:
            emit({"type": "error", "message": f"Gemini model {cfg['model']!r} was not found. Set \"gemini_model\" in config.json to a current model name."})
        else:
            emit({"type": "error", "message": f"Gemini API error {code}: {msg}"})
    except genai_errors.ServerError as e:
        emit({"type": "error", "message": f"Gemini server error {getattr(e, 'code', '')}. Try again in a moment."})


def flush_results(results, emit):
    for r in results:
        emit({"type": "tool_result", "ok": r["ok"], "summary": r["summary"]})
    results.clear()


def verify_gemini(key, model):
    client = genai.Client(api_key=key)
    try:
        client.models.get(model=model)
        return True, f"Key works. Using {model}."
    except genai_errors.ClientError as e:
        msg = (getattr(e, "message", None) or str(e))
        if getattr(e, "code", None) == 404:
            names = []
            try:
                for m in client.models.list():
                    n = (m.name or "").removeprefix("models/")
                    if n.startswith("gemini") and "generateContent" in (m.supported_actions or []):
                        names.append(n)
            except Exception:
                pass
            hint = (" Available: " + ", ".join(sorted(names)[:12])) if names else ""
            return True, f"Key works, but the model {model!r} was not found. Set \"gemini_model\" in config.json.{hint}"
        if getattr(e, "code", None) in (400, 401, 403):
            return False, "Google rejected this key: " + msg.strip()[:200]
        raise


def verify_claude(key, model):
    client = anthropic.Anthropic(api_key=key)
    try:
        client.models.retrieve(model)
        return True, f"Key works. Using {model}."
    except anthropic.AuthenticationError:
        return False, "Anthropic rejected this key."
    except anthropic.NotFoundError:
        return True, f"Key works, but the model {model!r} was not found. Set \"claude_model\" in config.json."


# Gemini models the API lists that are not for chatting (embeddings, speech, images, live audio...).
GEMINI_NON_CHAT = ("embedding", "tts", "image", "live", "transcribe", "audio", "robotics", "aqa",
                   "imagen", "veo", "lyria", "banana", "computer-use", "deep-research")


def models_gemini(key):
    """Models this Gemini key can chat with, newest first."""
    client = genai.Client(api_key=key)   # keep a reference: the list is fetched page by page
    out = []
    for m in client.models.list():
        mid = (m.name or "").removeprefix("models/")
        if not mid.startswith("gemini") or "generateContent" not in (m.supported_actions or []):
            continue
        if any(w in mid for w in GEMINI_NON_CHAT):
            continue
        out.append({"id": mid, "name": m.display_name or mid})
    def newest_first(d):
        m = re.match(r"gemini-(\d+(?:\.\d+)?)", d["id"])
        return (-(float(m.group(1)) if m else 0.0), d["id"])     # unnumbered previews go last
    out.sort(key=newest_first)
    return out


def models_claude(key):
    """Active Claude models this key can use, newest first (as the API returns them)."""
    client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    return [{"id": m.id, "name": m.display_name or m.id} for m in client.models.list(lifecycle=["active"])]


MODEL_CACHE = {}           # (provider, key) -> (time, models); lists change rarely
MODEL_CACHE_SECONDS = 600
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


def available_models(provider, key, refresh=False):
    """Ask the provider which models this key may use. Raises on network or key problems."""
    cached = MODEL_CACHE.get((provider, key))
    if cached and not refresh and time.time() - cached[0] < MODEL_CACHE_SECONDS:
        return cached[1]
    models = PROVIDERS[provider]["models"](key)
    MODEL_CACHE[(provider, key)] = (time.time(), models)
    return models


# ---------------------------------------------------------------------------------------------
# AI providers. To add one, write a run_<id>() and verify_<id>() like the ones above and add an
# entry here; the key form, status line and config handling all read from this table.
# See "Adding another AI provider" in README.md.
# ---------------------------------------------------------------------------------------------
PROVIDERS = {
    "gemini": {
        "name": "Gemini",                         # shown in the chat drawer and the provider menu
        "prefixes": ("AQ.", "AIza"),              # key prefixes used to guess the provider ("" = none)
        "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),  # environment variables that may hold a key
        "default_model": "gemini-4-hydrogen",     # override with "gemini_model" in config.json
        "package": "google-genai",                # pip package the code below needs
        "available": lambda: genai is not None,
        "verify": verify_gemini,                  # (key, model) -> (ok, message)
        "run": run_gemini,                        # (cfg, msgs, system, emit) -> None
        "models": models_gemini,                  # (key) -> [{"id", "name"}] for the model picker
    },
    "claude": {
        "name": "Claude",
        "prefixes": ("sk-ant-",),
        "env": ("ANTHROPIC_API_KEY",),
        "default_model": "claude-opus-5-5",
        "package": "anthropic",
        "available": lambda: anthropic is not None,
        "verify": verify_claude,
        "run": run_claude,
        "models": models_claude,
    },
}
FALLBACK_PROVIDER = "gemini"   # used when a key's prefix matches no provider


def tool_summary(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "replace_text":
        old = (inp.get("old_text") or "").strip().splitlines()
        return "Editing: " + ((old[0][:50] + ("…" if len(old) > 1 or len(old[0]) > 50 else "")) if old else "…")
    if name == "write_document":
        return "Rewriting the whole document"
    if name == "compile":
        return "Compiling"
    if name == "read_document":
        return "Reading the document"
    return name


class Handler(BaseHTTPRequestHandler):
    server_version = "Underfoliage/1.0"

    def log_message(self, fmt, *args):
        line = fmt % args
        if "/api/doc" not in line:  # the editor polls this every 1.5 s; keep the log readable
            sys.stderr.write("%s - %s\n" % (self.address_string(), line))
            sys.stderr.flush()

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype, download_name=None):
        if not os.path.exists(path):
            self.send_json({"error": "not found"}, 404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if download_name:
            ascii_name = download_name.encode("ascii", "ignore").decode() or "document.pdf"
            self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"; '
                                                    f"filename*=UTF-8''{quote(download_name)}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_file(INDEX, "text/html; charset=utf-8")
        elif path == "/api/doc":
            self.send_json(read_doc())
        elif path == "/api/docs":
            self.send_json({"current": current_name(), "folder": DOC_DIR, "docs": list_docs()})
        elif path == "/api/status":
            eng = detect_engine()
            name = current_name()
            pdf = pdf_file(name)
            self.send_json({"engine": eng[0] if eng else None, "name": name, "path": doc_file(name),
                            "pdf_mtime": os.path.getmtime(pdf) if os.path.exists(pdf) else None})
        elif path == "/api/chat/status":
            cfg = chat_settings()
            p = cfg["provider"]
            self.send_json({"configured": p is not None, "provider": p,
                            "provider_name": PROVIDERS[p]["name"] if p else "", "source": cfg["source"],
                            "model": cfg["model"], "sdk": sdk_available(p) if p else True,
                            "package": PROVIDERS[p]["package"] if p else None,
                            "providers": [{"id": k, "name": v["name"]} for k, v in PROVIDERS.items()]})
        elif path == "/favicon.ico" or path.startswith("/assets/"):
            fname = "favicon.ico" if path == "/favicon.ico" else path[len("/assets/"):]
            ext = os.path.splitext(fname)[1].lower()
            # Only plain file names directly inside assets/ with an image type; nothing else is served.
            if "/" in fname or "\\" in fname or fname.startswith(".") or ext not in ASSET_TYPES:
                self.send_json({"error": "not found"}, 404)
            else:
                self.send_file(os.path.join(ASSETS_DIR, fname), ASSET_TYPES[ext])
        elif path == "/api/images":
            self.send_json({"folder": IMAGE_DIR, "images": list_images()})
        elif path == "/api/chat/models":
            cfg = chat_settings()
            p = cfg["provider"]
            if not p:
                self.send_json({"error": "Add an API key first."}, 400)
                return
            info = {"provider": p, "provider_name": PROVIDERS[p]["name"], "current": cfg["model"],
                    "default": PROVIDERS[p]["default_model"], "models": []}
            if "models" not in PROVIDERS[p] or not sdk_available(p):
                info["error"] = f"{PROVIDERS[p]['name']} can't list its models here; type a model name instead."
            else:
                refresh = (parse_qs(urlparse(self.path).query).get("refresh") or ["0"])[0] == "1"
                try:
                    info["models"] = available_models(p, cfg["key"], refresh)
                except Exception as e:
                    info["error"] = f"Could not get the model list from {PROVIDERS[p]['name']} ({type(e).__name__}). You can still type a model name."
            self.send_json(info)
        elif path == "/doc-image":
            rel = (parse_qs(urlparse(self.path).query).get("path") or [""])[0]
            f = doc_image_file(rel)
            if not f:
                self.send_json({"error": "not found"}, 404)
            else:
                self.send_file(f, TEX_IMAGE_TYPES[os.path.splitext(f)[1].lower()])
        elif path == "/pdf":
            self.send_file(pdf_file(current_name()), "application/pdf")
        elif path == "/download/pdf":
            name = current_name()
            self.send_file(pdf_file(name), "application/pdf", download_name=pdf_download_name(name))
        elif path == "/log":
            self.send_file(log_file(current_name()), "text/plain; charset=utf-8")
        else:
            self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/doc":
            body = self.read_body()
            if not isinstance(body.get("source"), str):
                self.send_json({"error": "source must be a string"}, 400)
                return
            name = body.get("name") or current_name()
            if not DOC_NAME_RE.match(name):
                self.send_json({"error": "bad document name"}, 400)
                return
            # Saving is tied to the document the editor had open, so a save that arrives just
            # after switching documents can never land in the new one.
            self.send_json({"name": name, "mtime": write_doc(body["source"], name)})
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/images":
            # Raw file body; the original file name comes in the X-Filename header (URL-encoded).
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_IMAGE_BYTES:
                self.send_json({"error": f"The image is larger than {MAX_IMAGE_BYTES // 1_000_000} MB."}, 413)
                return
            data = self.rfile.read(n) if n else b""
            raw_name = unquote(self.headers.get("X-Filename") or "image")
            try:
                rel = save_image(data, raw_name)
            except ValueError as e:
                self.send_json({"error": str(e)}, 400)
                return
            self.send_json({"path": rel, "images": list_images()})
        elif path == "/api/chat/model":
            cfg = chat_settings()
            p = cfg["provider"]
            model = (self.read_body().get("model") or "").strip()
            if not p:
                self.send_json({"error": "Add an API key first."}, 400)
                return
            if model and not MODEL_ID_RE.match(model):
                self.send_json({"error": "That doesn't look like a model name."}, 400)
                return
            saved = load_config()
            if not model or model == PROVIDERS[p]["default_model"]:
                saved.pop(f"{p}_model", None)      # back to the default
            else:
                saved[f"{p}_model"] = model
            save_config(saved)
            self.send_json({"provider": p, "model": model_for(p, saved)})
        elif path == "/api/chat/key":
            body = self.read_body()
            key = clean_key(body.get("key"))
            chosen = body.get("provider") if body.get("provider") in PROVIDERS else None
            detected, recognised = detect_key_provider(key)
            provider = chosen or detected
            if len(key) < 20:
                self.send_json({"error": "That key looks too short. Copy the whole key and paste it again."}, 400)
                return
            cfg = load_config()
            ok, note = verify_key(provider, key, model_for(provider, cfg))
            if ok is False:
                if not chosen and not recognised:
                    note += " If this key is for another provider, choose it from the menu and save again."
                self.send_json({"error": note, "needs_provider": not chosen and not recognised}, 400)
                return
            cfg[f"{provider}_api_key"] = key
            if provider == "claude":
                cfg.pop("anthropic_api_key", None)   # name used by earlier versions
            cfg["provider"] = provider
            save_config(cfg)
            self.send_json({"configured": True, "provider": provider, "verified": ok, "note": note})
        elif path == "/api/chat":
            body = self.read_body()
            history = body.get("messages") if isinstance(body.get("messages"), list) else []
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(ev):
                try:
                    self.wfile.write(("data: " + json.dumps(ev) + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            run_chat(history, emit, body.get("name"))
        elif path == "/api/compile":
            body = self.read_body()
            name = current_name()
            if isinstance(body.get("source"), str) and body.get("name") in (None, name):
                write_doc(body["source"], name)
            result = compile_doc(name)
            result["mtime"] = os.path.getmtime(doc_file(name))
            self.send_json(result)
        elif path == "/api/docs/open":
            name = self.read_body().get("name") or ""
            if not DOC_NAME_RE.match(name) or not os.path.isfile(os.path.join(DOC_DIR, name)):
                self.send_json({"error": f"{name or 'That document'} was not found in the documents folder."}, 404)
                return
            set_current(name)
            self.send_json(read_doc(name))
        elif path == "/api/docs/new":
            name, err = create_doc(self.read_body().get("name"))
            if err:
                self.send_json({"error": err}, 400)
                return
            set_current(name)
            self.send_json(read_doc(name))
        else:
            self.send_json({"error": "not found"}, 404)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    name = current_name()  # creates doc/main.tex on first run
    eng = detect_engine()
    print(f"Underfoliage:    http://127.0.0.1:{port}", flush=True)
    print(f"Documents:    {DOC_DIR}  (open: {name})")
    print(f"Engine:       {eng[0] if eng else 'none found (install tectonic or MacTeX)'}", flush=True)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):  # address already in use
            print(f"Underfoliage is already running on http://127.0.0.1:{port} -- open that in your browser.",
                  flush=True)
            print(f"To run a second copy use another port:  python3 server.py {port + 1}", flush=True)
            sys.exit(0)
        raise
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
