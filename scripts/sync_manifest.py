from __future__ import annotations

import argparse
import sys

from manifest_utils import dump_json, package_manifest_path, read_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync root plugin.json into package.")
    parser.add_argument("--check", action="store_true", help="Compare only.")
    args = parser.parse_args()

    manifest = read_manifest()
    target = package_manifest_path(manifest)
    expected = dump_json(manifest)

    if target.exists():
        current = target.read_text(encoding="utf-8")
    else:
        current = ""

    if current == expected:
        print(f"PASS manifest package {target.relative_to(target.parents[1])}")
        return 0

    if args.check:
        print(f"FAIL MANIFEST_MISMATCH {target.relative_to(target.parents[1])}")
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8")
    print(f"PASS manifest synced {target.relative_to(target.parents[1])}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL SCRIPT_ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
