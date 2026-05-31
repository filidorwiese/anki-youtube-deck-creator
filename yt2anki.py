#!/usr/bin/env python3
"""yt2anki - YouTube video -> Anki deck pipeline for Japanese learning.

Stages: download -> transcribe -> sentence-split -> translate -> build-deck.
Each stage logs what it produced and (unless --yes) waits for Enter.
Intermediate files live in a per-video working dir so runs don't collide.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path

# --- subprocess PATH: sandboxes/cron may strip it; rebuild a sane one so the
#     external tools (installed in the usual user/cargo/brew dirs) are found ---
_EXTRA_PATHS = [
    "/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin",
    str(Path.home() / ".local/bin"),
    str(Path.home() / ".cargo/bin"),
    "/opt/homebrew/bin",
]
os.environ["PATH"] = os.pathsep.join(
    dict.fromkeys(_EXTRA_PATHS + os.environ.get("PATH", "").split(os.pathsep))
)


# ---------------------------------------------------------------------------
# .env loading (stdlib only; no python-dotenv dependency)
# ---------------------------------------------------------------------------
def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Existing environment variables win, so an explicit export overrides .env.
    Supports `export KEY=val`, # comments, and single/double-quoted values.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# Logging / color
# ---------------------------------------------------------------------------
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def header(step: int, total: int, title: str) -> None:
    bar = "=" * 70
    print()
    print(_c("1;36", bar))
    print(_c("1;36", f"  STAGE {step}/{total}: {title}"))
    print(_c("1;36", bar))


def info(msg: str) -> None:
    print(_c("0;37", f"  · {msg}"))


def good(msg: str) -> None:
    print(_c("1;32", f"  ✓ {msg}"))


def warn(msg: str) -> None:
    print(_c("1;33", f"  ! {msg}"))


def err(msg: str) -> None:
    print(_c("1;31", f"  ✗ {msg}"), file=sys.stderr)


def produced(path: Path) -> None:
    size = path.stat().st_size if path.exists() else 0
    good(f"produced: {path}  ({size:,} bytes)")


def die(msg: str, hint: str = "") -> None:
    err(msg)
    if hint:
        print(_c("0;33", f"    hint: {hint}"), file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    info("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def prompt_edit(label: str, default: str) -> str:
    """Prompt for a value pre-filled with `default` and editable in place.

    Uses readline to seed the input line so the user can tweak the suggestion.
    Falls back to showing `[default]` if readline is unavailable; an empty reply
    keeps the default either way.
    """
    try:
        import readline
    except ImportError:
        readline = None
    if readline is not None:
        readline.set_startup_hook(lambda: readline.insert_text(default))
        try:
            ans = input(label)
        except (KeyboardInterrupt, EOFError):
            print()
            return default
        finally:
            readline.set_startup_hook()
        return ans.strip() or default
    try:
        ans = input(f"{label}[{default}] ")
    except (KeyboardInterrupt, EOFError):
        print()
        return default
    return ans.strip() or default


# once the user picks [r]ecreate at any prompt, every later stage recreates too
_RECREATE_REST = False


def reuse(path: Path, auto_yes: bool) -> bool:
    """If an output already exists, ask whether to skip the step or recreate it.

    Returns True to skip (reuse the existing file), False to recreate it. With
    --yes (unattended) the existing file is reused without asking. Picking
    [r]ecreate is sticky: all subsequent stages recreate without re-asking.
    """
    global _RECREATE_REST
    if not path.exists() or path.stat().st_size == 0:
        return False
    if auto_yes:
        info(f"exists, reusing: {path}")
        return True
    if _RECREATE_REST:
        info(f"recreating: {path}")
        return False
    try:
        ans = input(_c(
            "1;33", f"  ? {path} exists. [s]kip / [r]ecreate? [S/r] "
        )).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return True
    if ans in ("r", "recreate"):
        _RECREATE_REST = True
        info(f"recreating (this and all later stages): {path}")
        return False
    info(f"reusing: {path}")
    return True


# ---------------------------------------------------------------------------
# Tool checks
# ---------------------------------------------------------------------------
INSTALL_HINTS = {
    "yt-dlp": "pipx install yt-dlp   (or: python3 -m pip install -U yt-dlp)",
    "whisper": "python3 -m pip install -U openai-whisper",
    "ffmpeg": "sudo apt install ffmpeg   (Debian/Ubuntu)  |  brew install ffmpeg",
}


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def check_tools(required: list[str], optional: list[str] = ()) -> None:
    """Print a ✓/✗ checklist of tools found on $PATH. Exit if a required one is missing."""
    print()
    print(_c("1;36", "  Tool check ($PATH)"))
    missing = []
    for t in required:
        if have(t):
            print(_c("1;32", f"   [✓] {t}"))
        else:
            print(_c("1;31", f"   [✗] {t}  (required)"))
            print(_c("0;33", f"       {INSTALL_HINTS.get(t, '')}"))
            missing.append(t)
    for t in optional:
        if have(t):
            print(_c("1;32", f"   [✓] {t}  (optional)"))
        else:
            print(_c("1;33", f"   [✗] {t}  (optional)"))
            print(_c("0;33", f"       {INSTALL_HINTS.get(t, '')}"))
    if missing:
        die("missing required tools: " + ", ".join(missing),
            "install the tools marked [✗] above, then re-run")


# ---------------------------------------------------------------------------
# Video id / working dir
# ---------------------------------------------------------------------------
def video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    if m:
        return m.group(1)
    if have("yt-dlp"):
        try:
            out = subprocess.run(
                ["yt-dlp", "--print", "id", "--skip-download", url],
                capture_output=True, text=True, check=True,
            )
            vid = out.stdout.strip().splitlines()[-1]
            if vid:
                return vid
        except subprocess.CalledProcessError:
            pass
    die(f"could not determine video id from URL: {url}")


def video_title(url: str, wd: Path, vid: str) -> str:
    """Fetch the YouTube title (cached to <vid>.title); fall back to the id."""
    cache = wd / f"{vid}.title"
    if cache.exists() and cache.stat().st_size:
        return cache.read_text(encoding="utf-8").strip()
    if have("yt-dlp"):
        try:
            out = subprocess.run(
                ["yt-dlp", "--print", "title", "--skip-download", url],
                capture_output=True, text=True, check=True,
            )
            title = out.stdout.strip().splitlines()[-1].strip()
            if title:
                cache.write_text(title, encoding="utf-8")
                return title
        except subprocess.CalledProcessError:
            pass
    return vid


# ===========================================================================
# STAGE 1 — DOWNLOAD
# ===========================================================================
def stage_download(url: str, wd: Path, vid: str, auto_yes: bool) -> tuple[Path, Path, bool]:
    mp3 = wd / f"{vid}.mp3"
    # video container is unknown ahead of time; find any existing non-mp3 media
    existing_video = next(
        (p for p in wd.glob(f"{vid}.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm")),
        None,
    )

    ran = False
    # --- audio mp3 ---
    if reuse(mp3, auto_yes):
        info("skipping audio download")
    else:
        run(["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
             "-o", str(wd / f"{vid}.%(ext)s"), url])
        ran = True
    if not mp3.exists():
        die("audio mp3 not produced", "check yt-dlp output above")
    produced(mp3)

    # --- source video (kept for later frame extraction) ---
    if existing_video and reuse(existing_video, auto_yes):
        video = existing_video
        info("skipping video download")
    else:
        run(["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", str(wd / f"{vid}.%(ext)s"), url])
        video = next(
            (p for p in wd.glob(f"{vid}.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm")),
            None,
        )
        ran = True
    if not video or not video.exists():
        die("source video not produced", "check yt-dlp output above")
    produced(video)
    return mp3, video, ran


# ===========================================================================
# STAGE 2 — TRANSCRIBE (Whisper, word-level JSON)
# ===========================================================================
LANG_NAMES = {
    "en": "English", "ja": "Japanese", "zh": "Chinese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "ru": "Russian", "nl": "Dutch", "pl": "Polish",
    "tr": "Turkish", "ar": "Arabic", "hi": "Hindi", "id": "Indonesian",
    "vi": "Vietnamese", "th": "Thai", "sv": "Swedish", "uk": "Ukrainian",
}


def lang_name(code: str) -> str:
    """Human-readable language name for a code (falls back to the code itself)."""
    return LANG_NAMES.get(code.strip().lower(), code)


def stage_transcribe(mp3: Path, wd: Path, vid: str, model: str,
                     source_lang: str, auto_yes: bool) -> tuple[Path, bool]:
    out_json = wd / f"{vid}.json"
    if reuse(out_json, auto_yes):
        produced(out_json)
        return out_json, False
    run(["whisper", str(mp3),
         "--language", source_lang, "--task", "transcribe",
         "--word_timestamps", "True", "--model", model,
         "--output_format", "json", "--output_dir", str(wd)])
    # whisper names output after the input stem: <vid>.json
    if not out_json.exists():
        die(f"whisper did not produce {out_json}", "check whisper output above")
    produced(out_json)
    return out_json, True


# ===========================================================================
# STAGE 3 — SENTENCE SPLIT  (pure python; strict SRT)   [unit-tested]
# ===========================================================================
SENT_END = "。！？!?"


def iter_words(whisper_json: dict):
    """Yield (text, start, end, seg_end) for every word across all segments.

    seg_end is True on the last token of each Whisper segment so callers can
    treat a segment boundary as a sentence break when no punctuation is present.
    """
    for seg in whisper_json.get("segments", []):
        words = seg.get("words")
        if words:
            for i, w in enumerate(words):
                txt = w.get("word", "")
                if txt is None:
                    continue
                yield txt, w.get("start"), w.get("end"), i == len(words) - 1
        else:
            # segment without word timestamps: treat whole segment as one unit
            yield seg.get("text", ""), seg.get("start"), seg.get("end"), True


def split_sentences(whisper_json: dict) -> list[dict]:
    """Re-split word stream into one cue per sentence.

    Break on sentence-ending punctuation; failing that, break at Whisper segment
    boundaries (its Japanese word-level output rarely emits 。！？, so without
    this everything collapses into one cue). start = first word, end = closing word.
    """
    cues: list[dict] = []
    buf: list[str] = []
    start = None
    last_end = None

    def flush(end):
        nonlocal buf, start
        text = "".join(buf).strip()
        if text and start is not None and end is not None:
            cues.append({"start": start, "end": end, "text": text})
        buf = []
        start = None

    for txt, s, e, seg_end in iter_words(whisper_json):
        if txt is None:
            txt = ""
        if start is None and txt.strip():
            start = s
        buf.append(txt)
        if e is not None:
            last_end = e
        # close on terminal punctuation, else at the segment boundary
        if seg_end or any(p in txt for p in SENT_END):
            flush(e if e is not None else last_end)
    # trailing remainder with no closing punctuation
    flush(last_end)
    return cues


def dedupe(cues: list[dict]) -> list[dict]:
    """Drop exact-match duplicate sentences, keep first occurrence."""
    seen: set[str] = set()
    out = []
    for c in cues:
        if c["text"] in seen:
            continue
        seen.add(c["text"])
        out.append(c)
    return out


def _ts(seconds: float) -> str:
    if seconds is None or seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(cues: list[dict]) -> str:
    """Strict SRT: 1-based int index, comma-ms timestamps, text on next line,
    blank line between blocks, trailing newline."""
    blocks = []
    for i, c in enumerate(cues, 1):
        blocks.append(f"{i}\n{_ts(c['start'])} --> {_ts(c['end'])}\n{c['text']}\n")
    return "\n".join(blocks) + ("\n" if blocks else "")


def stage_sentence_split(json_path: Path, wd: Path, vid: str,
                         auto_yes: bool) -> tuple[Path, bool]:
    srt_path = wd / f"{vid}.srt"
    if reuse(srt_path, auto_yes):
        produced(srt_path)
        return srt_path, False
    data = json.loads(json_path.read_text(encoding="utf-8"))
    cues = split_sentences(data)
    info(f"split into {len(cues)} sentence cues")
    cues = dedupe(cues)
    info(f"after dedup: {len(cues)} cues")
    if not cues:
        die("no sentences produced from transcript", "is the Whisper JSON empty/malformed?")
    srt_path.write_text(to_srt(cues), encoding="utf-8")
    produced(srt_path)
    return srt_path, True


# ===========================================================================
# STAGE 4 — TRANSLATE  (swappable backend)
# ===========================================================================
def parse_srt(srt_text: str) -> list[dict]:
    """Minimal strict-ish SRT parser -> [{index,start,end,text}]."""
    cues = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if len(lines) < 2:
            continue
        # find the timestamp line
        ts_i = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if ts_i is None:
            continue
        a, b = [x.strip() for x in lines[ts_i].split("-->")]
        text = " ".join(lines[ts_i + 1:]).strip()
        if text:
            cues.append({"start": a, "end": b, "text": text})
    return cues


def _anthropic_call(prompt: str, model: str, max_tokens: int = 4096) -> str:
    """Single Anthropic Messages call -> concatenated text content."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        die("ANTHROPIC_API_KEY not set", "export ANTHROPIC_API_KEY=sk-ant-...")
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        die(f"Anthropic API error {e.code}: {e.read().decode(errors='ignore')[:300]}")
    return "".join(p.get("text", "") for p in resp.get("content", []))


