# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-file CLI (`yt2anki.py`) that turns a YouTube video into an Anki deck for
Japanese learning. Pure stdlib Python (no third-party imports); orchestrates
external CLI tools instead.

## Run

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # required for translate stage (.env holds it locally)
./yt2anki.py "<youtube-url>"               # interactive, gates between stages
./yt2anki.py "<url>" --yes                 # unattended, no gates
./yt2anki.py "<url>" --model medium        # bigger Whisper model
```

Key flags: `--yes` (skip all gates), `--model` (Whisper size, default `small`),
`--translate-model` (default `claude-sonnet-4-6`), `--translate-backend`
(default `anthropic`), `--workdir` (default `./work`).

External tools required (checked at startup, install hints printed if missing):
`yt-dlp`, `whisper`, `substudy`, `ffmpeg`. `deno` is a soft dep yt-dlp needs for
YouTube JS challenges.

## Architecture

5-stage pipeline in `main()`, each stage a `stage_*` function:

1. `stage_download` — yt-dlp: `<vid>.mp3` (audio) + source video (mp4/mkv/webm).
2. `stage_transcribe` — Whisper word-level JSON `<vid>.json`.
3. `stage_sentence_split` — pure-python re-split of the word stream into one cue
   per Japanese sentence -> strict SRT `<vid>.srt`. The only logic-heavy,
   side-effect-free part (`split_sentences`/`dedupe`/`to_srt`/`parse_srt`).
4. `stage_translate` — JA->EN via backend registry -> TSV `<vid>.tsv`.
5. `stage_build_deck` — substudy cuts per-cue audio clips + csv, merged with
   translations into `<vid>.anki.tsv` (front=JA, back=EN + `[sound:clip]`).

Cross-cutting conventions to preserve when editing:

- **Per-video working dir** `work/<video_id>/`; every artifact is named
  `<vid>.<ext>` so reruns are isolated and individually reusable.
- **Idempotency**: each stage calls `reuse(path)` first; if the output exists it
  offers to skip. Keep new stages following this so partial reruns work.
- **Gate model**: `gate()` pauses for Enter after each stage unless `--yes`.
- **Fail loud**: use `die(msg, hint)` for unrecoverable errors (exits 1 with an
  install/fix hint), `warn()` for recoverable misalignment.
- **Translation backends** live in the `TRANSLATORS` dict — add a backend by
  registering a `fn(sentences, model) -> list[str]`, don't touch callers.
- `_japanese_field` / `_audio_field` / header-detection in stage 5 are defensive
  heuristics because substudy's CSV column layout isn't guaranteed.

## Notes

- No test suite exists despite the `[unit-tested]` comment on stage 3; the
  split/SRT functions are the natural place to add tests if asked.
- `.env` and `__pycache__` are gitignored. Generated `work/` output is large
  (audio/video) and should not be committed.
