#!/bin/sh
# Git Update 설치 스크립트.
#
#   curl -fsSL https://raw.githubusercontent.com/info-sum/gitupdate/main/install.sh | sh
#
# 하는 일:
#   1) 저장소를 ~/.gitupdate 에 clone (이미 있으면 최신으로 갱신)
#   2) ~/.local/bin/gitupdate 실행 파일 생성
#   3) 더블클릭용 GitUpdate.command 위치 안내
#
# 환경 변수로 위치를 바꿀 수 있다:
#   GITUPDATE_REPO  가져올 저장소 (기본: GitHub)
#   GITUPDATE_HOME  설치 위치     (기본: ~/.gitupdate)
#   GITUPDATE_BIN   실행 파일 위치 (기본: ~/.local/bin)

set -eu

REPO="${GITUPDATE_REPO:-https://github.com/info-sum/gitupdate.git}"
DEST="${GITUPDATE_HOME:-$HOME/.gitupdate}"
BIN_DIR="${GITUPDATE_BIN:-$HOME/.local/bin}"
BRANCH="${GITUPDATE_BRANCH:-main}"

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git 이 필요합니다. xcode-select --install 로 설치하세요."

PYTHON=""
for candidate in /usr/bin/python3 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys,http.server; sys.exit(0 if sys.version_info[:2] >= (3,7) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
[ -n "$PYTHON" ] || fail "python3 (3.7 이상) 이 필요합니다. xcode-select --install 로 설치하세요."

say "Git Update 설치"
say "  저장소 : $REPO"
say "  위치   : $DEST"
say "  파이썬 : $PYTHON"
say ""

if [ -d "$DEST/.git" ]; then
  say "이미 설치돼 있어 최신으로 갱신합니다."
  git -C "$DEST" fetch --quiet origin "$BRANCH"
  git -C "$DEST" reset --quiet --hard "origin/$BRANCH"
else
  [ -e "$DEST" ] && fail "$DEST 가 이미 있는데 git 저장소가 아닙니다. 먼저 옮기거나 지워주세요."
  git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$DEST"
fi

mkdir -p "$BIN_DIR"
LAUNCHER="$BIN_DIR/gitupdate"
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# Git Update 실행 (install.sh 가 만든 파일)
exec "$PYTHON" "$DEST/main.py" "\$@"
EOF
chmod +x "$LAUNCHER"
chmod +x "$DEST/GitUpdate.command" 2>/dev/null || true

say "설치 완료"
say ""
say "실행 방법"
say "  gitupdate                     터미널에서 실행 (브라우저 창이 열립니다)"
say "  open $DEST                    Finder 에서 GitUpdate.command 더블클릭"
say "  gitupdate --list              화면 없이 저장소 상태만 출력"
say ""

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    say "참고: $BIN_DIR 이 PATH 에 없습니다. 아래 한 줄을 추가하세요."
    say "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && exec zsh"
    say ""
    say "지금 바로 실행하려면: $LAUNCHER"
    ;;
esac
