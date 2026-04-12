# Headless Linux (Proxmox) Deployment Example

This guide shows one proven pattern for running Obsidian + obsidian-web-mcp on a headless Linux VM.
It is designed for remote Claude/Cowork usage via HTTPS.

This is an example architecture, not a required setup.

## Architecture

```text
Obsidian Sync <-> Linux VM (Proxmox)
                 |- Obsidian (headless via Xvfb)
                 `- obsidian-web-mcp (:8420)
                         |
                    Cloudflare Tunnel
                         |
                 https://vault-mcp.example.com
                         |
                     Claude/Cowork
```

## 1) VM Baseline

- Ubuntu 24.04 LTS Server
- 2 vCPU, 2-3 GB RAM, 20 GB disk
- Correct timezone and up-to-date packages

```bash
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone Europe/Vienna
```

## 2) Install Xvfb (for headless Obsidian)

```bash
sudo apt install -y xvfb libgtk-3-0 libnotify4 libnss3 libxss1 \
  libxtst6 xdg-utils libatspi2.0-0 libdrm2 libgbm1 \
  libsecret-1-0 libasound2t64 fonts-liberation wget curl libfuse2t64
```

Create a systemd service:

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

Enable and start:

```bash
sudo systemctl enable xvfb
sudo systemctl start xvfb
echo 'export DISPLAY=:99' | sudo tee /etc/profile.d/display.sh
```

## 3) Install and initialize Obsidian

```bash
mkdir -p ~/apps
wget -O ~/apps/Obsidian.AppImage "https://github.com/obsidianmd/obsidian-releases/releases/latest/download/Obsidian.AppImage"
chmod +x ~/apps/Obsidian.AppImage
```

Create `obsidian.service`:

```ini
[Unit]
Description=Obsidian (Headless)
After=xvfb.service
Requires=xvfb.service

[Service]
Type=simple
User=<your-user>
Environment=DISPLAY=:99
Environment=ELECTRON_DISABLE_GPU=1
ExecStart=/home/<your-user>/apps/Obsidian.AppImage --no-sandbox
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

For first-time setup (vault selection + Sync login), use temporary VNC:

```bash
sudo apt install -y x11vnc
x11vnc -display :99 -nopw -listen 0.0.0.0 -shared -forever &
DISPLAY=:99 ELECTRON_DISABLE_GPU=1 ~/apps/Obsidian.AppImage --no-sandbox &
```

After setup:

```bash
pkill x11vnc
pkill -f Obsidian
sudo systemctl enable obsidian
sudo systemctl start obsidian
```

## 4) Install obsidian-web-mcp

```bash
sudo apt install -y python3 python3-pip python3-venv git
git clone https://github.com/mleitnercom/obsidian-web-mcp.git
cd obsidian-web-mcp
python3 -m venv venv
source venv/bin/activate
pip install .
```

Important: if you change source code later, run `pip install .` again before restarting the service.

## 5) Configure and run as service

Generate secrets:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Example `obsidian-mcp.service`:

```ini
[Unit]
Description=Obsidian Web MCP Server
After=obsidian.service
Wants=obsidian.service

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/obsidian-web-mcp
Environment=PATH=/home/<your-user>/obsidian-web-mcp/venv/bin:/usr/bin
Environment=VAULT_PATH=/home/<your-user>/Vault
Environment=VAULT_MCP_TOKEN=<set-a-random-64-hex-token>
Environment=VAULT_OAUTH_CLIENT_SECRET=<set-a-random-64-hex-token>
Environment=VAULT_MCP_PORT=8420
ExecStart=/home/<your-user>/obsidian-web-mcp/venv/bin/vault-mcp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable obsidian-mcp
sudo systemctl start obsidian-mcp
```

## 6) Cloudflare Tunnel and Claude/Cowork

- Expose your MCP endpoint through a tunnel hostname (for example `vault-mcp.example.com`).
- Configure Claude connector URL with `/mcp`:
  - `https://vault-mcp.example.com/mcp`

Without `/mcp`, clients may hit `/` and fail tool calls.

## Operational Notes

- Increase inotify watchers for large vaults:

```bash
echo "fs.inotify.max_user_watches=524288" | sudo tee /etc/sysctl.d/99-inotify.conf
sudo sysctl --system
```

- Keep VNC disabled unless needed for troubleshooting.
- Rotate `VAULT_MCP_TOKEN` and `VAULT_OAUTH_CLIENT_SECRET` regularly.
- Avoid publishing your connector URL.

## Security Notes

- For internet-exposed setups, configure:
  - `VAULT_OAUTH_AUTH_USERNAME`
  - `VAULT_OAUTH_AUTH_PASSWORD`
- OAuth state is intentionally in-memory by default in this fork.
- Requests are authenticated and rate-limited; path traversal and symlink traversal are blocked.
