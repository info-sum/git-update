"""git CLI 래퍼. GUI가 멈추지 않도록 프롬프트를 막고 타임아웃을 둔다."""

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

STATUS_TIMEOUT = 45      # 로컬 조회
NETWORK_TIMEOUT = 240    # fetch / pull

# 목록 정렬 우선순위 (손이 필요한 것부터)
STATE_RANK = {
    "behind": 0,
    "diverged": 1,
    "error": 2,
    "dirty": 3,
    "ahead": 4,
    "detached": 5,
    "no-upstream": 6,
    "clean": 7,
    "loading": 8,
}

# git stash 참조 형식만 허용 (git 인자로 그대로 넘어가므로 반드시 검증)
STASH_REF_RE = re.compile(r"^stash@\{\d{1,4}\}$")

PULL_MODES = ("ff-only", "rebase", "merge")

_ENV_CACHE = None


def git_env() -> dict:
    """인증 프롬프트나 에디터 때문에 프로세스가 멈추지 않도록 환경을 고정한다."""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"          # 터미널 로그인 프롬프트 금지
        env["GIT_ASKPASS"] = ""                   # GUI 비밀번호 창 금지
        env["GIT_EDITOR"] = "true"                # 머지 커밋 메시지 에디터 금지
        env["LC_ALL"] = "C"                       # 출력 파싱 안정화
        env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
        for stale in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(stale, None)
        _ENV_CACHE = env
    return dict(_ENV_CACHE)


