#!/usr/bin/env python3
"""yt2anki - YouTube video -> Anki deck pipeline for Japanese learning.

Stages: download -> transcribe -> sentence-split -> translate -> build-deck.
Each stage logs what it produced and (unless --yes) waits for Enter.
Intermediate files live in a per-video working dir so runs don't collide.
"""
import argparse
import csv
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
# Process / gate helpers
# ---------------------------------------------------------------------------
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    info("$ " + " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def gate(auto_yes: bool) -> None:
    """Manual continue? gate after each stage."""
    if auto_yes:
        return
    try:
        input(_c("1;35", "  → press Enter to continue (Ctrl-C to abort) "))
    except (KeyboardInterrupt, EOFError):
        print()
        die("aborted by user")


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


def reuse(path: Path, auto_yes: bool) -> bool:
    """If an output already exists, ask whether to skip the step or recreate it.

    Returns True to skip (reuse the existing file), False to recreate it. With
    --yes (unattended) the existing file is reused without asking.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    if auto_yes:
        info(f"exists, reusing: {path}")
        return True
    try:
        ans = input(_c(
            "1;33", f"  ? {path} exists. [s]kip / [r]ecreate? [S/r] "
        )).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return True
    if ans in ("r", "recreate"):
        info(f"recreating: {path}")
        return False
    info(f"reusing: {path}")
    return True


# ---------------------------------------------------------------------------
# Tool checks
# ---------------------------------------------------------------------------
INSTALL_HINTS = {
    "yt-dlp": "pipx install yt-dlp   (or: python3 -m pip install -U yt-dlp)",
    "whisper": "python3 -m pip install -U openai-whisper",
    "substudy": "cargo install substudy   (needs Rust: https://rustup.rs)",
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
def stage_transcribe(mp3: Path, wd: Path, vid: str, model: str,
                     auto_yes: bool) -> tuple[Path, bool]:
    out_json = wd / f"{vid}.json"
    if reuse(out_json, auto_yes):
        produced(out_json)
        return out_json, False
    run(["whisper", str(mp3),
         "--language", "ja", "--task", "transcribe",
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
    """Yield (text, start, end) for every word across all segments, in order."""
    for seg in whisper_json.get("segments", []):
        words = seg.get("words")
        if words:
            for w in words:
                txt = w.get("word", "")
                if txt is None:
                    continue
                yield txt, w.get("start"), w.get("end")
        else:
            # segment without word timestamps: treat whole segment as one unit
            yield seg.get("text", ""), seg.get("start"), seg.get("end")


def split_sentences(whisper_json: dict) -> list[dict]:
    """Re-split word stream into one cue per Japanese sentence.

    Break on sentence-ending punctuation; start = first word, end = closing word.
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

    for txt, s, e in iter_words(whisper_json):
        if txt is None:
            txt = ""
        if start is None and txt.strip():
            start = s
        buf.append(txt)
        if e is not None:
            last_end = e
        # close sentence when this token carries terminal punctuation
        if any(p in txt for p in SENT_END):
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


def _anthropic_translate(sentences: list[str], model: str) -> list[str]:
    """Batch-translate JA->EN via the Anthropic Messages API (stdlib HTTP)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        die("ANTHROPIC_API_KEY not set", "export ANTHROPIC_API_KEY=sk-ant-...")
    out: list[str] = []
    BATCH = 50  # few API calls, not one-per-sentence
    for i in range(0, len(sentences), BATCH):
        chunk = sentences[i:i + BATCH]
        numbered = "\n".join(f"{j+1}. {s}" for j, s in enumerate(chunk))
        prompt = (
            "Translate each numbered Japanese sentence into natural but faithful "
            "English for an A1 beginner learner. Keep the same numbering. "
            "Return ONLY a JSON array of strings, in order, no commentary.\n\n"
            + numbered
        )
        body = json.dumps({
            "model": model,
            "max_tokens": 4096,
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
        text = "".join(p.get("text", "") for p in resp.get("content", []))
        arr = _extract_json_array(text)
        if len(arr) != len(chunk):
            warn(f"batch {i//BATCH}: expected {len(chunk)} translations, got {len(arr)}; "
                 "padding/truncating to align")
            arr = (arr + [""] * len(chunk))[:len(chunk)]
        out.extend(arr)
        info(f"translated {min(i+BATCH, len(sentences))}/{len(sentences)}")
    return out


def _extract_json_array(text: str) -> list[str]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
        return [str(x) for x in arr]
    except json.JSONDecodeError:
        return []


# backend registry: swap translation backend by name without touching callers
TRANSLATORS = {"anthropic": _anthropic_translate}


def translate_sentences(sentences: list[str], backend: str, model: str) -> list[str]:
    fn = TRANSLATORS.get(backend)
    if not fn:
        die(f"unknown translate backend: {backend}",
            f"available: {', '.join(TRANSLATORS)}")
    return fn(sentences, model)


def stage_translate(srt_path: Path, wd: Path, vid: str, backend: str,
                    model: str, auto_yes: bool) -> tuple[Path, bool]:
    tsv_path = wd / f"{vid}.tsv"
    if reuse(tsv_path, auto_yes):
        produced(tsv_path)
        return tsv_path, False
    cues = parse_srt(srt_path.read_text(encoding="utf-8"))
    if not cues:
        die("SRT empty/malformed; nothing to translate", f"inspect {srt_path}")
    ja = [c["text"] for c in cues]
    info(f"translating {len(ja)} sentences via backend '{backend}' ({model})")
    en = translate_sentences(ja, backend, model)
    with tsv_path.open("w", encoding="utf-8", newline="") as f:
        for j, e in zip(ja, en):
            # TAB-separated; strip tabs/newlines from fields to keep it clean
            f.write(j.replace("\t", " ").replace("\n", " ") + "\t" +
                    e.replace("\t", " ").replace("\n", " ") + "\n")
    produced(tsv_path)
    return tsv_path, True


# ===========================================================================
# STAGE 5 — BUILD DECK  (substudy export csv + merge translations -> .apkg)
# ===========================================================================
def _find_substudy_csv(out_dir: Path) -> Path:
    # substudy nests output in a <name>_csv/ subdir, so search recursively
    for name in ("cards.csv", "index.csv", "export.csv"):
        hits = list(out_dir.rglob(name))
        if hits:
            return hits[0]
    csvs = list(out_dir.rglob("*.csv"))
    if csvs:
        return csvs[0]
    die(f"could not find substudy csv in {out_dir}", "inspect that folder manually")


_AUDIO_RE = re.compile(r"([^\s\[\]\"'<>:]+\.(?:mp3|m4a|wav|oga|ogg))", re.I)


def _audio_field(row_vals: list[str]) -> str | None:
    """Find an audio clip filename in a substudy csv row.

    substudy wraps it as `[sound:clip.mp3]`, so match the filename anywhere in
    the field rather than requiring the value to *end* in an audio extension.
    """
    for v in row_vals:
        if isinstance(v, str):
            m = _AUDIO_RE.search(v)
            if m:
                return Path(m.group(1)).name
    return None


def _ts_like(s: str) -> bool:
    return bool(re.match(r"^\d+(\.\d+)?$|^\d{2}:\d{2}", s.strip()))


def _japanese_field(row_vals: list[str]) -> str:
    """Pick the field most likely to be the Japanese subtitle text."""
    cand = [v for v in row_vals
            if isinstance(v, str)
            and not v.strip().lower().endswith((".mp3", ".m4a", ".wav", ".jpg", ".png", ".ogg", ".oga"))
            and not _ts_like(v)]
    if not cand:
        return ""
    # prefer the field containing CJK characters
    for v in cand:
        if re.search(r"[぀-ヿ一-鿿]", v):
            return v.strip()
    return max(cand, key=len).strip()


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
                     vid: str, deck_name: str, upstream_ran: bool,
                     auto_yes: bool) -> tuple[Path, Path]:
    export_dir = wd / f"{vid}_substudy"
    apkg = wd / f"{vid}.apkg"

    # reuse an existing apkg only if no upstream stage actually ran this run;
    # otherwise rebuild it so it reflects the fresh upstream output.
    if not upstream_ran and reuse(apkg, auto_yes):
        produced(apkg)
        _print_import_instructions(apkg)
        return apkg, export_dir
    if apkg.exists() and upstream_ran:
        info("upstream stage ran; rebuilding .apkg")

    if export_dir.exists():
        # substudy refuses to overwrite; clear stale dir
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    # substudy export csv <media> <foreign-subs>  -> cuts per-cue audio clips + csv
    # absolute paths: we run with cwd=export_dir, so relative inputs wouldn't resolve
    run(["substudy", "export", "csv", str(mp3.resolve()), str(srt_path.resolve())],
        cwd=export_dir)

    sub_csv = _find_substudy_csv(export_dir)
    clip_dir = sub_csv.parent  # mp3 clips live next to the csv
    info(f"substudy csv: {sub_csv}")

    # english translations aligned to srt order
    en = []
    for line in tsv_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        en.append(parts[1] if len(parts) > 1 else "")

    # read substudy rows (gives us the per-cue audio clip filename + JA text)
    with sub_csv.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        die("substudy csv is empty", f"inspect {sub_csv}")

    # detect header vs data
    header_row = rows[0]
    has_header = any(not _ts_like(c) and "." not in c for c in header_row) and \
        not _audio_field(header_row)
    data_rows = rows[1:] if has_header else rows
    info(f"substudy produced {len(data_rows)} card rows")

    if len(data_rows) != len(en):
        warn(f"row count mismatch: {len(data_rows)} clips vs {len(en)} translations; "
             "aligning by index (extra entries dropped)")

    # Build cards: front=JA + [sound:clip] (audio on the JP side), back=EN.
    cards: list[tuple[str, str, list[Path]]] = []
    for i in range(min(len(data_rows), len(en))):
        row_vals = data_rows[i]
        front = _japanese_field(row_vals)
        back = en[i]
        clips: list[Path] = []
        audio = _audio_field(row_vals)
        if audio:
            clip = clip_dir / audio
            if clip.exists():
                front = f"{front}<br>[sound:{audio}]"
                clips.append(clip)
        cards.append((front, back, clips))

    write_apkg(apkg, deck_name, cards)
    produced(apkg)
    _print_import_instructions(apkg)
    return apkg, export_dir


# ===========================================================================
# MAIN
# ===========================================================================
STAGES_TOTAL = 5


def main() -> None:
    ap = argparse.ArgumentParser(description="YouTube -> Anki deck (Japanese)")
    ap.add_argument("url", help="YouTube video URL")
    ap.add_argument("--yes", action="store_true", help="skip all continue? gates (unattended)")
    ap.add_argument("--model", default="small", help="Whisper model (default: small)")
    ap.add_argument("--translate-backend", default="anthropic",
                    help="translation backend (default: anthropic)")
    ap.add_argument("--translate-model", default="claude-sonnet-4-6",
                    help="translation model (default: claude-sonnet-4-6)")
    ap.add_argument("--workdir", default="work", help="base working dir (default: ./work)")
    ap.add_argument("--title", default=None,
                    help="deck title (default: the YouTube video title)")
    args = ap.parse_args()

    # load .env (script dir first, then cwd); real env vars still take precedence
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path(".env"))

    check_tools(["yt-dlp", "whisper", "substudy", "ffmpeg"])

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
    gate(args.yes)

    header(2, STAGES_TOTAL, f"TRANSCRIBE (Whisper, model={args.model})")
    json_path, ran_tx = stage_transcribe(mp3, wd, vid, args.model, args.yes)
    gate(args.yes)

    header(3, STAGES_TOTAL, "SENTENCE SPLIT (strict SRT, deduped)")
    srt_path, ran_sp = stage_sentence_split(json_path, wd, vid, args.yes)
    gate(args.yes)

    header(4, STAGES_TOTAL, f"TRANSLATE (JA->EN, {args.translate_backend})")
    tsv_path, ran_tr = stage_translate(srt_path, wd, vid, args.translate_backend,
                                       args.translate_model, args.yes)
    gate(args.yes)

    header(5, STAGES_TOTAL, "BUILD DECK (substudy clips + translations -> .apkg)")
    upstream_ran = ran_dl or ran_tx or ran_sp or ran_tr
    apkg, export_dir = stage_build_deck(mp3, srt_path, tsv_path, wd, vid,
                                        deck_name, upstream_ran, args.yes)
    gate(args.yes)

    print()
    good(f"DONE. Anki deck: {apkg}")


if __name__ == "__main__":
    main()
