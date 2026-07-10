# Windows 로그인 후 서버 자동 시작 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `<WINDOWS_HOST>\<WINDOWS_USER>` 로그인 직후 CodexOAuthAPI가 `127.0.0.1:47307`에서 자동 시작되도록 Windows 예약 작업을 등록하고 검증한다.

**Architecture:** Windows 작업 스케줄러에 `CodexOAuthAPI-47307` 작업 하나를 등록한다. 작업은 프로젝트 폴더를 현재 작업 폴더로 사용하고 가상환경의 `pythonw.exe`로 서버 진입 모듈을 백그라운드 실행하며, 중복 실행과 비정상 종료 재시작 정책을 작업 설정으로 관리한다.

**Tech Stack:** Windows Task Scheduler, PowerShell ScheduledTasks 모듈, Python `pythonw.exe`, FastAPI health endpoint

## Global Constraints

- 실행 계정은 `<WINDOWS_HOST>\<WINDOWS_USER>`이다.
- 예약 작업 이름은 `CodexOAuthAPI-47307`이다.
- 작업 폴더는 `<PROJECT_ROOT>`이다.
- 실행 파일은 `<PROJECT_ROOT>\.venv\Scripts\pythonw.exe`이다.
- 서버 인수는 `-m codex_oauth_api.main serve --host 127.0.0.1 --port 47307`과 정확히 일치해야 한다.
- 중복 실행 정책은 `IgnoreNew`, 실패 재시작은 1분 간격 최대 3회, 실행 시간 제한은 없음으로 설정한다.
- 로그인 화면에 콘솔 창을 띄우지 않는다.
- 다른 키, 경로 또는 실행 인수를 탐색하는 fallback을 두지 않는다.

---

### Task 1: 로그인 자동 시작 작업 등록 및 실기동 검증

**Files:**
- Read: `<PROJECT_ROOT>\docs\superpowers\specs\2026-07-10-windows-logon-autostart-design.md`
- Create: `<PROJECT_ROOT>\src\codex_oauth_api\main.py`, `<PROJECT_ROOT>\tests\test_main.py`

**Interfaces:**
- Consumes: 위 Global Constraints의 예약 작업 계약과 프로젝트의 기존 `.env`, `.codex-oauth-api-state`
- Produces: Windows 예약 작업 `CodexOAuthAPI-47307`과 `http://127.0.0.1:47307/health`에서 확인 가능한 실행 서버

- [ ] **Step 1: 등록 전 충돌 상태 확인**

  동일한 예약 작업 이름과 `47307` 포트의 기존 수신 프로세스를 조회한다. 충돌이 없으면 등록으로 진행하고, 충돌이 있으면 명령줄과 소유 작업을 확인해 동일 목적의 인스턴스인지 판단한다.

- [ ] **Step 2: 예약 작업 등록**

  ScheduledTasks 모듈로 사용자 로그인 트리거, `pythonw.exe` 백그라운드 실행, 정확한 작업 폴더·실행 경로·인수, `IgnoreNew`, 1분 간격 3회 재시작, 무제한 실행 시간을 가진 `CodexOAuthAPI-47307` 작업을 등록한다. 등록 권한이 필요하면 관리자 승인으로 동일한 등록을 다시 수행한다.

- [ ] **Step 3: 등록 계약 검증**

  `Get-ScheduledTask -TaskName CodexOAuthAPI-47307`과 작업 XML을 읽어 이름, 계정, 로그인 트리거, 작업 폴더, 실행 경로, 인수 및 실행 정책이 Global Constraints와 정확히 일치하는지 확인한다.

- [ ] **Step 4: 작업 수동 시작 및 프로세스 검증**

  등록된 작업을 한 번 시작하고 작업 상태, 마지막 작업 결과, `47307` 수신 소켓과 해당 프로세스 명령줄을 확인한다. 예약 작업이 실행 중이며 서버 명령줄이 계약과 일치해야 한다.

- [ ] **Step 5: HTTP 상태 검증**

  `Invoke-RestMethod http://127.0.0.1:47307/health`를 실행한다. HTTP 성공 응답과 애플리케이션 상태 본문을 확인하고, 실패하면 예약 작업 결과와 프로세스 상태를 함께 조사한다.

- [ ] **Step 6: 최종 상태 확인**

  `git status --short --branch`로 의도하지 않은 변경이 없음을 확인하고, 예약 작업 계약과 현재 서버 상태를 최종 보고한다.