def _anthropic_batch(sentences: list[str], model: str, instruction: str,
                     label: str) -> list[str]:
    """Run a per-sentence Anthropic task returning one output string per input.

    `instruction` must ask for a JSON array of strings in input order; we batch
    to keep the number of API calls low and re-align if a batch comes back short.
    """
    out: list[str] = []
    BATCH = 50  # few API calls, not one-per-sentence
    for i in range(0, len(sentences), BATCH):
        chunk = sentences[i:i + BATCH]
        numbered = "\n".join(f"{j+1}. {s}" for j, s in enumerate(chunk))
        arr = _extract_json_array(_anthropic_call(instruction + "\n\n" + numbered, model))
        # the model sometimes echoes the "N. " list prefix into each item
        arr = [re.sub(r"^\s*\d+\.\s*", "", x) for x in arr]
        if len(arr) != len(chunk):
            warn(f"batch {i//BATCH}: expected {len(chunk)} {label}, got {len(arr)}; "
                 "padding/truncating to align")
            arr = (arr + [""] * len(chunk))[:len(chunk)]
        out.extend(arr)
        info(f"{label} {min(i+BATCH, len(sentences))}/{len(sentences)}")
    return out


def _anthropic_translate(sentences: list[str], model: str,
                         source_lang: str, user_lang: str) -> list[str]:
    """Batch-translate source_lang->user_lang via the Anthropic Messages API."""
    instruction = (
        f"Translate each numbered {lang_name(source_lang)} sentence into "
        f"natural but faithful {lang_name(user_lang)} for an A1 beginner "
        "learner. Return ONLY a JSON array of the translations in the same "
        "order, with no numbering and no commentary."
    )
    return _anthropic_batch(sentences, model, instruction, "translated")


