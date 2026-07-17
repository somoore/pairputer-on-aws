#!/usr/bin/env python3
"""Download or print offline VLM benchmark candidate GGUF files."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "vlm_candidates.json"
DEFAULT_DEST = HERE / "models"


def load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def selected_candidates(manifest: dict[str, Any], names: list[str], *, all_candidates: bool) -> dict[str, Any]:
    candidates = manifest.get("candidates") or {}
    if all_candidates:
        return candidates
    if not names:
        return {name: candidates[name] for name in sorted(candidates, key=lambda key: int(candidates[key].get("priority", 999)))[:1]}
    missing = [name for name in names if name not in candidates]
    if missing:
        raise SystemExit(f"Unknown candidate(s): {', '.join(missing)}. Available: {', '.join(sorted(candidates))}")
    return {name: candidates[name] for name in names}


def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)


def run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    chosen = selected_candidates(manifest, args.model, all_candidates=args.all)
    print(f"destination: {args.dest}")
    for name, candidate in chosen.items():
        print(f"\n{name}: {candidate.get('label')}")
        print(f"  repo: {candidate.get('repo')}")
        print(f"  llama-server: {candidate.get('llama_server')}")
        for item in candidate.get("files") or []:
            filename = str(item["filename"])
            dest = args.dest / name / filename
            url = str(item["url"])
            print(f"  {item.get('role', 'file')}: {filename}")
            print(f"    {url}")
            if args.dry_run:
                continue
            if dest.exists() and not args.force:
                print(f"    exists: {dest}")
                continue
            print(f"    downloading -> {dest}")
            download_file(url, dest)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", nargs="*", help="Candidate key from vlm_candidates.json. Defaults to highest priority.")
    parser.add_argument("--all", action="store_true", help="Download/print every candidate.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--dry-run", action="store_true", help="Print files and commands without downloading.")
    parser.add_argument("--force", action="store_true", help="Redownload existing files.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(list(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    raise SystemExit(main())
