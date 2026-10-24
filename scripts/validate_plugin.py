from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

from manifest_utils import (
    PLUGIN_ID_PATTERN,
    RISK_TAGS,
    const_path_for_app,
    dump_json,
    iter_declared_apps,
    package_manifest_path,
    plugin_id,
    plugin_package_dir,
    project_root,
    read_manifest,
    render_const,
)


class Reporter:
    def __init__(self) -> None:
        self.failed: bool = False

    def pass_(self, label: str, path: Path | str) -> None:
        print(f"PASS {label} {path}")

    def fail(self, code: str, path: Path | str) -> None:
        self.failed = True
        print(f"FAIL {code} {path}")


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(project_root()))
    except ValueError:
        return str(path)


def validate_manifest(reporter: Reporter, strict: bool) -> dict:
    root_manifest = project_root() / "plugin.json"
    if not root_manifest.exists():
        reporter.fail("MANIFEST_MISSING", "plugin.json")
        return {}

    manifest = read_manifest(root_manifest)
    reporter.pass_("manifest root", "plugin.json")

    current_id = plugin_id(manifest)
    if not PLUGIN_ID_PATTERN.fullmatch(current_id):
        reporter.fail("INVALID_ID", "plugin.json")
    else:
        reporter.pass_("manifest id", current_id)

    package_dir = plugin_package_dir(manifest)
    if package_dir.name != current_id or not package_dir.exists():
        reporter.fail("PACKAGE_MISMATCH", package_dir.name)
    else:
        reporter.pass_("package", relative(package_dir))

    package_manifest = package_manifest_path(manifest)
    if not package_manifest.exists():
        reporter.fail("MANIFEST_MISSING", relative(package_manifest))
    else:
        package_content = package_manifest.read_text(encoding="utf-8")
        expected_content = dump_json(manifest)
        if package_content != expected_content:
            reporter.fail("MANIFEST_MISMATCH", relative(package_manifest))
        else:
            reporter.pass_("manifest package", relative(package_manifest))

    version_code = manifest.get("versionCode")
    if not isinstance(version_code, int) or version_code <= 0:
        reporter.fail("INVALID_VERSION_CODE", "plugin.json")
    else:
        reporter.pass_("versionCode", str(version_code))

    risks = manifest.get("risk")
    if not isinstance(risks, list) or any(risk not in RISK_TAGS for risk in risks):
        reporter.fail("INVALID_RISK", "plugin.json")
    else:
        reporter.pass_("risk", ",".join(str(risk) for risk in risks))

    homepage = str(manifest.get("homepage", ""))
    parsed_homepage = urlparse(homepage)
    if strict and parsed_homepage.scheme not in {"http", "https"}:
        reporter.fail("INVALID_HOMEPAGE", "plugin.json")
    elif homepage:
        reporter.pass_("homepage", homepage)

    app_ids: set[str] = set()
    for app in iter_declared_apps(manifest):
        app_id = str(app.get("id", ""))
        if app_id in app_ids or not app_id.startswith(f"{current_id}_"):
            reporter.fail("APP_ID_MISMATCH", app_id)
        else:
            reporter.pass_("app id", app_id)
            app_ids.add(app_id)

    return manifest


def validate_files(reporter: Reporter, manifest: dict, strict: bool) -> None:
    package_dir = plugin_package_dir(manifest)
    current_id = plugin_id(manifest)

    required_files = [
        (package_dir / "__init__.py", "INIT_MISSING"),
        (package_dir / f"{current_id}_factory.py", "FACTORY_MISSING"),
        (package_dir / f"{current_id}.py", "APP_MISSING"),
    ]
    for path, code in required_files:
        if path.exists():
            reporter.pass_("file", relative(path))
        else:
            reporter.fail(code, relative(path))

    const_path = const_path_for_app(manifest)
    if not const_path.exists():
        reporter.fail("CONST_MISSING", relative(const_path))
    else:
        expected = render_const(manifest)
        current = const_path.read_text(encoding="utf-8")
        if current != expected:
            reporter.fail("CONST_OUTDATED", relative(const_path))
        else:
            reporter.pass_("const", relative(const_path))

    for app in iter_declared_apps(manifest):
        extra_const = const_path_for_app(manifest, app)
        if not extra_const.exists():
            reporter.fail("CONST_MISSING", relative(extra_const))
        elif extra_const.read_text(encoding="utf-8") != render_const(manifest, app):
            reporter.fail("CONST_OUTDATED", relative(extra_const))
        else:
            reporter.pass_("const", relative(extra_const))

    release_files = [
        (project_root() / "README.md", "README_MISSING"),
        (project_root() / "LICENSE", "LICENSE_MISSING"),
    ]
    for path, code in release_files:
        if path.exists():
            reporter.pass_("file", relative(path))
        elif strict:
            reporter.fail(code, relative(path))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate plugin template package.")
    parser.add_argument("--strict", action="store_true", help="Enable release checks.")
    args = parser.parse_args()

    reporter = Reporter()
    manifest = validate_manifest(reporter, args.strict)
    if manifest:
        validate_files(reporter, manifest, args.strict)

    return 1 if reporter.failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL SCRIPT_ERROR {exc}", file=sys.stderr)
        raise SystemExit(2)
