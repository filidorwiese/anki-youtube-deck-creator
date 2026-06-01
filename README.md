# yt2anki

Turn a YouTube video into an [Anki](https://apps.ankiweb.net/) deck for learning
a language. Each card is one sentence: the source-language sentence plus its
audio clip on the front, and a translation into your language on the back.
Each card also gets a short grammar/usage note on the back (the key grammar point
plus the dictionary form of an inflected verb/adjective/adverb). Japanese sources
additionally get furigana readings over the kanji, and with `--color-words` the
matching words in the source and translation are shown in the same color.

It's a single Python script that chains together a few command-line tools:
download the video, transcribe the speech, split it into sentences, translate
them, and cut the audio into per-sentence clips.

## What you need

The script calls a few external command-line tools (it checks for them on
startup and tells you how to install any that are missing). Most are Python
packages installed from `requirements.txt` into the venv; only `ffmpeg` is a
separate system binary:

| Tool | What it does | Install                                      |
| --- | --- |----------------------------------------------|
| [genanki](https://github.com/kerrickstaley/genanki) | builds the `.apkg` deck | `requirements.txt`                           |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | downloads the video & audio | `requirements.txt`                           |
| [whisper](https://github.com/openai/whisper) | transcribes the speech with word-level timing | `requirements.txt` (pulls in torch — large)  |
| [ffmpeg](https://ffmpeg.org/) | cuts the per-sentence audio clips | `apt install ffmpeg` / `brew install ffmpeg` |

`yt-dlp` and `whisper` are found in the venv automatically — no need to activate
it or install them globally. `ffmpeg` stays a system dependency (it isn't
pip-installable, and whisper needs it at runtime too).

You also need an [Anthropic API key](https://console.anthropic.com/) for the
translation step.

## Setup

1. Install the Python dependencies in a virtual environment (this includes
   whisper's torch, so it's a large download the first time):

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

   Then install `ffmpeg` via your system package manager
   (`apt install ffmpeg` / `brew install ffmpeg`).

2. Put your API key in a `.env` file next to the script:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   (`.env` is gitignored. You can also just export the variable in your shell —
   an exported value takes precedence over `.env`.)

## Usage

Run with the venv's Python. `--source-lang` (the spoken language of the video)
is required; `--user-lang` defaults to `en`:

```bash
.venv/bin/python yt2anki.py --source-lang ja "https://www.youtube.com/watch?v=..."
```

It runs straight through the five steps. The deck is named after the YouTube
video; the title is shown as an editable prompt so you can tweak it.

Handy options:

| Option | Default             | Meaning |
| --- |---------------------| --- |
| `--source-lang` | _required_          | spoken language of the video (e.g. `ja`, `fr`) |
| `--user-lang` | `en`                | the language to translate into |
| `--yes` | off                 | run unattended: reuse existing output and skip the title prompt |
| `--title` | video title         | name the deck yourself (skips the title prompt) |
| `--clip-pad` | `0.50`              | seconds of audio padding added to each side of a clip |
| `--color-words` | off                 | color-code matching words across the source and translation (extra Claude pass) |
| `--screenshots` | off                 | add a video frame (at each sentence start) to the card front (larger `.apkg`) |
| `--shot-offset` | `0.3`               | seconds after the sentence start to grab the screenshot (`0` = exact start, risks scene-cut frames) |
| `--model` | `small`             | Whisper model size (`tiny`/`base`/`small`/`medium`/`large`) — bigger is more accurate but slower |
| `--translate-model` | `claude-sonnet-4-6` | which Claude model translates |
| `--workdir` | `./work`            | where intermediate files are saved |

Everything for a video is saved under `work/<video-id>/`. If a step's output
already exists, it asks whether to reuse it or recreate it, so it's safe to stop
and start again. Choosing recreate at one step recreates every later step too;
`--yes` always reuses without asking.

## What you get

When it finishes you'll have, inside `work/<video-id>/`:

- `<video-id>.apkg` — a ready-to-import Anki deck with the audio bundled in

## Importing into Anki

Double-click the `.apkg`, or in Anki choose **File > Import** and select it. The
note type, cards, and audio are all included — no manual media copying needed.

## How it works

The pipeline runs in five steps:

1. **Download** the video and extract the audio (yt-dlp).
2. **Transcribe** the audio with word-level timing (Whisper).
3. **Split** the transcript into one card per sentence (on sentence punctuation,
   falling back to Whisper's segment boundaries when none is present).
4. **Translate** each sentence into your language (Claude). The same call also
   writes a short grammar/usage note (and, for Japanese, furigana readings), and
   with `--color-words` it tags which source words match which translation words
   so they can be colored the same.
5. **Build the deck** — cut a tight audio clip per sentence (ffmpeg, padded by
   `--clip-pad`), optionally grab a video frame per sentence (`--screenshots`),
   and package the sentences, translations, clips, and frames into a single
   `.apkg` (genanki).