def anthropic_translate_furigana(sentences: list[str], model: str,
                                 source_lang: str, user_lang: str
                                 ) -> tuple[list[str], list[str]]:
    """One call per batch that BOTH translates and adds furigana (saves a pass).

    Returns (translations, furigana_sentences). Furigana is Anki ruby notation
    (`今日[きょう]`) that `ruby_to_html` renders later. Used for JA + anthropic;
    other languages/backends go through `translate_sentences` instead.
    """
    instruction = (
        f"For each numbered {lang_name(source_lang)} sentence, return its "
        f"translation into natural but faithful {lang_name(user_lang)} for an A1 "
        "beginner, and a furigana copy of the original: immediately after every "
        "kanji run append its kana reading in square brackets, e.g. "
        "今日[きょう]は早[はや]いです, adding nothing else, no spaces, and leaving "
        "kana, punctuation and numbers unchanged. Return ONLY a JSON array, one "
        'object per sentence in order, each {"t": "<translation>", '
        '"f": "<furigana sentence>"}, with no numbering and no commentary.'
    )
    strip = lambda x: re.sub(r"^\s*\d+\.\s*", "", str(x))  # noqa: E731
    tgt, fura = [], []
    BATCH = 40
    for i in range(0, len(sentences), BATCH):
        chunk = sentences[i:i + BATCH]
        numbered = "\n".join(f"{j+1}. {s}" for j, s in enumerate(chunk))
        arr = _extract_json(_anthropic_call(instruction + "\n\n" + numbered, model, 8192))
        if not isinstance(arr, list) or len(arr) != len(chunk):
            warn(f"batch {i//BATCH}: expected {len(chunk)} items, got "
                 f"{len(arr) if isinstance(arr, list) else 'none'}; aligning")
            arr = (list(arr) if isinstance(arr, list) else []) + [{}] * len(chunk)
            arr = arr[:len(chunk)]
        for o in arr:
            o = o if isinstance(o, dict) else {}
            tgt.append(strip(o.get("t", "")))
            fura.append(strip(o.get("f", "")))
        info(f"translated+furigana {min(i+BATCH, len(sentences))}/{len(sentences)}")
    return tgt, fura


