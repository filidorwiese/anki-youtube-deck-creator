# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-file CLI (`yt2anki.py`) that turns a YouTube video into an Anki deck for
Japanese learning. Mostly stdlib; one pip dep (`genanki`, lazily imported in
`write_apkg`) and several external CLI tools it orchestrates.

## Run

Debian is PEP-668 (externally-managed), so genanki lives in a project venv:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python yt2anki.py "<youtube-url>"          # interactive, gates between stages
.venv/bin/python yt2anki.py "<url>" --yes            # unattended, no gates
.venv/bin/python yt2anki.py "<url>" --model medium   # bigger Whisper model
```

`ANTHROPIC_API_KEY` is required for the translate stage; `load_dotenv()` reads it
from `.env` (script dir then cwd) at the top of `main()`, but a real env var wins.

Key flags: `--yes` (skip all gates), `--model` (Whisper size, default `small`),
`--translate-model` (default `claude-sonnet-4-6`), `--translate-backend`
(default `anthropic`), `--workdir` (default `./work`).

External tools required (checked at startup with a ✓/✗ checklist, install hints
printed if missing): `yt-dlp`, `whisper`, `substudy`, `ffmpeg`.

## Architecture

5-stage pipeline in `main()`, each stage a `stage_*` function:

1. `stage_download` — yt-dlp: `<vid>.mp3` (audio) + source video (mp4/mkv/webm).
2. `stage_transcribe` — Whisper word-level JSON `<vid>.json`.
3. `stage_sentence_split` — pure-python re-split of the word stream into one cue
   per Japanese sentence -> strict SRT `<vid>.srt`. The only logic-heavy,
   side-effect-free part (`split_sentences`/`dedupe`/`to_srt`/`parse_srt`).
4. `stage_translate` — JA->EN via backend registry -> TSV `<vid>.tsv`.
5. `stage_build_deck` — substudy cuts per-cue audio clips + csv; merged with
   translations and packaged via `write_apkg` (genanki) into a self-contained
   `<vid>.apkg` (front=JA + `[sound:clip]` so audio is on the JP side, back=EN,
   audio bundled). Deck is named `youtube::<title>`; the title comes from
   `--title`, else `video_title()` (yt-dlp, cached to `<vid>.title`), and unless
   `--yes`/`--title` the user can edit it via an inline `prompt_edit()` prompt.

Cross-cutting conventions to preserve when editing:

- **Per-video working dir** `work/<video_id>/`; every artifact is named
  `<vid>.<ext>` so reruns are isolated and individually reusable.
- **Idempotency**: each stage calls `reuse(path, auto_yes)` first; if the output
  exists it asks skip-vs-recreate (auto-reuses under `--yes`). Stages return a
  `ran` flag; `stage_build_deck` rebuilds the `.apkg` when any upstream stage ran
  (`upstream_ran`). Keep new stages following this so partial reruns work.
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
- `.env`, `__pycache__`, `.venv/`, `.idea/`, and `work/` are gitignored.
  Generated `work/` output is large (audio/video) and should not be committed.
- `.apkg` uses genanki's legacy schema_v11; stable `APKG_MODEL_ID`/`APKG_DECK_ID`
  so re-imports update rather than duplicate the model/deck.
