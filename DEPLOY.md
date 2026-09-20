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

## Option A: Railway (easiest if you already use it)

Same app, no server to manage. About $10 to $15 a month depending on how much you render.

1. Push this repo to GitHub (it is already there) and open https://railway.app → **New Project** → **Deploy from GitHub repo** → pick `clipforge`. Railway finds the `Dockerfile` and builds it (5 to 10 minutes the first time).
2. In the service, open **Variables** and add: `APP_PASSWORD`, `ANTHROPIC_API_KEY`, `APP_HTTPS=1`, `CLIPFORGE_DATA_DIR=/data`, and optionally `PEXELS_API_KEY`, `HF_TOKEN`, `YTDLP_COOKIES=/data/cookies.txt`.
   If Claude replies *"this API key is not scoped to a workspace"*, either make a new key inside a workspace at console.anthropic.com, or add `ANTHROPIC_WORKSPACE_ID` with that workspace's ID. **Status → Claude → Test my Claude key** tells you which it is.
3. Open **Settings** → **Volumes** → **Add volume**, mount path `/data`, size 20 GB or more (source videos are big; they are deleted after 7 days).
4. **Settings** → **Networking** → **Generate domain**. Open it on your phone, log in, add to home screen.
5. Cookies: turn the exported `youtube.com_cookies.txt` into one line and paste it as the variable `YTDLP_COOKIES_B64`:
   - Mac/Linux terminal: `base64 -w0 youtube.com_cookies.txt | pbcopy` (Linux: `| xclip -selection clipboard`)
   - Windows PowerShell: `[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME\Downloads\youtube.com_cookies.txt")) | Set-Clipboard`
   Then Variables → New Variable → name `YTDLP_COOKIES_B64`, paste the value. The app decodes it into a file by itself.
6. **YouTube from a server IP** (needed if links fail with "not a bot" or "page needs to be reloaded" even with cookies):
   add a second service in the same Railway project that hands yt-dlp proof-of-origin tokens.
   - Project canvas → **+ New** → **Docker Image** → image `brainicism/bgutil-ytdlp-pot-provider:latest` → deploy.
   - Open that new service → **Settings** → rename it `bgutil` (its private host becomes `bgutil.railway.internal`).
   - Back in the CLIPFORGE service → Variables → add `POT_PROVIDER_URL` = `http://bgutil.railway.internal:4416` → deploy.
   It costs a few cents a month; it only runs when a download asks it for a token.
   The image also ships a JavaScript runtime (deno) and the `yt-dlp-ejs` solver, which YouTube's challenge needs.
   **Cookies are optional and often harmful**: YouTube rotates them, and rotated cookies block downloads. Start with
   no `YTDLP_COOKIES_B64` at all. If a link fails, open **Status → Check a YouTube link**: it says per client what
   came back and names the one thing to fix.

   **If YouTube still refuses with everything set up**, its block is on the server's IP address, which is normal for
   cloud hosts. Two ways through:
   - `YTDLP_PROXY` = a residential proxy, e.g. `http://user:pass@host:port`. Buy credit once, do not take a monthly
     plan: because only the API calls go through it, one gigabyte lasts months. Providers that sell non-expiring
     credit suit this best. After setting it, the Status page shows whether the proxy works and which address
     YouTube sees. Only YouTube's small API calls go
     through it; the video file itself comes straight from Google's servers, which do not check the address. That
     keeps a pay-per-gigabyte proxy at a few pennies a month instead of a few euros an episode. Any HTTP proxy
     works; residential or mobile ones are the kind that YouTube trusts. (To force everything through the proxy,
     set `proxy_metadata_only: false` in `config.yaml`.)
   - Upload the episode file instead. Everything after the download (clips, Telegram, posting) is identical.
7. Updates: every `git push` redeploys. Logs are in the **Deployments** tab. Restarts on crash are on by default.

Keep the service on the Hobby plan or higher (8 GB RAM); the speech model needs about 3 GB while transcribing.

## Option B: your own server (Hetzner)

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

## 7. Telegram (clips arrive in your chat with Post / Skip buttons)

1. In Telegram open **@BotFather**, send `/newbot`, give it a name and a username ending in `bot`. It replies with a token like `123456:ABC...`.
2. Add it as the variable `TELEGRAM_BOT_TOKEN` (Railway: Variables; server: `.env`), redeploy.
3. Open your new bot in Telegram and send `/start`. That links your chat. From then on every finished clip arrives as a video with its
   fact-check verdict and two buttons. `/status` shows the queue, `/post <youtube link>` clips an episode right away, `/pause` and `/resume`
   control the channel watcher.

## 8. YouTube posting (auto title, description, hashtags, scheduled publish)

Google's rules, plainly: an app that uploads to YouTube needs an OAuth client from a Google Cloud project. Videos uploaded through a project
that Google has not audited are forced to **private**. So there are two phases:

**Phase 1 (15 minutes): uploads work, videos land private, you tap Publish in the YouTube app.**

1. https://console.cloud.google.com → create a project called `clipforge`.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → OAuth consent screen** (Google Auth Platform): User type **External**, app name `CLIPFORGE`, your email as support and
   developer contact. Under **Scopes** add `.../auth/youtube.upload` and `.../auth/youtube.readonly`. Under **Audience** click **Publish app**
   (status "In production"; Google shows an "unverified app" warning during login, that is fine for your own channel).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**: type **Web application**, name `clipforge`,
   **Authorised redirect URI**: `https://YOUR-APP-ADDRESS/oauth/youtube/callback` (your Railway domain, no trailing slash). Create.
5. Copy the **Client ID** and **Client secret** into the variables `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`, redeploy.
6. In the app open **Autopilot → Connect my channel**, log in with the Google account that owns **TheTruthUntold**, click through the
   "unverified app" warning (Advanced → Go to CLIPFORGE), allow. The page then shows your channel name.

**Phase 2: fully public, hands off.** Apply for the YouTube API compliance audit so uploads can be public:
https://support.google.com/youtube/contact/yt_api_form . Describe the app honestly ("my own tool that cuts my podcast clips and uploads
them to my own channel"). It usually takes a few days to two weeks. When approved, videos upload as **scheduled** and go public at the
posting times you set on the Autopilot page.

Quota: Google gives 10,000 units a day; one upload costs about 1,600, so up to 6 uploads a day. Plenty.

## 9. Install on your phone

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