# a kanji run directly followed by [reading] -> <ruby> for Anki/HTML display.
# Matching only kanji as the base avoids grabbing leading kana (お茶[ちゃ] -> 茶).
_RUBY_RE = re.compile(r"([一-鿿々〆ヶ]+)\[([^\[\]]+)\]")


def ruby_to_html(text: str) -> str:
    """`今日[きょう]は` -> `<ruby>今日<rt>きょう</rt></ruby>は`."""
    return _RUBY_RE.sub(r"<ruby>\1<rt>\2</rt></ruby>", text)


# alignment groups are wrapped by the LLM as [[n]]...[[/n]] in both sentences;
# the same n gets the same color so matching words line up across source/target.
ALIGN_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd",
                "#17a2b8", "#8c564b", "#e377c2", "#bcbd22", "#393b79"]
_GROUP_RE = re.compile(r"\[\[(\d+)\]\](.*?)\[\[/\1\]\]", re.S)


def _group_nums(text: str) -> set[int]:
    """Group numbers that are both opened and closed in `text`."""
    return {int(m.group(1)) for m in _GROUP_RE.finditer(text)}


def colorize(text: str) -> str:
    """`[[1]]today[[/1]]` -> `<span style="color:…">today</span>`.

    Color is picked by group number; stray/unmatched tags are stripped so a
    malformed LLM response degrades to plain text rather than leaking `[[..]]`.
    """
    def repl(m: re.Match) -> str:
        color = ALIGN_COLORS[(int(m.group(1)) - 1) % len(ALIGN_COLORS)]
        return f'<span style="color:{color}">{m.group(2)}</span>'

    return re.sub(r"\[\[/?\d+\]\]", "", _GROUP_RE.sub(repl, text))


