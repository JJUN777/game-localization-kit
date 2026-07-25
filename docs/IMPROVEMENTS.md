# 개선 작업 추적

이 문서는 코드베이스의 안정성·복구성·성능·구조 개선을 관리하는 단일 기준입니다.
정적 분석 원문은 `DASHBOARD_WORK_HISTORY.md`의 부록에 보존하되, 실제 구현 순서와
완료 상태는 이 문서만 갱신합니다.

현재 사용자 문서는 역할별로 정리되어 있습니다. `README.md`는 설치와 빠른
시작, `docs/GUI.md`는 화면 사용법, `docs/WORKFLOW.md`는 CLI·파일·상태 규칙,
`docs/ARCHITECTURE.md`는 구현 경계를 담당합니다. 과거 분석과 구현 과정은
`DASHBOARD_WORK_HISTORY.md`에 보존하되 현재 동작의 기준으로 사용하지 않습니다.
작업 도중에는 관련 항목의 상태와 검증 결과를 같은 커밋에서 갱신합니다.

항목을 `진행 중`으로 바꾸기 전에는 구현 범위, 측정 가능한 완료 기준과 필수
회귀 테스트를 해당 항목에 추가합니다. 범위가 큰 항목은 한 번에 완료 처리하지
않고 독립적으로 검증할 수 있는 하위 항목으로 나눕니다.

2026-07-26 2차 분석에서 현재 소스와 실행 환경을 다시 확인했습니다. 기존
설정으로 unittest 269개, mypy 48개 파일과 ruff 91개 파일이 통과했으며,
`follow_imports = "normal"`에서는 3개 파일의 타입 오류가 확인됐습니다.
새 항목은 발견 시점과 관계없이 실제 영향에 따라 아래 P1~P3에 배치합니다.

## 상태 표기

- `[ ]` 대기
- `[-]` 진행 중
- `[x]` 완료
- `[~]` 보류 또는 실측 뒤 결정

## P0 — 데이터 보존과 작업 중단 방지

사용자 파일 손실, 무기한 작업 점유 또는 실행 중 프로젝트 변경 가능성이 있는
항목입니다. 다른 기능보다 먼저 처리합니다.

- [x] `SAFE-001` 원본 교체 복구에 실패하면 백업을 삭제하지 않고 보존한다.
  - 복구가 완전히 성공한 경우에만 `.glk/source-replacement-*`를 삭제한다.
  - 복구 실패 시 보존 위치를 오류와 상태 기록에서 확인할 수 있게 한다.
  - PDF·이미지 각각의 등록 실패와 복구 실패를 강제로 발생시키는 테스트를 추가한다.
  - 완료 기준: 정상 교체와 정상 rollback에서는 임시 백업이 제거되고, PDF·이미지
    rollback 중 파일 복원에 실패하면 원본이 든 백업과 백업 경로 안내가 남아야 한다.
  - 검증: PDF·이미지 복구 실패, 정상 교체와 정상 rollback 테스트를 포함해
    전체 190개 테스트를 통과했다.
- [x] `CONC-001` 프로젝트 변경과 background job 시작 사이의 경합을 제거한다.
  - 원본 등록·교체, 프롬프트 수정, 프로젝트 삭제의 활성 job 검사를
    `mutation_lock` 안에서 실제 변경 직전에 다시 수행한다.
  - 동시에 들어온 요청에서 실행 중 프로젝트가 변경되지 않는 테스트를 추가한다.
  - 완료 기준: 최초 검사 뒤 job이 시작되어도 원본 등록·교체, 프롬프트 수정과
    프로젝트 삭제가 모두 409로 중단되고 workspace 내용이 바뀌지 않아야 한다.
  - 검증: 최초 검사와 lock 획득 사이에 job 상태가 바뀌는 테스트에서 등록·교체,
    프롬프트 수정과 삭제가 모두 409로 차단되는 것을 확인했다.
- [x] `AI-001` Gemini 호출에 유한한 요청 타임아웃을 적용한다.
  - layout, OCR, translation provider에 동일한 timeout 정책을 적용한다.
  - 타임아웃 뒤 job이 실패 상태로 종료되고 다른 작업을 시작할 수 있어야 한다.
  - 완료 기준: 세 provider의 SDK client에 180초 timeout과 SDK 자체 재시도
    비활성화가 동일하게 적용되고, timeout 예외는 제한된 재시도 뒤 종료되어야 한다.
  - 검증: 세 provider의 client 옵션과 timeout 예외의 3회 시도·2회 대기를
    공통 정책 테스트에서 확인했다.
- [x] `OCR-001` OCR 재실행 실패 시 이전 성공 결과를 빈 파일로 덮어쓰지 않는다.
  - 기존 개별 OCR 결과는 유지하고 실패 상태와 부분 결과만 별도로 기록한다.
  - 성공 후 일시적 실패가 발생하는 회귀 테스트를 추가한다.
  - 완료 기준: 강제 재실행 중 provider가 실패해도 기존 개별 TXT와
    `combined.txt`는 바뀌지 않고, 실패 상태와 재사용 가능한 기존 텍스트는
    `combined.partial.txt`와 `image_ocr.json`에 기록되어야 한다.
  - 검증: 성공한 OCR 뒤 강제 재실행을 실패시키는 회귀 테스트에서 기존 산출물
    바이트가 유지되고 partial 산출물과 실패 상태가 생성되는 것을 확인했다.

## P1 — 오류 복구와 사용자 신뢰성

