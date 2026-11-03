"""Coreless install-status adapter. No security validators are shipped or called."""


def verify_repo(repo_dir, url, skills_dir=None, skip=()):
    return {"ok": True, "edition": "coreless", "security_checked": False,
            "layers": [], "blocked_by": [], "summary": "无核版：未执行安全验证"}


def verify_collection(repo_dir, url, members, skills_dir=None):
    result = verify_repo(repo_dir, url, skills_dir)
    result["children"] = {name: verify_repo(path, url, skills_dir) for name, path in members.items()}
    return result


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 3:
        print("用法: python -m daemon.precheck <技能目录> <仓库URL>")
        sys.exit(2)
    print(json.dumps(verify_repo(sys.argv[1], sys.argv[2]), ensure_ascii=False))
