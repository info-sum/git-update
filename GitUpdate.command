#!/bin/zsh
# Finder 에서 더블클릭하면 실행된다.
# 로컬 서버(127.0.0.1)를 띄우고 기본 브라우저로 화면을 연다.
# macOS 기본 python3 만 쓰므로 추가 설치가 필요 없다.
cd "$(dirname "$0")" || exit 1
exec /usr/bin/python3 main.py "$@"
