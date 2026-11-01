# Streamer Hunt — video ingestion component

This is the first reusable service component for Streamer Hunt. It converts an authorised YouTube video, Shorts video, or live replay to MP4 and exposes the conversion as an MCP tool and a small phone-friendly web page.

## Files

- `Dockerfile` — Python 3.12 + FFmpeg container, runs as a non-root user.
- `requirements.txt` — Python dependencies.
- `mp4_mcp_server.py` — MCP tool, phone API, conversion and signed download route.

## Required hosting environment variables

- `ACCESS_KEY` — random secret, at least 24 characters. Used by both the MCP tool and web API.
- `PUBLIC_BASE_URL` — public HTTPS base URL of the deployed service, with no trailing slash.
- `PORT` — defaults to `8000` (most container hosts set this automatically).
- `MCP_PATH` — defaults to `/mcp`.
- `MAX_MINUTES` — defaults to `60`.
- `OUT_DIR` — defaults to `/app/downloads` in the container.

Generate a secret locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Store it as a hosting secret; do not commit it to Git.

## Endpoints

- `GET /` — phone-friendly converter page.
- `POST /api/convert` — JSON body: `{"url":"https://www.youtube.com/watch?v=...","max_height":720,"key":"..."}`.
- `GET /files/{filename}?expires=...&sig=...` — signed download link, valid for 24 hours.
- `/mcp` — Streamable HTTP MCP endpoint. Call `url_to_mp4` with `url`, `access_key`, and optional `max_height` (480, 720, or 1080).

## Current scope and limitations

- This component accepts YouTube video, Shorts, and live-replay URLs only. Other platforms need explicit adapters and testing.
- It does not discover streamers, monitor live broadcasts, detect highlights, create TikTok edits, or publish posts. Those are later pipeline components.
- The service does not determine whether a user has rights to a video. Only process footage you own or have explicit permission to save, and follow the source platform's terms.
- Download links expire after 24 hours. Files are not automatically deleted yet; configure persistent storage and a retention/cleanup job before production use.
- Set a persistent volume for `/app/downloads` if the host's container filesystem is ephemeral.

## Local run

```bash
export ACCESS_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export PUBLIC_BASE_URL="http://localhost:8000"
pip install -r requirements.txt
python mp4_mcp_server.py
```

For a public deployment, set `PUBLIC_BASE_URL` to the HTTPS address assigned by the hosting provider. Never expose the service publicly without a strong `ACCESS_KEY`.
