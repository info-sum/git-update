"""로컬 전용 HTTP 서버 + 상태 관리.

브라우저를 화면으로 쓰는 로컬 앱이다. 127.0.0.1 에만 바인딩하고,
실행할 때마다 새로 만드는 토큰을 모든 요청에서 확인한다.
(git pull 을 실행하는 엔드포인트이므로 다른 웹사이트가 접근하지 못하게 막아야 한다.)
"""

import json
import secrets
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, __version__
from .config import PULL_STRATEGIES, THEMES, Config, display_path
from .git_ops import (
    PULL_MODES,
    STASH_REF_RE,
    STATE_RANK,
    RepoStatus,
    fetch,
    git_version,
    humanize_age,
    pull,
    read_status,
    stash_drop,
    stash_list,
    stash_push,
    stash_restore,
    suggest_retry,
    summarize_pull,
    summarize_stash,
)
from .scanner import find_repos

WEB_DIR = Path(__file__).resolve().parent / "web"
IDLE_TIMEOUT = 900.0  # 브라우저에서 15분간 아무 요청이 없으면 서버 종료

# 화면이 쓰는 정적 파일. 경로를 조립하지 않고 이 표에 있는 것만 그대로 내보낸다.
STATIC_FILES = {
    "/tokens.css": "text/css; charset=utf-8",
    "/quiet-observer.css": "text/css; charset=utf-8",
    "/fonts/Cafe24PROUP.otf": "font/otf",
}


def repo_payload(st: RepoStatus, busy: Optional[str], result: Optional[dict]) -> dict:
    data = asdict(st)
    data.update(
        {
            "state": st.state,
            "rank": STATE_RANK.get(st.state, 9),
            "dirty": st.dirty,
            "diverged": st.diverged,
            "has_update": st.has_update,
            "summary": st.summary(),
            "display_path": display_path(st.path),
            "fetch_age": humanize_age(st.last_fetch) if st.last_fetch else "",
            "busy": busy,
            "result": result,
        }
    )
    return data


