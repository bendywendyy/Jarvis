#!/usr/bin/env python3
"""Index a folder of markdown notes for the Knowledge Galaxy.

    python3 build.py [NOTES_DIR]        (default: ./notes)

Writes two files:
  viewer/graph-data.js  -> const GRAPH = {nodes: [...], links: [...]}   (loaded by the browser)
  notes-index.json      -> full note text for the /chat brain            (project root, never served)

Every node's numeric id equals its index in GRAPH.nodes.
Standard library only.
"""
import json
import re
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent
GRAPH_JS = ROOT / "viewer" / "graph-data.js"
BRAIN_INDEX = ROOT / "notes-index.json"

EXCERPT_CHARS = 700
FULL_TEXT_CHARS = 20000   # per note, for the brain
MAX_TITLE_WORDS = 8       # longest title we look for when matching mentions in text
SHARED_LINK_CAP = 12      # a [[target]] shared by more notes than this is a hub; don't wire every pair
MIN_SHARED_TARGETS = 3    # notes sharing this many existing-note [[targets]] get linked...
                          # ...while one shared [[target]] with no note of its own (a topic/tag) is enough
SKIP_DIRS = {"node_modules", "__pycache__"}
# Single-word titles this generic would link half the vault by accident.
GENERIC_TITLES = {"index", "readme", "notes", "note", "todo", "ideas", "misc", "inbox", "draft", "untitled", "home"}

WIKILINK = re.compile(r"!?\[\[([^\]\|#\^]*)(?:[#\^][^\]\|]*)?(?:\|([^\]]*))?\]\]")
MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
FRONTMATTER = re.compile(r"\A---\s*\n.*?\n---\s*(?:\n|\Z)", re.S)
WORD = re.compile(r"[^\W_]+")


def title_key(text):
    """Normalise a title or wikilink target for comparison: lowercase words only."""
    return " ".join(WORD.findall(text.lower()))


def label_from_filename(path):
    label = re.sub(r"[_]+", " ", path.stem).strip()
    if "-" in label and " " not in label:          # kebab-case filename
        label = label.replace("-", " ")
    if label == label.lower():                      # all lowercase -> Title Case
        label = " ".join(w[:1].upper() + w[1:] for w in label.split())
    return label or path.stem


def clean_markdown(body):
    """Strip markdown syntax but keep line structure, for excerpts and the brain."""
    text = WIKILINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), body)
    text = MD_IMAGE.sub("", text)
    text = MD_LINK.sub(r"\1", text)
    text = re.sub(r"<[^>\n]+>", "", text)                          # inline HTML
    lines = []
    for line in text.splitlines():
        s = line.rstrip()
        if re.match(r"^\s*(```|~~~)", s):
            continue
        if re.match(r"^\s*\|?\s*:?-{3,}", s):                        # table separator row
            continue
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s)                      # headings
        s = re.sub(r"^\s*>\s?", "", s)                               # blockquotes
        s = re.sub(r"^(\s*)[-*+]\s+\[[xX]\]\s+", r"\1☑ ", s)          # done tasks
        s = re.sub(r"^(\s*)[-*+]\s+\[ \]\s+", r"\1☐ ", s)             # open tasks
        s = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", s)                     # bullets
        if s.strip().startswith("|"):                                 # table row -> cells
            s = "  ·  ".join(c.strip() for c in s.strip().strip("|").split("|"))
        s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)                     # bold
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)  # italics
        s = re.sub(r"`([^`]*)`", r"\1", s)                            # inline code
        s = s.replace("==", "")
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def make_excerpt(text, limit=EXCERPT_CHARS):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-·•\n") + "…"


def collect_notes(notes_dir):
    files = []
    for path in notes_dir.rglob("*"):
        rel = path.relative_to(notes_dir)
        if any(p.startswith(".") or p in SKIP_DIRS for p in rel.parts[:-1]):
            continue
        if path.is_file() and path.suffix.lower() == ".md" and not path.name.startswith("."):
            files.append(path)
    return sorted(files, key=lambda p: str(p.relative_to(notes_dir)).lower())


