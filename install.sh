#!/usr/bin/env bash
#
# Install agenthook: the webhook gateway (python service) AND the ratatui TUI.
#
# One-liner (no checkout needed — clones itself):
#   curl -fsSL https://raw.githubusercontent.com/cloiworks/agenthook/main/install.sh | bash
#
# Or from a checkout:
#   ./install.sh
#
# Idempotent — never clobbers an existing routes.json. On a machine that already
# has the repo, it fast-forwards and reinstalls.
#
# Env overrides:
#   AGENTHOOK_DIR=~/agenthook     where the source/clone lives (service runs here)
#   BIN_DIR=~/.local/bin          where the agenthook-tui binary is installed
#   AGENTHOOK_REPO=<git url>      source repo
#   SKIP_SERVICE=1               build+install the TUI only, skip systemd service
set -euo pipefail

REPO_URL="${AGENTHOOK_REPO:-https://github.com/cloiworks/agenthook.git}"
APP_DIR="${AGENTHOOK_DIR:-$HOME/agenthook}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="$HOME/.config/systemd/user"
SERVICE="agenthook"
# shellcheck disable=SC1091
source "$HOME/.cargo/env" 2>/dev/null || true

# --- 0. locate the source tree: a checkout we're inside of, or clone it -------
SELF="${BASH_SOURCE[0]:-}"
HERE=""
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  HERE="$(cd "$(dirname "$SELF")" && pwd)"
fi

if [ -n "$HERE" ] && [ -f "$HERE/agenthook.py" ] && [ -f "$HERE/tui/Cargo.toml" ]; then
  SRC="$HERE"                                  # running inside a checkout
  echo ">> [0/4] 소스: $SRC (체크아웃)"
else
  command -v git >/dev/null || { echo "!! git 가 필요합니다."; exit 1; }
  if [ -d "$APP_DIR/.git" ]; then
    echo ">> [0/4] 기존 클론 업데이트: $APP_DIR"
    git -C "$APP_DIR" pull --ff-only
  elif [ -e "$APP_DIR" ]; then
    echo "!! $APP_DIR 가 이미 있지만 git 저장소가 아닙니다. AGENTHOOK_DIR 로 다른 경로를 주세요."
    exit 1
  else
    echo ">> [0/4] 클론: $REPO_URL -> $APP_DIR"
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi
  SRC="$APP_DIR"
fi

CONFIG="$SRC/routes.json"

command -v python3 >/dev/null || { echo "!! python3 가 필요합니다."; exit 1; }

if ! command -v cargo >/dev/null; then
  if [ "${NO_RUSTUP:-0}" = "1" ]; then
    echo "!! cargo(Rust) 가 없습니다. https://rustup.rs 로 설치 후 다시 실행하세요."
    exit 1
  fi
  command -v curl >/dev/null || { echo "!! Rust 자동 설치에 curl 이 필요합니다."; exit 1; }
  echo ">> cargo 없음 → rustup(minimal) 자동 설치 (건너뛰려면 NO_RUSTUP=1)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  # shellcheck disable=SC1091
  source "$HOME/.cargo/env" 2>/dev/null || export PATH="$HOME/.cargo/bin:$PATH"
  command -v cargo >/dev/null || { echo "!! rustup 설치 후에도 cargo 를 찾지 못했습니다."; exit 1; }
  echo "   설치됨: $(cargo --version)"
fi

echo ">> [1/4] TUI 빌드 (ratatui)"
cargo build --release --manifest-path "$SRC/tui/Cargo.toml"
mkdir -p "$BIN_DIR"
install -m 0755 "$SRC/tui/target/release/agenthook-tui" "$BIN_DIR/agenthook-tui"
echo "   설치됨: $BIN_DIR/agenthook-tui"

echo ">> [2/4] 설정 파일"
if [ -f "$CONFIG" ]; then
  echo "   기존 $CONFIG 유지 (덮어쓰지 않음)"
else
  cp "$SRC/routes.example.json" "$CONFIG"
  chmod 600 "$CONFIG"
  echo "   routes.example.json → routes.json 생성 (chmod 600). 시크릿을 채우세요."
fi

if [ "${SKIP_SERVICE:-0}" = "1" ]; then
  echo ">> [3/4] systemd 서비스 건너뜀 (SKIP_SERVICE=1)"
else
  echo ">> [3/4] systemd user 서비스"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/$SERVICE.service" <<EOF
[Unit]
Description=agenthook inbound gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=$SRC
ExecStart=/usr/bin/python3 $SRC/agenthook.py routes.json
Restart=always
RestartSec=5
StandardOutput=append:$SRC/agenthook.log
StandardError=append:$SRC/agenthook.log

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE" >/dev/null 2>&1 || true
  systemctl --user restart "$SERVICE"
fi

echo ">> [4/4] 완료"
PORT="$(python3 -c "import json;print(json.load(open('$CONFIG')).get('port',8644))" 2>/dev/null || echo 8644)"
if [ "${SKIP_SERVICE:-0}" != "1" ]; then
  sleep 1
  systemctl --user --no-pager status "$SERVICE" 2>/dev/null | sed -n '1,3p' || true
  echo
  echo "  webhook:  http://127.0.0.1:$PORT/webhooks/<route>   (헬스: /health)"
fi
echo "  설정 TUI: agenthook-tui $CONFIG"
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) echo "  ⚠ $BIN_DIR 가 PATH에 없습니다. 추가하거나 풀경로로 실행하세요." ;;
esac
echo "  설정 변경 후:  systemctl --user restart $SERVICE"
