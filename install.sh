#!/usr/bin/env bash
#
# Install agenthook: the webhook gateway (python service) AND the ratatui TUI.
# Idempotent — never clobbers an existing routes.json.
#
#   ./install.sh
#   BIN_DIR=~/bin ./install.sh        # override where the TUI binary lands
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HOME/.cargo/env" 2>/dev/null || true

BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
UNIT_DIR="$HOME/.config/systemd/user"
CONFIG="$HERE/routes.json"
SERVICE="agenthook"

command -v python3 >/dev/null || { echo "!! python3 가 필요합니다."; exit 1; }
command -v cargo   >/dev/null || { echo "!! cargo(Rust) 가 필요합니다: https://rustup.rs"; exit 1; }

echo ">> [1/4] TUI 빌드 (ratatui)"
cargo build --release --manifest-path "$HERE/tui/Cargo.toml"
mkdir -p "$BIN_DIR"
install -m 0755 "$HERE/tui/target/release/agenthook-tui" "$BIN_DIR/agenthook-tui"
echo "   설치됨: $BIN_DIR/agenthook-tui"

echo ">> [2/4] 설정 파일"
if [ -f "$CONFIG" ]; then
  echo "   기존 $CONFIG 유지 (덮어쓰지 않음)"
else
  cp "$HERE/routes.example.json" "$CONFIG"
  chmod 600 "$CONFIG"
  echo "   routes.example.json → routes.json 생성 (chmod 600). 시크릿을 채우세요."
fi

echo ">> [3/4] systemd user 서비스"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$SERVICE.service" <<EOF
[Unit]
Description=agenthook inbound gateway
After=network.target

[Service]
Type=simple
WorkingDirectory=$HERE
ExecStart=/usr/bin/python3 $HERE/agenthook.py routes.json
Restart=always
RestartSec=5
StandardOutput=append:$HERE/agenthook.log
StandardError=append:$HERE/agenthook.log

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable "$SERVICE" >/dev/null 2>&1 || true
systemctl --user restart "$SERVICE"

echo ">> [4/4] 완료"
sleep 1
PORT="$(python3 -c "import json;print(json.load(open('$CONFIG')).get('port',8644))" 2>/dev/null || echo 8644)"
systemctl --user --no-pager status "$SERVICE" 2>/dev/null | sed -n '1,3p' || true
echo
echo "  webhook:  http://127.0.0.1:$PORT/webhooks/<route>   (헬스: /health)"
echo "  설정 TUI: agenthook-tui $CONFIG"
case ":$PATH:" in
  *":$BIN_DIR:"*) : ;;
  *) echo "  ⚠ $BIN_DIR 가 PATH에 없습니다. 추가하거나 풀경로로 실행하세요." ;;
esac
echo "  설정 변경 후:  systemctl --user restart $SERVICE"