일반적인 실패 상황을 정확히 분류하고 사용자가 재시도·복구할 수 있게 하는
항목입니다. P0 완료 후 순서대로 진행합니다.

- [x] `AI-002` Gemini 오류와 재시도 여부를 SDK 예외 타입·상태 코드로 판정한다.
  - 문자열에서 `400`, `404`, `not found`를 부분 검색하는 로직을 제거한다.
  - 세 provider가 즉시 함께 사용할 공통 오류·재시도 정책 모듈을 도입한다.
  - 환경 로딩과 전체 provider 골격까지 합치는 후속 작업은 `ARCH-005`에서 완료한다.
  - 완료 기준: SDK `APIError.code`의 400·401·403·404는 즉시 실패하고,
    408·429·5xx는 재시도하며 예외 메시지의 숫자는 판정에 사용하지 않아야 한다.
  - 검증: 영구 4xx와 재시도 가능한 상태 코드, 숫자가 섞인 일반 예외를
    분리하는 단위 테스트를 통과했다.
- [x] `AI-003` 429 사용량 제한에 별도 재시도 정책을 적용한다.
  - `Retry-After`가 있으면 우선 사용하고, 없으면 상한이 있는 긴 백오프를 적용한다.
  - 잘못된 키·권한·없는 모델처럼 재시도해도 해결되지 않는 오류는 즉시 종료한다.
  - 완료 기준: 429는 `Retry-After`를 최대 300초 범위에서 우선 적용하고,
    헤더가 없으면 요청 사이에 최소 60초를 기다려야 한다.
  - 검증: 숫자·HTTP 날짜 형식의 `Retry-After`, 300초 상한, 잘못된 헤더와
    헤더 없는 60초 fallback 테스트를 통과했다.
- [x] `ERROR-001` source·glossary background job의 내부 예외를 안전한 사용자
  메시지로 변환한다.
  - 절대 경로와 SDK 내부 메시지는 상태 파일과 브라우저 응답에 그대로 노출하지 않는다.
  - 기술 진단이 필요하면 별도 로컬 로그에 보존한다.
  - 완료 기준: runner가 예외를 던져도 source·glossary job 상태에는 안전한
    한글 안내만 저장되고 예외의 절대 경로와 내부 상세가 남지 않아야 한다.
  - 검증: source·glossary runner가 절대 경로가 든 예외를 던져도 상태 파일에는
    안전한 한글 안내만 저장되는 테스트를 통과했다.
- [x] `ERROR-002` HTTP 오류 응답을 안정적인 예외 코드 중심으로 전환한다.
  - 명시적으로 전달된 오류 코드가 detail 문자열 추론보다 항상 우선해야 한다.
  - 전체 문구 매칭 제거는 각 도메인 예외에 코드를 추가하면서 점진적으로 진행한다.
  - 1차 완료 기준은 `error_response.py`에서 명시적 code가 detail 문구 추론보다
    우선하고, 현재 HTTP API의 상태 코드·한글 메시지 회귀 테스트를 통과하는 것이다.
  - 남은 문구 매칭 제거는 도메인별 하위 항목으로 분리해 별도로 추적한다.
  - 검증: 명시적 `INVALID_REQUEST`가 stale detail보다 우선하는 단위 테스트와
    dashboard·source·glossary·translation review HTTP 회귀 테스트를 통과했다.
- [x] `ERROR-004` 번역 검수 재번역 실패에서 내부 예외 원문을 UI와 job
  응답에 노출하지 않는다.
  - `_safe_retry_error()`는 알려진 검수 충돌과 validation 문구 외의 예외를
    최대 600자까지 그대로 반환하므로 Gemini SDK 상세와 파일 절대 경로가
    브라우저에 표시될 수 있다.
  - SDK 상태 코드와 명시적 도메인 예외를 기준으로 API 키·모델·권한·사용량·
    네트워크·검수 충돌·결과 검증 오류를 안전한 한글 안내로 분류한다.
  - 예상하지 못한 예외는 고정 안내만 반환하고 내부 상세를 job JSON과 HTTP
    응답에 저장하지 않는다.
  - 완료 기준: Gemini `APIError`, 네트워크 예외와 절대 경로가 포함된
    `OSError`를 강제로 발생시켜도 job의 `error`와 API 응답에 SDK 원문·키·
    절대 경로가 없어야 하며, 검수 충돌과 validation 오류는 기존 행동 안내를
    유지해야 한다.
  - 검증: API 키·권한·모델·사용량·5xx·네트워크·검수 충돌·결과 검증과
    미분류 파일 오류를 각각 강제로 발생시켜 안전한 행동 안내와 원문 비노출을
    확인했고, 전체 279개 테스트와 mypy·ruff 검사를 통과했다.
- [x] `ERROR-005` 용어 후보 생성 job의 안전한 도메인 오류 안내를 보존한다.
  - 현재 모든 runner 예외를 하나의 고정 문구로 바꿔 stale 용어 검수 파일처럼
    사용자가 직접 해결할 수 있는 원인까지 숨긴다.
  - 자동 덮어쓰기 차단처럼 명시적으로 만든 안전한 도메인 상태는 안정적
    오류 코드와 한글 안내로 전달하고, 경로·파싱 상세를 포함할 수 있는 일반
    `ProjectError`와 예상하지 못한 예외는 고정 문구로 정제한다.
  - 완료 기준: 승인 원문 불일치·stale 검수 파일은 재생성 방법을 안내하고,
    절대 경로가 포함된 프로젝트·파일시스템 예외는 UI와 job state에 노출하지
    않아야 한다.
  - 검증: `GLOSSARY_REVIEW_STALE` 도메인 오류는 기존 편집 보존·정리 후
    재생성을 안내하고, 승인 원문 오류와 미분류 예외의 절대 경로는 job state에
    남지 않음을 확인했다.
