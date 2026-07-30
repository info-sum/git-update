"""지정한 폴더들을 훑어 git 저장소를 찾아낸다."""

import os
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


def is_repo(path: str) -> bool:
    """.git 이 디렉터리(일반)거나 파일(worktree/submodule)이면 저장소로 본다."""
    return os.path.exists(os.path.join(path, ".git"))


def find_repos(
    roots: Sequence[str],
    excludes: Iterable[str] = (),
    max_depth: int = 4,
) -> List[Tuple[str, str]]:
    """(경로, 표시이름) 목록을 이름순으로 돌려준다. 저장소 안쪽은 더 내려가지 않는다."""
    skip = {e.strip() for e in excludes if e and e.strip()}
    found = {}

    for raw_root in roots:
        root = os.path.abspath(os.path.expanduser(str(raw_root)))
        if not os.path.isdir(root):
            continue
        if is_repo(root):
            found.setdefault(os.path.realpath(root), root)
            continue

        stack = [(root, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = list(os.scandir(current))
            except (PermissionError, OSError):
                continue
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                name = entry.name
                if name.startswith(".") or name in skip:
                    continue
                if is_repo(entry.path):
                    found.setdefault(os.path.realpath(entry.path), entry.path)
                    continue  # 저장소 내부는 스캔하지 않음
                if depth + 1 < max_depth:
                    stack.append((entry.path, depth + 1))

    result = [(p, label(p, roots)) for p in found.values()]
    result.sort(key=lambda item: (item[1].lower(), item[0]))
    return result


def label(path: str, roots: Sequence[str]) -> str:
    """스캔 루트 기준 상대 경로를 이름으로 쓴다 (hermes/hermes-agent 처럼)."""
    best = None
    for raw_root in roots:
        root = os.path.abspath(os.path.expanduser(str(raw_root)))
        if path == root:
            return os.path.basename(path)
        prefix = root.rstrip(os.sep) + os.sep
        if path.startswith(prefix):
            rel = path[len(prefix):]
            if best is None or len(rel) < len(best):
                best = rel
    return best or os.path.basename(path) or path


def home_relative(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path
