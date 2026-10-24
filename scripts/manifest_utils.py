from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
RISK_TAGS: set[str] = {
    "filesystem",
    "network",
    "account",
    "automation",
    "ocr",
    "yolo",
    "clipboard",
    "process",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def dump_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def read_manifest(manifest_path: Path | None = None) -> dict[str, Any]:
    path = manifest_path or project_root() / "plugin.json"
    return load_json(path)


def plugin_id(manifest: dict[str, Any]) -> str:
    value = manifest.get("id")
    if not isinstance(value, str):
        return ""
    return value


def plugin_package_dir(manifest: dict[str, Any]) -> Path:
    return project_root() / plugin_id(manifest)


def package_manifest_path(manifest: dict[str, Any]) -> Path:
    return plugin_package_dir(manifest) / "plugin.json"


def const_path_for_app(manifest: dict[str, Any], app: dict[str, Any] | None = None) -> Path:
    package_dir = plugin_package_dir(manifest)
    if app is None:
        app_id = plugin_id(manifest)
        return package_dir / f"{app_id}_const.py"

    app_id = str(app.get("id", ""))
    feature = app_id.removeprefix(f"{plugin_id(manifest)}_")
    return package_dir / feature / f"{feature}_const.py"


def py_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def py_bool(value: Any) -> str:
    return "True" if bool(value) else "False"


def render_const(manifest: dict[str, Any], app: dict[str, Any] | None = None) -> str:
    author = manifest.get("author") if isinstance(manifest.get("author"), dict) else {}
    if app is None:
        app_id = plugin_id(manifest)
        name = manifest.get("name", "")
        summary = manifest.get("summary", "")
        default_group = manifest.get("defaultGroup", False)
        need_notify = manifest.get("needNotify", False)
    else:
        app_id = app.get("id", "")
        name = app.get("name", "")
        summary = app.get("summary", "")
        default_group = app.get("defaultGroup", False)
        need_notify = app.get("needNotify", False)

    return (
        "# Generated from plugin.json. Run scripts/generate_const.py after editing manifest.\n"
        f"APP_ID = {py_string(app_id)}\n"
        f"APP_NAME = {py_string(name)}\n"
        f"DEFAULT_GROUP = {py_bool(default_group)}\n"
        f"NEED_NOTIFY = {py_bool(need_notify)}\n"
        "\n"
        f"PLUGIN_AUTHOR = {py_string(author.get('name', ''))}\n"
        f"PLUGIN_HOMEPAGE = {py_string(manifest.get('homepage', ''))}\n"
        f"PLUGIN_VERSION = {py_string(manifest.get('version', ''))}\n"
        f"PLUGIN_DESCRIPTION = {py_string(summary)}\n"
    )


def iter_declared_apps(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    apps = manifest.get("apps", [])
    if not isinstance(apps, list):
        return []
    return [app for app in apps if isinstance(app, dict)]