- [x] `JOB-001` 저장된 background job 상태를 스키마로 검증한다.
  - 타입, 허용 상태, 필수 필드와 프로젝트 ID를 검증한다.
  - 프로젝트 ID는 가능한 경우 상태 파일 내용보다 상위 프로젝트 경로를 기준으로 한다.
  - 완료 기준: source·glossary·translation job이 schema version, 필수·미지
    필드, 상태, 진행률, 결과, 오류, timestamp와 job별 필드를 검증한 뒤에만
    메모리에 복원되어야 한다.
  - 검증: 잘못된 schema·상태·타입·timestamp·필수 필드를 가진 레코드는
    무시하고, 저장 내용의 프로젝트 ID 대신 상위 폴더의 canonical ID를 사용하는
    회귀 테스트를 통과했다.
- [x] `REVIEW-001` 번역 검수의 Gemini 재번역을 background job으로 전환한다.
  - HTTP 요청과 mutation lock을 장시간 점유하지 않는다.
  - 진행률, 실패 사유와 재시도 가능 상태를 검수 화면에 표시한다.
  - 완료 기준: 재번역 시작 API가 편집 내용을 저장한 뒤 `202 Accepted`로
    즉시 반환하고, 별도 worker의 진행·완료·실패 상태를 조회할 수 있어야 한다.
    실행 중에는 중복 재번역과 검수 변경을 막고 실패 뒤에는 같은 화면에서 다시
    시작할 수 있어야 한다.
  - 검증: 실행 중 검수 조회 응답, 진행률 갱신, 중복 실행 409, 완료 결과 반영과
    실패 사유 표시 후 재시작 회귀 테스트를 통과했다.
- [x] `ENV-001` AI 설정 파일의 기준 경로를 실행 CWD와 분리한다.
  - 저장소 실행, 설치된 CLI 실행과 다른 디렉터리에서 실행할 때 동일한 설정을 찾는다.
  - 저장 후 `api_key_source`가 실제 상태와 일치하게 갱신한다.
  - 완료 기준: 명시적 `--settings-root`, `GLK_SETTINGS_ROOT`, 검증된 editable
    checkout root, OS별 사용자 설정 디렉터리 순으로 하나의 기준 경로를 선택하고
    dashboard와 provider가 같은 `.env`를 사용해야 한다.
  - 검증: 다른 CWD, editable checkout, macOS·Linux 사용자 설정 경로와 provider
    로딩 경로 테스트를 통과했고, 저장 직후 `api_key_source=env_file`을 확인했다.
- [x] `ENV-003` 대시보드의 명시적 `--settings-root`를 background job
  provider까지 전달한다.
  - 대시보드는 해석된 root를 `AiSettingsService`에만 전달하고 provider의
    `from_environment()`는 인자 없이 기본 설정 경로를 다시 찾는다. 이 때문에
    custom root에 기존 `.env`가 있는 첫 실행은 화면에서 설정 완료로 보여도
    실제 원문 준비·번역에서 키를 찾지 못할 수 있다.
  - 대시보드가 해석한 단일 settings root를 job manager와 source·translation
    runner를 거쳐 provider 생성까지 명시적으로 전달하고, `save()`가 process
    환경변수에 키를 복사하는 부수 효과에 의존하지 않는다.
  - 완료 기준: 셸의 Gemini 환경변수가 없는 상태에서
    `glk ui --settings-root <custom>`으로 시작하면 custom `.env`의 키와 모델을
    source·translation background job이 모두 사용하고, 기본 설정 경로의 값은
    섞이지 않아야 한다.
  - 회귀 테스트: custom `.env`를 미리 만든 첫 실행, GUI에서 저장한 뒤 실행,
    `GLK_SETTINGS_ROOT`와 명시적 옵션의 우선순위 및 테스트 간 process 환경
    오염 방지를 확인한다.
  - 검증: 명시적 root의 기존 `.env` 로딩과 환경 root보다 높은 우선순위,
    설정 저장 뒤 process 환경 무변경, source·translation·검수 재번역 job의
    단일 root 전달을 포함해 전체 274개 테스트와 mypy·ruff 검사를 통과했다.
- [x] `IO-001` 이미지 입력과 파생 출력의 대소문자 무시 충돌을 검사한다.
  - CLI와 대시보드가 같은 `casefold()` 기준을 사용한다.
  - 완료 기준: 원본 상대 경로와 확장자를 `.txt`로 바꾼 파생 경로를 각각
    `casefold()`해 비교하고, 충돌하면 프로젝트에 파일을 복사하기 전에 거부해야 한다.
  - 검증: `Card.png`/`card.png` 원본 충돌과 `Card.PNG`/`card.jpg` 파생 TXT
    충돌을 공통 등록 서비스에서 거부하는 테스트를 통과했다.
- [x] `STAGE-001` partial 번역이 번역 검수 단계로 잘못 표시되지 않게 단계 판정
  순서를 수정한다.
  - 완료 기준: `translation_status=partial`이면 남아 있는 review 상태와 관계없이
    대시보드 단계가 `translation_partial`이어야 한다.
  - 검증: review가 `stale` 또는 `qa_failed`인 partial 상태 모두
    `translation_partial`로 판정하는 단위 테스트를 통과했다.
