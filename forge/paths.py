from __future__ import annotations

import shutil
from pathlib import Path

APP_NAME = "forge"
LEGACY_APP_NAME = "pico"
WORKSPACE_STATE_DIRNAME = ".forge"
LEGACY_WORKSPACE_STATE_DIRNAME = ".pico"
PROJECT_CONFIG_NAME = ".forge.toml"
LEGACY_PROJECT_CONFIG_NAME = ".pico.toml"

MIGRATABLE_CHILDREN = ("sessions", "runs", "memory", "plans", "skills")


def workspace_state_dir(root) -> Path:
    return Path(root).resolve() / WORKSPACE_STATE_DIRNAME


def legacy_workspace_state_dir(root) -> Path:
    return Path(root).resolve() / LEGACY_WORKSPACE_STATE_DIRNAME


def workspace_state_path(root, *parts) -> Path:
    return workspace_state_dir(root).joinpath(*parts)


def legacy_workspace_state_path(root, *parts) -> Path:
    return legacy_workspace_state_dir(root).joinpath(*parts)


def _home_path() -> Path | None:
    try:
        return Path.home()
    except RuntimeError:
        return None


def default_user_config_path() -> Path:
    home = _home_path()
    if home is None:
        return Path(PROJECT_CONFIG_NAME)
    return home / ".config" / APP_NAME / "config.toml"


def legacy_user_config_path() -> Path:
    home = _home_path()
    if home is None:
        return Path(LEGACY_PROJECT_CONFIG_NAME)
    return home / ".config" / LEGACY_APP_NAME / "config.toml"


def migrate_workspace_layout(root) -> dict:
    root = Path(root).resolve()
    new_dir = workspace_state_dir(root)
    old_dir = legacy_workspace_state_dir(root)
    new_config = root / PROJECT_CONFIG_NAME
    old_config = root / LEGACY_PROJECT_CONFIG_NAME

    moved = []
    if old_dir.is_dir():
        new_dir.mkdir(parents=True, exist_ok=True)
        for name in MIGRATABLE_CHILDREN:
            source = old_dir / name
            target = new_dir / name
            if source.exists() and not target.exists():
                shutil.move(str(source), str(target))
                moved.append(name)
        try:
            old_dir.rmdir()
            legacy_dir_removed = True
        except OSError:
            legacy_dir_removed = False
    else:
        legacy_dir_removed = False

    config_migrated = False
    if old_config.exists() and not new_config.exists():
        shutil.move(str(old_config), str(new_config))
        config_migrated = True

    return {
        "moved_children": moved,
        "legacy_dir_removed": legacy_dir_removed,
        "config_migrated": config_migrated,
    }


def preferred_existing_state_dir(root) -> Path:
    new_dir = workspace_state_dir(root)
    if new_dir.exists():
        return new_dir
    old_dir = legacy_workspace_state_dir(root)
    if old_dir.exists():
        return old_dir
    return new_dir


def preferred_existing_state_path(root, *parts) -> Path:
    state_root = preferred_existing_state_dir(root)
    return state_root.joinpath(*parts)