def main():
    notes_dir = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else ROOT / "notes"
    if not notes_dir.is_dir():
        sys.exit(f"Notes folder not found: {notes_dir}\nUsage: python3 build.py /path/to/your/notes")

    files = collect_notes(notes_dir)
    if not files:
        print(f"Warning: no .md files found under {notes_dir}")

    nodes, brain, raw_wikilinks, word_lists = [], [], [], []
    for i, path in enumerate(files):
        raw = path.read_text(encoding="utf-8", errors="replace")
        body = FRONTMATTER.sub("", raw, count=1)
        label = label_from_filename(path)
        rel = path.relative_to(notes_dir)
        group = rel.parent.as_posix() if rel.parent != Path(".") else notes_dir.name

        text = clean_markdown(body)
        first, _, rest = text.partition("\n")
        if title_key(first) == title_key(label):     # drop a leading "# Title" that repeats the filename
            text = rest.strip()

        targets = set()
        for m in WIKILINK.finditer(body):
            key = title_key(m.group(1).split("/")[-1])
            if key:
                targets.add(key)
        raw_wikilinks.append(targets)
        word_lists.append(WORD.findall(text.lower()))

        nodes.append({
            "id": i,                     # == index in nodes[]; other code relies on this
            "label": label,
            "group": group,
            "excerpt": make_excerpt(text),
            "file": rel.as_posix(),
            "words": len(text.split()),
        })
        brain.append({"id": i, "label": label, "group": group, "text": text[:FULL_TEXT_CHARS]})

    # Title lookup: normalised title -> node ids (duplicates across folders are possible).
    by_title = {}
    for n in nodes:
        by_title.setdefault(title_key(n["label"]), []).append(n["id"])
    mentionable = {}
    for key, ids in by_title.items():
        words = tuple(key.split())
        if not words or len(words) > MAX_TITLE_WORDS:
            continue
        if len(words) == 1 and (len(words[0]) < 4 or words[0] in GENERIC_TITLES):
            continue
        mentionable[words] = ids

    links = {}

    def connect(a, b, weight, kind):
        if a == b:
            return
        pair = (min(a, b), max(a, b))
        link = links.setdefault(pair, {"source": pair[0], "target": pair[1], "value": 0, "kinds": set()})
        link["value"] += weight
        link["kinds"].add(kind)

    # 1. A note mentions another note's title (plain text or [[wikilink]]).
    for i, words in enumerate(word_lists):
        found = set()
        for start in range(len(words)):
            for size in range(1, MAX_TITLE_WORDS + 1):
                ids = mentionable.get(tuple(words[start:start + size]))
                if ids:
                    found.update(ids)
        for key in raw_wikilinks[i]:
            found.update(by_title.get(key, []))
        for j in found:
            connect(i, j, 2 if title_key(nodes[j]["label"]) in raw_wikilinks[i] else 1, "mention")

    # 2. Two notes share [[wikilinks]]. Sharing one link to an existing note is already visible as
    #    two hops through that note, so it takes several; a shared topic with no note of its own
    #    (e.g. [[Q4 priorities]]) links on its own.
    linkers = {}
    for i, targets in enumerate(raw_wikilinks):
        for key in targets:
            linkers.setdefault(key, set()).add(i)
    shared = {}
    for key, ids in linkers.items():
        if 2 <= len(ids) <= SHARED_LINK_CAP:
            for pair in combinations(sorted(ids), 2):
                count, topic = shared.get(pair, (0, False))
                shared[pair] = (count + 1, topic or key not in by_title)
    for (a, b), (count, topic) in shared.items():
        if topic or count >= MIN_SHARED_TARGETS:
            connect(a, b, 1, "shared")

    link_list = []
    for pair in sorted(links):
        link = links[pair]
        link["type"] = "mention" if "mention" in link["kinds"] else "shared"
        del link["kinds"]
        link_list.append(link)

    graph = {"nodes": nodes, "links": link_list}
    GRAPH_JS.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    GRAPH_JS.write_text(
        f"// Generated by build.py on {stamp} from {len(nodes)} notes. Do not edit by hand.\n"
        f"const GRAPH = {json.dumps(graph, ensure_ascii=False, separators=(',', ':'))};\n",
        encoding="utf-8",
    )
    BRAIN_INDEX.write_text(
        json.dumps({"generated": stamp, "notes": brain}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    groups = sorted({n["group"] for n in nodes})
    print(f"Indexed {len(nodes)} notes in {len(groups)} groups with {len(link_list)} links")
    print(f"  from   {notes_dir}")
    print(f"  wrote  {GRAPH_JS.relative_to(ROOT)}  and  {BRAIN_INDEX.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
