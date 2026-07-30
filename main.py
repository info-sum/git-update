#!/usr/bin/env python3
"""Git Update 실행 진입점.

로컬 git 저장소를 모아 보여주고, 저장소별 버튼으로 git pull 을 실행한다.
화면은 기본 브라우저로 열리고, 서버는 127.0.0.1 에만 바인딩된다.

사용법:
    python3 main.py                 앱 실행 (브라우저 자동 열림)
    python3 main.py --no-browser    서버만 실행하고 주소만 출력
    python3 main.py --port 8765     포트 지정 (기본: 빈 포트 자동 선택)
    python3 main.py --list          터미널에서 저장소 목록/상태만 출력
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gitupdate.config import Config, display_path  # noqa: E402
from gitupdate.git_ops import git_version, read_status  # noqa: E402
from gitupdate.scanner import find_repos  # noqa: E402


def cli_list() -> int:
    cfg = Config.load()
    print("git:", git_version())
    print("검색 폴더:", ", ".join(display_path(r) for r in cfg.roots) or "(없음)")
    repos = find_repos(cfg.roots, cfg.excludes, cfg.max_depth)
    print("저장소 %d개\n" % len(repos))
    for path, name in repos:
        st = read_status(path, name)
        print("%-34s %-12s %s" % (name[:34], st.state, st.summary()))
    return 0


def arg_value(argv, flag, default=None):
    if flag in argv:
        idx = argv.index(flag)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return default


def main(argv) -> int:
    args = argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return 0
    if "--list" in args or "-l" in args:
        return cli_list()

    try:
        port = int(arg_value(args, "--port", "0"))
    except ValueError:
        print("--port 값이 숫자가 아닙니다.")
        return 2

    from gitupdate.server import serve

    cfg = Config.load()
    serve(cfg, port=port, open_browser="--no-browser" not in args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
