#!/usr/bin/env node
"use strict";
/**
 * Git Update 런처.
 *
 * npx 로 실행할 때 진입점이다. 하는 일은 하나다:
 * 쓸 수 있는 python3 을 찾아 main.py 를 그대로 실행한다.
 * (앱 자체는 파이썬 표준 라이브러리만 쓰므로 설치할 의존성이 없다.)
 */

const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const ENTRY = path.join(ROOT, "main.py");

const PROBE = "import sys, http.server, json; print('%d.%d' % sys.version_info[:2])";

function usable(cmd) {
  if (!cmd) return false;
  const res = spawnSync(cmd, ["-c", PROBE], { encoding: "utf8" });
  if (res.error || res.status !== 0) return false;
  const [major, minor] = String(res.stdout).trim().split(".").map(Number);
  return major === 3 && minor >= 7;
}

function findPython() {
  const candidates = [
    process.env.GITUPDATE_PYTHON,
    "/usr/bin/python3", // macOS 기본 (항상 있음)
    "python3",
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
  ];
  for (const cmd of candidates) {
    if (usable(cmd)) return cmd;
  }
  return null;
}

function die(message) {
  process.stderr.write(message + "\n");
  process.exit(1);
}

if (!fs.existsSync(ENTRY)) {
  die("main.py 를 찾을 수 없습니다: " + ENTRY);
}

if (process.platform !== "darwin") {
  process.stderr.write(
    "주의: 이 앱은 macOS 용입니다. 브라우저 실행, Finder/터미널 열기, 폴더 선택 창이 macOS 기능을 씁니다.\n"
  );
}

const python = findPython();
if (!python) {
  die(
    [
      "python3 (3.7 이상) 을 찾을 수 없습니다.",
      "",
      "macOS 라면 아래 중 하나로 설치됩니다:",
      "  xcode-select --install       # Command Line Tools (기본 python3 포함)",
      "  brew install python",
      "",
      "다른 파이썬을 쓰려면: GITUPDATE_PYTHON=/path/to/python3 gitupdate",
    ].join("\n")
  );
}

const child = spawn(python, [ENTRY, ...process.argv.slice(2)], {
  cwd: ROOT,
  stdio: "inherit",
  env: process.env,
});

child.on("error", (err) => die("실행 실패: " + err.message));
child.on("exit", (code, signal) => {
  if (signal) process.exit(1);
  process.exit(code === null ? 1 : code);
});

// Ctrl-C 는 자식 프로세스가 직접 처리한다 (서버 정리 후 종료).
process.on("SIGINT", () => {});
process.on("SIGTERM", () => {
  if (!child.killed) child.kill("SIGTERM");
});
