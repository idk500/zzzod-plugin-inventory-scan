from __future__ import annotations

import argparse
import sys
from pathlib import Path

from manifest_utils import (
    const_path_for_app,
    iter_declared_apps,
    read_manifest,
    render_const,
)


def compare_or_write(path: Path, content: str, check: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    display_path = path.relative_to(path.parents[1])
    if current == content:
        print(f"PASS const {display_path}")
        return True

    if check:
        print(f"FAIL CONST_OUTDATED {display_path}")
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"PASS const generated {display_path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate *_const.py from plugin.json.")
    parser.add_argument("--manifest", default="plugin.json", help="Manifest path.")
    parser.add_argument("--check", action="store_true", help="Compare only.")
    args = parser.parse_args()

    manifest = read_manifest(Path(args.manifest))
    results: list[bool] = []
    results.append(
        compare_or_write(
            const_path_for_app(manifest),
            render_const(manifest),
            args.check,
        )
    )

    for app in iter_declared_apps(manifest):
        results.append(
            compare_or_write(
                const_path_for_app(manifest, app),
                render_const(manifest, app),
                args.check,
            )
        )

    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL SCRIPT_ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
