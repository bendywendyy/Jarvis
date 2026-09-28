# Jarvis: Knowledge Galaxy

Your markdown notes as a 3D galaxy you can fly through, plus a chat bar that answers questions from those notes.

- **`build.py`** scans every `.md` file and writes `viewer/graph-data.js`, the graph the browser draws.
- **`viewer/index.html`** is a single page built on [3d-force-graph](https://github.com/vasturiano/3d-force-graph), loaded from a CDN. There's no npm and no build step.
- **`server.py`** serves `viewer/` on port 4700 and answers `POST /chat` from your notes using OpenAI.

All of it is Python 3 standard library only.

## Run it

```bash
python3 build.py /path/to/your/notes     # or just `python3 build.py` for ./notes
python3 server.py                        # then open http://localhost:4700
```

Re-run `build.py` whenever your notes change, then refresh the page. The server picks up the new index without a restart.

`./notes` contains 30 sample notes about a fictional coffee roastery, so the galaxy works out of the box.

## Turn on the brain

Put your key and model in `config.json` in the project root:

```json
{
  "openai_api_key": "sk-...",
  "model": "gpt-5-5 Opus"
}
```

- The file is read on every question, so there's no restart needed.
- It lives outside `viewer/`, so the browser can never fetch it.
- It's in `.gitignore`, so your key doesn't get committed.
- If the file is missing, `server.py` recreates it with a placeholder.

## How it works

**Graph.**
- Each note becomes a node. Its `id` is its index in `GRAPH.nodes`, its label comes from the filename, its group is its folder, and it carries a roughly 700-character excerpt.
- Two notes are linked when one mentions the other's title (in plain text or as a `[[wikilink]]`).
- Two notes are also linked when they share `[[wikilinks]]`. That means one shared link to a topic with no note of its own (like `[[Q4 priorities]]`), or 3 or more shared links to the same notes.
- You can tune these rules at the top of `build.py`.

**Viewer.**
- Clicking a star flies the camera to it, lights up its neighbours and opens the note in a side panel.
- The galaxy slowly rotates when you're not interacting.
- Keyboard: <kbd>/</kbd> jumps to the chat bar and <kbd>Esc</kbd> clears the selection.

**Brain (`POST /chat`).**
1. Every note is scored against your question by keyword overlap, with title matches weighted higher.
2. The top 6 notes go to OpenAI with instructions to answer only from them, in two or three sentences, and to say plainly when the notes don't cover the question.
3. The server returns `{"answer": "...", "nodes": [ids used]}`, and those stars light up.
4. The last few exchanges are kept in memory on the server, per browser tab, so follow-up questions work. **New chat** clears them.

`build.py` also writes `notes-index.json` in the project root. It holds the full note text the brain searches, and it's never served to the browser.
