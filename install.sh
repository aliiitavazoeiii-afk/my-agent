#!/usr/bin/env bash
set -euo pipefail

CONTROL_REPO="${CONTROL_REPO:-aliiitavazoeiii-afk/my-agent}"
INSTALL_DIR="${INSTALL_DIR:-/opt/my-agent}"
CONFIG_DIR="/etc/my-agent"
WORKSPACE_DIR="/srv/my-agent/workspaces"
CADDY_SITES_DIR="/etc/caddy/my-agent-sites"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo: sudo bash install.sh" >&2
  exit 1
fi

if [[ ! -f "$INSTALL_DIR/agent.py" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [[ -f "$SCRIPT_DIR/agent.py" ]]; then
    if [[ "$SCRIPT_DIR" != "$INSTALL_DIR" ]]; then
      rm -rf "$INSTALL_DIR"
      mkdir -p "$(dirname "$INSTALL_DIR")"
      cp -a "$SCRIPT_DIR" "$INSTALL_DIR"
    fi
  else
    echo "agent.py not found. Clone the repository to $INSTALL_DIR first." >&2
    exit 1
  fi
fi

if [[ -n "${AGENT_USER:-}" ]]; then
  :
elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  AGENT_USER="$SUDO_USER"
else
  AGENT_USER="myagent"
fi

if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /var/lib/my-agent --shell /bin/bash "$AGENT_USER"
fi
AGENT_GROUP="$(id -gn "$AGENT_USER")"

echo "[1/9] Installing base packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-venv python3-pip git curl jq acl ca-certificates sudo

echo "[2/9] Preparing directories..."
mkdir -p "$CONFIG_DIR" "$WORKSPACE_DIR" /var/lib/my-agent
chown -R "$AGENT_USER:$AGENT_GROUP" "$WORKSPACE_DIR" /var/lib/my-agent
chown -R "$AGENT_USER:$AGENT_GROUP" "$INSTALL_DIR"
chmod +x "$INSTALL_DIR/agent.py" "$INSTALL_DIR/scripts/git-askpass.sh" "$INSTALL_DIR/scripts/my-agent-caddy-reload"

echo "[3/9] Python environment..."
sudo -u "$AGENT_USER" python3 -m venv "$INSTALL_DIR/.venv"
sudo -u "$AGENT_USER" "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$AGENT_USER" "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo
echo "Secrets are stored ONLY in $CONFIG_DIR/agent.env (mode 600)."
echo "Do not paste these secrets into ChatGPT or commit them to GitHub."
echo

OPENAI_API_KEY="${OPENAI_API_KEY:-}"
if [[ -z "$OPENAI_API_KEY" ]]; then
  read -r -s -p "OpenAI API key: " OPENAI_API_KEY
  echo
fi
if [[ -z "$OPENAI_API_KEY" ]]; then
  echo "OpenAI API key is required." >&2
  exit 1
fi

GITHUB_TOKEN="${GITHUB_TOKEN:-}"
if [[ -z "$GITHUB_TOKEN" ]] && command -v gh >/dev/null 2>&1; then
  GITHUB_TOKEN="$(sudo -u "$AGENT_USER" gh auth token 2>/dev/null || true)"
fi
if [[ -z "$GITHUB_TOKEN" ]]; then
  echo
  echo "GitHub fine-grained PAT required."
  echo "Recommended permissions: Metadata=Read, Contents=Read/Write, Issues=Read/Write."
  echo "Give it access to $CONTROL_REPO and every target project repo the agent should modify."
  echo "For future repos without updating the token, choose All repositories; selected repositories is safer."
  read -r -s -p "GitHub token: " GITHUB_TOKEN
  echo
fi
if [[ -z "$GITHUB_TOKEN" ]]; then
  echo "GitHub token is required." >&2
  exit 1
fi

TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
echo
read -r -p "Enable Telegram completion notifications? [Y/n]: " TG_ENABLE
TG_ENABLE="${TG_ENABLE:-Y}"
if [[ "$TG_ENABLE" =~ ^[Yy]$ ]]; then
  if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
    echo "Create a bot with @BotFather, then paste the bot token here."
    read -r -s -p "Telegram bot token: " TELEGRAM_BOT_TOKEN
    echo
  fi
  if [[ -n "$TELEGRAM_BOT_TOKEN" && -z "$TELEGRAM_CHAT_ID" ]]; then
    echo "Open your new bot in Telegram and send it /start."
    read -r -p "After sending /start, press Enter here..."
    TELEGRAM_CHAT_ID="$(
      curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
      | jq -r '[.result[] | .message?.chat?.id // .edited_message?.chat?.id // empty] | last // empty' \
      || true
    )"
    if [[ -z "$TELEGRAM_CHAT_ID" ]]; then
      echo "Could not auto-detect Telegram chat ID. Notifications will remain disabled until you set it."
    else
      echo "Detected Telegram chat ID: $TELEGRAM_CHAT_ID"
    fi
  fi
else
  TELEGRAM_BOT_TOKEN=""
  TELEGRAM_CHAT_ID=""
fi

echo "[4/9] Writing protected config..."
umask 077
cat >"$CONFIG_DIR/agent.env" <<EOF
OPENAI_API_KEY=$OPENAI_API_KEY
GITHUB_TOKEN=$GITHUB_TOKEN
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
MY_AGENT_CONFIG=$CONFIG_DIR/config.yaml
EOF
chmod 600 "$CONFIG_DIR/agent.env"
chown root:root "$CONFIG_DIR/agent.env"

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cp "$INSTALL_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
fi
chmod 644 "$CONFIG_DIR/config.yaml"

echo "[5/9] Granting project access..."
mapfile -t PROJECT_DIRS < <(find /opt -mindepth 1 -maxdepth 3 -type d -name .git -printf '%h\n' 2>/dev/null | sort -u)
for project in "${PROJECT_DIRS[@]:-}"; do
  [[ "$project" == "$INSTALL_DIR" ]] && continue
  echo "  ACL: $project"
  setfacl -Rm "u:${AGENT_USER}:rwX" "$project" || true
  setfacl -Rdm "u:${AGENT_USER}:rwX" "$project" || true
  sudo -u "$AGENT_USER" git config --global --add safe.directory "$project" || true
done

if getent group docker >/dev/null 2>&1; then
  usermod -aG docker "$AGENT_USER"
fi

echo "[6/9] Preparing optional Caddy integration..."
if command -v caddy >/dev/null 2>&1 && [[ -f /etc/caddy/Caddyfile ]]; then
  mkdir -p "$CADDY_SITES_DIR"
  chown "$AGENT_USER:$AGENT_GROUP" "$CADDY_SITES_DIR"
  chmod 750 "$CADDY_SITES_DIR"

  cp "$INSTALL_DIR/scripts/my-agent-caddy-reload" /usr/local/sbin/my-agent-caddy-reload
  chown root:root /usr/local/sbin/my-agent-caddy-reload
  chmod 755 /usr/local/sbin/my-agent-caddy-reload

  IMPORT_LINE="import ${CADDY_SITES_DIR}/*.caddy"
  if ! grep -Fq "$IMPORT_LINE" /etc/caddy/Caddyfile; then
    BACKUP="/etc/caddy/Caddyfile.my-agent-backup.$(date +%Y%m%d-%H%M%S)"
    cp /etc/caddy/Caddyfile "$BACKUP"
    {
      echo
      echo "# Managed site snippets for my-agent"
      echo "$IMPORT_LINE"
    } >> /etc/caddy/Caddyfile
    if ! caddy validate --config /etc/caddy/Caddyfile; then
      cp "$BACKUP" /etc/caddy/Caddyfile
      echo "WARNING: Caddy import validation failed; restored $BACKUP" >&2
    else
      systemctl reload caddy
    fi
  fi

  cat >"/etc/sudoers.d/my-agent" <<EOF
${AGENT_USER} ALL=(root) NOPASSWD: /usr/local/sbin/my-agent-caddy-reload
EOF
  chmod 440 /etc/sudoers.d/my-agent
  visudo -cf /etc/sudoers.d/my-agent >/dev/null
else
  echo "  Caddy not found; skipping Caddy helper."
fi

echo "[7/9] Checking GitHub token/control repo..."
GH_INFO="$(
  curl -fsS \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$CONTROL_REPO"
)"
VISIBILITY="$(printf '%s' "$GH_INFO" | jq -r '.visibility // "unknown"')"
echo "  Control repo visibility: $VISIBILITY"
if [[ "$VISIBILITY" != "private" ]]; then
  echo "  WARNING: $CONTROL_REPO is not private. The daemon still checks the issue author's exact GitHub login,"
  echo "  but making the control repo private is strongly recommended."
