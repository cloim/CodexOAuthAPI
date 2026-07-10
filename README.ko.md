# CodexOAuthAPI

> 격리된 Codex OAuth 세션을 OpenAI 호환 API처럼 사용할 수 있게 해주는 로컬 서버입니다.

[English README](README.md)

CodexOAuthAPI는 로컬 자동화 도구, 스크립트, 엣지 머신에서 OpenAI 스타일의
`/v1/chat/completions` API를 사용할 수 있게 해줍니다. Codex 로그인 상태는
프로젝트 안에 격리하고, API 요청은 로컬 Bearer 키와 직접 접속 IP allowlist로
제한할 수 있습니다.

## 왜 써볼 만한가요?

- **OpenAI 호환 형태**: `POST /v1/chat/completions`, `GET /v1/models`,
  streaming SSE 응답을 지원합니다.
- **전역 Codex 설정과 분리**: `CODEX_HOME`, `HOME`, `USERPROFILE`, 작업 경로를
  프로젝트 상태 디렉터리 아래로 격리합니다.
- **Codex는 OAuth로 로그인**: Codex device-code 로그인은 한 번만 수행하고,
  이후 서버가 격리된 로컬 상태를 재사용합니다.
- **엣지 머신에 맞는 접근 제어**: `/v1/*`를 `Authorization: Bearer`와 직접
  접속 IP allowlist로 보호합니다.
- **간단한 로컬 설정**: `.env`를 바로 사용할 수 있고, OS 환경변수가 있으면
  그 값이 우선합니다.
- **디버깅에 필요한 정보만 기록**: debug 로그에 요청/응답 본문과 client IP는
  남기고, 인증 헤더는 기록하지 않습니다.

## 빠른 시작

```powershell
uv sync
uv run codex-oauth-api login
uv run codex-oauth-api generate-key
```

`.env.example`을 참고해 `.env`를 만듭니다.

```env
CODEX_OAUTH_API_KEY=생성된키로교체
CODEX_OAUTH_API_ALLOWED_IPS=127.0.0.1
```

서버를 실행합니다.

```powershell
uv run codex-oauth-api serve --host 127.0.0.1 --port 8000
```

요청 예시:

```powershell
curl.exe http://127.0.0.1:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer 생성된키로교체" `
  -d '{ "model": "gpt-5.5", "messages": [{ "role": "user", "content": "CodexOAuthAPI에서 호출합니다" }] }'
```

## 스트리밍

`stream: true`를 넣으면 OpenAI 스타일 SSE chunk로 응답을 받을 수 있습니다.

```powershell
curl.exe -N http://127.0.0.1:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer 생성된키로교체" `
  -d '{ "model": "gpt-5.5", "stream": true, "messages": [{ "role": "user", "content": "스트리밍으로 답해줘" }] }'
```

## 엔드포인트

| Endpoint | 접근 | 용도 |
| --- | --- | --- |
| `GET /health` | 공개 | 헬스 체크 |
| `GET /v1/models` | 보호 | 기본 모델 조회 |
| `POST /v1/chat/completions` | 보호 | OpenAI 호환 채팅 completions |

접근 제어는 `/v1/*`에 적용됩니다. `/health`는 모니터링과 헬스 체크를 위해
공개 상태로 유지됩니다.

## 설정

CodexOAuthAPI는 현재 작업 디렉터리의 `.env` 파일을 읽습니다. 같은 키가 OS
환경변수에 이미 있으면 OS 환경변수 값이 우선합니다.

| 키 | 필수 여부 | 설명 |
| --- | --- | --- |
| `CODEX_OAUTH_API_KEY` | 선택 | 설정 시 `/v1/*`에 필요한 Bearer 키 |
| `CODEX_OAUTH_API_ALLOWED_IPS` | 선택 | `/v1/*` 호출을 허용할 직접 접속 IP 목록 |
| `CODEX_OAUTH_API_STATE_ROOT` | 선택 | 격리된 서버 상태 디렉터리 |
| `CODEX_OAUTH_API_DEFAULT_MODEL` | 선택 | `/v1/models`에 표시되고 요청에서 생략 시 사용할 모델 |
| `CODEX_OAUTH_API_AUTO_LOGIN` | 선택 | `true`면 요청 중 Codex 401 발생 시 device login 허용 |

참고:

- `CODEX_OAUTH_API_ALLOWED_IPS`는 `request.client.host`와 정확히 비교합니다.
- `X-Forwarded-For` 같은 forwarded header는 신뢰하지 않습니다.
- `CODEX_OAUTH_API_AUTO_LOGIN` 기본값은 `false`입니다.
- 실제 `.env` 파일은 git에 올라가지 않도록 ignore됩니다.

## 접근 제어

`CODEX_OAUTH_API_KEY`는 강한 랜덤 문자열이면 됩니다. 아래 명령으로 생성할 수 있습니다.

```powershell
uv run codex-oauth-api generate-key
```

출력은 `.env`에 바로 붙여넣기 좋은 형식입니다.

```env
CODEX_OAUTH_API_KEY=...
```

요청에는 정확한 Bearer 값을 보내야 합니다.

```http
Authorization: Bearer <CODEX_OAUTH_API_KEY>
```

IP와 Bearer 검사가 모두 켜져 있으면 IP 검사가 먼저 실행됩니다. 허용되지 않은
IP는 `403`, Bearer 값 누락 또는 불일치는 `401`을 받습니다.

## 격리 계약

각 Codex SDK 요청은 프로젝트 로컬 격리 설정을 사용합니다.

- `CODEX_HOME`은 설정된 상태 디렉터리 아래에 위치합니다.
- `HOME`과 `USERPROFILE`도 상태 디렉터리 아래로 격리됩니다.
- Codex `cwd`는 격리된 state workspace로 고정됩니다.
- `OPENAI_API_KEY`는 Codex SDK 환경으로 전달하지 않습니다.
- Codex thread는 `ephemeral=True`로 시작합니다.
- SDK 호출에는 hidden developer instructions와 skill instructions를 포함하지 않습니다.

기본 상태 디렉터리:

```powershell
.codex-oauth-api-state
```

## 디버깅

요청/응답 본문을 확인하려면 `--debug`로 실행합니다.

```powershell
uv run codex-oauth-api serve --host 127.0.0.1 --port 8000 --debug
```

debug 요청 로그에는 `client_ip`, method, path, body가 포함됩니다. `Authorization`
헤더와 Bearer 키는 로그에 남기지 않습니다.

## 테스트

```powershell
uv run pytest
```

## 면책 고지

본 프로젝트는 AI API 호출을 개발/연구용 테스트 목적으로 사용해 볼 수 있도록
제공됩니다. 실제 운영 서비스에 적용했을 때 발생하는 장애, 보안 사고, 비용 문제,
데이터 노출, 서비스 중단, 법적·운영상 손실 등 모든 문제에 대해 프로젝트 작성자는
책임을 지지 않습니다.

운영 또는 서비스 환경에서는 반드시 공식적으로 지원되는 정상적인 API 프로바이더
서비스를 직접 이용하는 것을 강력히 권고합니다. 이는 안정성, 보안, 컴플라이언스,
지원, 운영 책임 측면에서 권장되는 방식입니다.
