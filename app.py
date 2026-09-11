"""Torrent Downloader - FastAPI + libtorrent backend."""
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict

import libtorrent as lt
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# --- libtorrent session (tuned for peer discovery + throughput) ---
import atexit

DHT_STATE_FILE = BASE_DIR / "dht.state"

ses = lt.session({
    "listen_interfaces": "0.0.0.0:6881,[::]:6881",
    "enable_dht": True,
    "enable_lsd": True,
    "enable_upnp": True,      # try to open the listen port on the router automatically
    "enable_natpmp": True,
    # talk to every tracker, not just one per tier — more peers, faster
    "announce_to_all_trackers": True,
    "announce_to_all_tiers": True,
    # no artificial caps
    "connections_limit": 400,
    "active_downloads": -1,
    "active_seeds": -1,
    "active_limit": -1,
    "download_rate_limit": 0,
    "upload_rate_limit": 0,
    "unchoke_slots_limit": -1,  # generous uploading => peers unchoke us back
    "allow_multiple_connections_per_ip": True,
    "max_peerlist_size": 4000,
    "seeding_piece_quota": 20,
    "request_timeout": 20,
    "peer_timeout": 180,
    # aggressive peer acquisition + rotation
    "connection_speed": 100,      # new outbound connections per second
    "torrent_connect_boost": 100, # extra connect attempts for fresh torrents
    "inactivity_timeout": 300,    # drop dead peers faster, rotate to good ones
    "prioritize_partial_pieces": True,
    # disk headroom so I/O never throttles the pipe
    "cache_size": 4096,           # 16KiB blocks (~64 MB)
    "file_pool_size": 100,
})
for _host, _port in [
    ("router.bittorrent.com", 6881),
    ("router.utorrent.com", 6881),
    ("router.bitcomet.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("dht.libtorrent.org", 25401),
]:
    ses.add_dht_router(_host, _port)

# reuse DHT bootstrap nodes across restarts (cold DHT = slow peer discovery)
if DHT_STATE_FILE.exists():
    try:
        ses.load_state(lt.bdecode(DHT_STATE_FILE.read_bytes()))
    except Exception:
        pass
ses.start_dht()


def _save_dht_state():
    try:
        DHT_STATE_FILE.write_bytes(lt.bencode(ses.save_state()))
    except Exception:
        pass


atexit.register(_save_dht_state)


# --- user speed settings (persisted) ---
import json

SETTINGS_FILE = BASE_DIR / "settings.json"
DEFAULT_SETTINGS = {"download_limit_kb": 0, "upload_limit_kb": 0, "force_encryption": False,
                      "listen_port": 6881}


def _load_user_settings():
    try:
        if SETTINGS_FILE.exists():
            d = json.loads(SETTINGS_FILE.read_text())
            return {**DEFAULT_SETTINGS, **{k: d[k] for k in DEFAULT_SETTINGS if k in d}}
    except Exception:
        pass
    return dict(DEFAULT_SETTINGS)


def _apply_user_settings(s):
    pack = {
        "download_rate_limit": max(0, int(s["download_limit_kb"])) * 1024,
        "upload_rate_limit": max(0, int(s["upload_limit_kb"])) * 1024,
    }
    if s["force_encryption"]:
        # strict RC4: dodges ISP throttling but REFUSES plaintext peers (smaller pool)
        pack.update({"out_enc_policy": 0, "in_enc_policy": 0, "allowed_enc_level": 1,
                     "prefer_rc4": True})
    else:
        # middle ground: talk to everyone, prefer encrypted when the peer supports it
        pack.update({"out_enc_policy": 1, "in_enc_policy": 1, "allowed_enc_level": 2,
                     "prefer_rc4": True})
    try:
        port = int(s.get("listen_port", 6881))
    except (TypeError, ValueError):
        port = 6881
    if not 1 <= port <= 65535:
        port = 6881
    pack["listen_interfaces"] = f"0.0.0.0:{port},[::]:{port}"
    ses.apply_settings(pack)


_apply_user_settings(_load_user_settings())

# id (info-hash hex) -> handle
handles: Dict[str, lt.torrent_handle] = {}


class AddMagnet(BaseModel):
    magnet: str


def _torrent_id(handle: lt.torrent_handle) -> str:
    return str(handle.info_hash())


def _status_dict(tid: str, h: lt.torrent_handle) -> dict:
    s = h.status()
    has_meta = h.has_metadata()
    try:
        name = h.name() if has_meta else tid
    except Exception:
        name = tid
    total = s.total_wanted or 0
    done = s.total_wanted_done or 0
    progress = round(done / total * 100, 1) if total else 0.0
    # ETA
    rate = s.download_rate or 0
    eta = int((total - done) / rate) if rate > 0 and total > done else -1
    state_str = str(s.state)
    is_paused = h.is_paused() if hasattr(h, "is_paused") else False
    # libtorrent 2.x: flags via status? fallback to handle
    try:
        paused = h.status().paused
    except Exception:
        paused = is_paused
    return {
        "id": tid,
        "name": name,
        "progress": progress,
        "download_rate": rate,
        "upload_rate": s.upload_rate or 0,
        "peers": s.num_peers or 0,
        "seeds": s.num_seeds or 0,
        "total_size": total,
        "downloaded": done,
        "eta": eta,
        "state": state_str,
        "paused": bool(paused),
        "has_metadata": bool(has_meta),
        "is_seeding": bool(s.is_seeding),
    }


app = FastAPI(title="Torrent Downloader")


@app.get("/api/torrents")
def list_torrents():
    return [_status_dict(tid, h) for tid, h in handles.items()]


@app.post("/api/torrents")
def add_magnet(body: AddMagnet):
    magnet = (body.magnet or "").strip()
    if not magnet:
        raise HTTPException(400, "Empty magnet link")
    try:
        params = lt.parse_magnet_uri(magnet)
    except Exception as e:
        raise HTTPException(400, f"Invalid magnet link: {e}")
    return _add(params)


@app.post("/api/torrents/upload")
async def upload_torrent(file: UploadFile = File(...)):
    if not file.filename.endswith(".torrent"):
        raise HTTPException(400, "File must be a .torrent file")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".torrent") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    try:
        try:
            ti = lt.torrent_info(tmp_path)
        except Exception as e:
            raise HTTPException(400, f"Invalid .torrent file: {e}")
        params = lt.add_torrent_params()
        params.ti = ti
    finally:
        os.unlink(tmp_path)
    return _add(params)


