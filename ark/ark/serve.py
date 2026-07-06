"""ark serve — the sync receiver + web companion (P4, cross-platform).

Native Android/iOS apps need mobile toolchains this build environment doesn't
have. But the *value* of a companion app — "send my phone's photos to the vault
over the network" — is delivered here by a tiny HTTP receiver plus an installable
web app (PWA) that runs in any phone browser (Android Chrome, iOS Safari). The
phone is the client; ``pipeline.run`` on this machine does the real work.

Everything the receiver does is the same non-destructive pipeline used
everywhere: an upload lands in ``.ark/inbox/`` (ARK's own transient staging, not
your data), gets ingested (content-addressed, hash-verified, deduped) and then —
only once a *verified* object exists in the vault — the staging temp is removed.
A failed or review-flagged upload is kept, never dropped.

Auth is a shared token (like Jupyter): the server prints a URL carrying it; the
page keeps it and sends it on every upload. Bind to 127.0.0.1 by default; set
``--host 0.0.0.0`` to reach it from your phone on the same LAN.
"""

from __future__ import annotations

import json
import secrets
import socket
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from . import pipeline
from .config import Config, load_config
from .constants import DIR_META, DB_FILENAME
from .db import Database

WEBAPP_DIR = Path(__file__).parent / "webapp"
_MAX_UPLOAD = 1024 * 1024 * 1024        # 1 GiB per file — plenty for a photo/video
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".webmanifest": "application/manifest+json",
    ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon",
}


@dataclass
class IngestOutcome:
    ok: bool
    filename: str
    status: str = ""
    organized_path: str = ""
    hash: str = ""
    bytes: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "filename": self.filename, "status": self.status,
                "organized_path": self.organized_path, "hash": self.hash,
                "bytes": self.bytes, "error": self.error}


def ingest_upload(vault: Path, cfg: Config, filename: str, data: bytes) -> IngestOutcome:
    """Stage one uploaded file and run it through the normal pipeline.

    Non-destructive: the staging temp under ``.ark/inbox/`` is removed only after
    a hash-verified object exists in the vault; anything failed/needs-review is
    kept so nothing questionable is ever lost."""
    name = _safe_name(filename)
    inbox = vault / DIR_META / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    staged = inbox / f"{uuid.uuid4().hex}-{name}"
    try:
        staged.write_bytes(data)
    except OSError as e:
        return IngestOutcome(False, name, error=f"could not stage upload: {e}")

    try:
        stats = pipeline.run(staged, vault, cfg, dry_run=False)
    except Exception as e:  # noqa: BLE001 — never let one upload crash the server
        staged.unlink(missing_ok=True)
        return IngestOutcome(False, name, error=f"{type(e).__name__}: {e}")

    item = stats.items[0] if stats.items else None
    if item is None or item.status == "failed":
        # keep the staged bytes for inspection; report the failure
        return IngestOutcome(False, name, status="failed",
                             error=item.error if item else "nothing ingested")

    # A verified object exists now, so the staging temp is safe to drop (the
    # sender still holds the original; the vault holds the verified copy).
    if item.status in ("stored", "duplicate"):
        staged.unlink(missing_ok=True)
    return IngestOutcome(
        True, name, status=item.status, organized_path=item.organized_path,
        hash=item.hash, bytes=item.size,
    )


def _safe_name(filename: str) -> str:
    base = Path(filename or "").name.replace("\x00", "").strip()
    base = base.lstrip(".") or "upload"
    return base[:200]


# ---- HTTP server -----------------------------------------------------------

class ArkServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, vault: Path, cfg: Config, token: str):
        self.vault = vault
        self.cfg = cfg
        self.token = token
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ARK/1.0"

    # ---- helpers ----
    def _authed(self) -> bool:
        token = self.server.token
        header = self.headers.get("X-ARK-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            header = header or auth[7:]
        qs = parse_qs(urlparse(self.path).query)
        supplied = header or (qs.get("t", [""])[0])
        return secrets.compare_digest(str(supplied), str(token))

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:      # quiet by default; the CLI narrates
        pass

    # ---- routes ----
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path == "/status":
            if not self._authed():
                return self._send_json(401, {"ok": False, "error": "bad or missing token"})
            with Database(self.server.vault / DIR_META / DB_FILENAME) as db:
                s = db.stats()
            return self._send_json(200, {"ok": True, "assets": s["assets"],
                                         "objects": s["objects"], "duplicates": s["duplicates"]})
        # static assets from the webapp dir (path-traversal proof)
        return self._serve_static(path.lstrip("/"))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/ingest":
            return self._send_json(404, {"ok": False, "error": "not found"})
        if not self._authed():
            return self._send_json(401, {"ok": False, "error": "bad or missing token"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_UPLOAD:
            return self._send_json(413, {"ok": False, "error": "missing or oversized body"})
        data = self.rfile.read(length)
        filename = self.headers.get("X-Filename", "upload")
        outcome = ingest_upload(self.server.vault, self.server.cfg, filename, data)
        return self._send_json(200 if outcome.ok else 422, outcome.as_dict())

    def _serve_static(self, rel: str) -> None:
        if not rel:
            rel = "index.html"
        target = (WEBAPP_DIR / rel).resolve()
        try:
            target.relative_to(WEBAPP_DIR.resolve())
        except ValueError:
            return self._send_json(403, {"ok": False, "error": "forbidden"})
        if not target.is_file():
            return self._send_json(404, {"ok": False, "error": "not found"})
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _STATIC_TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(vault: Path, cfg: Config, host: str, port: int,
                token: Optional[str] = None) -> ArkServer:
    token = token or secrets.token_urlsafe(16)
    return ArkServer((host, port), vault, cfg, token)


def lan_ip() -> str:
    """Best-effort LAN IP so the printed URL is reachable from a phone."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"