- [x] `INPUT-001` 페이지 범위를 전개하기 전에 문서 페이지 수와 최대 범위를 검사한다.
  - 완료 기준: 단일 페이지와 범위의 양 끝을 `range()` 또는 set에 넣기 전에
    검증하고, `1-99999999` 같은 입력을 큰 집합 생성 없이 즉시 거부해야 한다.
  - 검증: 정상 선택, 중복 제거, 범위 역전, 0·초과 페이지, 1억 페이지에 가까운
    범위와 잘못된 문서 페이지 수를 다루는 전용 단위 테스트를 통과했다.
- [x] `HASH-001` 프롬프트와 리뷰 파일의 개행 정규화·해시 기준을 통일한다.
  - `read_text()` 자체는 보편적 개행을 정규화하므로, 정규화된 문자열 해시와
    원본 바이트 해시가 혼용되는 지점을 정리한다.
  - 동일한 논리 문서의 해시 입력을 원본 `bytes` 또는 개행이 정규화된
    `text.encode("utf-8")` 중 하나로 통일하고 모든 stale 판정에 같은 기준을 쓴다.
  - 완료 기준: 사용자 프롬프트는 LF로 정규화한 UTF-8 text hash를 사용하고,
    검수 파일처럼 byte 단위 동시성 검사가 필요한 문서는 모든 저장·비교 지점에서
    원본 byte hash를 유지해야 한다.
  - 검증: LF·CRLF·CR의 text hash 일치, CRLF 변경 뒤 이미지 OCR·source QA
    캐시 재사용과 번역 상태·캐시 유지 테스트를 통과했다.
- [x] `CACHE-001` 캐시 미스, 손상된 JSON, 권한·디스크 읽기 오류를 구분해 보고한다.
  - 완료 기준: 존재하지 않는 파일만 정상 캐시 미스로 처리하고, UTF-8·JSON·객체
    형식이 잘못된 파일은 손상 오류, 그 밖의 `OSError`는 저장소 읽기 오류로
    호출자에게 전달해야 한다.
  - PDF layout, 이미지 OCR, segmentation, source QA, glossary, translation과
    대시보드 단계 상태가 같은 JSON 캐시 판정기를 사용한다.
  - 검증: 미스·손상·권한 오류 단위 테스트, 손상된 OCR 캐시의 AI 재호출 방지,
    손상된 프로젝트 상태의 대시보드 경고 회귀 테스트를 통과했다.

## P2 — 성능과 내구성

실제 문서 크기와 프로젝트 수가 늘어날 때 영향을 주는 항목입니다. 간단한 중복
제거는 먼저 할 수 있지만, 큰 저장 구조 변경은 측정 결과를 남긴 뒤 진행합니다.

- [x] `PERF-001` 대시보드 문서 생성에서 프로젝트별 `inspect_project()` 중복
  호출을 제거한다.
  - 한 번의 검사 결과를 프로젝트 요약과 대시보드 문서가 공유한다.
  - 같은 문서 생성 안에서는 동일 파일 해시를 재사용한다.
  - 검증: 프로젝트 수와 `inspect_project()` 호출 수가 같고, 같은 snapshot 안의
    동일 경로는 byte hash를 한 번만 계산하는 회귀 테스트를 통과했다.
- [x] `PERF-002` 용어 증거 수집에서 용어마다 전체 코퍼스를 다시 순회하지 않게
  인덱스 또는 메모이제이션을 적용한다.
  - 검증: 용어 import에서 자동 후보 재생성 한 번과 전체 evidence index 생성
    한 번만 코퍼스를 순회하며, 용어 행마다 추가 순회하지 않는다.
- [x] `PERF-003` 번역 청크마다 누적 결과 전체를 다시 쓰는 비용을 실측한다.
  - 중단 복구 안전성을 유지하면서 병목이 확인된 경우에만 append/checkpoint 구조로 바꾼다.
  - 24개 단일 block 청크에서 최종 JSONL 15,501 bytes를 만들기 위해 기존에는
    209,061 bytes를 써 13.49배 증폭이 발생했다.
  - durable append와 byte 길이·SHA-256 checkpoint를 적용한 뒤 같은 입력의
    쓰기량은 15,501 bytes, 1.00배가 되었다.
- [~] `UPLOAD-001` 256 MiB multipart 본문 스트리밍 처리를 검토한다.
  - 로컬 도구의 실제 최대 파일 크기와 메모리 사용량을 측정한 뒤 결정한다.
- [x] `IO-002` 여러 산출물 기록 중 중단되었을 때 복구 가능한 상태를 제공한다.
  - 완전한 다중 파일 트랜잭션보다 `writing/failed` 상태, 임시 산출물 정리와
    재실행 안내를 우선한다.
  - 내구성이 필요한 쓰기에는 `os.replace` 뒤 부모 디렉터리 fsync를 검토한다.
  - 번역 청크 저장 범위는 `PERF-003` 실측과 저장 구조 결정을 선행 조건으로 삼고,
    append/checkpoint를 채택하면 성능과 중단 정합성을 함께 검증한다.
  - 번역은 첫 호출 전 0-byte checkpoint와 매 청크의 byte 길이·hash를 기록한다.
    state 반영 전 끊긴 append 꼬리는 resume 때 마지막 checkpoint로 되돌린다.
  - 모든 atomic replace와 새 append 파일은 지원 운영체제에서 부모 디렉터리를
    fsync한다.
  - 용어 후보 생성은 `writing/failed` state와 예상 출력 hash로 출력 기록 뒤
    state commit 중단을 다음 실행에서 복구하며, 불일치한 사용자 편집은 보존한다.
  - 검증: append 꼬리, 번역 최종 산출물 기록 중단과 용어 후보 state commit
    중단 뒤 재실행 회귀 테스트를 통과했다.
