#!/usr/bin/env bash
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then
    echo "Run this installer with sudo or as root." >&2
    exit 1
fi

for dependency in git python3 ssh systemctl runuser; do
    command -v "$dependency" >/dev/null || { echo "Missing dependency: $dependency" >&2; exit 1; }
done

account=atuotunnel
state=/var/lib/atuotunnel
checkout=/opt/atuotunnel
settings=/etc/atuotunnel-bot.env
if ! id "$account" >/dev/null 2>&1; then
    useradd --system --home-dir "$state" --shell /usr/sbin/nologin "$account"
fi
install -d -o "$account" -g "$account" -m 0700 "$state" "$state/.ssh"
if [[ ! -e "$checkout" ]]; then
    install -d -o "$account" -g "$account" -m 0750 "$checkout"
    runuser -u "$account" -- git clone --branch main --single-branch \
        https://github.com/ramin346/atuotunnel.git "$checkout"
fi
if [[ ! -f "$checkout/bot.py" ]]; then
    echo "Bot checkout is missing or incomplete: $checkout" >&2
    exit 1
fi

if [[ ! -e "$settings" ]]; then
    install -m 0600 /dev/null "$settings"
    cat > "$settings" <<'EOF'
BOT_TOKEN=
BOT_ADMIN_IDS=
EOF
fi
install -m 0644 "$checkout/atuotunnel-bot.service" /etc/systemd/system/atuotunnel-bot.service
systemctl daemon-reload
systemctl enable atuotunnel-bot.service
if grep -q '^BOT_TOKEN=.' "$settings" && grep -q '^BOT_ADMIN_IDS=.' "$settings"; then
    systemctl start atuotunnel-bot.service
else
    echo "Set BOT_TOKEN and BOT_ADMIN_IDS in $settings, then start atuotunnel-bot.service."
fi
