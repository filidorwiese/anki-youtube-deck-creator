# yt2anki

Turn a YouTube video into an [Anki](https://apps.ankiweb.net/) deck for learning
Japanese. Each card is one sentence: the Japanese on the front, an English
translation plus the matching audio clip on the back.

It's a single Python script that chains together a few command-line tools:
download the video, transcribe the speech, split it into sentences, translate
them, and cut the audio into per-sentence clips.

## What you need

The script needs one Python package ([genanki](https://github.com/kerrickstaley/genanki),
to build the `.apkg`) and calls these external command-line tools (it checks for
them on startup and tells you how to install any that are missing):

| Tool | What it does | Install |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | downloads the video & audio | `pipx install yt-dlp` |
| [whisper](https://github.com/openai/whisper) | transcribes Japanese speech | `pip install -U openai-whisper` |
| [substudy](https://github.com/emk/subtitles-rs) | cuts per-sentence audio clips | `cargo install substudy` |
| [ffmpeg](https://ffmpeg.org/) | audio/video processing | `apt install ffmpeg` / `brew install ffmpeg` |

You also need an [Anthropic API key](https://console.anthropic.com/) for the
translation step.

## Setup

1. Install the Python dependency in a virtual environment:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. Put your API key in a `.env` file next to the script:

   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   (`.env` is gitignored. You can also just export the variable in your shell —
   an exported value takes precedence over `.env`.)

## Usage

Run with the venv's Python:

```bash
.venv/bin/python yt2anki.py "https://www.youtube.com/watch?v=..."
```

By default it pauses after each step so you can check the output before
continuing. Press Enter to go on, Ctrl-C to stop.

Handy options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--yes` | off | run all the way through without pausing |
| `--model` | `small` | Whisper model size (`tiny`/`base`/`small`/`medium`/`large`) — bigger is more accurate but slower |
| `--translate-model` | `claude-sonnet-4-6` | which Claude model translates |
| `--workdir` | `./work` | where intermediate files are saved |

Everything for a video is saved under `work/<video-id>/`. If you re-run the same
video, finished steps are reused instead of redone, so it's safe to stop and
start again.

## What you get

When it finishes you'll have, inside `work/<video-id>/`:

- `<video-id>.apkg` — a ready-to-import Anki deck with the audio bundled in

## Importing into Anki

Double-click the `.apkg`, or in Anki choose **File > Import** and select it. The
note type, cards, and audio are all included — no manual media copying needed.

## How it works

The pipeline runs in five steps:

1. **Download** the video and extract the audio (yt-dlp).
2. **Transcribe** the Japanese audio with word-level timing (Whisper).
3. **Split** the transcript into one card per sentence.
4. **Translate** each sentence to English (Claude).
5. **Build the deck** — cut an audio clip per sentence (substudy) and package
   the sentences, translations, and clips into a single `.apkg` (genanki).
