#!/usr/bin/env python3
"""Upload repo WAV packs to the Reachy daemon speaker temp dir.

  python scripts/sound_sync.py --pack portalturret
  python scripts/sound_sync.py --pack portalturret --list
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_SOUNDS = _REPO / "sounds"


def load_manifest(pack: str) -> dict:
    path = _SOUNDS / pack / "manifest.json"
    data = json.loads(path.read_text())
    if "sounds" not in data:
        raise ValueError(f"invalid manifest: {path}")
    return data


def daemon_json(host: str, port: int, path: str, method: str = "GET",
                body: dict | None = None, timeout: float = 12.0) -> dict:
    url = f"http://{host}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def list_robot_sounds(host: str, port: int) -> set[str]:
    try:
        data = daemon_json(host, port, "/api/media/sounds", timeout=6)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return set()
    return {f for f in data.get("files", []) if f.endswith(".wav")}


def upload_wav(host: str, port: int, local: Path, timeout: float = 30.0) -> str:
    """Upload one WAV; return basename on robot."""
    boundary = uuid.uuid4().hex
    filename = local.name
    payload = local.read_bytes()
    parts = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    body = b"".join(parts)
    req = urllib.request.Request(
        f"http://{host}:{port}/api/media/sounds/upload",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    return Path(resp.get("path", filename)).name or filename


def sync_pack(host: str, port: int, pack: str, *,
              only: list[str] | None = None) -> dict:
    """Upload missing WAVs from a sound pack. Returns {uploaded, skipped, missing}."""
    manifest = load_manifest(pack)
    on_robot = list_robot_sounds(host, port)
    uploaded: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    pack_dir = _SOUNDS / pack

    entries = manifest["sounds"].items()
    if only:
        entries = [(k, manifest["sounds"][k]) for k in only if k in manifest["sounds"]]

    for sid, meta in entries:
        fname = meta["file"]
        local = pack_dir / fname
        if not local.is_file():
            missing.append(fname)
            continue
        if fname in on_robot:
            skipped.append(fname)
            continue
        upload_wav(host, port, local)
        on_robot.add(fname)
        uploaded.append(fname)
    return {"uploaded": uploaded, "skipped": skipped, "missing": missing}


def play_file(host: str, port: int, filename: str) -> None:
    daemon_json(host, port, "/api/media/play_sound", "POST", {"file": filename})


def resolve_cue(manifest: dict, cue: str) -> str | None:
    sid = manifest.get("cues", {}).get(cue)
    if not sid:
        return None
    meta = manifest["sounds"].get(sid)
    return meta["file"] if meta else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync sound packs to Reachy daemon")
    ap.add_argument("--pack", default="portalturret")
    ap.add_argument("--host", default=os.environ.get("REACHY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("REACHY_PORT", "8000")))
    ap.add_argument("--list", action="store_true", help="List pack + robot status")
    ap.add_argument("--play", metavar="ID", help="Play one sound id from manifest")
    args = ap.parse_args()

    manifest = load_manifest(args.pack)
    if args.list:
        on = list_robot_sounds(args.host, args.port)
        for sid, meta in manifest["sounds"].items():
            flag = "✓" if meta["file"] in on else "·"
            print(f"  {flag} {sid:28} {meta['label']}")
        print("cues:", manifest.get("cues", {}))
        return 0

    if args.play:
        meta = manifest["sounds"].get(args.play)
        if not meta:
            print(f"unknown id {args.play!r}", file=sys.stderr)
            return 2
        sync_pack(args.host, args.port, args.pack, only=[args.play])
        play_file(args.host, args.port, meta["file"])
        print(f"played {meta['file']}")
        return 0

    result = sync_pack(args.host, args.port, args.pack)
    print(json.dumps(result, indent=2))
    return 0 if not result["missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