def _add(params):
    params.save_path = str(DOWNLOAD_DIR)
    try:
        h = ses.add_torrent(params)
    except Exception as e:
        raise HTTPException(400, f"Could not add torrent: {e}")

    tid = _torrent_id(h)
    handles[tid] = h
    return {"id": tid, **_status_dict(tid, h)}


PEER_SOURCES = {1: "tracker", 2: "DHT", 4: "PeX", 8: "LSD", 16: "resume", 32: "incoming"}


@app.get("/api/torrents/{tid}/peers")
def torrent_peers(tid: str, limit: int = 50):
    """Per-peer diagnostics: who gives bytes, who chokes, who are seeds."""
    h = handles.get(tid)
    if not h:
        raise HTTPException(404, "Torrent not found")
    try:
        infos = h.get_peer_info()
    except Exception as e:
        raise HTTPException(400, f"Could not get peers: {e}")
    peers = []
    for p in infos:
        try:
            flags = int(p.flags)
            src = int(p.source)
            c = p.client
            if isinstance(c, (bytes, bytearray)):
                client = bytes(c).decode("utf-8", "replace")
            else:
                client = str(c)
            peers.append({
                "ip": p.ip[0],
                "port": p.ip[1],
                "client": client,
                "down": p.down_speed or 0,
                "up": p.up_speed or 0,
                "progress": round((p.progress or 0) * 100, 1),
                "seed": bool(flags & 0x400),
                "we_choke_them": bool(flags & 0x2),
                "they_choke_us": bool(flags & 0x8),
                "interested": bool(flags & 0x1),
                "connecting": bool(flags & 0xC0),
                "source": "|".join(n for b, n in PEER_SOURCES.items() if src & b) or "unknown",
            })
        except Exception:
            continue
    peers.sort(key=lambda x: x["down"], reverse=True)
    torrent_rate = (h.status().download_rate or 0)
    aggregate = sum(p["down"] for p in peers)
    over_100 = sum(1 for p in peers if p["down"] > 100 * 1024)
    over_1m = sum(1 for p in peers if p["down"] > 1024 * 1024)
    downloading = sum(1 for p in peers if (not p["seed"]) and (not p["they_choke_us"]) and (not p["connecting"]))
    return {
        "peers": peers[:max(1, min(limit, 100))],
        "total": len(peers),
        "seeds": sum(1 for x in peers if x["seed"]),
        "choking_us": sum(1 for x in peers if x["they_choke_us"]),
        "peer_bandwidth": {
            "torrent_rate": torrent_rate,
            "aggregate": aggregate,
            "fastest": max((p["down"] for p in peers), default=0),
            "over_100kb": over_100,
            "over_1mb": over_1m,
            "downloading": downloading,
            # aggregate >> torrent_rate  => high-level peer claims speed your session is not absorbing
            # aggregate << torrent_rate  => peers hide payload (encryption/overhead) or odd accounting
            "absorbed_pct": round((torrent_rate / max(aggregate, 1) * 100.0) * 100) / 100 if aggregate else 0,
        },
    }


