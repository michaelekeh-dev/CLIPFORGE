# Deploying CLIPFORGE

Plain steps. Budget about 30 minutes the first time. You need: a credit card for the server (about €7 a month)
and one for Anthropic (pay as you go, a few euros a month at 3 to 5 episodes a week).

## Where it runs

**One small always-on server (Hetzner Cloud CX32: 4 CPUs, 8 GB RAM, 80 GB disk, about €7/month).**
Everything runs on the CPU: transcription with NVIDIA Parakeet (about 7 minutes for a 2 hour episode),
rendering about 1 minute per clip. A 2 hour episode with 5 clips takes about 15 minutes end to end.
No GPU bill, nothing to switch on or off, data stays on the server's disk and survives restarts.

Estimated monthly cost at 3 to 5 episodes a week:

| What | Cost |
|---|---|
| Hetzner CX32 server | about €7 |
| Anthropic API (Sonnet 5 picks moments, Haiku 4.5 checks facts) | about €1 to €3 |
| Pexels (B-roll), Hugging Face | free |
| **Total** | **about €8 to €10 a month** |

If you ever want faster renders you can move the same Docker image to a machine with an NVIDIA GPU
(`pip install -r requirements-gpu.txt` in the Dockerfile and uncomment the GPU lines in `docker-compose.yml`);
Whisper large-v3 and pyannote then switch on by themselves.

## 1. Create the server (Hetzner)

1. Go to https://console.hetzner.cloud and create an account (email, then add a payment method under *Billing*).
2. Click **New project**, name it `clipforge`, open it.
3. Click **Add server**:
   - Location: the one closest to you (e.g. Falkenstein or Nuremberg).
   - Image: **Ubuntu 24.04**.
   - Type: **Shared vCPU → x86 → CX32** (4 vCPU, 8 GB). CX22 also works, it is just slower.
   - Networking: keep **Public IPv4** ticked.
   - SSH key: click **Add SSH key** and paste your public key (on Mac/Linux run `cat ~/.ssh/id_ed25519.pub`;
     if you have none, run `ssh-keygen -t ed25519` first). Windows: use PowerShell, same commands.
   - Name: `clipforge`. Click **Create & Buy now**.
4. Copy the server's IP address from the list (e.g. `65.108.1.2`).

## 2. Put CLIPFORGE on it (one command)

On your laptop:

```bash
ssh root@YOUR_SERVER_IP
apt-get update && apt-get install -y git
git clone https://github.com/michaelekeh-dev/clipforge.git /opt/clipforge
cd /opt/clipforge
cp .env.example .env
nano .env        # fill in the lines below, then Ctrl+O, Enter, Ctrl+X
bash deploy.sh
```

Lines to fill in `.env`:

```
APP_PASSWORD=choose-a-long-password
ANTHROPIC_API_KEY=sk-ant-...        (step 3)
YTDLP_COOKIES=/data/cookies.txt     (step 6, optional but recommended)
HF_TOKEN=hf_...                     (step 4, optional)
PEXELS_API_KEY=...                  (step 5, optional)
SITE_ADDRESS=65-108-1-2.sslip.io    (your IP with dashes + .sslip.io, or your own domain)
```

`deploy.sh` installs Docker, builds the app and starts it with HTTPS. Open `https://65-108-1-2.sslip.io`
(your address) in your phone's browser, log in with `APP_PASSWORD`.
The first start downloads the speech model (about 600 MB), so give it two or three minutes.

Updating later: `cd /opt/clipforge && git pull && bash deploy.sh`.
Logs: `docker compose logs -f clipforge`. Status page in the app: `/status`.

## 3. Anthropic key + credit

1. Go to https://console.anthropic.com and sign up.
2. Left menu **Billing** → **Add credits** → add €10 (that lasts months at this usage).
3. Left menu **API Keys** → **Create Key** → name it `clipforge` → copy it (it starts with `sk-ant-`).
4. Paste it into `.env` as `ANTHROPIC_API_KEY=` and run `bash deploy.sh` again.

## 4. Hugging Face token (optional, better speaker detection on a GPU machine)

1. https://huggingface.co/join → create an account.
2. Open https://huggingface.co/pyannote/speaker-diarization-3.1 and https://huggingface.co/pyannote/segmentation-3.0,
   click **Agree and access repository** on both.
3. https://huggingface.co/settings/tokens → **New token** (read) → copy → `.env` as `HF_TOKEN=`.

On a CPU-only server you can skip this; lip movement detection is used instead.

## 5. Pexels key (optional, free B-roll)

1. https://www.pexels.com/api/ → **Get Started** → sign up.
2. Your key is shown at https://www.pexels.com/api/new/ → copy → `.env` as `PEXELS_API_KEY=`.

## 6. YouTube cookies (recommended: YouTube often blocks servers)

YouTube blocks downloads from datacenter IPs. Giving yt-dlp your own logged-in cookies fixes it most of the time.
If a download still fails, the app tells you and you can upload the file instead.

1. In Chrome or Firefox on your laptop, log in to YouTube with a **spare Google account** (not your main one).
2. Install the extension **"Get cookies.txt LOCALLY"** (Chrome) or **"cookies.txt"** (Firefox).
3. Open https://www.youtube.com, click the extension, click **Export** → it saves `youtube.com_cookies.txt`.
4. Copy it to the server: `scp youtube.com_cookies.txt root@YOUR_SERVER_IP:/tmp/cookies.txt` then on the server:
   `docker compose cp /tmp/cookies.txt clipforge:/data/cookies.txt` (from `/opt/clipforge`).
5. `.env` has `YTDLP_COOKIES=/data/cookies.txt` → `bash deploy.sh`.

Cookies expire after some weeks; repeat when downloads start failing. Do not log out of that account in the browser.

## 7. Install on your phone

Open the site in Safari (iPhone) or Chrome (Android) → **Share** → **Add to Home Screen** (iPhone) or the
**Install app** prompt (Android). It opens full screen like an app.

## Backups and data

Everything lives in the Docker volume `clipforge_data` (database, clips, caches). Source videos are deleted
automatically after 7 days (`config.yaml` → `app.delete_sources_after_days`). To back up the clips:
`docker compose cp clipforge:/data/projects ./backup-projects`.

## Something is wrong?

- App does not open: `docker compose ps` and `docker compose logs --tail=100 clipforge`.
- "YouTube blocked the download": refresh the cookies (step 6) or upload the file.
- Out of disk: the status page shows storage. Delete old projects in the app, or lower `delete_sources_after_days`.
- Crash: the container restarts by itself (`restart: unless-stopped`). Check the status page for recent errors.
