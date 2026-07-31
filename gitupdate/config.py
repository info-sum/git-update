"""설정 파일 로드/저장. ~/.config/gitupdate/config.json 에 저장된다."""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List

# 설정 위치 (테스트나 여러 프로필용으로 GITUPDATE_CONFIG_DIR 로 바꿀 수 있다)
CONFIG_DIR = Path(os.environ.get("GITUPDATE_CONFIG_DIR") or (Path.home() / ".config" / "gitupdate"))
CONFIG_PATH = CONFIG_DIR / "config.json"

# 처음 실행할 때 실제로 존재하는 것만 스캔 대상으로 넣는다.
CANDIDATE_ROOTS = [
    "~/Documents/project",
    "~/Documents/projects",
    "~/Documents/GitHub",
    "~/Projects",
    "~/project",
    "~/dev",
    "~/develop",
    "~/src",
    "~/workspace",
    "~/git",
    "~/repos",
    "~/Developer",
    "~/Desktop",
]

# 스캔 시 건너뛸 디렉터리 이름 (하위에 git 저장소가 있을 가능성이 낮거나 너무 큼)
DEFAULT_EXCLUDES = [
    "node_modules",
    "venv",
    ".venv",
    "env",
    "vendor",
    "Library",
    "Applications",
    "build",
    "dist",
    "out",
    "target",
    "Pods",
    "__pycache__",
    "site-packages",
    "bower_components",
]

PULL_STRATEGIES = ["ff-only", "rebase", "merge"]

# 화면 밝기. 기본은 밝게 — OS 가 다크 모드여도 밝은 화면으로 시작한다.
# auto 를 고르면 그때부터 시스템 설정을 따라간다.
THEMES = ["light", "dark", "auto"]


@dataclass
class Config:
    roots: List[str] = field(default_factory=list)
    excludes: List[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    max_depth: int = 4
    pull_strategy: str = "ff-only"
    autostash: bool = True
    fetch_on_start: bool = True
    theme: str = "light"
    show_log: bool = True
    workers: int = 8

    # --- 직렬화 -------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            known = {f for f in cfg.__dataclass_fields__}
            for key, value in raw.items():
                if key in known:
                    setattr(cfg, key, value)
        if not cfg.roots:
            cfg.roots = default_roots()
        cfg.normalize()
        return cfg

    def save(self) -> None:
        self.normalize()
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, CONFIG_PATH)

    def normalize(self) -> None:
        seen = set()
        roots = []
        for r in self.roots:
            p = str(Path(str(r)).expanduser())
            if p not in seen:
                seen.add(p)
                roots.append(p)
        self.roots = roots
        self.excludes = [e.strip() for e in self.excludes if str(e).strip()]
        self.max_depth = max(1, min(int(self.max_depth), 8))
        if self.pull_strategy not in PULL_STRATEGIES:
            self.pull_strategy = "ff-only"
        if self.theme not in THEMES:
            self.theme = "light"
        self.workers = max(1, min(int(self.workers), 16))


def default_roots() -> List[str]:
    """존재하는 후보 디렉터리만 기본 스캔 대상으로 돌려준다."""
    found = []
    for cand in CANDIDATE_ROOTS:
        p = Path(cand).expanduser()
        if p.is_dir():
            found.append(str(p))
    return found


def display_path(path: str) -> str:
    """홈 디렉터리는 ~ 로 줄여서 보여준다."""
    home = str(Path.home())
    p = str(path)
    return "~" + p[len(home):] if p.startswith(home) else p
