# agenthook

Hermes 스타일 인바운드 webhook 게이트웨이. 외부 시스템이 `POST /webhooks/<route>` 로
JSON을 밀어넣으면, 즉시 `202 accepted`(단방향 ack)만 돌려주고 백그라운드에서
**헤드리스 Claude Code 에이전트**(`claude -p <prompt>`)를 실행한다.

**완전 독립 서비스** — cokacdir에 대한 의존이 전혀 없다. 텔레그램 전달도, 공유 봇
자격도 없다. webhook은 그저 에이전트를 띄울 뿐이고, 에이전트가 하는 일(API 호출,
스크립트 실행, 파일 작성)이 곧 결과다.

stdlib만 사용 — `pip install` 불필요. 실행에는 `claude` CLI(Claude Code)만 필요.

## 빠른 시작

```bash
# 1) 예시 설정을 복사하고 secret / CHANGE_ME 값들을 교체
cp routes.example.json routes.json
chmod 600 routes.json          # 시크릿 보호
# 2) 실행
python3 agenthook.py routes.json
# 헬스체크
curl http://127.0.0.1:8644/health   # {"status":"ok",...}
```

## 설정 TUI

`routes.json` 을 손으로 고치는 대신, 동봉된 curses TUI 로 엔드포인트를 추가하고
프롬프트(어떤 작업을 시킬지)를 작성할 수 있다. 역시 stdlib만 사용.

```bash
python3 agenthook_tui.py [routes.json]
```

- **라우트 목록**: `↑/↓` 이동 · `a` 추가 · `e`/`⏎` 편집 · `d` 삭제 · `g` 전역설정 · `w` 저장 · `q` 종료
- **라우트 폼**: `↑/↓` 필드 이동 · `⏎` 편집 · `←/→` 모드 토글 · `^G` 시크릿 생성 · `^S` 저장 · `Esc` 취소
- 프롬프트 필드는 `$EDITOR`(nano/vi)를 열어 멀티라인으로 편집한다.
- 저장 시 `routes.json` 을 `chmod 600` 으로 기록한다. 변경 후에는
  `systemctl --user restart agenthook` 로 반영(설정은 기동 시 로드).

> `routes.json` 은 시크릿을 담으므로 `.gitignore` 로 커밋에서 제외된다.
> 공개되는 건 `routes.example.json`(전부 `CHANGE_ME` 플레이스홀더) 뿐이다.

## 동작 흐름

1. `POST /webhooks/<route>` 수신 (JSON only, 1MB 제한)
2. HMAC 서명 검증 (route.secret, 없으면 전역 secret)
3. `route.events` 로 이벤트 필터 (`X-GitHub-Event`/`X-GitLab-Event`/payload.event_type)
4. `route.prompt` 템플릿에 페이로드 보간: `{a.b.c}` 점표기, `{__raw__}`(전체 JSON, 4000자 컷)
5. **즉시 202 ack 반환** → 백그라운드 스레드에서 에이전트 실행
6. 멱등성(`X-GitHub-Delivery`/`X-Request-ID`, 1h) · 레이트리밋(30/min/route)

## 실행 모드 (route.mode)

- `agent` (기본): 렌더된 프롬프트를 헤드리스 에이전트로 실행 →
  `claude -p <프롬프트> --dangerously-skip-permissions [--model …] [--add-dir …]`
  stdout/stderr은 `runs/<route>-<delivery_id>.log` 에 캡처된다. 에이전트가 수행하는
  작업 자체가 결과 — 별도 전달(텔레그램 등) 없음.
- `log`: 렌더된 프롬프트만 로그 출력, 에이전트 미실행 (드라이런/테스트).

### 에이전트 설정 (`agent` 블록, 전역 + 라우트별 override)

| 키 | 의미 | 기본값 |
|---|---|---|
| `bin` | claude CLI 경로 | `~/.local/bin/claude` |
| `workdir` | 에이전트 실행 cwd | `~` |
| `timeout` | 실행 제한(초) | `1800` |
| `model` | 모델 id | (CLI 기본) |
| `effort` | effort 레벨 | (미지정) |
| `add_dir` | 접근 허용 추가 디렉터리 배열 | `[]` |
| `tools` | 허용 도구 배열 | (전체) |
| `extra_args` | claude 추가 인자 배열 | `["--dangerously-skip-permissions"]` |

## HMAC 서명 헤더 (Hermes 동일)

- GitHub: `X-Hub-Signature-256: sha256=<HMAC-SHA256 hex>`
- GitLab: `X-Gitlab-Token: <plain token>`
- 범용:   `X-Webhook-Signature: <HMAC-SHA256 hex>`
- 테스트 전용(루프백): `"secret": "INSECURE_NO_AUTH"` → 검증 생략

범용 서명 만들기:
```bash
BODY='{"title":"hi","message":"world"}'
printf '%s' "$BODY" | openssl dgst -sha256 -hmac 'YOUR_SECRET' | sed 's/^.*= //'
```

## HTTP 응답 코드

| 코드 | 의미 |
|---|---|
| 202 | accepted — ack 반환 후 백그라운드 실행 |
| 200 (duplicate) | 멱등성 중복 |
| 200 (ignored) | 이벤트 필터 불일치 |
| 401 | 서명 불일치/누락 |
| 400 | JSON 파싱 실패 |
| 404 | 알 수 없는 라우트/경로 |
| 413 | 본문 1MB 초과 |
| 429 | 레이트리밋 초과 |

## systemd (상시 실행)

`~/.config/systemd/user/agenthook.service`:
```ini
[Unit]
Description=agenthook gateway
After=network.target

[Service]
WorkingDirectory=%h/agenthook
ExecStart=/usr/bin/python3 %h/agenthook/agenthook.py routes.json
Restart=always

[Install]
WantedBy=default.target
```
```bash
systemctl --user daemon-reload && systemctl --user enable --now agenthook
```

## 외부 노출 (traefik — hermes-webhooks 패턴 그대로)

`gitops/traefik/dynamic/agenthook.yml`:
```yaml
http:
  routers:
    agenthook:
      rule: "Host(`hooks.your-domain.com`) && PathPrefix(`/webhooks`)"
      entryPoints: [https]
      tls: { certResolver: letsencrypt }
      service: agenthook
      priority: 100
  services:
    agenthook:
      loadBalancer:
        servers:
          - url: "http://127.0.0.1:8644"
```
서버는 `127.0.0.1:8644` 로만 바인딩하고(외부 직접 노출 X), TLS·도메인은 traefik이 종단한다.
빠른 임시 노출이 필요하면 `cloudflared tunnel` 또는 `ngrok http 8644` 도 가능.

## 보안 메모

- 모든 라우트에 실제 `secret` 설정 (테스트 외 `INSECURE_NO_AUTH` 금지).
- `127.0.0.1` 바인딩 유지 + 리버스 프록시에서 TLS 종단.
- `routes.json` 은 라우트 secret을 담으므로 `chmod 600` + `.gitignore` 유지.
- `--dangerously-skip-permissions` 로 에이전트가 도므로, 라우트 인증(secret)이 곧
  코드 실행 권한이다. 모든 운영 라우트에 강한 secret을 반드시 설정할 것.
