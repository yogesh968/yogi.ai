#!/usr/bin/env bash
# One-time + repeatable server setup for Ubuntu 22.04/24.04 (Oracle Cloud Always Free, ARM or x86).
# Usage (run by deploy.ps1):  bash /tmp/setup_server.sh <your-domain>
set -euo pipefail
DOMAIN="${1:?usage: setup_server.sh <domain>}"
APP=/opt/assistant
SVC_USER=assistant

echo "==> Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-dev build-essential curl \
  debian-keyring debian-archive-keyring apt-transport-https gnupg iptables-persistent >/dev/null

if ! command -v caddy >/dev/null; then
  echo "==> Installing Caddy (automatic HTTPS)"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
  sudo apt-get update -qq && sudo apt-get install -y -qq caddy >/dev/null
fi

echo "==> Opening ports 80/443 in the VM firewall (Oracle images block them by default)"
for p in 80 443; do
  sudo iptables -C INPUT -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 5 -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null \
    || sudo iptables -I INPUT 1 -p tcp --dport "$p" -m state --state NEW -j ACCEPT
done
sudo netfilter-persistent save >/dev/null

echo "==> Installing app to $APP"
id "$SVC_USER" >/dev/null 2>&1 || sudo useradd --system --home "$APP" --shell /usr/sbin/nologin "$SVC_USER"
sudo mkdir -p "$APP/data/recordings"
# Replace code, keep data/ and the venv.
sudo find "$APP" -mindepth 1 -maxdepth 1 ! -name data ! -name .venv -exec rm -rf {} +
sudo tar -xzf /tmp/assistant.tar.gz -C "$APP"
sudo install -m 600 /tmp/assistant.env "$APP/.env"
rm -f /tmp/assistant.env
[ -d "$APP/.venv" ] || sudo python3 -m venv "$APP/.venv"
sudo "$APP/.venv/bin/pip" install -q --upgrade pip
sudo "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
sudo chown -R "$SVC_USER:$SVC_USER" "$APP"

echo "==> Running database migrations"
sudo -u "$SVC_USER" bash -c "cd $APP && .venv/bin/python -m app.migrate"

echo "==> Installing services"
sudo install -m 644 "$APP/deploy/assistant-server.service" /etc/systemd/system/
sudo install -m 644 "$APP/deploy/assistant-worker.service" /etc/systemd/system/
sed "s/__DOMAIN__/$DOMAIN/g" "$APP/deploy/Caddyfile" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable -q assistant-server assistant-worker caddy
sudo systemctl restart assistant-server assistant-worker
sudo systemctl reload caddy || sudo systemctl restart caddy

sleep 4
if curl -fsS http://127.0.0.1:8765/healthz -H "Host: 127.0.0.1" >/dev/null; then
  echo "==> App is running locally on the server."
else
  echo "!! App did not start. Logs:"; sudo journalctl -u assistant-server -n 40 --no-pager; exit 1
fi
echo "==> Done. Open https://$DOMAIN (first HTTPS certificate can take ~30 s)."