fi

echo "[8/9] Installing systemd service..."
SUPP_GROUP_LINE=""
if getent group docker >/dev/null 2>&1; then
  SUPP_GROUP_LINE="SupplementaryGroups=docker"
fi

cat >/etc/systemd/system/my-agent.service <<EOF
[Unit]
Description=Ali shared autonomous coding/deployment agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=$AGENT_USER
Group=$AGENT_GROUP
$SUPP_GROUP_LINE
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONFIG_DIR/agent.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/agent.py
Restart=always
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now my-agent

echo "[9/9] Health check..."
sleep 3
systemctl --no-pager --full status my-agent || {
  echo
  echo "Agent failed to start. Recent logs:"
  journalctl -u my-agent -n 120 --no-pager
  exit 1
}

if [[ -n "$TELEGRAM_BOT_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
  curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg c "$TELEGRAM_CHAT_ID" --arg t "✅ my-agent روی VPS نصب و فعال شد." '{chat_id:$c,text:$t}')" \
    >/dev/null || true
fi

echo
echo "=============================================="
echo "my-agent installed and running."
echo "Control repo: $CONTROL_REPO"
echo "Service:      systemctl status my-agent"
echo "Logs:         journalctl -u my-agent -f"
echo "Protocol:     $CONTROL_REPO/AGENT_CHAT_PROTOCOL.md"
echo "=============================================="
