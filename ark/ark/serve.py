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

Auth is a shared bearer token entered into the page and sent only in request
headers. Bind to 127.0.0.1 by default; set ``--host 0.0.0.0`` only on a trusted
LAN (the built-in server does not provide TLS).
"""

from __future__ import annotations

import io
import json
import os
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import pipeline
from .config import Config, load_config
from .constants import DIR_META, DB_FILENAME
from .db import Database

WEBAPP_DIR = Path(__file__).parent / "webapp"
_MAX_UPLOAD = 1024 * 1024 * 1024        # 1 GiB per file — plenty for a photo/video
_MAX_OUTSTANDING = 2 * 1024 * 1024 * 1024  # staged + declared in-flight bytes per server/vault
_MAX_CONNECTIONS = 4
_REQUEST_TIMEOUT = 60.0                 # header inactivity + total body-receive deadline
_READ_CHUNK = 1 << 20
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


@dataclass
class StagedReceive:
    path: Path
    filename: str
    received: int
    expected: int
    error: str = ""


class UploadError(RuntimeError):
    pass


def ingest_upload(vault: Path, cfg: Config, filename: str, data: bytes) -> IngestOutcome:
    """Stage one uploaded file and run it through the normal pipeline.

    Non-destructive: the staging temp under ``.ark/inbox/`` is removed only after
    a hash-verified object exists in the vault; anything failed/needs-review is
    kept so nothing questionable is ever lost."""
    try:
        received = stage_upload_stream(vault, filename, io.BytesIO(data), len(data))
    except (OSError, UploadError) as e:
        return IngestOutcome(False, _safe_name(filename), error=f"could not stage upload: {e}")
    if received.error or received.received != received.expected:
        return IngestOutcome(False, received.filename, bytes=received.received,
                             error=received.error or "truncated upload retained in staging")
    return ingest_staged(vault, cfg, received.filename, received.path)


def ingest_staged(vault: Path, cfg: Config, filename: str, staged: Path) -> IngestOutcome:
    """Ingest one already-staged upload; retain it on every failure path."""
    name = _safe_name(filename)
    inbox = _validated_inbox(vault)
    try:
        staged = staged.resolve(strict=True)
        staged.relative_to(inbox)
    except (OSError, ValueError) as e:
        return IngestOutcome(False, name, error=f"unsafe staged upload path: {e}")
    if staged.is_symlink():
        return IngestOutcome(False, name, error="unsafe staged upload symlink")

    try:
        stats = pipeline.run(staged, vault, cfg, dry_run=False)
    except Exception as e:  # noqa: BLE001 — never let one upload crash the server
        # The upload may be the sender's only delivered copy. Retain it for a
        # retry; only a verified stored/duplicate outcome permits unlinking.
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


def stage_upload_stream(vault: Path, filename: str, stream, expected_length: int,
                        *, deadline: Optional[float] = None,
                        connection=None) -> StagedReceive:
    """Stream exactly ``expected_length`` bytes into a private staged file.

    A short read, timeout, disk error, or disconnect is a failed upload. Any
    bytes received remain in the inbox and are never passed to the pipeline or
    acknowledged as backed up.
    """
    name = _safe_name(filename)
    inbox = _validated_inbox(vault)
    staged = inbox / f"{uuid.uuid4().hex}-{name}"
    try:
        staged.resolve(strict=False).relative_to(inbox)
    except ValueError as e:
        raise UploadError("staged path escaped inbox") from e

    received = 0
    error = ""
    try:
        with open(staged, "xb", buffering=0) as fd:
            while received < expected_length:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("upload receive deadline exceeded")
                    if connection is not None:
                        connection.settimeout(remaining)
                chunk = stream.read(min(_READ_CHUNK, expected_length - received))
                if not chunk:
                    break
                fd.write(chunk)
                received += len(chunk)
            fd.flush()
            os.fsync(fd.fileno())
    except (OSError, TimeoutError) as e:
        error = f"upload failed after {received} of {expected_length} bytes; staged bytes retained: {e}"

    if not error and received != expected_length:
        error = (f"truncated upload: received {received} of {expected_length} bytes; "
                 "staged bytes retained")
    return StagedReceive(staged, name, received, expected_length, error)


def _validated_inbox(vault: Path) -> Path:
    vault = Path(vault).resolve()
    meta = vault / DIR_META
    if meta.is_symlink():
        raise UploadError(f"refusing symlinked metadata directory: {meta}")
    meta.mkdir(parents=True, exist_ok=True)
    try:
        meta.resolve(strict=True).relative_to(vault)
    except ValueError as e:
        raise UploadError(f"metadata directory escapes vault: {meta}") from e
    inbox = meta / "inbox"
    if inbox.is_symlink():
        raise UploadError(f"refusing symlinked upload inbox: {inbox}")
    inbox.mkdir(parents=True, exist_ok=True)
    if inbox.is_symlink():
        raise UploadError(f"refusing symlinked upload inbox: {inbox}")
    try:
        resolved = inbox.resolve(strict=True)
        resolved.relative_to(meta.resolve(strict=True))
    except ValueError as e:
        raise UploadError(f"upload inbox escapes vault metadata: {inbox}") from e
    return resolved


def _safe_name(filename: str) -> str:
    base = Path(filename or "").name.replace("\x00", "").strip()
    base = base.lstrip(".") or "upload"
    return base[:200]


# ---- HTTP server -----------------------------------------------------------

class ArkServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = _MAX_CONNECTIONS

    def __init__(self, addr, vault: Path, cfg: Config, token: str):
        self.vault = vault
        self.cfg = cfg
        self.token = token
        self._connection_slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._quota_lock = threading.Lock()
        self._reserved_bytes = 0
        super().__init__(addr, _Handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(_REQUEST_TIMEOUT)
        return request, address

    def process_request(self, request, client_address) -> None:
        if not self._connection_slots.acquire(blocking=False):
            try:
                body = b'{"ok":false,"error":"server busy"}'
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Content-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def reserve_upload(self, declared: int, inbox: Path) -> bool:
        with self._quota_lock:
            try:
                staged = sum(
                    p.stat().st_size for p in inbox.iterdir()
                    if p.is_file() and not p.is_symlink()
                )
            except OSError:
                return False
            if staged + self._reserved_bytes + declared > _MAX_OUTSTANDING:
                return False
            self._reserved_bytes += declared
            return True

    def release_upload(self, declared: int) -> None:
        with self._quota_lock:
            self._reserved_bytes = max(0, self._reserved_bytes - declared)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ARK/1.0"

    # ---- helpers ----
    def _authed(self) -> bool:
        token = self.server.token
        header = self.headers.get("X-ARK-Token") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            header = auth[7:] or header
        supplied = header
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
        filename = self.headers.get("X-Filename", "upload")
        try:
            inbox = _validated_inbox(self.server.vault)
        except (OSError, UploadError) as e:
            return self._send_json(500, {"ok": False, "error": f"unsafe upload inbox: {e}"})
        if not self.server.reserve_upload(length, inbox):
            return self._send_json(507, {"ok": False, "error": "upload staging quota exceeded"})
        try:
            received = stage_upload_stream(
                self.server.vault, filename, self.rfile, length,
                deadline=time.monotonic() + _REQUEST_TIMEOUT,
                connection=self.connection,
            )
        finally:
            self.server.release_upload(length)
        if received.error or received.received != length:
            return self._send_json(400, {
                "ok": False, "filename": received.filename, "bytes": received.received,
                "error": received.error or "truncated upload",
            })
        outcome = ingest_staged(
            self.server.vault, self.server.cfg, received.filename, received.path)
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
