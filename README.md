# CLIPFORGE

Paste a YouTube link (or upload a video). CLIPFORGE finds the best moments, turns them into vertical Shorts
with face tracking, animated captions, hook titles, zooms, filler removal, B-roll and your brand template,
lets you edit them in a clean web app, and downloads them. Runs in the cloud, works from a phone.

## Quick start (local)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y ffmpeg          # needs ffmpeg with libass (Ubuntu's package has it)
cp .env.example .env                    # fill in APP_PASSWORD and ANTHROPIC_API_KEY
python -m clipforge serve               # open http://localhost:8000
```

Command line:

```bash
python -m clipforge "https://www.youtube.com/watch?v=..." --clips 5
python -m clipforge episode.mp4 --clips 3 --length medium --ratio 9:16
```

Output lands in `output/<project id>/clip_01.mp4` with a `clip_01.json` next to it that records every decision.

## How it works

1. Download (yt-dlp, cached) or take an upload.
2. Transcribe with word timestamps (Whisper large-v3 on a GPU, NVIDIA Parakeet on a CPU, cached).
3. Claude picks the best moments (JSON only), snapped to word boundaries. Without a key a keyword rule is used.
4. Claude Haiku fact-checks every clip and writes an honest title.
5. Render: frame for 9:16, loudness at -14 LUFS, then captions and effects.

See `config.yaml` for every tunable value and `DEPLOY.md` for hosting.

## Tests

```bash
pip install pytest && python -m pytest -q tests
```
