#!/usr/bin/env bash
# AUDIOS FM bot — one-command installer. Run inside the cloned repo:  sudo bash install.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo bash install.sh"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-root}"
cd "$DIR"

echo "==> Installing system packages (ffmpeg, python, git)…"
apt-get update -y
apt-get install -y ffmpeg python3 python3-venv python3-pip git

echo "==> Creating Python environment…"
sudo -u "$RUN_USER" python3 -m venv venv
sudo -u "$RUN_USER" ./venv/bin/pip install --upgrade pip -q
sudo -u "$RUN_USER" ./venv/bin/pip install -r requirements.txt -q

if [ ! -f .env ]; then
  echo "==> Enter your settings (saved to .env, never uploaded to GitHub)"
  ask()  { local v; read -r -p "$1: " v; printf '%s' "$v"; }
  asks() { local v; read -r -s -p "$1: " v; echo >&2; printf '%s' "$v"; }
  API_ID=$(ask "Telegram API_ID (my.telegram.org)")
  API_HASH=$(ask "Telegram API_HASH")
  BOT_TOKEN=$(ask "Bot token (@BotFather)")
  ADMIN_IDS=$(ask "Your Telegram user ID (@userinfobot)")
  SITE_URL=$(ask "Site folder URL (e.g. https://yourdomain.com/Audiosfm)")
  ADMIN_PIN=$(asks "Admin PIN (hidden)")
  umask 077
  cat > .env <<ENVEOF
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
SITE_URL=$SITE_URL
ADMIN_PIN=$ADMIN_PIN
DEFAULT_BITRATE=64
DEFAULT_MONO=1
DEFAULT_NAME_PATTERN=Ep {n:03d}
ENVEOF
  chown "$RUN_USER":"$RUN_USER" .env; chmod 600 .env
else
  echo "==> .env already exists — keeping it."
fi

echo "==> Setting up auto-start service…"
cat > /etc/systemd/system/audiosfm-bot.service <<SVC
[Unit]
Description=AUDIOS FM upload bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload
systemctl enable --now audiosfm-bot
sleep 2

echo
systemctl --no-pager --full status audiosfm-bot | head -n 8 || true
echo
echo "✅ Done. Open your bot in Telegram and send /help"
echo "   Logs:    journalctl -u audiosfm-bot -f"
echo "   Update:  sudo bash update.sh"
echo "   Restart: sudo systemctl restart audiosfm-bot"