- [x] `IO-003` 부모 디렉터리 fsync 미지원 환경에서 성공한 파일 교체를
  실패로 오인하지 않는다.
  - `os.replace()`가 성공한 뒤 `_fsync_parent()`에서 `EINVAL` 또는
    `ENOTSUP`이 발생하면 파일은 이미 교체됐지만 호출자는 전체 쓰기가 실패한
    것으로 판단한다.
  - 디렉터리 fsync를 지원하지 않는 것이 명확한 errno만 best-effort로
    처리하고, `EIO`·권한 오류처럼 실제 저장소 이상일 수 있는 오류는 숨기지
    않는다.
  - 완료 기준: 지원 파일시스템에서는 기존 directory fsync를 유지하고,
    `EINVAL`·`ENOTSUP`에서는 atomic replace와 신규 append가 성공으로
    끝나며, 그 밖의 `OSError`는 호출자에게 전달되어야 한다.
  - 회귀 테스트: `os.open`과 `os.fsync` 각각의 미지원 errno, 실제 I/O 오류,
    replace 이전 실패와 replace 이후 fsync 실패를 분리해 검사한다.
  - 검증: directory open·fsync의 `EINVAL`·`ENOTSUP`, 신규 append와 atomic
    replace 성공, replace 전 파일 fsync 실패, replace 후 `EIO` 및 권한 오류
    전파를 확인했고 전체 285개 테스트와 mypy·ruff 검사를 통과했다.
- [x] `ENV-002` 일반 설치 환경에서 provider의 `.env` 탐색 경로가 패키지 상위
  디렉터리를 잘못 가리키지 않게 한다.
  - 완료 기준: source checkout 표식이 실제로 있을 때만 checkout root를 사용하고,
    일반 설치에서는 패키지 파일의 임의 상위 경로가 아니라 OS별 사용자 설정
    디렉터리를 사용해야 한다.
  - 검증: editable root와 일반 설치 fallback을 분리한 경로 테스트 및 provider가
    해석된 단일 `.env`만 로드하는 테스트를 통과했다.
- [ ] `DOMAIN-001` 줄바꿈 하이픈 결합 결과를 원문 검수에서 확인할 수 있게 한다.
  - 단어 사전 기반 자동 교정보다 결합 경고와 원문 비교를 우선한다.

## P3 — 구조와 개발 도구

P0·P1 수정 과정에서 필요한 공통 부분부터 작게 추출합니다. 대규모 일괄
리팩터링은 기능 흐름이 안정된 뒤 별도 커밋으로 진행합니다.

- [x] `ARCH-001` 네 localhost 서버의 인증, 보안 헤더, JSON 입출력과 포트
  검증을 공통 기반으로 추출한다.
  - `LocalHttpServer`가 localhost bind, origin, session token과 mutation lock을
    제공한다.
  - `LocalHttpRequestHandler`가 Host·Origin·token 인증, 보안 헤더, JSON
    응답·오류·요청 파싱을 제공한다.
  - source 화면의 blob 이미지 CSP 허용만 명시적 변형으로 관리하고, 네 server
    factory가 동일한 port 검증을 사용한다.
  - 검증: 공통 상속·CSP 변형·port·복귀 URL·JSON 오류 타입 단위 테스트와
    네 서버 통합 테스트를 통과한다.
- [x] `ARCH-002` source·glossary·translation job의 저장·조회·실행 골격을
  파라미터화한다.
  - 공통 `DashboardJobRecord`가 상태·진행률·결과·시간 필드를 제공한다.
  - `_JobStore`가 job 종류별 state 경로·parser만 받아 저장, 복원, 중단 전환과
    최신 목록을 공통 처리한다.
  - `_queue_job`과 `_execute_job`이 단일 active 검사, thread 시작, running 전환,
    진행률 저장과 terminal 결과 저장을 공통 처리한다.
  - 검증: 세 job의 성공·실패·진행률·상호 배제와 실행 중 저장 상태의
    interrupted 복원 회귀 테스트를 통과한다.
- [x] `ARCH-003` dashboard HTTP 경로 분석과 인증을 라우팅 계층으로 분리한다.
  - 정적·프로젝트별 동적 경로를 메서드와 함께 단일 라우터에서 판별하고,
    경로별 접근 정책을 public·localhost·session으로 명시한다.
  - handler는 라우터가 반환한 경로 이름과 project ID만 사용하며, 인증은
    업무 요청 본문을 읽기 전에 공통 진입점에서 한 번 수행한다.
  - 완료 기준: 모든 dashboard 경로의 메서드·접근 정책·query·project ID를
    독립적으로 검사할 수 있고, 잘못된 메서드나 중첩 경로가 업무 로직에
    진입하지 않아야 한다.
  - 검증: 정적·동적 경로, 1회 URL decode, query 분리, 잘못된 메서드·빈 ID·
    중첩 경로 단위 테스트를 포함한 전체 246개 테스트와 mypy 23개 파일 검사를
    통과했다.
