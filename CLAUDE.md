# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Single-file CLI (`yt2anki.py`) that turns a YouTube video into an Anki deck for
language learning (source language set via `--source-lang`, translated into
`--user-lang`, default `en`). Mostly stdlib; one pip dep (`genanki`, lazily
imported in `write_apkg`) and several external CLI tools it orchestrates.

## Run

Debian is PEP-668 (externally-managed), so genanki lives in a project venv:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python yt2anki.py --source-lang ja "<youtube-url>"   # --source-lang is REQUIRED
.venv/bin/python yt2anki.py --source-lang ja "<url>" --yes     # unattended (auto-reuse)
.venv/bin/python yt2anki.py --source-lang fr --user-lang nl "<url>"
```

Without `--source-lang` the script exits with an argparse usage error.

`ANTHROPIC_API_KEY` is required for the translate stage; `load_dotenv()` reads it
from `.env` (script dir then cwd) at the top of `main()`, but a real env var wins.

Key flags: `--source-lang` (required), `--user-lang` (default `en`), `--title`,
`--yes` (unattended: auto-reuse outputs, skip title prompt), `--clip-pad`
(seconds of audio padding each side of a clip, default `0.15`), `--model`
(Whisper size, default `small`), `--translate-model` (default
`claude-sonnet-4-6`), `--translate-backend` (default `anthropic`), `--workdir`
(default `./work`). `lang_name()` maps common ISO codes to names for the prompt;
unknown codes pass through unchanged.

External tools required (checked at startup with a ✓/✗ checklist, install hints
printed if missing): `yt-dlp`, `whisper`, `ffmpeg`.

## Architecture

5-stage pipeline in `main()`, each stage a `stage_*` function:

1. `stage_download` — yt-dlp: `<vid>.mp3` (audio) + source video (mp4/mkv/webm).
2. `stage_transcribe` — Whisper word-level JSON `<vid>.json` (`--language` =
   `--source-lang`).
3. `stage_sentence_split` — pure-python re-split of the word stream into one cue
   per sentence -> strict SRT `<vid>.srt`. Breaks on terminal punctuation, else
   at Whisper segment boundaries (`iter_words` flags each segment's last token
   via `seg_end`; Whisper's word-level JA output rarely emits 。！？, so the
   fallback prevents everything collapsing into one cue). The only logic-heavy,
   side-effect-free part (`split_sentences`/`dedupe`/`to_srt`/`parse_srt`).
4. `stage_translate` — source-lang->user-lang via backend registry -> TSV
   `<vid>.tsv`.
5. `stage_build_deck` — `cut_clip` (ffmpeg) slices one tight clip per SRT cue,
   padded by `--clip-pad`; source text comes from the SRT, translations from the
   TSV, packaged via `write_apkg` (genanki) into a self-contained `<vid>.apkg`
   (front=source + `[sound:clip]` so audio is on the source side, back=user,
   audio bundled). Deck is named `youtube::<title>`; the title comes from
   `--title`, else `video_title()` (yt-dlp, cached to `<vid>.title`), and unless
   `--yes`/`--title` the user can edit it via an inline `prompt_edit()` prompt.

Cross-cutting conventions to preserve when editing:

- **Per-video working dir** `work/<video_id>/`; every artifact is named
  `<vid>.<ext>` so reruns are isolated and individually reusable.
- **Idempotency**: each stage calls `reuse(path, auto_yes)` first; if the output
  exists it asks skip-vs-recreate (auto-reuses under `--yes`). Picking recreate
  is sticky (`_RECREATE_REST`): every later stage recreates without re-asking.
  Stages return a `ran` flag; `stage_build_deck` rebuilds the `.apkg` when any
  upstream stage ran (`upstream_ran`). Keep new stages following this so partial
  reruns work.
- **No inter-stage gates**: the pipeline runs straight through (the old `gate()`
  Enter-pause between stages was removed); `--yes` only affects reuse + the title
  prompt.
- **Fail loud**: use `die(msg, hint)` for unrecoverable errors (exits 1 with an
  install/fix hint), `warn()` for recoverable misalignment.
- **Translation backends** live in the `TRANSLATORS` dict — add a backend by
  registering a `fn(sentences, model, source_lang, user_lang) -> list[str]`,
  don't touch callers.

## Notes

- No test suite exists despite the `[unit-tested]` comment on stage 3; the
  split/SRT functions are the natural place to add tests if asked.
- `.env`, `__pycache__`, `.venv/`, `.idea/`, and `work/` are gitignored.
  Generated `work/` output is large (audio/video) and should not be committed.
- `.apkg` uses genanki's legacy schema_v11; stable `APKG_MODEL_ID`/`APKG_DECK_ID`
  so re-imports update rather than duplicate the model/deck.
