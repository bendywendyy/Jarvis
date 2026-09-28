# Jarvis: Knowledge Galaxy

Your markdown notes as a 3D galaxy you can fly through, plus a brain you can talk to (by typing or out loud) that answers from those notes.

- **`build.py`** scans every `.md` file and writes `viewer/graph-data.js`, the graph the browser draws.
- **`viewer/index.html`** is a single page built on [3d-force-graph](https://github.com/vasturiano/3d-force-graph), loaded from a CDN. There's no npm and no build step.
- **`viewer/voice.js`** speaks every answer and lets you dictate questions, using only the browser's built-in speech features.
- **`server.py`** serves `viewer/` on port 4700 and answers `POST /chat` from your notes using **Claude Opus 5.5**.

All of it is Python 3 standard library only.

## Run it

```bash
python3 build.py /path/to/your/notes     # or just `python3 build.py` for ./notes
python3 server.py                        # then open http://localhost:4700
```

Re-run `build.py` whenever your notes change, then refresh the page. The server picks up the new index without a restart.

`./notes` contains 30 sample notes about a fictional coffee roastery, so the galaxy works out of the box.

## Turn on the brain

Put your Anthropic API key in `config.json` in the project root. Get a key from console.anthropic.com.

```json
{
  "anthropic_api_key": "sk-ant-...",
  "model": "claude-opus-5-5"
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
2. The top 6 notes go to Claude Opus 5.5 with instructions to answer only from them, in two or three sentences, and to say plainly when the notes don't cover the question. It runs at low effort for a fast spoken reply; change `EFFORT` in `server.py` to `"medium"` if answers feel thin.
3. The server returns `{"answer": "...", "nodes": [ids used]}`, and those stars light up.
4. The last few exchanges are kept in memory on the server, per browser tab, so follow-up questions work. **New chat** clears them.

`build.py` also writes `notes-index.json` in the project root. It holds the full note text the brain searches, and it's never served to the browser.

## Voice

Voice uses only what the browser already has, with no paid speech services. It works best in Chrome or Edge.

- **Speaking.** Every answer is read aloud with `speechSynthesis`, in a British English voice when your system has one. Browsers block sound until you interact with the page, so nothing is spoken before your first click or key press. Your first click unlocks audio and plays a short greeting. Hover the status line to see which voice was picked.
- **Listening.** Click the mic, talk, and click again to stop. Your words appear in the input box as you speak, then go through the same `/chat` flow as typing. In Chrome, the built-in recogniser runs on Google's servers, so dictation needs an internet connection.
- **Mid-sentence pauses.** The recogniser finalises a phrase every time you pause. Those phrases are buffered, and each new bit of speech appends and restarts the wait. Only a pause longer than `FINISH_MS` (900 ms, the first line of `voice.js`) sends the whole sentence. Switching the mic off sends right away.
- **Interrupts.** Saying just "stop", "wait", "cancel", "hold on" or similar skips the buffer. It acts the moment it's heard: it silences speech, drops a half-finished question, and abandons an answer that's still coming. "Stop, what's our churn?" interrupts and then asks the new question. **Esc** does the same from the keyboard.
- **Status line.** The pill above the input shows who the galaxy thinks is talking: **You**, as *Listening*, *Hearing you* or *Sending when you pause* (with a bar filling over the 900 ms), or **Galaxy**, as *Thinking* or *Speaking*.
- **Echo.** While it's speaking, the mic ignores everything except interrupt words, so the galaxy doesn't answer its own voice through your speakers. Headphones work best.

Open the browser console to see a `[voice]` log of each phrase heard, what was buffered, and why a question was sent.
