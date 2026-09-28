#!/usr/bin/env python3
"""Knowledge Galaxy server.

    python3 server.py        ->  http://localhost:4700

GET  /...          static files from viewer/ only (nothing else on disk is reachable)
POST /chat         {"question": "...", "session": "..."} -> {"answer": "...", "nodes": [ids]}
POST /chat/reset   {"session": "..."} clears that browser's conversation history

The brain is Claude Opus 5.5 (Anthropic Messages API). The key lives in config.json in the
project root. It is read on every request (so you can paste it in without restarting) and is
never sent to the browser.
Standard library only.
"""
import json
import math
import mimetypes
import os
import re
import socket
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter, OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
VIEWER_DIR = (ROOT / "viewer").resolve()
CONFIG_PATH = ROOT / "config.json"
INDEX_PATH = ROOT / "notes-index.json"
HOST, PORT = "127.0.0.1", 4700

PLACEHOLDER_KEY = "PUT-YOUR-KEY-HERE"
DEFAULT_CONFIG = {"anthropic_api_key": PLACEHOLDER_KEY, "model": "claude-opus-5-5"}
# Raw HTTP on purpose: server.py is standard-library only, so no `anthropic` SDK.
ANTHROPIC_URL = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
FALLBACK_BETA = "server-side-fallback-2026-07-01"  # a declined request is retried on the model Anthropic recommends
REQUEST_TIMEOUT = 90
MAX_TOKENS = 16000        # thinking is always on for Opus 5.5 and counts toward this, so leave room
EFFORT = "low"            # short grounded answers, spoken aloud: favour a fast first word; try "medium" if answers feel thin

TOP_K = 6                 # notes sent to the model per question
NOTE_CHARS = 3000         # max characters of each note sent to the model
HISTORY_MESSAGES = 8      # last 4 question/answer pairs kept per browser session
MAX_SESSIONS = 200
MAX_BODY = 16 * 1024
MAX_QUESTION = 1000

STOPWORDS = set("""
a about above after again against all also am an and any are aren as at be because been before being below
between both but by can cannot could did didn do does doesn doing don down during each few for from further
had has have having he her here hers herself him himself his how i if in into is isn it its itself just let
me more most my myself no nor not now of off on once only or other ought our ours ourselves out over own
please same she should so some such than that the their theirs them themselves then there these they this
those through to too under until up us very was we were what when where which while who whom why will with
would you your yours yourself yourselves tell know give show explain describe anything something thing things
much many get got going go want need like think say said notes note
""".split())
WORD = re.compile(r"[^\W_]+")


# ---------------------------------------------------------------- retrieval

def stem(word):
    """Tiny suffix stripper so 'prices', 'priced' and 'pricing' all meet at 'pric'."""
    w = word
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
    elif len(w) > 4 and w.endswith("ed"):
        w = w[:-2]
    elif len(w) > 4 and (w.endswith(("ches", "shes", "sses", "xes", "zes"))):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if len(w) > 4 and len(w) != len(word) and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]                                   # shipping -> shipp -> ship
    if len(w) > 3 and w.endswith("e"):
        w = w[:-1]
    return w


def terms(text):
    return [stem(w) for w in WORD.findall(text.lower()) if w not in STOPWORDS and (len(w) > 1 or w.isdigit())]


class NoteIndex:
    """Full note text written by build.py; reloaded automatically when it changes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mtime = None
        self.notes = []
        self.idf = {}

    def load(self):
        try:
            mtime = INDEX_PATH.stat().st_mtime
        except FileNotFoundError:
            raise RuntimeError("notes-index.json is missing. Run `python3 build.py` first.")
        with self.lock:
            if mtime != self.mtime:
                data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
                notes = []
                for n in data["notes"]:
                    notes.append({
                        **n,
                        "tf": Counter(terms(n["label"] + "\n" + n["text"])),
                        "title_terms": set(terms(n["label"])),
                        "title_phrase": " ".join(WORD.findall(n["label"].lower())),
                    })
                df = Counter()
                for n in notes:
                    df.update(n["tf"].keys())
                total = max(len(notes), 1)
                self.idf = {t: math.log(1 + total / c) for t, c in df.items()}
                self.notes, self.mtime = notes, mtime
            return self.notes, self.idf


INDEX = NoteIndex()


def score_notes(question, notes, idf):
    """Keyword overlap, idf-weighted, with title matches counting triple."""
    q_terms = set(terms(question))
    q_phrase = " " + " ".join(WORD.findall(question.lower())) + " "
    scores = []
    for n in notes:
        s = 0.0
        for t in q_terms:
            tf = n["tf"].get(t, 0)
            if tf:
                s += idf.get(t, 1.0) * (1 + math.log(tf))
            if t in n["title_terms"]:
                s += 3 * idf.get(t, 1.0)
        if len(n["title_phrase"]) > 3 and f" {n['title_phrase']} " in q_phrase:
            s += 8                                    # the whole title appears in the question
        scores.append(s)
    return scores


def retrieve(question, previous_question=None):
    notes, idf = INDEX.load()
    scores = score_notes(question, notes, idf)
    if previous_question:                              # follow-ups ("what about in November?") lean on the last question
        for i, s in enumerate(score_notes(previous_question, notes, idf)):
            scores[i] += 0.5 * s
    ranked = sorted(range(len(notes)), key=lambda i: scores[i], reverse=True)
    return [notes[i] for i in ranked[:TOP_K] if scores[i] > 0]


# ---------------------------------------------------------------- the brain

SYSTEM_PROMPT = """You are the memory of the user's personal notes. Answer ONLY from the notes provided below. \
Never use outside knowledge, and never guess.
Answer in two or three sentences, plainly. If the notes do not cover the question, say so plainly \
(for example: "Your notes don't cover that.") instead of answering.
Your answer is read aloud, so write natural spoken sentences: no markdown, bullet points or tables.
Reply with a single JSON object and nothing else:
{"answer": "<your two or three sentences>", "used": [<id numbers of the notes you actually relied on>]}
Use "used": [] when the notes don't cover it.

