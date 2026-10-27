#!/usr/bin/env bash
# Add another independent bot (own token/settings/queue) on the same VPS, same code.
# Usage:  sudo bash add_bot.sh NAME        e.g.  sudo bash add_bot.sh second
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo bash add_bot.sh NAME"; exit 1; }
NAME="${1:-}"; [[ "$NAME" =~ ^[a-zA-Z0-9_-]+$ ]] || { echo "Usage: sudo bash add_bot.sh NAME   (letters, numbers, - _ only)"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR"
RUN_USER="${SUDO_USER:-root}"
[ -x venv/bin/python ] || { echo "Run  sudo bash install.sh  first."; exit 1; }
INST="$DIR/instances/$NAME"; SVC="audiosfm-bot-$NAME"
[ ! -e "$INST/.env" ] || { echo "Bot '$NAME' already exists ($INST). Edit its .env and: sudo systemctl restart $SVC"; exit 1; }

old() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }   # value from your main bot's .env
ask()  { local d="$2" v; read -r -p "$1${d:+ [Enter = same as main bot]}: " v; printf '%s' "${v:-$d}"; }
asks() { local d="$2" v; read -r -s -p "$1${d:+ [Enter = same as main bot]}: " v; echo >&2; printf '%s' "${v:-$d}"; }

echo "==> Settings for bot '$NAME'  (create a NEW bot in @BotFather — a token can't be shared)"
BOT_TOKEN=$(ask  "NEW bot token" "")
[ -n "$BOT_TOKEN" ] || { echo "Bot token is required."; exit 1; }
API_ID=$(ask   "Telegram API_ID" "$(old API_ID)")
API_HASH=$(ask "Telegram API_HASH" "$(old API_HASH)")
ADMIN_IDS=$(ask "Allowed Telegram user ID(s)" "$(old ADMIN_IDS)")
SITE_URL=$(ask  "Site folder URL" "$(old SITE_URL)")
ADMIN_PIN=$(asks "Admin PIN (hidden)" "$(old ADMIN_PIN)")

mkdir -p "$INST"; umask 077
cat > "$INST/.env" <<ENVEOF
API_ID=$API_ID
API_HASH=$API_HASH
BOT_TOKEN=$BOT_TOKEN
ADMIN_IDS=$ADMIN_IDS
SITE_URL=$SITE_URL
ADMIN_PIN=$ADMIN_PIN
DEFAULT_COMPRESS=0
DEFAULT_BITRATE=64
DEFAULT_MONO=1
DEFAULT_NAME_PATTERN=Ep {n:03d}
ENVEOF
chown -R "$RUN_USER":"$RUN_USER" "$INST"; chmod 600 "$INST/.env"

cat > "/etc/systemd/system/$SVC.service" <<SVCEOF
[Unit]
Description=AUDIOS FM upload bot ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
Environment=ENV_FILE=$INST/.env
Environment=DATA_DIR=$INST
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable --now "$SVC"
sleep 2
systemctl --no-pager status "$SVC" | head -n 6 || true
echo
echo "✅ Bot '$NAME' is running. Open it in Telegram and send /help"
echo "   Logs:    journalctl -u $SVC -f"
echo "   Restart: sudo systemctl restart $SVC"
echo "   Stop:    sudo systemctl disable --now $SVC"
