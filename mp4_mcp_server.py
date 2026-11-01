#!/usr/bin/env python3
"""Authenticated URL-to-MP4 ingestion service for authorised YouTube videos.

Run with ACCESS_KEY and PUBLIC_BASE_URL set. The MCP tool and phone API both
require ACCESS_KEY. Download links are signed and expire after 24 hours.
Only download videos you own or have explicit permission to save.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from yt_dlp import YoutubeDL

PORT = int(os.environ.get("PORT", "8000"))
BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
OUT_DIR = Path(os.environ.get("OUT_DIR", "./downloads")).resolve()
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "60"))
ACCESS_KEY = os.environ.get("ACCESS_KEY", "")
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
LINK_TTL_SECONDS = 24 * 60 * 60
ALLOWED_HEIGHTS = {480, 720, 1080}
ALLOWED_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"}
OUT_DIR.mkdir(parents=True, exist_ok=True)

if not ACCESS_KEY or len(ACCESS_KEY) < 24:
    raise RuntimeError("Set ACCESS_KEY to a random secret of at least 24 characters before starting.")
if not MCP_PATH.startswith("/") or MCP_PATH == "/":
    raise RuntimeError("MCP_PATH must be an absolute path such as /mcp.")

mcp = FastMCP("streamer-hunt-video-ingest", host="0.0.0.0", port=PORT, streamable_http_path=MCP_PATH)


def _check_key(candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(str(candidate), ACCESS_KEY)


def _validate_url(url: str) -> str:
    parsed = urlsplit((url or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in ALLOWED_HOSTS:
        raise ValueError("Use an HTTPS YouTube video URL. Other sites are not enabled in this component yet.")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("The video URL contains unsupported credentials or port information.")
    if host == "youtu.be" and not parsed.path.strip("/"):
        raise ValueError("The YouTube short link is missing its video ID.")
    if host != "youtu.be" and not (parsed.path.startswith("/watch") or parsed.path.startswith("/shorts/") or parsed.path.startswith("/live/") or parsed.path.startswith("/embed/")):
        raise ValueError("Provide a YouTube video, Shorts, or live-replay URL, not a channel or playlist page.")
    return url.strip()


def _signed_url(filename: str) -> str:
    expires = int(time.time()) + LINK_TTL_SECONDS
    payload = f"{filename}:{expires}".encode("utf-8")
    signature = hmac.new(ACCESS_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"{BASE_URL}/files/{filename}?expires={expires}&sig={signature}"


def _download(url: str, max_height: int) -> dict:
    url = _validate_url(url)
    if max_height not in ALLOWED_HEIGHTS:
        raise ValueError("max_height must be 480, 720, or 1080.")

    token = secrets.token_urlsafe(12)
    opts = {
        "format": f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={max_height}][ext=mp4]/best[height<={max_height}]",
        "merge_output_format": "mp4",
        "outtmpl": str(OUT_DIR / f"{token}_%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "max_filesize": 2 * 1024 * 1024 * 1024,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration") or 0
        if duration > MAX_MINUTES * 60:
            raise ValueError(f"Video is longer than the configured {MAX_MINUTES}-minute limit.")
        info = ydl.extract_info(url, download=True)
        candidates = [info.get("filepath"), info.get("_filename")]
        candidates.extend(item.get("filepath") for item in info.get("requested_downloads", []) if isinstance(item, dict))
        path = next((Path(p) for p in candidates if p and Path(p).is_file()), None)
        if path is None:
            # yt-dlp may report the pre-merge extension; check the merged MP4 name.
            prepared = Path(ydl.prepare_filename(info))
            mp4_path = prepared.with_suffix(".mp4")
            if mp4_path.is_file():
                path = mp4_path
        if path is None or not path.is_file() or path.suffix.lower() != ".mp4":
            raise RuntimeError("The MP4 output could not be located after conversion.")

    filename = path.name
    return {
        "title": info.get("title") or "Converted video",
        "duration_seconds": info.get("duration"),
        "filename": filename,
        "download_url": _signed_url(filename),
        "expires_in_seconds": LINK_TTL_SECONDS,
    }


@mcp.tool()
async def url_to_mp4(url: str, access_key: str, max_height: int = 720) -> dict:
    """Convert an authorised YouTube video URL to MP4.

    Args:
        url: HTTPS YouTube video/Shorts/live-replay URL. Only use footage you own or are authorised to save.
        access_key: The ACCESS_KEY configured on the hosted service.
        max_height: Output height; one of 480, 720, or 1080.
    """
    if not _check_key(access_key):
        return {"error": "Unauthorised. Check the service access key."}
    try:
        return await asyncio.to_thread(_download, url, max_height)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.custom_route("/files/{name}", methods=["GET"])
async def serve_file(request: Request):
    name = request.path_params["name"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+_[A-Za-z0-9_-]+\.mp4", name):
        return PlainTextResponse("Not found", status_code=404)
    try:
        expires = int(request.query_params.get("expires", "0"))
    except ValueError:
        return PlainTextResponse("Not found", status_code=404)
    signature = request.query_params.get("sig", "")
    expected = hmac.new(ACCESS_KEY.encode("utf-8"), f"{name}:{expires}".encode("utf-8"), hashlib.sha256).hexdigest()
    if expires < int(time.time()) or expires > int(time.time()) + LINK_TTL_SECONDS + 60 or not hmac.compare_digest(signature, expected):
        return PlainTextResponse("This download link is invalid or has expired.", status_code=403)
    path = (OUT_DIR / name).resolve()
    if path.parent != OUT_DIR or not path.is_file():
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path, media_type="video/mp4", filename=name, headers={"Cache-Control": "private, no-store"})


PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Streamer Hunt — Video Ingest</title><style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--card:#f1f2f5;--accent:#356df3}
@media(prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--card:#24252a}}
body{margin:0;padding:22px;max-width:560px;margin-inline:auto;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
h1{font-size:23px}input,select,button{box-sizing:border-box;width:100%;padding:13px;margin:7px 0;border:0;border-radius:10px;background:var(--card);color:var(--fg);font:inherit}
button{background:var(--accent);color:white;font-weight:650}button:disabled{opacity:.6}#out{margin-top:15px;overflow-wrap:anywhere}.dl{display:block;padding:13px;border-radius:10px;background:var(--card);color:var(--accent);text-align:center;font-weight:650;text-decoration:none}
small{opacity:.75}
</style></head><body><h1>Streamer Hunt — Video Ingest</h1>
<p>Convert an authorised YouTube video or replay into an MP4.</p>
<input id="url" type="url" placeholder="Paste YouTube video link" autocomplete="url">
<select id="quality"><option value="480">480p</option><option value="720" selected>720p</option><option value="1080">1080p</option></select>
<input id="key" type="password" placeholder="Service access key" autocomplete="current-password">
<button id="go">Convert to MP4</button><div id="out" role="status"></div>
<small>Only process footage you own or have explicit permission to save. Links expire after 24 hours.</small>
<script>
const el=id=>document.getElementById(id);
el('key').value=sessionStorage.getItem('streamerHuntKey')||'';
el('go').addEventListener('click',async()=>{
 const button=el('go'),out=el('out');button.disabled=true;out.textContent='Converting…';
 sessionStorage.setItem('streamerHuntKey',el('key').value);
 try{const response=await fetch('/api/convert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:el('url').value,max_height:Number(el('quality').value),key:el('key').value})});
 const data=await response.json();if(!response.ok||data.error)throw new Error(data.error||'Conversion failed');
 out.replaceChildren();const title=document.createElement('p');title.textContent=data.title||'Your MP4 is ready';const link=document.createElement('a');link.className='dl';link.href=data.download_url;link.textContent='Download MP4';out.append(title,link);
 }catch(error){out.textContent='Error: '+error.message;}finally{button.disabled=false;}
});
</script></body></html>'''


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request):
    return HTMLResponse(PAGE)


@mcp.custom_route("/api/convert", methods=["POST"])
async def api_convert(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
    if not isinstance(body, dict) or not _check_key(body.get("key")):
        return JSONResponse({"error": "Unauthorised. Check the service access key."}, status_code=401)
    try:
        result = await asyncio.to_thread(_download, str(body.get("url", "")), int(body.get("max_height", 720)))
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


if __name__ == "__main__":
    if BASE_URL.startswith("http://") and "localhost" not in BASE_URL:
        raise RuntimeError("Use HTTPS for PUBLIC_BASE_URL when hosting this service publicly.")
    mcp.run(transport="streamable-http")
