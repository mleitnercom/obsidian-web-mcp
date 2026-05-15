# Headless Linux on Proxmox

Back to [README](../../README.md).

This is a sanitized deployment pattern for running Obsidian, Obsidian Sync, and `obsidian-web-mcp` on a headless Linux VM.

No real hostnames, vault paths, tokens, or API keys are included here. Replace placeholders before use.

## VM Baseline

| Parameter | Example |
|---|---|
| OS | Ubuntu 24.04 LTS Server |
| CPU | 2 vCPU |
| RAM | 3-4 GB recommended |
| Disk | 20 GB+ |
| Network | bridged DHCP or static |

```bash
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone Europe/Vienna
```

## Xvfb

Obsidian/Electron needs an X display even when running headless.

```bash
sudo apt install -y xvfb libgtk-3-0 libnotify4 libnss3 libxss1 \
  libxtst6 xdg-utils libatspi2.0-0 libdrm2 libgbm1 \
  libsecret-1-0 libasound2t64 fonts-liberation wget curl
```

Create `/etc/systemd/system/xvfb.service`:

```ini
[Unit]
Description=X Virtual Frame Buffer
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :99 -screen 0 1024x768x24 -ac
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now xvfb
```

## Obsidian

Install an Obsidian AppImage under the service user, for example:

```bash
mkdir -p ~/apps
wget -O ~/apps/Obsidian.AppImage "REPLACE_WITH_OBSIDIAN_APPIMAGE_URL"
chmod +x ~/apps/Obsidian.AppImage
sudo apt install -y libfuse2t64
```

Create `/etc/systemd/system/obsidian.service`:

```ini
[Unit]
Description=Obsidian (Headless)
After=xvfb.service
Requires=xvfb.service

[Service]
Type=simple
User=REPLACE_WITH_SERVICE_USER
Environment=DISPLAY=:99
Environment=ELECTRON_DISABLE_GPU=1
ExecStart=/home/REPLACE_WITH_SERVICE_USER/apps/Obsidian.AppImage --no-sandbox
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

For the first Obsidian Sync setup, temporarily use VNC:

```bash
sudo apt install -y x11vnc
x11vnc -display :99 -nopw -listen 0.0.0.0 -shared -forever &
DISPLAY=:99 ELECTRON_DISABLE_GPU=1 ~/apps/Obsidian.AppImage --no-sandbox &
```

Open the vault folder, enable Obsidian Sync, wait for sync completion, then stop the manual process and enable the service:

```bash
pkill x11vnc
pkill -f Obsidian
sudo systemctl enable --now obsidian
```

## MCP Server

```bash
sudo apt install -y python3 python3-pip python3-venv git
cd /home/REPLACE_WITH_SERVICE_USER
git clone https://github.com/mleitnercom/obsidian-web-mcp.git obsidian-web-mcp
cd obsidian-web-mcp
python3 -m venv venv
./venv/bin/pip install -e .
```

Create `/etc/systemd/system/obsidian-mcp.service`:

```ini
[Unit]
Description=Obsidian Web MCP Server
After=network.target
Wants=obsidian.service

[Service]
Type=simple
User=REPLACE_WITH_SERVICE_USER
WorkingDirectory=/home/REPLACE_WITH_SERVICE_USER/obsidian-web-mcp
Environment=PATH=/home/REPLACE_WITH_SERVICE_USER/obsidian-web-mcp/venv/bin:/usr/bin
Environment=VAULT_PATH=REPLACE_WITH_VAULT_PATH
Environment=VAULT_MCP_TOKEN=REPLACE_WITH_RANDOM_TOKEN
Environment=VAULT_OAUTH_CLIENT_SECRET=REPLACE_WITH_RANDOM_SECRET
Environment=VAULT_PUBLIC_BASE_URL=https://REPLACE_WITH_HOSTNAME
Environment=VAULT_ALLOWED_HOSTS=127.0.0.1:*,localhost:*,[::1]:*,REPLACE_WITH_HOSTNAME
Environment=VAULT_MCP_PORT=8420
ExecStart=/home/REPLACE_WITH_SERVICE_USER/obsidian-web-mcp/venv/bin/vault-mcp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now obsidian-mcp
curl -s http://127.0.0.1:8420/health
```

## Reverse Proxy or Tunnel

Forward public HTTPS traffic to:

```text
http://REPLACE_WITH_VM_IP:8420
```

Use the public connector URL:

```text
https://REPLACE_WITH_HOSTNAME/mcp
```

## Optional Local REST API

Install and enable the Obsidian Local REST API plugin in the headless vault. Then add a root-only systemd drop-in for the MCP service:

```ini
[Service]
Environment=VAULT_OBSIDIAN_REST_URL=https://127.0.0.1:27124
Environment=VAULT_OBSIDIAN_REST_API_KEY=REPLACE_WITH_LOCAL_REST_API_KEY
Environment=VAULT_OBSIDIAN_REST_VERIFY_TLS=false
Environment=VAULT_TEMPLATER_FOLDER=Templates
```

Permissions:

```bash
sudo chmod 600 /etc/systemd/system/obsidian-mcp.service.d/local-rest.conf
sudo systemctl daemon-reload
sudo systemctl restart obsidian-mcp
```

## Maintenance

```bash
sudo systemctl status obsidian
sudo systemctl status obsidian-mcp
sudo journalctl -u obsidian-mcp -n 100 --no-pager
```

For large vaults, raise inotify limits:

```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee /etc/sysctl.d/99-inotify.conf
sudo sysctl --system
sudo systemctl restart obsidian
sudo systemctl restart obsidian-mcp
```
