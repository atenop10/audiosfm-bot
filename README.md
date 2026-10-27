# AUDIOS FM – Telegram Bulk Upload Bot (VPS, uses your existing admin panel)

Nothing on the website is changed, and the bot needs **no database password and no R2 keys**.
It logs in to `r2admin.php` with your admin PIN (like a browser) and uses your existing `r2_upload_api.php`
multipart upload, so every episode is saved exactly the way the admin page saves it.

Flow, **one file at a time**: download from Telegram → ffmpeg compress → auto-rename (`Ep 001.mp3`) → upload to R2 → episode saved by the site → delete the temp file → next file.
Only one episode is ever on the VPS disk at once (a ZIP is kept until its files are done).

## Quick start with GitHub (recommended)

**On your PC – once:** create a **private** GitHub repo and push this folder (`.gitignore` already keeps `.env` out):
```bash
git init && git add . && git commit -m "bot"
git branch -M main
git remote add origin https://github.com/YOURNAME/audiosfm-bot.git
git push -u origin main
```

**On the VPS – once:**
```bash
git clone https://github.com/YOURNAME/audiosfm-bot.git
cd audiosfm-bot
sudo bash install.sh
```
(For a private repo use a GitHub personal access token as the password when cloning.)
The installer installs ffmpeg + Python, asks for your settings, creates `.env`, and starts the bot as a service that restarts automatically.

**Later – after you change code and push to GitHub:**
```bash
cd audiosfm-bot && sudo bash update.sh
```

**Handy commands**
```bash
journalctl -u audiosfm-bot -f        # live logs
sudo systemctl restart audiosfm-bot  # restart
sudo systemctl stop audiosfm-bot     # stop
```

---
(Manual installation steps below, if you prefer not to use the script.)

## Run more than one bot on the same VPS
Each extra bot has its own Telegram token, settings and queue, but shares this code and the Python environment.
1. In Telegram, create a **new bot** with @BotFather and copy its token (a token can't be shared between bots).
2. On the VPS:
```bash
cd ~/audiosfm-bot
sudo bash add_bot.sh second        # "second" is just a name you choose
```
It asks for the new token; press **Enter** on the other questions to copy the main bot's values
(or type a different site URL / PIN / allowed user for a different website).
Its files live in `instances/second/` (never uploaded to GitHub).

```bash
journalctl -u audiosfm-bot-second -f          # logs of that bot
sudo systemctl restart audiosfm-bot-second    # restart it
sudo systemctl disable --now audiosfm-bot-second   # stop & remove from auto-start
```
`sudo bash update.sh` updates the code and restarts **all** bots at once.

---
## Install on the VPS (Ubuntu/Debian)
```bash
sudo apt update && sudo apt install -y ffmpeg python3-pip python3-venv
unzip audiosfm_bot.zip && cd audiosfm_bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
python bot.py
```
Fill `.env`: `API_ID`/`API_HASH` (my.telegram.org), `BOT_TOKEN` (@BotFather), `ADMIN_IDS` (your Telegram ID),
`SITE_URL` (e.g. `https://yourdomain.com/Audiosfm`) and `ADMIN_PIN` (the PIN you use on the admin login page).

## Keep it running (systemd)
`sudo nano /etc/systemd/system/audiosfm-bot.service`
```ini
[Unit]
Description=AUDIOS FM upload bot
After=network-online.target

[Service]
WorkingDirectory=/home/YOURUSER/audiosfm_bot
ExecStart=/home/YOURUSER/audiosfm_bot/venv/bin/python bot.py
Restart=always
User=YOURUSER

[Install]
WantedBy=multi-user.target
```
`sudo systemctl daemon-reload && sudo systemctl enable --now audiosfm-bot` · logs: `journalctl -u audiosfm-bot -f`

## Using it
`/help` lists everything.
```
/stories → /select 12      (or /newstory Title, then send a photo captioned /poster)
[send many mp3 files, or a ZIP]
/go
```
Commands: `/stories /select /newstory /poster /info /queue /go /clear /cancel /compress /bitrate /mono /rename /mode`

`/rename off` keeps your original file names; `/rename on` goes back to the pattern.

- Compression: MP3 64 kbps mono 44.1 kHz, metadata stripped (`/bitrate`, `/mono`, `/compress off`). MP3s already ≤ target are not re-encoded.
- Naming: `/rename Ep {n:03d}` → `Ep 001.mp3`. The website numbers episodes in upload order (last + 1), so the bot sorts files
  naturally (Ep 2 before Ep 10) and names each one with the number the site will give it.
- `/mode detect` (default): files whose number is already on the site are skipped, so you can safely resend a batch after a failure.
  `/mode seq`: upload every file.
- Failed uploads are aborted on R2 (no half files). The bot reports the final episode count and warns if it doesn't match.
- `/poster` and `/newstory` use the same forms as your admin panel; the story's title, description and "popular" flag are preserved.

## Notes
- The bot behaves like one admin session. If you log in to the admin panel in a browser at the same time, that's fine.
- Numbering assumes the story has no gaps (count = last episode number), which matches your current data.
- If you change the admin PIN, update `ADMIN_PIN` in `.env`. Keep `.env` private.

- **Failures never drop a file:** each file is retried 3 times; if it still fails the bot stops (`/onfail stop`, default) and keeps that file and the rest in the queue — send `/go` to continue from it. `/onfail skip` keeps going and leaves the failed file queued.