def run_git(repo: str, args: Sequence[str], timeout: int = STATUS_TIMEOUT) -> Tuple[int, str]:
    """(returncode, 합쳐진 출력) 을 돌려준다. 예외를 밖으로 던지지 않는다."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            universal_newlines=True,
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, "시간 초과 (%ds)" % timeout
    except FileNotFoundError:
        return 127, "git 명령을 찾을 수 없습니다."
    except OSError as exc:
        return 1, str(exc)


@dataclass
class RepoStatus:
    path: str
    name: str
    branch: str = ""
    detached: bool = False
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    changed: int = 0
    untracked: int = 0
    last_commit: str = ""
    last_fetch: Optional[float] = None
    remote: str = ""
    error: str = ""
    loading: bool = False
    stash_count: int = 0

    @property
    def dirty(self) -> bool:
        return self.changed > 0 or self.untracked > 0

    @property
    def has_update(self) -> bool:
        return self.behind > 0

    @property
    def diverged(self) -> bool:
        """받을 커밋과 보낼 커밋이 동시에 있는 상태. ff-only 로는 합칠 수 없다."""
        return self.behind > 0 and self.ahead > 0

    @property
    def state(self) -> str:
        """UI 색상/정렬에 쓰는 한 단어 상태값."""
        if self.loading:
            return "loading"
        if self.error:
            return "error"
        if self.detached:
            return "detached"
        if not self.upstream:
            return "no-upstream"
        if self.diverged:
            return "diverged"
        if self.behind:
            return "behind"
        if self.dirty:
            return "dirty"
        if self.ahead:
            return "ahead"
        return "clean"

    @property
    def sort_key(self):
        return (STATE_RANK.get(self.state, 9), self.name.lower(), self.path)

    def summary(self) -> str:
        """행에 표시할 요약 문구."""
        if self.loading:
            return "상태 확인 중…"
        if self.error:
            return self.error
        bits = []
        if self.detached:
            bits.append("HEAD 분리됨")
        elif self.branch:
            bits.append(self.branch)
        if self.behind:
            bits.append("받을 커밋 %d" % self.behind)
        if self.ahead:
            bits.append("보낼 커밋 %d" % self.ahead)
        if not self.upstream and not self.detached:
            bits.append("업스트림 없음")
        if self.changed:
            bits.append("수정 %d" % self.changed)
        if self.untracked:
            bits.append("추적 안 됨 %d" % self.untracked)
        if self.stash_count:
            bits.append("보관 %d" % self.stash_count)
        if len(bits) <= 1 and self.upstream and not self.dirty:
            bits.append("최신 상태")
        if self.last_fetch:
            bits.append("확인 " + humanize_age(self.last_fetch))
        return "  ·  ".join(bits)


def humanize_age(ts: float) -> str:
    delta = max(0, int(time.time() - ts))
    if delta < 90:
        return "방금"
    if delta < 3600:
        return "%d분 전" % (delta // 60)
    if delta < 86400:
        return "%d시간 전" % (delta // 3600)
    if delta < 86400 * 30:
        return "%d일 전" % (delta // 86400)
    return "%d개월 전" % (delta // (86400 * 30))


def _last_fetch_time(repo: str) -> Optional[float]:
    """FETCH_HEAD / refs 수정 시각으로 마지막 fetch 시점을 추정한다."""
    git_dir = Path(repo) / ".git"
    if git_dir.is_file():  # worktree: gitdir: <path>
        try:
            text = git_dir.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                git_dir = Path(text.split(":", 1)[1].strip())
        except OSError:
            return None
    newest = None
    for name in ("FETCH_HEAD", "refs/remotes"):
        target = git_dir / name
        try:
            mtime = target.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def read_status(path: str, name: Optional[str] = None) -> RepoStatus:
    """네트워크를 쓰지 않고 로컬 정보만으로 저장소 상태를 읽는다."""
    st = RepoStatus(path=path, name=name or Path(path).name)

    code, out = run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if code != 0:
        low = out.lower()
        if "unknown revision" in low or "ambiguous argument" in low:
            st.error = "커밋이 없는 저장소"
        else:
            st.error = out.splitlines()[0] if out else "git 상태를 읽을 수 없음"
        return st
    st.branch = out
    st.detached = out == "HEAD"
    if st.detached:
        _, short = run_git(path, ["rev-parse", "--short", "HEAD"])
        st.branch = short or "HEAD"

    code, out = run_git(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if code == 0 and out:
        st.upstream = out
        code, out = run_git(path, ["rev-list", "--left-right", "--count", "@{upstream}...HEAD"])
        if code == 0 and out:
            parts = out.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                st.behind, st.ahead = int(parts[0]), int(parts[1])

    code, out = run_git(path, ["status", "--porcelain"])
    if code == 0 and out:
        for line in out.splitlines():
            if line.startswith("??"):
                st.untracked += 1
            elif line.strip():
                st.changed += 1

    code, out = run_git(path, ["log", "-1", "--format=%h %cr %s"])
    if code == 0 and out:
        st.last_commit = out[:120]

    code, out = run_git(path, ["stash", "list", "--format=%gd"])
    if code == 0 and out:
        st.stash_count = len([l for l in out.splitlines() if l.strip()])

    code, out = run_git(path, ["remote", "get-url", "origin"])
    if code == 0 and out:
        st.remote = out.splitlines()[0]

    st.last_fetch = _last_fetch_time(path)
    return st


def fetch(path: str) -> Tuple[bool, str]:
    """원격 정보만 갱신 (작업 트리는 건드리지 않음)."""
    code, out = run_git(path, ["fetch", "--all", "--prune", "--quiet"], timeout=NETWORK_TIMEOUT)
    return code == 0, out


def pull(path: str, strategy: str = "ff-only", autostash: bool = True) -> Tuple[bool, str]:
    """실제 업데이트. fetch 까지 포함되므로 버튼 한 번으로 최신 상태가 된다.

    strategy:
      ff-only  빨리 감기만 허용 (로컬 커밋이 있으면 중단 → 안전)
      rebase   원격 커밋 위로 내 커밋을 다시 쌓음 (이력 깔끔)
      merge    병합 커밋을 만들어 합침
    """
    if strategy not in PULL_MODES:
        strategy = "ff-only"
    args: List[str] = ["pull"]
    if strategy == "rebase":
        args.append("--rebase")
    elif strategy == "merge":
        args += ["--no-rebase", "--no-edit"]
    else:
        args.append("--ff-only")
    if autostash:
        args.append("--autostash")
    args.append("--prune")
    code, out = run_git(path, args, timeout=NETWORK_TIMEOUT)
    return code == 0, out or ("완료" if code == 0 else "실패")


# ---------------------------------------------------------------- stash
def stash_list(path: str) -> List[Dict[str, object]]:
    """보관(stash) 목록. 최신 항목이 앞에 온다."""
    fmt = "--format=%gd%x1f%gs%x1f%ct%x1f%H"
    code, out = run_git(path, ["stash", "list", fmt])
    if code != 0 or not out:
        return []
    entries = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        try:
            ts = float(parts[2])
        except ValueError:
            ts = 0.0
        subject = parts[1]
        # "On main: 메시지" / "WIP on main: 1a2b3c 커밋제목" 형태를 나눠 담는다
        branch, _, rest = subject.partition(":")
        entries.append(
            {
                "ref": parts[0],
                "subject": subject,
                "branch": branch.replace("WIP on ", "").replace("On ", "").strip(),
                "message": rest.strip() or subject,
                "age": humanize_age(ts) if ts else "",
                "sha": parts[3][:9],
            }
        )
    return entries


def stash_push(path: str, message: str = "", include_untracked: bool = True) -> Tuple[bool, str]:
    """현재 작업 내용을 보관한다. 되돌리려면 stash_restore 를 쓴다."""
    args = ["stash", "push"]
    if include_untracked:
        args.append("--include-untracked")
    msg = " ".join((message or "").split())[:200]
    if msg:
        args += ["-m", msg]
    code, out = run_git(path, args, timeout=NETWORK_TIMEOUT)
    return code == 0, out


def stash_restore(path: str, ref: str, pop: bool = True) -> Tuple[bool, str]:
    """보관 항목을 작업 트리로 되돌린다. pop=True 면 목록에서 제거한다."""
    if not STASH_REF_RE.match(ref or ""):
        return False, "잘못된 보관 항목 참조: %r" % (ref,)
    code, out = run_git(path, ["stash", "pop" if pop else "apply", ref], timeout=NETWORK_TIMEOUT)
    return code == 0, out


def stash_drop(path: str, ref: str) -> Tuple[bool, str]:
    """보관 항목을 버린다 (되돌리기 어려우므로 호출 전에 확인을 받아야 한다)."""
    if not STASH_REF_RE.match(ref or ""):
        return False, "잘못된 보관 항목 참조: %r" % (ref,)
    code, out = run_git(path, ["stash", "drop", ref])
    return code == 0, out


def git_version() -> str:
    code, out = run_git(str(Path.home()), ["--version"])
    return out if code == 0 else "git 없음"


# git 실패 출력 → 사람이 바로 조치할 수 있는 한국어 안내
ERROR_HINTS = [
    ("Not possible to fast-forward", "로컬 커밋이 있어 빨리감기가 안 됩니다. 방식을 rebase 나 merge 로 바꿔보세요."),
    ("Need to specify how to reconcile", "합치는 방식이 지정되지 않았습니다. 방식을 rebase 나 merge 로 선택해 보세요."),
    ("local changes to the following files would be overwritten",
     "로컬 변경이 덮어써질 수 있어 중단했습니다. 커밋하거나 보관(stash) 후 다시 시도하세요."),
    ("cannot pull with rebase", "커밋되지 않은 변경 때문에 rebase 할 수 없습니다. 커밋하거나 보관 후 시도하세요."),
    ("unstaged changes", "커밋되지 않은 변경이 있습니다. 커밋하거나 보관 후 시도하세요."),
    ("There is no tracking information", "이 브랜치에 업스트림이 없습니다. git push -u 로 한 번 연결해 주세요."),
    ("no such ref", "원격에 해당 브랜치가 없습니다."),
    ("Permission denied (publickey)", "SSH 키 인증에 실패했습니다. ssh-add 로 키를 등록해 주세요."),
    ("could not read Username", "인증 정보가 필요합니다. 터미널에서 한 번 인증하거나 SSH 로 바꿔주세요."),
    ("Authentication failed", "인증에 실패했습니다. 토큰이나 자격 증명을 확인해 주세요."),
    ("Could not resolve host", "네트워크에 연결할 수 없습니다."),
    ("unable to access", "원격 저장소에 접근할 수 없습니다."),
    ("CONFLICT", "충돌이 발생했습니다. 터미널에서 직접 해결해야 합니다."),
    ("시간 초과", "시간이 너무 오래 걸려 중단했습니다."),
]


def _hint_for(text: str) -> str:
    for needle, hint in ERROR_HINTS:
        if needle in text:
            return hint
    return ""


def summarize_pull(ok: bool, out: str) -> Tuple[bool, str, List[str]]:
    """git pull 출력에서 (문제없음, 한 줄 요약, 전체 줄) 을 뽑아낸다.

    autostash 복원이 충돌한 경우 git 은 0 을 돌려주지만 사용자가 확인해야 하므로
    경고로 구분한다.
    """
    lines = [l.rstrip() for l in (out or "").splitlines() if l.strip()]
    joined = "\n".join(lines)
    stashed = "Applying autostash resulted in conflicts" in joined

    if not ok:
        raw = ""
        for key in ("fatal:", "error:", "CONFLICT", "hint:"):
            hit = next((l for l in lines if l.lstrip().startswith(key) or key in l), None)
            if hit:
                raw = hit.strip()
                break
        if not raw:
            raw = lines[-1].strip() if lines else "업데이트 실패"
        hint = _hint_for(joined)
        return False, ((hint + " · " if hint else "") + raw)[:220], lines

    if "Already up to date" in joined or "Already up-to-date" in joined:
        head = "이미 최신 상태"
    else:
        stat = next((l for l in lines if "changed" in l and "file" in l), "")
        moved = next((l for l in lines if l.startswith("Updating ") or l.startswith("Fast-forward")), "")
        head = "업데이트 완료"
        extra = (stat or moved).strip()
        if extra:
            head += " · " + extra[:120]
    if stashed:
        head += " · 보관한 로컬 변경 복원 충돌 (git stash 확인 필요)"
    return (not stashed), head, lines


def suggest_retry(out: str) -> List[str]:
    """실패 출력에 따라 화면에 띄울 다음 수단을 알려준다."""
    joined = out or ""
    if "Not possible to fast-forward" in joined or "Need to specify how to reconcile" in joined:
        return ["rebase", "merge"]
    if ("local changes to the following files would be overwritten" in joined
            or "cannot pull with rebase" in joined
            or "unstaged changes" in joined
            or "Your local changes" in joined):
        return ["stash"]
    return []


STASH_OK_TEXT = {
    "push": "로컬 변경을 보관했습니다",
    "pop": "보관 항목을 작업 트리로 꺼냈습니다",
    "apply": "보관 항목을 적용했습니다 (목록에 그대로 남아 있음)",
    "drop": "보관 항목을 삭제했습니다",
}


def summarize_stash(kind: str, ok: bool, out: str) -> Tuple[bool, str, List[str]]:
    """git stash 출력에서 (성공, 한 줄 요약, 전체 줄) 을 뽑아낸다."""
    lines = [l.rstrip() for l in (out or "").splitlines() if l.strip()]
    joined = "\n".join(lines)

    if kind == "push" and "No local changes to save" in joined:
        return True, "보관할 로컬 변경이 없습니다", lines

    if ok:
        head = STASH_OK_TEXT.get(kind, "완료")
        if kind == "push":
            saved = next((l for l in lines if l.startswith("Saved working directory")), "")
            if saved:
                head += " · " + saved[:120]
        return True, head, lines

    if kind in ("pop", "apply") and "CONFLICT" in joined:
        hit = next((l for l in lines if "CONFLICT" in l), "").strip()
        return False, "충돌이 발생했습니다. 보관 항목은 남아 있으니 터미널에서 해결해 주세요. · " + hit[:150], lines

    hint = _hint_for(joined)
    raw = next((l for l in lines if l.lstrip().startswith(("fatal:", "error:"))), "")
    if not raw:
        raw = lines[-1] if lines else "실패"
    return False, ((hint + " · " if hint else "") + raw.strip())[:220], lines
