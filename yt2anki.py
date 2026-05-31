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


def reuse(path: Path) -> bool:
    """Idempotency: if an output already exists, skip the step that makes it.

    Returns True (skip) whenever the file is present and non-empty, so reruns
    don't redo expensive work like Whisper. Delete the file to force a redo.
    """
    if not path.exists() or path.stat().st_size == 0:
        return False
    info(f"exists, skipping step (delete to redo): {path}")
    return True


# ---------------------------------------------------------------------------
# Tool checks
# ---------------------------------------------------------------------------
INSTALL_HINTS = {
    "yt-dlp": "pipx install yt-dlp   (or: python3 -m pip install -U yt-dlp)",
    "whisper": "python3 -m pip install -U openai-whisper",
    "substudy": "cargo install substudy   (needs Rust: https://rustup.rs)",
    "ffmpeg": "sudo apt install ffmpeg   (Debian/Ubuntu)  |  brew install ffmpeg",
    "deno": "curl -fsSL https://deno.land/install.sh | sh",
}


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def check_tools(required: list[str]) -> None:
    missing = [t for t in required if not have(t)]
    if missing:
        err("missing required tools: " + ", ".join(missing))
        for t in missing:
            print(_c("0;33", f"    {t}: {INSTALL_HINTS.get(t, '')}"), file=sys.stderr)
        sys.exit(1)
    good("all required tools present: " + ", ".join(required))


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


# ===========================================================================
# STAGE 1 — DOWNLOAD
# ===========================================================================
def stage_download(url: str, wd: Path, vid: str, auto_yes: bool) -> tuple[Path, Path]:
    mp3 = wd / f"{vid}.mp3"
    # video container is unknown ahead of time; find any existing non-mp3 media
    existing_video = next(
        (p for p in wd.glob(f"{vid}.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm")),
        None,
    )

    if not have("deno"):
        warn("deno (JS runtime) not found. Recent yt-dlp needs it for YouTube's "
             "JS challenges; download may fail with 'player' / nsig errors.")
        print(_c("0;33", f"    install deno: {INSTALL_HINTS['deno']}"), file=sys.stderr)

    # --- audio mp3 ---
    if reuse(mp3):
        info("skipping audio download")
    else:
        run(["yt-dlp", "-x", "--audio-format", "mp3", "--audio-quality", "0",
             "-o", str(wd / f"{vid}.%(ext)s"), url])
    if not mp3.exists():
        die("audio mp3 not produced", "check yt-dlp output above; deno may be required")
    produced(mp3)

    # --- source video (kept for later frame extraction) ---
    if existing_video and reuse(existing_video):
        video = existing_video
        info("skipping video download")
    else:
        run(["yt-dlp", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", str(wd / f"{vid}.%(ext)s"), url])
        video = next(
            (p for p in wd.glob(f"{vid}.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm")),
            None,
        )
    if not video or not video.exists():
        die("source video not produced", "check yt-dlp output above")
    produced(video)
    return mp3, video


# ===========================================================================
# STAGE 2 — TRANSCRIBE (Whisper, word-level JSON)
# ===========================================================================
def stage_transcribe(mp3: Path, wd: Path, vid: str, model: str, auto_yes: bool) -> Path:
    out_json = wd / f"{vid}.json"
    if reuse(out_json):
        produced(out_json)
        return out_json
    run(["whisper", str(mp3),
         "--language", "ja", "--task", "transcribe",
         "--word_timestamps", "True", "--model", model,
         "--output_format", "json", "--output_dir", str(wd)])
    # whisper names output after the input stem: <vid>.json
    if not out_json.exists():
        die(f"whisper did not produce {out_json}", "check whisper output above")
    produced(out_json)
    return out_json


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


def stage_sentence_split(json_path: Path, wd: Path, vid: str, auto_yes: bool) -> Path:
    srt_path = wd / f"{vid}.srt"
    if reuse(srt_path):
        produced(srt_path)
        return srt_path
    data = json.loads(json_path.read_text(encoding="utf-8"))
    cues = split_sentences(data)
    info(f"split into {len(cues)} sentence cues")
    cues = dedupe(cues)
    info(f"after dedup: {len(cues)} cues")
    if not cues:
        die("no sentences produced from transcript", "is the Whisper JSON empty/malformed?")
    srt_path.write_text(to_srt(cues), encoding="utf-8")
    produced(srt_path)
    return srt_path


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
                    model: str, auto_yes: bool) -> Path:
    tsv_path = wd / f"{vid}.tsv"
    if reuse(tsv_path):
        produced(tsv_path)
        return tsv_path
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
    return tsv_path


# ===========================================================================
# STAGE 5 — BUILD DECK  (substudy export csv + merge translations)
# ===========================================================================
def _find_substudy_csv(out_dir: Path) -> Path:
    for name in ("cards.csv", "index.csv", "export.csv"):
        p = out_dir / name
        if p.exists():
            return p
    csvs = list(out_dir.glob("*.csv"))
    if csvs:
        return csvs[0]
    die(f"could not find substudy csv in {out_dir}", "inspect that folder manually")


def _audio_field(row: dict, row_vals: list[str]) -> str | None:
    """Find an mp3/audio reference in a substudy csv row."""
    for v in list(row.values()) + row_vals:
        if isinstance(v, str) and v.strip().lower().endswith((".mp3", ".m4a", ".wav", ".oga", ".ogg")):
            return Path(v.strip()).name
    return None


def stage_build_deck(mp3: Path, srt_path: Path, tsv_path: Path, wd: Path,
                     vid: str, auto_yes: bool) -> tuple[Path, Path]:
    export_dir = wd / f"{vid}_substudy"
    final = wd / f"{vid}.anki.tsv"

    if final.exists() and export_dir.exists() and reuse(final):
        produced(final)
        _print_import_instructions(final, export_dir)
        return final, export_dir

    if export_dir.exists():
        # substudy refuses to overwrite; clear stale dir
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)

    # substudy export csv <media> <foreign-subs>  -> cuts per-cue audio clips + csv
    run(["substudy", "export", "csv", str(mp3), str(srt_path)], cwd=export_dir)

    sub_csv = _find_substudy_csv(export_dir)
    info(f"substudy csv: {sub_csv}")

    # english translations aligned to srt order
    en = []
    for line in tsv_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        en.append(parts[1] if len(parts) > 1 else "")

    # read substudy rows (gives us the per-cue audio clip filename + JA text)
    with sub_csv.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        die("substudy csv is empty", f"inspect {sub_csv}")

    # detect header vs data
    header_row = rows[0]
    has_header = any(not _ts_like(c) and "." not in c for c in header_row) and \
        not _audio_field({}, header_row)
    data_rows = rows[1:] if has_header else rows
    info(f"substudy produced {len(data_rows)} card rows")

    if len(data_rows) != len(en):
        warn(f"row count mismatch: {len(data_rows)} clips vs {len(en)} translations; "
             "aligning by index (extra entries dropped)")

    # Build final TAB-separated import file: front=JA, back=EN <br> [sound:clip]
    with final.open("w", encoding="utf-8", newline="") as f:
        n = min(len(data_rows), len(en))
        for i in range(n):
            row_vals = data_rows[i]
            audio = _audio_field({}, row_vals)
            ja = _japanese_field(row_vals)
            back = en[i]
            if audio:
                back = f"{back}<br>[sound:{audio}]"
            f.write(f"{ja}\t{back}\n")
    produced(final)
    _print_import_instructions(final, export_dir)
    return final, export_dir


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


