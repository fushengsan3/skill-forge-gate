"""Expose Skill Forge installations to Codex through local directory links."""
import argparse
import json
import os
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

from daemon import safe_paths

_lock = threading.Lock()


def _is_link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _exists(path):
    return os.path.lexists(path) or _is_link(path)


def _remove_link(path):
    if path.is_symlink():
        path.unlink()
    elif _is_link(path):
        os.rmdir(path)  # Remove the junction itself, never its target.


def _create_link(link, source):
    if os.name != "nt":
        link.symlink_to(source, target_is_directory=True)
        return
    env = dict(os.environ, SF_SYNC_LINK=str(link), SF_SYNC_SOURCE=str(source))
    subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
        "-Path $env:SF_SYNC_LINK -Target $env:SF_SYNC_SOURCE | Out-Null",
    ], env=env, check=True, capture_output=True, timeout=30,
       creationflags=subprocess.CREATE_NO_WINDOW)


def sync_skills(skills_root, forge_root, names=None, *, adopt=False):
    """Sync registered names. Existing independent copies require explicit adoption.

    Adoption moves each existing copy outside the discovery tree before linking.
    Subsequent source replacements are immediately visible through the same link.
    """
    result = dict(ok=True, enabled=False, linked=[], unchanged=[], removed=[],
                  skipped=[], independent=[], backups={}, errors=[])
    with _lock:
        try:
            config_file = Path(forge_root) / "templates" / "codex-sync.json"
            if not config_file.exists():
                return result
            config = json.loads(config_file.read_text(encoding="utf-8"))
            if not config.get("enabled"):
                return result
            result["enabled"] = True
            source_root = Path(skills_root).resolve()
            target_root = Path(config["target_dir"]).expanduser().resolve()
            if (source_root.is_relative_to(target_root)
                    or target_root.is_relative_to(source_root)):
                raise ValueError("Claude 与 Codex 技能目录不能相同或互相包含")
            sources = json.loads((Path(forge_root) / "sources.json").read_text(encoding="utf-8"))
            independent_file = Path(forge_root) / "templates" / "codex-sources.json"
            independent = json.loads(independent_file.read_text(encoding="utf-8")) if independent_file.exists() else {}
            target_root.mkdir(parents=True, exist_ok=True)
            result["target_dir"] = str(target_root)
            backup_root = target_root.parent / ".skill-forge-backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            for name in dict.fromkeys(names if names is not None else sources):
                try:
                    safe_paths.check_name(name)
                    source = safe_paths.resolve_within(source_root, name)
                    target = target_root / name  # Do not resolve through an existing link.
                    registered = sources.get(name)
                    has_entry = (source / "SKILL.md").is_file()
                    owned = _is_link(target) and target.resolve() == source.resolve()
                    if not registered:
                        if owned:
                            _remove_link(target)
                            result["removed"].append(name)
                        else:
                            result["skipped"].append(name)
                        continue
                    if name in independent:
                        if _exists(target) and not owned:
                            result["independent"].append(name)
                        else:
                            raise ValueError("Codex 独立版本的登记与目录不一致，请检查：" + name)
                        continue
                    if not has_entry:
                        # Collection roots are dependencies, not duplicate skill entries.
                        if registered.get("type") == "collection":
                            if owned:
                                _remove_link(target)
                                result["removed"].append(name)
                            elif adopt and _exists(target):
                                backup_root.mkdir(parents=True, exist_ok=True)
                                backup = backup_root / name
                                target.rename(backup)
                                result["backups"][name] = str(backup)
                        result["skipped"].append(name)
                        continue
                    if owned:
                        result["unchanged"].append(name)
                        continue
                    if _exists(target) and not adopt:
                        raise ValueError("Codex 已有独立副本，未覆盖；请先备份并接管：" + name)
                    stage = target_root / (".sf-link-" + uuid.uuid4().hex)
                    backup = None
                    try:
                        _create_link(stage, source)
                        if _exists(target):
                            backup_root.mkdir(parents=True, exist_ok=True)
                            backup = backup_root / name
                            target.rename(backup)
                        try:
                            stage.rename(target)
                        except Exception:
                            if backup:
                                backup.rename(target)
                            raise
                        if backup:
                            result["backups"][name] = str(backup)
                        result["linked"].append(name)
                    finally:
                        if _is_link(stage):
                            _remove_link(stage)
                except Exception as exc:
                    result["errors"].append(f"{name}: {exc}")
        except Exception as exc:
            result["errors"].append(str(exc))
    result["ok"] = not result["errors"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-root", type=Path, default=Path.home() / ".claude" / "skills")
    parser.add_argument("--forge-root", type=Path)
    parser.add_argument("--enable", action="store_true")
    parser.add_argument("--target-dir", type=Path, default=Path.home() / ".agents" / "skills")
    parser.add_argument("--adopt-existing", action="store_true", help="Back up independent Codex copies before linking")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    forge = args.forge_root or args.skills_root / "skill-forge"
    if args.enable:
        config = forge / "templates" / "codex-sync.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"enabled": True, "target_dir": str(args.target_dir.resolve())},
                                     ensure_ascii=False, indent=2), encoding="utf-8")
    result = sync_skills(args.skills_root, forge, args.names or None, adopt=args.adopt_existing)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
