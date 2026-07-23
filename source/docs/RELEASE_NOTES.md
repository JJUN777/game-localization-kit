# 릴리즈 노트

## v1.0.0 — 2026-07-23

Game Localization Kit의 첫 정식 릴리즈입니다.

PDF 룰북 또는 이미지 폴더에서 원문을 가져오고, 사람이 원문과 번역을 검수한 뒤 최종 한국어 TXT를 만드는 전체 흐름을 하나의 `glk` CLI로 제공합니다.

## 주요 기능

### 통합 CLI

- Windows와 macOS에서 동일한 `glk` 명령 사용
- 프로젝트별 독립 workspace 생성
- 프로젝트별 `01_input/pdf`, `01_input/images` 원본 투입 폴더 자동 생성
- 사용자 작업 결과를 `02_source`~`05_output` 단계별 폴더로 분리
- 캐시·상태·내부 JSONL을 직접 수정하지 않는 `.glk` 영역으로 분리
- 새 단계별 workspace를 manifest `schema_version: 2`로 명시
- 한쪽 입력 폴더에 원본이 있으면 `glk run`에서 종류와 경로 자동 감지
- `glk projects`로 프로젝트 ID, 입력 방식, 진행 단계와 완료 여부 조회
- 대화형 또는 명시적인 CLI 옵션으로 PDF·이미지 입력 선택
- 단계별 상태, 캐시와 stale 여부 확인

```bash
glk init "Project Name" --project-id project_id
glk projects
glk run --project project_id
glk status --project project_id
```

### PDF 원문 획득

- PDF 텍스트 fragment와 좌표 추출
- 1단·2단·3단을 미리 지정하지 않는 페이지별 읽기 순서 판정
- Gemini를 이용한 fragment 순서와 block 묶음 결정
- PDF 원문 fragment를 이용한 문장·문단 재조립
- fragment 누락·중복·알 수 없는 ID 검증
- 페이지별 결과 캐시와 선택 페이지 처리

### 이미지 폴더 OCR

- PNG, JPG, JPEG와 WebP 이미지 처리
- 하위 폴더 재귀 탐색과 출력 구조 보존
- 전체 이미지용 `ocr_prompt.txt`
- 특정 이미지용 `이미지파일명.prompt.txt`
- 아이콘을 `{HP}`, `{DEF}` 같은 token으로 기록하는 prompt 지원
- 이미지별 TXT와 하나의 통합 TXT 생성

### 원문 QA와 사람 검수

- PDF와 이미지 결과를 같은 block 구조로 정규화
- 자동 생성 기준본 `02_source/draft.txt`
- 사람이 수정하는 `02_source/review.txt`
- 숫자·아이콘 token·판독 불가 표시와 OCR 혼동 후보 검사
- marker, block 순서와 원문 hash 검증
- 사람이 승인한 최종 원문과 JSONL 생성

### 용어집

- 승인된 원문에서 반복 용어와 고유명사 후보를 로컬 규칙으로 수집
- Excel, Numbers와 일반 편집기에서 수정 가능한 TSV 생성
- `approved`, `keep`, `rejected` 상태 지원
- 자동 후보에 없는 수동 용어 추가
- 후보 ID, 중복, 원문 근거와 보호 token 검증
- 검증된 `termbase.json` 생성

### 초벌 번역

- 승인 원문 block ID를 유지하는 Gemini 번역
- hard rule, termbase, 프로젝트 번역 지침의 우선순위 적용
- 현재 번역 청크에 필요한 용어만 prompt에 포함
- 숫자, 아이콘 token, HTML tag와 확정 용어 검증
- 검증 실패 청크 재요청
- 청크별 체크포인트와 `--resume`
- 기존 사람 검토본을 자동으로 덮어쓰지 않는 stale 처리

### 번역 검수 화면

- localhost에서 실행되는 원문·번역 비교 화면
- block 검색과 오류·경고·수정됨 필터
- 번역 본문만 안전하게 저장
- 브라우저에서 로컬 QA 실행
- QA ERROR block만 Gemini로 선택 재번역
- 정상 block과 초벌 draft를 보존하고 교체 전·후 번역 revision 기록
- 오류가 없는 번역 최종 승인
- 외부 CDN·script 없이 동작하며 선택 재번역만 Gemini를 호출하는 로컬 UI

최종 결과:

```text
workspaces/<project_id>/05_output/translation.txt
```

## 구조와 보안

- 현재 사용하는 코드와 문서를 `source/`에 통합
- 이전 코드와 PoC 자료는 로컬 `legacy/`로 분리하고 Git 추적에서 제외
- project workspace 전체를 Git에서 제외
- API 키는 `source/.env` 또는 환경변수로만 관리
- 추적 파일에서 Google API 키 패턴 미검출 확인
- `.DS_Store`, IDE 설정, Python 캐시와 생성 결과 제거
- localhost 검수 서버에 session token, Host·Origin과 동시 편집 hash 검사 적용

## 설치

Python 3.10 이상이 필요합니다.

macOS/Linux:

```bash
cd source
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
glk version
```

Windows PowerShell:

```powershell
cd source
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
glk version
```

정상 출력:

```text
glk 1.0.0
```

## 검증

- 전체 자동 테스트 88개 통과
- CLI 설치와 `glk version` 확인
- PDF·이미지 원문 획득과 검수 흐름 회귀 검사
- 용어 후보 생성·import 검증 회귀 검사
- 번역 prompt, 청크 저장과 재개 회귀 검사
- 번역 검수 서버의 저장·QA·승인과 요청 차단 검사
- Markdown 링크와 코드 블록 검사
- Git 추적 파일의 API 키 패턴 검사
- Git 추적 대상에 `legacy/`와 workspace가 없음을 확인

## 알려진 제한사항

- 설치형 실행 파일과 데스크톱 GUI는 제공하지 않음

스캔 PDF와 텍스트·스캔 페이지가 섞인 Hybrid PDF는 전체 페이지를 번호가 붙은 이미지로 변환한 뒤 이미지 폴더 OCR 흐름으로 처리할 수 있습니다. 자세한 입력 방법은 [README](../README.md)를 참고합니다.

표와 자유 배치 구성의 읽기 순서는 자동 결과를 생성한 뒤 기본 원문 검수 단계에서 사용자가 원본과 비교해 조정합니다.

현재 제한사항에 대한 사용 방법과 대안은 [README](../README.md)를 참고합니다.