@app.get("/api/torrents/{tid}/files")
def list_files(tid: str):
    h = handles.get(tid)
    if not h:
        raise HTTPException(404, "Torrent not found")
    if not h.has_metadata():
        return {"files": [], "has_metadata": False}
    ti = h.get_torrent_info()
    fs = ti.files()
    # per-file progress
    try:
        fp = h.file_progress()
    except Exception:
        fp = []
    out = []
    for i in range(fs.num_files()):
        out.append({
            "index": i,
            "path": fs.file_path(i),
            "size": fs.file_size(i),
            "progress": 0.0,  # filled below if available
            "done": fp[i] if i < len(fp) else 0,
        })
        if out[-1]["size"]:
            out[-1]["progress"] = round(out[-1]["done"] / out[-1]["size"] * 100, 1)
    return {"files": out, "has_metadata": True}


@app.get("/api/download/{tid}/{index}")
def download_file(tid: str, index: int):
    h = handles.get(tid)
    if not h or not h.has_metadata():
        raise HTTPException(404, "Torrent/file not ready")
    ti = h.get_torrent_info()
    fs = ti.files()
    if index < 0 or index >= fs.num_files():
        raise HTTPException(404, "File index out of range")
    rel = Path(fs.file_path(index))
    # prevent path traversal
    full = (DOWNLOAD_DIR / rel).resolve()
    if DOWNLOAD_DIR.resolve() not in full.parents and full != DOWNLOAD_DIR.resolve():
        raise HTTPException(403, "Invalid path")
    # handle single-file torrents where path may already include torrent name
    if not full.exists():
        # fallback: try torrent name prefix variants
        raise HTTPException(404, "File not downloaded yet")
    return FileResponse(str(full), filename=full.name)


@app.post("/api/torrents/{tid}/pause")
def pause_torrent(tid: str):
    h = handles.get(tid)
    if not h:
        raise HTTPException(404, "Torrent not found")
    h.pause()
    return {"ok": True}


@app.post("/api/torrents/{tid}/resume")
def resume_torrent(tid: str):
    h = handles.get(tid)
    if not h:
        raise HTTPException(404, "Torrent not found")
    h.resume()
    return {"ok": True}


@app.delete("/api/torrents/{tid}")
def remove_torrent(tid: str, delete_files: bool = False):
    h = handles.pop(tid, None)
    if not h:
        raise HTTPException(404, "Torrent not found")
    try:
        ses.remove_torrent(h, int(bool(delete_files)))
    except Exception:
        pass
    return {"ok": True}


class SettingsIn(BaseModel):
    download_limit_kb: int = 0
    upload_limit_kb: int = 0
    force_encryption: bool = False
    listen_port: int = 6881


@app.get("/api/settings")
def get_settings():
    return _load_user_settings()


@app.post("/api/settings")
def save_settings(s: SettingsIn):
    try:
        port = int(s.listen_port)
    except (TypeError, ValueError):
        port = 6881
    if not 1 <= port <= 65535:
        raise HTTPException(400, "Listen port must be 1–65535")
    d = {
        "download_limit_kb": max(0, s.download_limit_kb),
        "upload_limit_kb": max(0, s.upload_limit_kb),
        "force_encryption": bool(s.force_encryption),
        "listen_port": port,
    }
    SETTINGS_FILE.write_text(json.dumps(d))
    _apply_user_settings(d)
    return d


@app.get("/api/session")
def session_stats():
    st = ses.status()
    try:
        dht_on = ses.is_dht_running()
    except Exception:
        dht_on = True
    return {
        "dht_nodes": st.dht_nodes or 0,
        "dht_running": bool(dht_on),
        "download_rate": st.download_rate or 0,
        "upload_rate": st.upload_rate or 0,
        "peers": st.num_peers or 0,
        "has_incoming": bool(getattr(st, "has_incoming_connections", False)),
        "listen_port": int(_load_user_settings().get("listen_port", 6881)),
    }


@app.post("/api/torrents/{tid}/reannounce")
def reannounce(tid: str):
    """Force re-announce to all trackers + DHT — kickstarts stalled torrents."""
    h = handles.get(tid)
    if not h:
        raise HTTPException(404, "Torrent not found")
    try:
        h.force_reannounce()
    except Exception as e:
        raise HTTPException(400, f"Re-announce failed: {e}")
    try:
        h.force_dht_announce()
    except Exception:
        pass
    return {"ok": True}


# --- frontend ---
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
