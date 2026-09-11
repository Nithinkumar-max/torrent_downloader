# Torrent Downloader

A self-hosted web torrent downloader built on **FastAPI + libtorrent** with a clean live-progress UI.

## Features
- Add torrents by **magnet link** or **.torrent file upload**
- **Live progress**: speed, ETA, peer count, seeding status (auto-refreshing)
- **Multi-torrent** management: pause / resume / remove / delete-files
- Per-torrent **file list** with individual progress + direct download
- Per-torrent **peer inspector**: seeds, choke state, source (tracker/DHT/PeX), client
- **Speed settings** (persisted): download/upload caps, RC4 encryption, configurable listen port
- Aggressive peer discovery: all trackers/tiers, DHT (persisted across restarts), LSD, PeX, UPnP/NAT-PMP

## Run locally
```bash
pip install -r requirements.txt
python -m uvicorn app:app --port 8000
```
Open http://127.0.0.1:8000   

or visit  : https://bolttorrent.vercel.app/

Requires **Python 3.10+** (libtorrent 2.1 has wheels for Windows/macOS/Linux).

## Speed notes
- Download speed is bounded by the **swarm** (seed supply) and **your line** — no client setting can exceed those. Slow torrents = thin seeds.
- Common wins: move the folder out of cloud-sync drives (OneDrive/Dropbox), add it to antivirus exclusions, cap upload at ~75% of your real uplink, use a non-default listen port, enable encryption if your ISP throttles BitTorrent.
- Want 10-100 MB/s consistently? That's what **seedbox / debrid** services sell — they run in datacenters (1-10 Gbps) or serve cached copies from fast HTTPS links. It's physical infrastructure, not a client tweak.

## Deploying to Vercel
**Vercel only hosts the frontend** — libtorrent cannot run on serverless. Two options:

1. **Frontend on Vercel + backend on a VPS**: deploy `static/` to Vercel, run `app.py` on any always-on box, then:
   - Set the backend URL in `static/index.html`: `window.API_BASE = 'https://your-backend.example.com'`
   - Update the `destination` in `vercel.json` rewrites, or call the backend directly with `API_BASE`.
   - Point `DOWNLOAD_DIR`, session/listen port, and encryption settings via `settings.json` on the server.

2. **Everything on a VPS**: `git clone` the repo on any Linux box, run uvicorn there, skip Vercel entirely. Cheaper and simpler.

## API (summary)
| Method | Path | Description |
|---|---|---|
| GET | `/api/torrents` | list all torrents + live status |
| POST | `/api/torrents` | add magnet (`{"magnet": "..."}`) |
| POST | `/api/torrents/upload` | upload `.torrent` file |
| GET | `/api/torrents/{id}/files` | file list + per-file progress |
| GET | `/api/torrents/{id}/peers` | peer inspector |
| POST | `/api/torrents/{id}/pause` / `/resume` / `/reannounce` | control a torrent |
| DELETE | `/api/torrents/{id}?delete_files=true` | remove (optionally delete files) |
| GET/POST | `/api/settings` | read/update caps, encryption, listen port |
| GET | `/api/session` | session stats: DHT size, aggregate speeds, incoming-connection status |

## Project structure
```
├── app.py            # FastAPI + libtorrent backend
├── static/index.html # frontend (served by FastAPI or Vercel)
├── requirements.txt
├── vercel.json       # Vercel frontend rewrites (optional)
└── downloads/        # completed downloads (gitignored)
```