- [x] `ROUTE-001` 라우터와 HTTP handler 분기의 불일치를 즉시 실패로
  감지한다.
  - 현재 등록된 route는 모두 처리되지만, 이후 라우터에 이름을 추가하고
    `do_GET`·`do_POST`·`do_PUT`·`do_PATCH` 분기를 빠뜨리면 응답을 쓰지 않은
    채 handler가 종료될 수 있다.
  - 각 메서드의 마지막에 미처리 route fallback을 두거나 route 이름과 handler
    callable을 구조적으로 연결해 등록 누락을 테스트에서 확인한다.
  - 완료 기준: 라우터가 반환한 이름을 handler가 처리하지 못하면 보안 헤더가
    포함된 500 또는 501 응답을 즉시 반환하고, 모든 등록 route가 정확히 하나의
    handler에 연결됐음을 단위 테스트로 고정해야 한다.
  - 검증: 메서드별 등록 route와 handler 계약의 완전 일치, 미등록 route 이름의
    즉시 500 fallback 및 GET·POST·PUT·PATCH·DELETE 분기 말단 응답을 고정했고
    전체 288개 테스트와 mypy·ruff 검사를 통과했다.
- [x] `ARCH-004` `translate_project`, `import_project_glossary`,
  `_inspect_pipeline_status` 등 대형 함수를 책임별로 분리한다.
  - [x] `_inspect_pipeline_status`를 원문, 용어, 번역 실행, 번역 검수 상태
    판정과 최종 조립 단계로 분리했다.
    - 검증: pipeline 단계 전환과 최종 번역 승인·stale 판정을 포함한 전체
      246개 테스트와 mypy 24개 파일 검사를 통과했다.
  - [x] `import_project_glossary`의 입력 준비, TSV 필드·ID·증거 검증,
    정규화, cache 판정과 결과 저장을 분리했다.
    - 검증: 자동 후보 보존, 수동 용어 증거, 중복·미완료·변경 ID 거부,
      cache 재사용을 포함한 전체 246개 테스트와 mypy 25개 파일 검사를
      통과했다.
  - [x] `translate_project`의 입력 준비·checkpoint 복원·청크 실행·최종화를
    분리한다.
    - [x] 프로젝트·termbase·prompt·model 입력 준비, dry-run 결과 생성과
      기존 checkpoint 검증·꼬리 복구·cache 반환을 분리했다.
      - 검증: dry-run·cache·partial resume·append 꼬리 복구·force 재시작을
        포함한 전체 246개 테스트와 mypy 26개 파일 검사를 통과했다.
    - [x] 청크 요청·2회 구조/내용 검증과 `TranslationSegment` 생성을
      분리했다.
      - 검증: 구조 오류 재시도, 내용 경고 보존과 provider 실패 resume를
        포함한 전체 246개 테스트와 mypy 26개 파일 검사를 통과했다.
    - [x] 초기·실패·진행 state 기록, 청크 append와 최종 review 생성을
      분리한다.
      - 검증: 초기·실패·진행 checkpoint 기록, 중단 뒤 resume, 청크 append와
        최종 draft·review·state 생성을 포함한 전체 246개 테스트와 mypy
        26개 파일 검사를 통과했다.
  - 완료 기준: 세 진입 함수는 한 use case의 순서만 조정하고, 각 하위 함수는
    독립된 상태 판정·검증·저장 책임 하나만 가져야 한다.
- [x] `ARCH-005` 세 Gemini provider의 공통 동작을 기반 모듈로 추출한다.
  - 재시도 루프, 환경 설정 로딩, SDK 오류 판정과 timeout 정책을 공통 관리한다.
  - layout, OCR, translation 고유 책임은 prompt·schema·응답 변환으로 제한한다.
  - `AI-001`~`AI-003`에서 도입한 공통 정책을 provider 전체 구조로 확장한다.
  - `GeminiProviderBase`가 API 키 검증, 모델 결정, SDK client 생성,
    `from_environment`와 공통 재시도 실행을 담당한다.
  - 검증: 세 provider의 공통 환경 생성·누락 키 오류·timeout 설정 회귀 테스트와
    공통 기반 및 세 provider의 mypy 검사를 통과한다.
- [ ] `ARCH-006` 상태 판정·I/O·provider 호출이 다시 섞인 대형 use case를
  작은 단계로 분리한다.
  - AST 기준 현재 `src`에는 80줄 이상 함수가 30개 있지만 줄 수 자체를
    일괄 축소 목표로 삼지 않는다. 선언형 CLI parser와 단순 dispatch는
    복잡도·변경 위험을 따로 판단한다.
  - 우선 범위는 `translate_project`, `extract_project_pdf`,
    `ocr_project_images`, `save_project_source_review`,
    `retry_failed_translations`로 제한한다. `build_parser`와 dashboard
    `do_*`는 각각 CLI 구성과 `ROUTE-001` 범위에서 다룬다.
  - 입력 준비, cache/checkpoint 복원, 외부 호출, 결과 검증과 최종 저장을
    독립 helper로 분리하되 진입 함수는 use case 순서와 조기 반환만 조정한다.
  - 완료 기준: 우선 범위의 각 진입 함수가 120줄 이하이고, 추출한 단계별
    helper에 성공·부분 실패·재개·stale 회귀 테스트가 있어야 한다. 줄 수를
    맞추기 위한 의미 없는 wrapper 분리는 허용하지 않는다.
