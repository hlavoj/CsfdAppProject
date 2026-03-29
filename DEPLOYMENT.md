# Deployment Guide — Hostinger VPS

## Server Details

| | |
|---|---|
| **Host** | `srv1475341.hstgr.cloud` |
| **IP** | `187.77.147.171` |
| **OS** | Ubuntu 24.04 with Docker + Traefik |
| **Plan** | KVM 1 — 1 CPU, 4 GB RAM, 50 GB disk |
| **Addon URL** | `https://srv1475341.hstgr.cloud/manifest.json` |

SSH: `ssh root@187.77.147.171`

---

## Architecture

```
Internet
    │
    ▼
Traefik (port 80/443, host network)
    │  HTTPS + Let's Encrypt cert
    ▼
stremio-addon (Flask, port 7000, bridge network)
    │  internal Docker DNS
    ▼
media-source-finder (FastAPI, port 8080, bridge network)
    │
    ├── TMDB API
    ├── Webshare API
    └── OpenRouter API
```

**Traefik** — reverse proxy already running on the VPS. Watches Docker for containers with `traefik.enable=true` label, automatically issues Let's Encrypt SSL certs, and forwards HTTPS traffic to the stremio-addon.

**media-source-finder** — never exposed to the internet. Only the stremio-addon talks to it via Docker's internal DNS: `http://media-source-finder:8080`.

**stremio-addon** — the only public-facing service. Traefik routes `https://srv1475341.hstgr.cloud` to it via labels in `docker-compose.yml`.

---

## Files on the Server

```
/docker/
├── traefik/            # Hostinger-managed Traefik setup
├── openclaw-amgw/      # Hostinger management agent
└── csfd/               # Our app (git clone of CsfdAppProject)
    ├── docker-compose.yml
    ├── .env            # Secrets — never committed to git
    └── services/
        ├── media-source-finder/
        └── stremio-addon/
```

---

## .env on the Server

Located at `/docker/csfd/.env` — never committed to git, created manually on the server.

```env
WEBSHARE_USERNAME=hlavoj
WEBSHARE_PASSWORD=Italie2019
OMDB_API_KEY=4f3f86a3
TMDB_API_KEY=7787923fe42abded82141804d9f315a1
OPENROUTER_API_KEY=sk-or-v1-...
ADDON_URL=https://srv1475341.hstgr.cloud
DOMAIN=srv1475341.hstgr.cloud
```

Docker Compose reads this automatically and injects the variables into each container.

---

## How Traefik Routing Works

The stremio-addon container has these labels in `docker-compose.yml`:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.stremio.rule=Host(`srv1475341.hstgr.cloud`)"
  - "traefik.http.routers.stremio.entrypoints=websecure"
  - "traefik.http.routers.stremio.tls.certresolver=letsencrypt"
  - "traefik.http.services.stremio.loadbalancer.server.port=7000"
```

Traefik reads these labels from the Docker socket, creates an HTTPS router for the domain, fetches a Let's Encrypt cert automatically, and forwards traffic to port 7000 on the container.

---

## Deploying Updates

```bash
# SSH into the server
ssh root@187.77.147.171

# Pull latest code and rebuild
cd /docker/csfd
git pull
docker compose up -d --build
```

The `--build` flag rebuilds Docker images from the Dockerfiles. Pip dependencies are cached — only reinstalled if `requirements.txt` changes.

---

## Useful Commands

```bash
# View live logs
docker compose logs -f stremio-addon
docker compose logs -f media-source-finder

# Check running containers
docker compose ps

# Restart everything
docker compose restart

# Restart one service
docker compose restart stremio-addon

# Stop everything
docker compose down

# Rebuild and start
docker compose up -d --build
```

---

## Installing the Addon in Stremio

Works on any device — Android TV, desktop app, web (web.stremio.com):

1. Open Stremio → **Addons**
2. Paste in the URL bar: `https://srv1475341.hstgr.cloud/manifest.json`
3. Click **Install**
4. Open any movie or TV series → **Watch** → streams appear

---

## How a Code Update Flows

```
Your PC           GitHub              VPS
  │                 │                  │
  ├─ git push ─────►│                  │
  │                 │                  │
  │          SSH + git pull ──────────►│
  │          docker compose up --build►│ rebuilds images
  │                                    │ restarts containers
  │                                    │ new version live
```