class AppState:
    """스캔/상태/pull 작업을 백그라운드에서 돌리고 결과를 모아둔다."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="git")
        self.repos: Dict[str, RepoStatus] = {}
        self.busy: Dict[str, str] = {}       # path -> "pull" | "fetch"
        self.results: Dict[str, dict] = {}   # path -> 마지막 작업 결과
        self.logs = deque(maxlen=400)
        self.generation = 0
        self.scanning = False
        self.pending = 0
        self.total = 0
        self.started = time.time()
        self.last_seen = time.time()
        self.git = git_version()

    # --- 로그 --------------------------------------------------------
    def log(self, text: str, level: str = "info"):
        with self.lock:
            self.logs.append({"t": time.strftime("%H:%M:%S"), "level": level, "text": text})

    # --- 스냅샷 ------------------------------------------------------
    def snapshot(self) -> dict:
        with self.lock:
            repos = [
                repo_payload(st, self.busy.get(path), self._fresh_result(path))
                for path, st in self.repos.items()
            ]
            counts = {
                "total": len(repos),
                "update": sum(1 for r in repos if r["has_update"]),
                "dirty": sum(1 for r in repos if r["dirty"]),
                "clean": sum(1 for r in repos if r["state"] == "clean"),
                "issue": sum(1 for r in repos if r["state"] in ("error", "no-upstream", "detached")),
            }
            return {
                "app": {"name": APP_NAME, "version": __version__, "git": self.git},
                "repos": repos,
                "counts": counts,
                "scanning": self.scanning,
                "pending": self.pending,
                "total": self.total,
                "working": bool(self.busy) or self.scanning or self.pending > 0,
                "logs": list(self.logs),
                "config": {
                    "roots": self.cfg.roots,
                    "roots_display": [display_path(r) for r in self.cfg.roots],
                    "excludes": self.cfg.excludes,
                    "max_depth": self.cfg.max_depth,
                    "pull_strategy": self.cfg.pull_strategy,
                    "strategies": PULL_STRATEGIES,
                    "autostash": self.cfg.autostash,
                    "fetch_on_start": self.cfg.fetch_on_start,
                    "theme": self.cfg.theme,
                    "themes": THEMES,
                },
            }

    def _fresh_result(self, path: str) -> Optional[dict]:
        res = self.results.get(path)
        if not res:
            return None
        age = time.time() - res["ts"]
        if age > 120:
            return None
        out = dict(res)
        out["age"] = int(age)
        return out

    # --- 스캔 --------------------------------------------------------
    def refresh(self, do_fetch: bool = False):
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
            self.generation += 1
            gen = self.generation
        self.log("검색 시작: %s" % (", ".join(display_path(r) for r in self.cfg.roots) or "(폴더 없음)"))
        self.pool.submit(self._scan_worker, gen, do_fetch)

    def _scan_worker(self, gen: int, do_fetch: bool):
        try:
            found = find_repos(self.cfg.roots, self.cfg.excludes, self.cfg.max_depth)
        except Exception as exc:  # 스캔 실패로 서버가 죽지 않게
            self.log("검색 실패: %r" % (exc,), "err")
            with self.lock:
                self.scanning = False
            return
        with self.lock:
            if gen != self.generation:
                return
            self.repos = {p: RepoStatus(path=p, name=n, loading=True) for p, n in found}
            self.results = {p: r for p, r in self.results.items() if p in self.repos}
            self.total = len(found)
            self.pending = len(found)
        self.log("저장소 %d개 발견" % len(found))
        if not found:
            with self.lock:
                self.scanning = False
            return
        for path, name in found:
            self.pool.submit(self._status_worker, gen, path, name, do_fetch)

    def _status_worker(self, gen: int, path: str, name: str, do_fetch: bool):
        try:
            if do_fetch:
                ok, out = fetch(path)
                if not ok:
                    first = (out or "").splitlines()
                    self.log("fetch 실패 · %s · %s" % (name, first[0][:160] if first else ""), "warn")
            st = read_status(path, name)
        except Exception as exc:
            st = RepoStatus(path=path, name=name, error="상태 확인 실패: %r" % (exc,))
        with self.lock:
            if gen != self.generation:
                return
            self.repos[path] = st
            self.pending = max(0, self.pending - 1)
            if self.pending == 0:
                self.scanning = False

    # --- 개별 작업 ----------------------------------------------------
    def start_pull(self, path: str, strategy: Optional[str] = None) -> bool:
        mode = strategy if strategy in PULL_MODES else self.cfg.pull_strategy
        with self.lock:
            st = self.repos.get(path)
            if st is None or path in self.busy:
                return False
            self.busy[path] = "pull:" + mode
            name = st.name
        self.pool.submit(self._pull_worker, path, name, mode)
        return True

    def _pull_worker(self, path: str, name: str, strategy: str):
        try:
            ok, out = pull(path, strategy, self.cfg.autostash)
        except Exception as exc:
            ok, out = False, "실행 실패: %r" % (exc,)
        clean_ok, headline, lines = summarize_pull(ok, out)
        headline = "[%s] %s" % (strategy, headline)
        try:
            st = read_status(path, name)
        except Exception as exc:
            st = RepoStatus(path=path, name=name, error="상태 확인 실패: %r" % (exc,))
        with self.lock:
            self.repos[path] = st
            self.busy.pop(path, None)
            self.results[path] = {
                "kind": "pull",
                "mode": strategy,
                "ok": bool(ok),
                "warn": bool(ok and not clean_ok),
                "text": headline,
                "detail": lines[:40],
                "suggest": [] if ok else suggest_retry(out),
                "ts": time.time(),
            }
        level = "ok" if clean_ok else ("warn" if ok else "err")
        self.log("%s · %s" % (name, headline), level)
        for line in lines:
            self.log("    " + line[:200], "detail")

    # --- 보관(stash) ---------------------------------------------------
    def start_stash(self, path: str, kind: str, ref: str = "",
                    message: str = "", untracked: bool = True) -> bool:
        if kind not in ("push", "pop", "apply", "drop"):
            return False
        # stash 참조는 git 인자로 그대로 넘어가므로 형식을 먼저 검증한다
        if kind != "push" and not STASH_REF_RE.match(ref or ""):
            return False
        with self.lock:
            st = self.repos.get(path)
            if st is None or path in self.busy:
                return False
            self.busy[path] = "stash"
            name = st.name
        self.pool.submit(self._stash_worker, path, name, kind, ref, message, untracked)
        return True

    def _stash_worker(self, path: str, name: str, kind: str, ref: str,
                      message: str, untracked: bool):
        try:
            if kind == "push":
                ok, out = stash_push(path, message, untracked)
            elif kind == "drop":
                ok, out = stash_drop(path, ref)
            else:
                ok, out = stash_restore(path, ref, pop=(kind == "pop"))
        except Exception as exc:
            ok, out = False, "실행 실패: %r" % (exc,)
        good, headline, lines = summarize_stash(kind, ok, out)
        try:
            st = read_status(path, name)
        except Exception as exc:
            st = RepoStatus(path=path, name=name, error="상태 확인 실패: %r" % (exc,))
        with self.lock:
            self.repos[path] = st
            self.busy.pop(path, None)
            self.results[path] = {
                "kind": "stash-" + kind,
                "ok": bool(good),
                "warn": False,
                "text": headline,
                "detail": lines[:40],
                "suggest": [],
                "ts": time.time(),
            }
        self.log("%s · %s" % (name, headline), "ok" if good else "err")
        for line in lines:
            self.log("    " + line[:200], "detail")

    def stashes(self, path: str) -> Optional[list]:
        with self.lock:
            known = path in self.repos
        if not known:
            return None
        return stash_list(path)

    def start_fetch(self, path: str) -> bool:
        with self.lock:
            st = self.repos.get(path)
            if st is None or path in self.busy:
                return False
            self.busy[path] = "fetch"
            name = st.name
        self.pool.submit(self._fetch_worker, path, name)
        return True

    def _fetch_worker(self, path: str, name: str):
        try:
            ok, out = fetch(path)
            st = read_status(path, name)
        except Exception as exc:
            ok, out = False, "실행 실패: %r" % (exc,)
            st = self.repos.get(path) or RepoStatus(path=path, name=name)
        with self.lock:
            self.repos[path] = st
            self.busy.pop(path, None)
            self.results[path] = {
                "kind": "fetch",
                "ok": bool(ok),
                "warn": False,
                "text": "원격 확인 완료" if ok else (out or "fetch 실패").splitlines()[0][:200],
                "detail": [],
                "ts": time.time(),
            }
        self.log("원격 확인 · %s%s" % (name, "" if ok else " · " + (out or "")[:160]), "ok" if ok else "err")

    def pull_targets(self) -> List[str]:
        with self.lock:
            return [p for p, st in self.repos.items() if st.has_update and p not in self.busy]

    def shutdown(self):
        self.pool.shutdown(wait=False)


class Handler(BaseHTTPRequestHandler):
    server_version = "GitUpdate/" + __version__
    protocol_version = "HTTP/1.1"

    # --- 공통 --------------------------------------------------------
    def log_message(self, fmt, *args):  # 터미널을 조용하게
        pass

    @property
    def state(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def _authorized(self, query: dict) -> bool:
        """토큰 + Host + Origin 확인. 로컬 브라우저 외의 접근을 막는다."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            allowed = {"http://127.0.0.1:%d" % self.server.server_port,
                       "http://localhost:%d" % self.server.server_port}
            if origin not in allowed:
                return False
        token = self.headers.get("X-Token") or (query.get("t", [""])[0])
        return secrets.compare_digest(token or "", self.server.token)  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict, code: int = 200):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    # --- 라우팅 ------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        self.state.last_seen = time.time()

        if url.path in ("/", "/index.html"):
            try:
                html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            except OSError:
                self._send(500, b"index.html missing", "text/plain")
                return
            html = html.replace("__TOKEN__", self.server.token)  # type: ignore[attr-defined]
            # 첫 페인트 전에 밝기를 확정해 화면이 번쩍이지 않게 한다.
            html = html.replace("__THEME__", self.state.cfg.theme)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if url.path in STATIC_FILES:
            try:
                blob = (WEB_DIR / url.path[1:]).read_bytes()
            except OSError:
                self._send(404, b"asset missing", "text/plain; charset=utf-8")
                return
            self._send(200, blob, STATIC_FILES[url.path])
            return
        if url.path == "/api/state":
            self._json(self.state.snapshot())
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            self._json({"error": "forbidden"}, 403)
            return
        self.state.last_seen = time.time()
        body = self._body()
        st = self.state

        if url.path == "/api/refresh":
            st.refresh(do_fetch=bool(body.get("fetch")))
            self._json({"ok": True})
        elif url.path == "/api/pull":
            path = str(body.get("path") or "")
            mode = body.get("strategy")
            self._json({"ok": st.start_pull(path, mode if mode in PULL_MODES else None)})
        elif url.path == "/api/fetch":
            path = str(body.get("path") or "")
            self._json({"ok": st.start_fetch(path)})
        elif url.path == "/api/pull-all":
            mode = body.get("strategy")
            mode = mode if mode in PULL_MODES else None
            targets = st.pull_targets()
            if targets:
                st.log("전체 업데이트 시작 (%d개, %s)" % (len(targets), mode or st.cfg.pull_strategy))
            for path in targets:
                st.start_pull(path, mode)
            self._json({"ok": True, "count": len(targets)})
        elif url.path == "/api/stash/list":
            entries = st.stashes(str(body.get("path") or ""))
            if entries is None:
                self._json({"error": "unknown repo"}, 404)
            else:
                self._json({"ok": True, "entries": entries})
        elif url.path == "/api/stash":
            self._json({"ok": st.start_stash(
                str(body.get("path") or ""),
                str(body.get("kind") or ""),
                str(body.get("ref") or ""),
                str(body.get("message") or ""),
                body.get("untracked", True) is not False,
            )})
        elif url.path == "/api/config":
            self._json(self._save_config(body))
        elif url.path == "/api/pick-folder":
            self._json({"path": pick_folder_native()})
        elif url.path == "/api/reveal":
            path = str(body.get("path") or "")
            ok = path in st.repos
            if ok:
                subprocess.Popen(["open", path])
            self._json({"ok": ok})
        elif url.path == "/api/terminal":
            path = str(body.get("path") or "")
            ok = path in st.repos
            if ok:
                subprocess.Popen(["open", "-a", "Terminal", path])
            self._json({"ok": ok})
        elif url.path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)

    def _save_config(self, body: dict) -> dict:
        cfg = self.state.cfg
        changed = []
        if isinstance(body.get("roots"), list):
            cfg.roots = [str(r) for r in body["roots"] if str(r).strip()]
            changed.append("roots")
        if isinstance(body.get("excludes"), list):
            cfg.excludes = [str(e) for e in body["excludes"] if str(e).strip()]
        if body.get("max_depth") is not None:
            try:
                cfg.max_depth = int(body["max_depth"])
            except (TypeError, ValueError):
                pass
        if body.get("pull_strategy") in PULL_STRATEGIES:
            cfg.pull_strategy = body["pull_strategy"]
        if body.get("theme") in THEMES:
            cfg.theme = body["theme"]
        if body.get("autostash") is not None:
            cfg.autostash = bool(body["autostash"])
        if body.get("fetch_on_start") is not None:
            cfg.fetch_on_start = bool(body["fetch_on_start"])
        cfg.save()
        if body.get("rescan") or "roots" in changed:
            self.state.refresh(do_fetch=False)
        return {"ok": True, "config": self.state.snapshot()["config"]}


def pick_folder_native() -> str:
    """macOS 기본 폴더 선택 창을 띄운다 (브라우저에는 폴더 선택기가 없음)."""
    script = 'POSIX path of (choose folder with prompt "git 저장소를 찾을 폴더 선택")'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip().rstrip("/")


def serve(cfg: Config, port: int = 0, open_browser: bool = True, run_forever: bool = True):
    state = AppState(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    httpd.state = state          # type: ignore[attr-defined]
    httpd.token = secrets.token_urlsafe(24)  # type: ignore[attr-defined]
    url = "http://127.0.0.1:%d/?t=%s" % (httpd.server_port, httpd.token)

    state.refresh(do_fetch=cfg.fetch_on_start)

    def idle_watch():
        while True:
            time.sleep(30)
            if time.time() - state.last_seen > IDLE_TIMEOUT:
                httpd.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()

    print("%s %s" % (APP_NAME, __version__))
    print("주소: %s" % url)
    print("종료: 이 터미널에서 Control-C (또는 화면 오른쪽 아래 '종료')")
    if open_browser:
        subprocess.Popen(["open", url])
    if not run_forever:
        return httpd, state, url
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        cfg.save()
        state.shutdown()
        httpd.server_close()
    return httpd, state, url