- [x] `API-001` 결과 객체의 `ok` 의미를 통일하거나 상수 필드를 제거한다.
  - 성공하거나 예외를 던지는 결과 객체는 상수 `ok`를 노출하지 않는다.
  - 일부 실패나 QA 불합격처럼 정상 반환 안에 실제 결과 차이가 있는 객체만
    계산된 `ok`를 유지하고, HTTP 응답 envelope의 `ok`와 구분한다.
  - 완료 기준: application 결과 객체에 상수 `ok`가 남아 있지 않고, 계산된
    `ok`는 해당 결과의 성공·실패 값과 함께 바뀌어야 한다.
  - 검증: 성공 전용 11개 결과와 QA 결과의 직렬화 계약 테스트를 포함한 전체
    248개 테스트와 mypy 26개 파일 검사를 통과했다.
- [x] `SECURITY-001` 세션 토큰 비교에 `secrets.compare_digest`를 사용한다.
  - dashboard, source, glossary, translation API와 source asset URL의 토큰을
    상수 시간 비교한다.
  - 검증: 네 HTTP handler와 source asset 인증이 모두 `compare_digest`를
    호출하는 전용 회귀 테스트를 통과한다.
- [x] `SECURITY-002` 공통 HTTP handler의 기본 오류 응답에도 보안 헤더를
  적용하고 서버 버전 노출을 제거한다.
  - `HEAD`, `OPTIONS`와 그 밖의 미지원 메서드는
    `BaseHTTPRequestHandler`의 기본 501 응답을 사용해 공통 CSP·no-store
    헤더가 빠지고 `Server` 헤더에 Python 버전이 포함된다.
  - 공통 handler에서 미지원 메서드를 명시적 405로 처리하고, 기본 오류 경로도
    `_send_standard_headers()`를 통과하게 한다. 서버 식별자는 제품명만
    사용하고 Python 런타임 버전은 보내지 않는다.
  - 완료 기준: 네 localhost 서버의 `HEAD`, `OPTIONS`와 임의 미지원 메서드
    응답에 공통 보안 헤더가 있고, 응답 헤더 어디에도 Python 버전이 없어야
    한다. 정상 GET·POST 응답과 source의 Range 요청은 기존 계약을 유지한다.
  - 검증: 네 handler의 `HEAD`, `OPTIONS`, 임의 `BREW` 요청이
    `Server: GLK`, 공통 CSP·no-store·Allow 헤더가 포함된 405를 반환하고
    Python 버전을 노출하지 않음을 확인했다.
- [x] `QUALITY-001` ruff를 작은 규칙 집합부터 도입하고 포맷 변경은 별도 커밋으로
  분리한다.
  - 오류 가능성이 높은 `E4`, `E7`, `E9`, `F`만 `src`와 `tests`에 적용하고,
    formatter나 자동 수정은 도입하지 않는다.
  - dev 의존성과 CI static checks에 동일한 `ruff check src tests`를 추가한다.
  - 완료 기준: 선택한 규칙을 명시한 설정과 CI 검사가 있고, 기존 동작 변경 없이
    모든 lint 오류를 제거해야 한다.
  - 검증: 미사용 import 3건을 제거한 뒤 Python 88개 파일의 ruff 검사,
    전체 248개 테스트와 mypy 26개 파일 검사를 통과했다.
- [x] `QUALITY-002` mypy 대상을 도메인 계층부터 점진적으로 확대한다.
  - application/domain/extraction 전체 37개 파일을 디렉터리 단위로 편입해
    앞으로 추가되는 파일도 자동 검사한다.
  - Optional 상태 축소, 고정 길이 bbox tuple, PDF fragment 타입과 PDF·이미지
    결과 union을 명시해 6개 파일의 15개 오류를 제거했다.
  - 완료 기준: 기존 infrastructure 검사와 세 디렉터리를 함께 검사하고,
    명시한 전체 범위에서 mypy 오류가 없어야 한다.
  - 검증: 전체 48개 파일의 mypy 검사, 88개 Python 파일의 ruff 검사와
    전체 248개 테스트를 통과했다.
- [x] `QUALITY-003` 결정적 규칙부터 전용 단위 테스트를 보강한다.
  - `domain/translation_qa.py`: token·태그·숫자 보존, 용어 variant·경계와
    원문 유지 규칙을 직접 검사한다.
  - `extraction/layout.py`: fragment 누락·추가·중복, block 필드, text 결합과
    문단 merge 경계를 직접 검사한다.
  - `domain/approved_translation.py`, `domain/translation_segment.py`: 정상
    round-trip과 스키마·ID·상태·고정 길이 hash 검증 실패를 직접 검사한다.
  - 완료 기준: 네 모듈의 핵심 규칙을 외부 파일·AI 호출 없이 재현하고,
    잘못된 입력의 구체적인 실패 조건까지 고정해야 한다.
  - 검증: 전용 단위 테스트 21개를 추가해 전체 269개 테스트, mypy 48개 파일과
    ruff 91개 Python 파일 검사를 통과했다.
- [ ] `QUALITY-004` mypy의 모듈 경계 검사를 활성화한다.
  - 현재 `follow_imports = "skip"`을 `normal`로 바꾸면 48개 대상에서 3개
    오류가 확인된다. `gemini_common.py`의 `PathLike` 경계와 layout·OCR
    provider의 `contents` 타입을 실제 SDK 계약에 맞게 정리한다.
  - 오류가 없는 파일부터 편입하는 장기 계획 대신 현재 확인된 3개 오류를
    한 묶음에서 수정한 뒤 설정을 `normal`로 전환한다.
  - 완료 기준: `follow_imports = "normal"` 상태에서 CI의 기존 48개 파일이
    mypy 0 오류이고 unittest·ruff도 통과해야 한다.