NOTES:
"""


def build_request(question, notes, history):
    """System prompt carrying this question's notes, plus the short text-only history.

    History holds only plain question/answer text (never thinking blocks), so the fresh notes
    in the system prompt each turn don't trip Opus 5.5's preserved-thinking prefix check."""
    blocks = []
    for n in notes:
        text = n["text"] if len(n["text"]) <= NOTE_CHARS else n["text"][:NOTE_CHARS] + " …"
        blocks.append(f"[id {n['id']}] {n['label']} (folder: {n['group']})\n{text}")
    context = "\n\n---\n\n".join(blocks) if blocks else "(no notes matched this question)"
    return SYSTEM_PROMPT + context, [*history, {"role": "user", "content": question}]


class ChatError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def load_config():
    if not CONFIG_PATH.exists():
        raise ChatError(500, "config.json is missing from the project root. Restart server.py to recreate it.")
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ChatError(500, f"config.json isn't valid JSON ({e}). Check the quotes and commas.")
    if not isinstance(cfg, dict):
        raise ChatError(500, "config.json should be a JSON object with anthropic_api_key and model.")
    if "anthropic_api_key" not in cfg and "openai_api_key" in cfg:
        raise ChatError(503, "config.json is still in the old OpenAI format. The brain now runs on Claude Opus 5.5: "
                             'rename "openai_api_key" to "anthropic_api_key", paste an Anthropic key, '
                             'and set "model" to "claude-opus-5-5".')
    key = str(cfg.get("anthropic_api_key") or "").strip()
    model = str(cfg.get("model") or "").strip()
    if not key or key == PLACEHOLDER_KEY or key.startswith("PUT-"):
        raise ChatError(503, "No Anthropic API key yet. Open config.json in the project root, replace "
                             "PUT-YOUR-KEY-HERE with your key, save, and ask again (no restart needed).")
    if not model:
        raise ChatError(503, 'config.json has no "model" set. Use "claude-opus-5-5".')
    return key, model


_fallbacks_ok = True      # switched off for this run if the API ever rejects the fallback option


def call_claude(key, model, system, messages):
    """One Messages API call. Returns the answer text (thinking blocks are skipped)."""
    global _fallbacks_ok
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
        "output_config": {"effort": EFFORT},   # no `thinking` field: Opus 5.5 always thinks; effort sets how much
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if _fallbacks_ok:
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = FALLBACK_BETA
    req = urllib.request.Request(ANTHROPIC_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
        except Exception:
            err = {}
        detail = err.get("message") or e.reason
        if e.code == 400 and _fallbacks_ok and "fallback" in str(detail).lower():
            _fallbacks_ok = False                 # the fallback option isn't available here: carry on without it
            sys.stderr.write("  note: server-side fallbacks unavailable, continuing without them\n")
            return call_claude(key, model, system, messages)
        if e.code == 401:
            raise ChatError(502, "Anthropic rejected the API key in config.json (401). Check it was pasted in full "
                                 "(Anthropic keys start with sk-ant-).")
        if e.code == 404:
            raise ChatError(502, f"Anthropic doesn't recognise the model in config.json ({detail}).")
        if e.code == 429:
            raise ChatError(502, "Anthropic's rate limit was hit (429). Wait a moment and ask again.")
        if e.code >= 500:
            raise ChatError(502, f"Anthropic is overloaded or having trouble ({e.code}). Try again shortly.")
        raise ChatError(502, f"Anthropic returned an error ({e.code}): {detail}")
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        reason = getattr(e, "reason", e)
        raise ChatError(504, f"Couldn't reach Anthropic ({reason}). Check your internet connection and try again.")
    except json.JSONDecodeError:
        raise ChatError(502, "Anthropic sent back something that wasn't JSON. Try again.")

    if data.get("stop_reason") == "refusal":
        raise ChatError(502, "Claude declined to answer that one. Try rephrasing the question.")
    blocks = data.get("content") if isinstance(data.get("content"), list) else []
    text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
    if not text.strip():
        raise ChatError(502, "Claude returned an empty answer. Try rephrasing the question.")
    return text


def parse_reply(content, retrieved_ids):
    """The model is asked for {"answer", "used"}; fall back gracefully if it replies in prose."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            answer = str(obj.get("answer", "")).strip()
            if answer:
                used = []
                for x in obj.get("used") or []:
                    try:
                        i = int(x)
                    except (TypeError, ValueError):
                        continue
                    if i in retrieved_ids and i not in used:
                        used.append(i)
                return answer, used
        except (json.JSONDecodeError, AttributeError):
            pass
    return content.strip(), list(retrieved_ids)