def _print_import_instructions(final: Path, export_dir: Path) -> None:
    media = "~/.local/share/Anki2/<profile>/collection.media/"
    print()
    print(_c("1;32", "  HOW TO IMPORT INTO ANKI"))
    print(_c("0;37", f"   1. Copy clips:  cp {export_dir}/*.mp3  {media}"))
    print(_c("0;37", f"   2. Anki: File > Import > {final}"))
    print(_c("0;37", "   3. Type: Basic. Field separator: Tab. Allow HTML in fields: YES."))
    print(_c("0;37", "   4. Field mapping:  column 1 -> Front (Japanese)"))
    print(_c("0;37", "                      column 2 -> Back  (English + [sound:...])"))


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
    args = ap.parse_args()

    # load .env (script dir first, then cwd); real env vars still take precedence
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(Path(".env"))

    check_tools(["yt-dlp", "whisper", "substudy", "ffmpeg"])

    vid = video_id(args.url)
    wd = Path(args.workdir) / vid
    wd.mkdir(parents=True, exist_ok=True)
    good(f"video id: {vid}   working dir: {wd}")

    header(1, STAGES_TOTAL, "DOWNLOAD (yt-dlp: mp3 audio + source video)")
    mp3, _video = stage_download(args.url, wd, vid, args.yes)
    gate(args.yes)

    header(2, STAGES_TOTAL, f"TRANSCRIBE (Whisper, model={args.model})")
    json_path = stage_transcribe(mp3, wd, vid, args.model, args.yes)
    gate(args.yes)

    header(3, STAGES_TOTAL, "SENTENCE SPLIT (strict SRT, deduped)")
    srt_path = stage_sentence_split(json_path, wd, vid, args.yes)
    gate(args.yes)

    header(4, STAGES_TOTAL, f"TRANSLATE (JA->EN, {args.translate_backend})")
    tsv_path = stage_translate(srt_path, wd, vid, args.translate_backend,
                               args.translate_model, args.yes)
    gate(args.yes)

    header(5, STAGES_TOTAL, "BUILD DECK (substudy export csv + merge)")
    final, export_dir = stage_build_deck(mp3, srt_path, tsv_path, wd, vid, args.yes)
    gate(args.yes)

    print()
    good(f"DONE. Import file: {final}")
    good(f"Audio clips dir:  {export_dir}")


if __name__ == "__main__":
    main()