- [ ] `ERROR-003` 예외 문구 부분 검색에 의존하는 오류 판정을 도메인별
  안정적 코드로 교체한다.
  - 대상은 `error_response.py`의 detail 추론,
    `dashboard_job_service.py`의 provider 분류,
    `translation_retry_job_service.py`와 세 review server의 충돌·validation
    판정이다.
  - API 키 null byte, Windows 예약 이름과 OCR `[ILLEGIBLE]`처럼 사용자
    입력·본문을 검증하는 정상 문자열 검사는 범위에 포함하지 않는다.
  - `ERROR-004`와 `ERROR-005`에서 새 예외 코드를 먼저 도입하고 이후
    source·glossary·translation·review 순서로 기존 fallback을 축소한다.
  - 완료 기준: 예외 문구 부분 검색이 오류 코드, HTTP 상태 또는 사용자 메시지를
    결정하는 지점이 0개이고, 같은 예외 타입·code가 CLI와 HTTP에서 같은
    행동 안내를 만들어야 한다.
- [x] `DOMAIN-002` 번역 내용 검증 문제가 있는 `TranslationSegment`를
  `flagged` 상태로 기록한다.
  - `TRANSLATION_STATUSES`에는 `flagged`가 있지만 현재 생성 코드는 모든
    segment를 `translated`로 저장하고 문제 block ID는 실행 state의 개수로만
    남긴다. 이 상태는 최초 스키마부터 존재했으므로 새 회귀가 아니라 미완성
    도메인 계약이다.
  - `_build_translation_segments()`에서 숫자·token·용어 등 내용 검증 오류가
    있는 block은 `flagged`, 나머지는 `translated`로 저장하고 resume 때도
    같은 검증 결과를 재현한다.
  - 완료 기준: `translation.jsonl`만 읽어도 문제 block을 식별할 수 있고,
    flagged 결과도 현재처럼 번역 검수로 넘어가 사람이 수정할 수 있어야 한다.
    QA 통과와 최종 승인 뒤의 `approved_translation.jsonl` 계약은 변경하지 않는다.
  - 검증: 숫자·token 검증 문제가 있는 block만 `flagged`로 기록되고 정상
    block은 `translated`를 유지하며, partial resume·완료 cache에서 상태와
    오류 개수가 보존됨을 확인했다. 검수 수정 뒤 최종 산출물은 기존처럼
    `approved`로 생성되며 전체 289개 테스트와 mypy·ruff 검사를 통과했다.
- [x] `REPO-001` 테스트 샘플을 저장소 최상위에서 `docs/samples/` 또는
  `tests/fixtures/`로 옮긴다.
  - 수동 대시보드 확인용 PDF 1개와 이미지 2개를 `docs/samples/`로 옮기고
    `tmp_` 접두사를 용도가 드러나는 이름으로 바꾼다.
  - 샘플별 용도, 등록 조합과 실제 AI 호출 비용 가능성을 README에 안내한다.
  - 완료 기준: 저장소 최상위에 추적되는 PDF·이미지가 없고, 이동한 파일의
    형식·페이지 수·해시가 유지되어야 한다.
  - 검증: PNG 2개의 1800×1140 크기와 PDF의 2페이지 구성·이동 전후 SHA-256
    일치를 확인했고, 전체 269개 테스트와 mypy·ruff 검사를 통과했다.
- [~] `REPO-002` 배포·공개 범위를 결정한 뒤 LICENSE를 추가한다.
  - 현재는 LICENSE를 추가하지 않고 보류한다.
  - 배포 정책, 라이선스 종류와 저작권자 표기를 결정한 뒤 다시 진행한다.

## 제품 기능 후속 항목

안정성 개선과 별도로 관리하는 사용자 기능입니다. 아래의 다음 구현 계획을
마친 뒤 현재 제품 흐름과 함께 우선순위를 다시 결정합니다.

- [ ] `FEATURE-001` AI 설정 연결 테스트
  - 실제 Gemini 최소 호출과 비용 발생 가능성을 명시한다.
  - 로컬 형식 검사와 API 키·모델·권한·한도 진단을 구분한다.
- [ ] `FEATURE-002` 원문 검수 선택 영역 OCR 재인식과 입력창 자동 채움
- [ ] `FEATURE-003` 프로젝트 내보내기·가져오기 범위 결정 및 구현

## 다음 구현 계획

완료된 작업 순서는 각 항목의 상태와 검증 기록으로 보존합니다. 앞으로의 작업은
서로 연관된 항목을 다음 순서로 나눠 커밋합니다.

1. `QUALITY-004`: 확인된 3개 타입 오류 수정과 `follow_imports = "normal"` 전환
2. `ERROR-003`: source·glossary·translation·review 순서로 문구 추론 제거
3. `ARCH-006`: 선택한 use case를 각각 독립 커밋으로 분리
4. `DOMAIN-001`: 하이픈 결합 경고의 검수 화면 표시 방법 확정과 구현
5. 제품 기능 후속 항목의 우선순위 결정

`UPLOAD-001`은 실제 대용량 파일의 메모리 사용량을 측정하기 전까지, `REPO-002`는
배포 정책과 라이선스를 결정하기 전까지 구현 순서에 넣지 않습니다.

각 묶음은 관련 회귀 테스트, 전체 테스트, 필요한 경우 Orca 브라우저 검증을
통과해야 완료로 표시합니다.
