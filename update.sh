#!/usr/bin/env bash
# Pull the latest code from GitHub and restart the bot:  sudo bash update.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo:  sudo bash update.sh"; exit 1; }
DIR="$(cd "$(dirname "$0")" && pwd)"; cd "$DIR"
RUN_USER="$(stat -c %U "$DIR")"
sudo -u "$RUN_USER" git pull --ff-only
sudo -u "$RUN_USER" ./venv/bin/pip install -r requirements.txt -q
systemctl restart audiosfm-bot
echo "✅ Updated and restarted."; journalctl -u audiosfm-bot -n 5 --no-pager