# ---------------------------------------------------------------- conversation memory

class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = OrderedDict()

    def get(self, sid):
        with self.lock:
            s = self.data.pop(sid, None) or {"history": [], "last_question": None}
            self.data[sid] = s
            while len(self.data) > MAX_SESSIONS:
                self.data.popitem(last=False)
            return {"history": list(s["history"]), "last_question": s["last_question"]}

    def record(self, sid, question, reply_json):
        with self.lock:
            s = self.data.setdefault(sid, {"history": [], "last_question": None})
            s["history"] = (s["history"] + [
                {"role": "user", "content": question},
                {"role": "assistant", "content": reply_json},
            ])[-HISTORY_MESSAGES:]
            s["last_question"] = question

    def reset(self, sid):
        with self.lock:
            self.data.pop(sid, None)


SESSIONS = Sessions()


def answer(question, sid):
    state = SESSIONS.get(sid)
    try:
        notes = retrieve(question, state["last_question"])
    except RuntimeError as e:
        raise ChatError(503, str(e))
    ids = [n["id"] for n in notes]
    try:
        key, model = load_config()
        if not notes and not state["history"]:
            return {"answer": "Your notes don't seem to cover that. Nothing matched those words.", "nodes": []}
        system, messages = build_request(question, notes, state["history"])
        content = call_claude(key, model, system, messages)
    except ChatError as e:
        e.nodes = ids                                   # the galaxy can still light up the best matches
        raise
    text, used = parse_reply(content, ids)
    SESSIONS.record(sid, question, json.dumps({"answer": text, "used": used}, ensure_ascii=False))
    return {"answer": text, "nodes": used}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "KnowledgeGalaxy/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.command} {self.path.split('?')[0]} -> {args[1] if len(args) > 1 else ''}\n")

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --- static files: viewer/ only
    def do_GET(self):
        self.serve_static(head=False)

    def do_HEAD(self):
        self.serve_static(head=True)

    def serve_static(self, head):
        path = unquote(urlsplit(self.path).path)
        if path.endswith("/"):
            path += "index.html"
        try:
            target = (VIEWER_DIR / path.lstrip("/")).resolve()
            ok = target.is_relative_to(VIEWER_DIR) and target.is_file()
        except (OSError, ValueError):
            ok = False
        if not ok:
            self.send_error(404, "Not found")
            return
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    # --- the brain
    def do_POST(self):
        route = urlsplit(self.path).path.rstrip("/")
        if route not in ("/chat", "/chat/reset"):
            self.send_json(404, {"error": "Not found"})
            return
        # Only this page may call /chat: blocks other websites from spending your API credit.
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            self.send_json(403, {"error": "Cross-origin requests are not allowed."})
            return
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.send_json(415, {"error": "Send JSON with Content-Type: application/json."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.send_json(413, {"error": "Request too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            self.send_json(400, {"error": "Body must be a JSON object."})
            return

        sid = str(payload.get("session") or "default")[:64]
        if route == "/chat/reset":
            SESSIONS.reset(sid)
            self.send_json(200, {"ok": True})
            return

        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            self.send_json(400, {"error": 'Send {"question": "..."}.'})
            return
        question = question.strip()[:MAX_QUESTION]
        try:
            self.send_json(200, answer(question, sid))
        except ChatError as e:
            self.send_json(e.status, {"error": e.message, "nodes": getattr(e, "nodes", [])})
        except Exception as e:                          # never let one bad request kill the server
            sys.stderr.write(f"  /chat failed: {type(e).__name__}: {e}\n")
            self.send_json(500, {"error": f"Something went wrong on the server ({type(e).__name__}). "
                                          "Check the terminal running server.py.", "nodes": []})


def main():
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("application/javascript", ".mjs")
    if not VIEWER_DIR.is_dir():
        sys.exit("viewer/ folder not found next to server.py.")
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
        print("Created config.json. Put your Anthropic API key in it to enable chat.")
    if not (VIEWER_DIR / "graph-data.js").exists() or not INDEX_PATH.exists():
        print("Heads up: no index yet. Run `python3 build.py /path/to/notes` and refresh the page.")
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if str(cfg.get("anthropic_api_key", "")).strip() in ("", PLACEHOLDER_KEY):
            print("Chat is off until you paste an Anthropic API key into config.json (no restart needed).")
    except (OSError, ValueError, AttributeError):
        print("Warning: config.json isn't valid JSON; /chat will report the problem.")

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        sys.exit(f"Can't listen on port {PORT}: {e.strerror}. Is server.py already running?")
    server.daemon_threads = True
    print(f"Knowledge Galaxy running at http://localhost:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
