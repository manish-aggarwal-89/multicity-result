#!/usr/bin/env python3
"""Decompress the gzip+base64 search dumps into plain JSON.

Search responses are stored as `*.json.gz.b64` (base64 of gzip of JSON) to keep
the repo small. This writes the decoded JSON alongside each one, stripping the
`.gz.b64` suffix (so `01_search.response.json.gz.b64` -> `01_search.response.json`).

Usage:
    python decompress_search.py                 # all of runs/, skip existing
    python decompress_search.py runs/TC-283     # just one subtree (file or dir)
    python decompress_search.py --force         # overwrite existing .json
    python decompress_search.py --stdout FILE   # print one file's JSON to stdout
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SUFFIX = ".gz.b64"


def decode(path: Path) -> bytes:
    """base64 -> gzip -> raw JSON bytes for one .gz.b64 file."""
    return gzip.decompress(base64.b64decode(path.read_bytes()))


def out_path(path: Path) -> Path:
    return path.with_name(path.name[: -len(SUFFIX)])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", default="runs", help="file or directory to process (default: runs)")
    ap.add_argument("--force", action="store_true", help="overwrite existing decoded .json")
    ap.add_argument("--stdout", metavar="FILE", help="print the decoded JSON of one file and exit")
    args = ap.parse_args()

    if args.stdout:
        sys.stdout.write(decode(Path(args.stdout)).decode("utf-8"))
        return 0

    root = (REPO / args.path) if not Path(args.path).is_absolute() else Path(args.path)
    files = [root] if root.is_file() else sorted(root.rglob(f"*{SUFFIX}"))
    if not files:
        print(f"no *{SUFFIX} files under {root}")
        return 0

    done = skipped = failed = 0
    for f in files:
        dst = out_path(f)
        if dst.exists() and not args.force:
            skipped += 1
            continue
        try:
            raw = decode(f)
            json.loads(raw)  # validate it really is JSON before writing
            dst.write_bytes(raw)
            done += 1
        except Exception as exc:  # noqa: BLE001 — report and continue over the batch
            failed += 1
            print(f"FAILED {f}: {exc}", file=sys.stderr)

    print(f"decompressed={done} skipped(existing)={skipped} failed={failed} total={len(files)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