def _extract_json_array(text: str) -> list[str]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        return [str(x) for x in arr]
    except json.JSONDecodeError:
        return []


def _extract_json(text: str):
    """Parse the first top-level JSON array found (elements kept as-is)."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return []


def anthropic_align(srcs: list[str], tgts: list[str], model: str) -> list[tuple[str, str]]:
    """Tag corresponding word groups in each source/target pair with [[n]] tags.

    Returns (tagged_src, tagged_target) per pair; `colorize` later turns matching
    tag numbers into matching colors. The source keeps any furigana notation.
    """
    instruction = (
        "For each numbered SOURCE/TARGET sentence pair, mark the parts that "
        "correspond. Wrap each group of corresponding words in matching tags "
        "[[n]]...[[/n]] in BOTH the source and the target, using the same number "
        "n for parts that mean the same thing. Break each sentence into several "
        "small groups, one per content word or short phrase; NEVER wrap a whole "
        "sentence as a single group. Number groups 1,2,3,... restarting per pair. "
        "Leave grammatical words with no counterpart untagged. Do not reorder, "
        "translate, or change any characters, including existing furigana like "
        "今日[きょう]; only insert the [[n]] tags. Return ONLY a JSON array, one "
        'object per pair in order, each {"src": "<tagged source>", '
        '"tgt": "<tagged target>"}, no commentary.'
    )
    out: list[tuple[str, str]] = []
    BATCH = 30
    for i in range(0, len(srcs), BATCH):
        cs, ct = srcs[i:i + BATCH], tgts[i:i + BATCH]
        items = "\n".join(
            f"{j+1}. SOURCE: {s}\n   TARGET: {t}"
            for j, (s, t) in enumerate(zip(cs, ct)))
        arr = _extract_json(_anthropic_call(instruction + "\n\n" + items, model, 8192))
        if not isinstance(arr, list) or len(arr) != len(cs):
            warn(f"batch {i//BATCH}: expected {len(cs)} alignments, got "
                 f"{len(arr) if isinstance(arr, list) else 'none'}; leaving plain")
            arr = (list(arr) if isinstance(arr, list) else []) + [{}] * len(cs)
            arr = arr[:len(cs)]
        for o in arr:
            asrc, atgt = ((o.get("src", ""), o.get("tgt", ""))
                          if isinstance(o, dict) else ("", ""))
            # drop useless alignments (e.g. whole sentence as one group): keep
            # only if at least two groups appear on BOTH sides.
            if len(_group_nums(asrc) & _group_nums(atgt)) < 2:
                asrc, atgt = "", ""
            out.append((asrc, atgt))
        info(f"aligned {min(i+BATCH, len(srcs))}/{len(srcs)}")
    return out


# backend registry: swap translation backend by name without touching callers
TRANSLATORS = {"anthropic": _anthropic_translate}


def translate_sentences(sentences: list[str], backend: str, model: str,
                        source_lang: str, user_lang: str) -> list[str]:
    fn = TRANSLATORS.get(backend)
    if not fn:
        die(f"unknown translate backend: {backend}",
            f"available: {', '.join(TRANSLATORS)}")
    return fn(sentences, model, source_lang, user_lang)


def stage_translate(srt_path: Path, wd: Path, vid: str, backend: str,
                    model: str, source_lang: str, user_lang: str,
                    align: bool, auto_yes: bool) -> tuple[Path, bool]:
    tsv_path = wd / f"{vid}.tsv"
    if reuse(tsv_path, auto_yes):
        produced(tsv_path)
        return tsv_path, False
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    if not cues:
        die("SRT empty/malformed; nothing to translate", f"inspect {srt_path}")
    src = [c["text"] for c in cues]

    # JA + anthropic: translate and furigana in one call. Other languages/backends
    # translate via the registry and get an empty furigana column.
    # TSV layout: src \t translation \t furigana \t algn_src \t algn_tgt
    if source_lang.lower() == "ja" and backend == "anthropic":
        info(f"translating + furigana {len(src)} sentences -> "
             f"{lang_name(user_lang)} ({model})")
        tgt, fura = anthropic_translate_furigana(src, model, source_lang, user_lang)
    else:
        info(f"translating {len(src)} sentences "
             f"{lang_name(source_lang)}->{lang_name(user_lang)} "
             f"via backend '{backend}' ({model})")
        tgt = translate_sentences(src, backend, model, source_lang, user_lang)
        fura = [""] * len(src)

    # color-alignment columns (LLM tags matching word groups in both sentences);
    # align on the furigana source where present so the colors keep the readings.
    if align and backend == "anthropic":
        info(f"aligning {len(src)} sentence pairs")
        algn_src = [fura[i] if fura[i].strip() else src[i] for i in range(len(src))]
        aligned = anthropic_align(algn_src, tgt, model)
    else:
        aligned = [("", "")] * len(src)

    def clean(x: str) -> str:
        return x.replace("\t", " ").replace("\n", " ")

    with tsv_path.open("w", encoding="utf-8", newline="") as f:
        for s, t, fr, (asrc, atgt) in zip(src, tgt, fura, aligned):
            f.write("\t".join(clean(x) for x in (s, t, fr, asrc, atgt)) + "\n")
    produced(tsv_path)
    return tsv_path, True


# ===========================================================================
# STAGE 5 — BUILD DECK  (ffmpeg per-cue clips + merge translations -> .apkg)
# ===========================================================================
def _srt_ts_to_sec(ts: str) -> float:
    """'00:01:02,500' (or with '.') -> 62.5 seconds."""
    h, m, rest = ts.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


def cut_clip(mp3: Path, start: float, end: float, pad: float, out: Path) -> None:
    """Extract [start-pad, end+pad] from mp3 into out (re-encoded, tight cut).

    Input seeking (-ss before -i) keeps it fast even for clips late in a long
    file; re-encoding keeps the cut frame-accurate near our small pad.
    """
    ss = max(0.0, start - pad)
    dur = max(0.05, (end + pad) - ss)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(mp3),
         "-c:a", "libmp3lame", "-q:a", "5", str(out)],
        check=True,
    )


# --- .apkg writer (genanki) ------------------------------------------------
# Stable ids so re-imports update the same model/deck instead of duplicating.
APKG_MODEL_ID = 1726000000001
APKG_DECK_ID = 1726000000002
_APKG_CSS = (
    ".card{font-family:arial;font-size:28px;text-align:center;"
    "color:black;background:white}"
)


def write_apkg(out_path: Path, deck_name: str,
               cards: list[tuple[str, str, list[Path]]]) -> None:
    """Write a self-contained Anki .apkg (Basic note type: Front / Back).

    cards: (front_html, back_html, [media_paths]). Media files are bundled into
    the package, so no manual copy into collection.media is needed on import.
    """
    try:
        import genanki
    except ImportError:
        die("genanki not installed (needed to build the .apkg)",
            "python3 -m pip install genanki")

    model = genanki.Model(
        APKG_MODEL_ID, "yt2anki Basic",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[{
            "name": "Card 1",
            "qfmt": "{{Front}}",
            "afmt": "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}",
        }],
        css=_APKG_CSS,
    )
    deck = genanki.Deck(APKG_DECK_ID, deck_name)
    media: list[str] = []
    for front, back, paths in cards:
        deck.add_note(genanki.Note(model=model, fields=[front, back]))
        media.extend(str(p) for p in paths)

    genanki.Package(deck, media_files=media).write_to_file(str(out_path))


def _print_import_instructions(apkg: Path) -> None:
    print()
    print(_c("1;32", "  HOW TO IMPORT INTO ANKI"))
    print(_c("0;37", f"   Double-click {apkg}  (or Anki: File > Import > that file)."))
    print(_c("0;37", "   Audio is bundled in the .apkg; no manual media copying needed."))


def stage_build_deck(mp3: Path, srt_path: Path, tsv_path: Path, wd: Path,
                     vid: str, deck_name: str, clip_pad: float,
                     upstream_ran: bool, auto_yes: bool) -> tuple[Path, Path]:
    clip_dir = wd / f"{vid}_clips"
    apkg = wd / f"{vid}.apkg"

    # reuse an existing apkg only if no upstream stage actually ran this run;
    # otherwise rebuild it so it reflects the fresh upstream output.
    if not upstream_ran and reuse(apkg, auto_yes):
        produced(apkg)
        _print_import_instructions(apkg)
        return apkg, clip_dir
    if apkg.exists() and upstream_ran:
        info("upstream stage ran; rebuilding .apkg")

    if clip_dir.exists():
        shutil.rmtree(clip_dir)
    clip_dir.mkdir(parents=True, exist_ok=True)

    # cues drive both the audio cuts and the card source text
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    if not cues:
        die("no cues in SRT", f"inspect {srt_path}")

    # TSV cols: 1 src, 2 translation, 3 furigana, 4 aligned-src, 5 aligned-tgt
    en, fura, asrc, atgt = [], [], [], []
    for line in tsv_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        en.append(parts[1] if len(parts) > 1 else "")
        fura.append(parts[2] if len(parts) > 2 else "")
        asrc.append(parts[3] if len(parts) > 3 else "")
        atgt.append(parts[4] if len(parts) > 4 else "")
    if len(cues) != len(en):
        warn(f"row count mismatch: {len(cues)} cues vs {len(en)} translations; "
             "aligning by index (extra entries dropped)")

    # cut one tight clip per cue with ffmpeg (pad each side by clip_pad)
    info(f"cutting {len(cues)} clips (pad {clip_pad:.2f}s each side)")
    cards: list[tuple[str, str, list[Path]]] = []
    for i in range(min(len(cues), len(en))):
        c = cues[i]
        name = f"{vid}_{i:05d}.mp3"
        clip = clip_dir / name
        cut_clip(mp3, _srt_ts_to_sec(c["start"]), _srt_ts_to_sec(c["end"]),
                 clip_pad, clip)
        # front: colored+furigana > furigana > plain. ruby runs after colorize so
        # color spans wrap the ruby. back: colored translation when aligned.
        if asrc[i].strip():
            sentence = ruby_to_html(colorize(asrc[i]))
        elif fura[i].strip():
            sentence = ruby_to_html(fura[i])
        else:
            sentence = c["text"]
        front = f"{sentence}<br>[sound:{name}]"
        back = colorize(atgt[i]) if atgt[i].strip() else en[i]
        cards.append((front, back, [clip]))

    write_apkg(apkg, deck_name, cards)
    produced(apkg)
    _print_import_instructions(apkg)
    return apkg, clip_dir


# ===========================================================================
# MAIN
# ===========================================================================
STAGES_TOTAL = 5


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube -> Anki deck (language learning)")
    ap.add_argument("url", help="YouTube video URL")
    ap.add_argument("--source-lang", default=None,
                    help="spoken language of the video, e.g. ja (REQUIRED)")
    ap.add_argument("--user-lang", default="en",
                    help="your language to translate into (default: en)")
    ap.add_argument("--yes", action="store_true",
                    help="unattended: auto-reuse existing outputs, skip title prompt")
    ap.add_argument("--model", default="small", help="Whisper model (default: small)")
    ap.add_argument("--translate-backend", default="anthropic",
                    help="translation backend (default: anthropic)")
    ap.add_argument("--translate-model", default="claude-sonnet-4-6",
                    help="translation model (default: claude-sonnet-4-6)")
    ap.add_argument("--workdir", default="work", help="base working dir (default: ./work)")
    ap.add_argument("--clip-pad", type=float, default=0.50,
                    help="seconds of audio padding each side of a clip (default: 0.50)")
    ap.add_argument("--color-words", action="store_true",
                    help="color-code matching words across source/translation "
                         "(extra LLM pass)")
    ap.add_argument("--title", default=None,
                    help="deck title (default: the YouTube video title)")
    args = ap.parse_args()
    if not args.source_lang:
        ap.error("--source-lang is required: set the spoken language of the "
                 "video, e.g. --source-lang ja  (--user-lang defaults to en)")

    # load .env (script dir first, then cwd); real env vars still take precedence
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path(".env"))

    check_tools(["yt-dlp", "whisper", "ffmpeg"])

    vid = video_id(args.url)
    wd = Path(args.workdir) / vid
    wd.mkdir(parents=True, exist_ok=True)
    good(f"video id: {vid}   working dir: {wd}")

    title = args.title or video_title(args.url, wd, vid)
    if not args.title and not args.yes:
        title = prompt_edit(_c("1;35", "  deck title: "), title)
    deck_name = f"youtube::{title}"
    good(f"deck name: {deck_name}")

    header(1, STAGES_TOTAL, "DOWNLOAD (yt-dlp: mp3 audio + source video)")
    mp3, _video, ran_dl = stage_download(args.url, wd, vid, args.yes)

    header(2, STAGES_TOTAL,
           f"TRANSCRIBE (Whisper, model={args.model}, lang={args.source_lang})")
    json_path, ran_tx = stage_transcribe(mp3, wd, vid, args.model,
                                         args.source_lang, args.yes)

    header(3, STAGES_TOTAL, "SENTENCE SPLIT (strict SRT, deduped)")
    srt_path, ran_sp = stage_sentence_split(json_path, wd, vid, args.yes)

    header(4, STAGES_TOTAL,
           f"TRANSLATE ({lang_name(args.source_lang)}->{lang_name(args.user_lang)}, "
           f"{args.translate_backend})")
    tsv_path, ran_tr = stage_translate(srt_path, wd, vid, args.translate_backend,
                                       args.translate_model, args.source_lang,
                                       args.user_lang, args.color_words,
                                       args.yes)

    header(5, STAGES_TOTAL, "BUILD DECK (ffmpeg clips + translations -> .apkg)")
    upstream_ran = ran_dl or ran_tx or ran_sp or ran_tr
    apkg, _clip_dir = stage_build_deck(mp3, srt_path, tsv_path, wd, vid,
                                       deck_name, args.clip_pad, upstream_ran,
                                       args.yes)

    print()
    good(f"DONE. Anki deck: {apkg}")


if __name__ == "__main__":
    main()
