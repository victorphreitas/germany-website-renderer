# Deployment Guide — Remote Interactive Browser via noVNC

## Architecture Overview

```
Your Laptop (browser)
        │
        │  HTTP + WebSocket  :8080
        ▼
  DigitalOcean Droplet
  ┌─────────────────────────────────────────┐
  │  Docker Container                       │
  │  ┌────────┐   ┌──────────┐  ┌────────┐ │
  │  │  Xvfb  │──▶│ Chromium │  │Fluxbox │ │
  │  │ :99    │   │(Playwright│  │  WM    │ │
  │  └────────┘   └──────────┘  └────────┘ │
  │       │                                 │
  │  ┌────▼───┐   ┌────────────────────┐   │
  │  │ x11vnc │──▶│ websockify / noVNC │   │
  │  │  :5900 │   │        :8080       │   │
  │  └────────┘   └────────────────────┘   │
  └─────────────────────────────────────────┘
```

---

## Step 1 — Provision the Droplet

1. Log in to [DigitalOcean](https://cloud.digitalocean.com/) and create a new Droplet:
   - **Image**: Ubuntu 22.04 (LTS) x64
   - **Plan**: Basic — at least **2 vCPUs / 4 GB RAM** (Chromium needs headroom)
   - **Datacenter**: closest region to you for low latency
   - Add your SSH key

2. Note the droplet's public IP (referred to as `<DROPLET_IP>` below).

---

## Step 2 — SSH into the Droplet and Install Docker

```bash
ssh root@<DROPLET_IP>

# Update the system
apt-get update && apt-get upgrade -y

# Install Docker and Docker Compose plugin in one step
curl -fsSL https://get.docker.com | bash

# Verify
docker --version
docker compose version
```

---

## Step 3 — Copy the Project to the Droplet

**Option A — git clone** (if you push this repo to GitHub/GitLab):
```bash
git clone https://github.com/<your-user>/German-Website.git
cd German-Website
```

**Option B — scp from your local machine** (run this on your laptop):
```bash
scp -r /path/to/German-Website root@<DROPLET_IP>:/root/German-Website
ssh root@<DROPLET_IP>
cd /root/German-Website
```

---

## Step 4 — Open the Required Firewall Port

DigitalOcean droplets default to allowing all inbound traffic, but if you have
a Cloud Firewall attached, add a rule for **TCP port 8080**.

```bash
# UFW (if enabled on the droplet)
ufw allow 8080/tcp
ufw allow 5900/tcp   # only if you also want native VNC access
ufw reload
```

---

## Step 5 — Build and Run the Container

```bash
# Inside /root/German-Website on the droplet

# Build the image (takes ~3-5 minutes the first time — Playwright downloads Chromium)
docker compose build

# Start in detached mode
docker compose up -d

# Watch logs to confirm everything started cleanly
docker compose logs -f
```

You should see output similar to:
```
[entrypoint] Starting Xvfb on display :99 …
[entrypoint] Starting Fluxbox window manager …
[entrypoint] Starting x11vnc on port 5900 …
[entrypoint] Starting noVNC / websockify on port 8080 …
[entrypoint] noVNC ready.  Open in your browser:
[entrypoint]   http://<DROPLET_IP>:8080/vnc.html
[entrypoint] Launching main.py (Playwright + Chromium) …
[*] Navigating to https://www.umbreitshopsolution.de/ …
[*] Page loaded.  Browser is now open — take control via noVNC.
```

---

## Step 6 — Access the Live Browser Session

Open this URL in **any web browser** on your laptop:

```
http://<DROPLET_IP>:8080/vnc.html
```

- Click **Connect** on the noVNC page (no password required).
- You will see the remote Chromium window displaying the target website.
- You can **click, type, scroll**, and fully interact with the browser as if it
  were running on your own machine.

---

## Useful Management Commands

| Action | Command |
|--------|---------|
| Stop the container | `docker compose down` |
| Restart after code change | `docker compose up -d --build` |
| View live logs | `docker compose logs -f` |
| Open a shell inside the container | `docker compose exec browser bash` |
| Check resource usage | `docker stats german_browser` |

---

## Security Note

Port 8080 is **unauthenticated** by default — anyone who knows the IP can
control the browser.  For production or sensitive sessions:

1. Add a VNC password by modifying `entrypoint.sh` (add `-passwd yourpassword`
   to the `x11vnc` call and configure `--password` in the websockify call).
2. Or restrict inbound port 8080 to your laptop's IP only via the DigitalOcean
   Cloud Firewall.

---

## Changing the Target URL

Edit `main.py` and update `TARGET_URL`, then rebuild:

```bash
# On the droplet
docker compose up -d --build
```
