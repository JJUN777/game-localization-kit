# 릴리즈 노트

## 다음 릴리즈 준비 중 — 2026-07-26

`v1.1.0` 이후 로컬 웹 대시보드와 전체 GUI 작업 흐름을 추가하고, background
job·파일 저장·오류 처리의 안정성을 보강했습니다. 아직 새 버전 번호와 배포
정책은 확정하지 않았습니다.

### 주요 변경사항

- `glk ui`에서 프로젝트 생성·휴지통 삭제, PDF 한 개 또는 이미지 여러 장 등록,
  처리 전 원본 교체를 지원
- 대시보드 `AI 설정`에서 API 키 설정 여부와 실제 Gemini 모델 ID를 관리
- 원문 준비, 용어 후보 생성과 초벌 번역을 단일 active background job 정책으로
  실행하고 진행률·실패·중단 상태를 저장
- 원문 승인 뒤 용어 후보 생성, termbase 확정 뒤 초벌 번역으로 이어지는 전체
  GUI 흐름 연결
- OCR·번역 프롬프트의 사전 편집, 번역 청크 이어하기와 기존 결과를 revision에
  보관하는 전체 재번역 지원
- 번역 검증 문제를 결과 폐기 대신 검수 화면의 수정 가능한 QA 항목으로 전달
- 용어 검수 검색·정렬·일괄 상태 변경·수동 용어 추가와 번역 검수의 적용 용어
  표시·오류 선택 재번역 개선
- 최종 승인 hash가 일치하는 `05_output` 파일만 대시보드에서 다운로드
- localhost 서버 보안, 동시 변경 차단, 원자적 저장·복구, job 상태 공통화,
  Gemini provider 재시도·환경 로딩과 오류 분류 공통화

### 문서 구조

- `README.md`: 설치, GUI·CLI 빠른 시작과 문서 안내
- `docs/GUI.md`: 대시보드와 세 검수 화면의 단일 사용 가이드
- `docs/WORKFLOW.md`: CLI, 파일과 상태 전이 기준
- `docs/ARCHITECTURE.md`: 코드·데이터·보안 경계
- `docs/IMPROVEMENTS.md`: 현재 안정화 작업과 완료 내역
- `docs/BACKLOG.md`: 나중에 검토할 제품 기능·실측·정책 항목
- `DASHBOARD_WORK_HISTORY.md`: 현재 사양과 분리한 과거 구현 기록

### 현재 검증 기준

- 전체 269개 unittest
- `mypy` 설정 범위 48개 Python 파일
- `ruff` 검사 대상 91개 Python 파일
- Windows와 macOS의 Python 3.10·3.14 CI
- 실제 Gemini API 호출과 API 키 없이 자동 검사

---

## v1.1.0 — 2026-07-24

v1.0 번역 흐름을 그대로 유지하면서 설치 호환성, 파일 저장 안정성, 오류 안내와 개발 검증 체계를 강화한 유지보수 릴리즈입니다.

기존 workspace 구조와 `glk` 명령은 변경되지 않으므로 별도 마이그레이션 없이 기존 프로젝트를 계속 사용할 수 있습니다.

### 주요 변경사항

#### 파일 처리 안정성

- application service에 반복되던 atomic write, JSON write와 파일 복사 로직을 공통 `_io.py`로 통합
- 대상 파일과 같은 폴더에 고유 임시 파일을 생성해 동시 실행 시 `.tmp` 이름 충돌 방지
- 기록 실패 시 임시 파일을 정리하고, 파일 교체 전 `flush`와 `fsync` 수행
- byte와 파일 SHA-256 계산을 공통 `_hashing.py`로 통합

#### 번역 모듈 구조

- 번역과 선택 재번역이 공유하는 승인 원문, termbase, 번역 prompt 로딩을 `_translation_context.py`로 분리
- 선택 재번역 service가 번역 service의 private 함수를 직접 import하던 결합 제거
- 공통 번역 예외와 provider 계약을 `translation_types.py`로 분리

#### 사용자 오류 안내

- CLI와 원문·용어·번역 검수 서버의 실패 응답을 `code`, 한글 `message`, 기술 진단용 `detail` 구조로 통일
- 검수 충돌, 세션 만료, API 키 누락, 용어 미확정과 입력 경로 오류에 해결 방법 안내
- 일반 CLI는 한글 오류를 표시하고 `--verbose`에서 상세 진단 출력

#### 설치 및 호환성

- `google-genai`, Pillow, PyPDF2, PyMuPDF, python-dotenv의 지원 버전 범위 명시
- Python 3.10에서 해석할 수 없던 최신 f-string 문법 제거
- Windows와 macOS에서 Python 3.10·3.14를 자동 검증하는 GitHub Actions 추가
- push, pull request와 수동 실행에서 설치, 의존성, 구문, 전체 테스트와 CLI entry point 검사

#### 타입 안정성

- 원문·용어·번역 검수 API document와 오류 payload에 `TypedDict` 계약 적용
- 검증 전 외부 JSON과 검증이 끝난 browser document의 타입 경계 분리
- Python 3.10을 기준으로 주요 review API 경계 8개 파일을 `mypy`로 검사
- 정적 검사에서 발견한 nullable PDF 페이지, server host, 분기별 결과 타입 문제 정리

### 검증 결과

- 전체 111개 unittest 통과
- Windows Python 3.10 / 3.14 통과
- macOS Python 3.10 / 3.14 통과
- review API `mypy` 검사 통과
- `pip check`, Python 구문 검사, `glk --version`, `glk --help` 통과
- 실제 Gemini API 호출과 API 키 없이 모든 CI 검사 수행

### 호환성

| 항목 | v1.1.0 |
|---|---|
| Python | 3.10 이상 |
| 운영체제 | Windows, macOS |
| 기존 workspace | 그대로 사용 가능 |
| CLI 명령 | v1.0과 호환 |
| 설정 마이그레이션 | 필요 없음 |

### 알려진 제한사항

| 제한 | 대안 |
|---|---|
| 설치형 실행 파일과 데스크톱 GUI 없음 | CLI + 브라우저 검수 화면 사용 |
| 한국어 번역 전용 | 다른 언어 지원은 향후 검토 |
| Gemini API만 지원 | 다른 LLM provider 미지원 |
| 프로젝트당 PDF 1개 | 여러 PDF는 프로젝트를 나눠서 처리 |
| 스캔 PDF 직접 처리 불가 | 페이지를 이미지로 변환 후 이미지 OCR 흐름 사용 |
| 표·자유 배치의 읽기 순서 자동 복원 한계 | 원문 검수 단계에서 사람이 순서 조정 |

---

## v1.0.0 — 2026-07-23

Game Localization Kit의 첫 정식 릴리즈입니다.

PDF 룰북 또는 이미지 폴더에서 원문을 가져오고, 사람이 원문과 번역을 검수한 뒤 최종 한국어 TXT를 만드는 전체 흐름을 하나의 `glk` CLI로 제공합니다.

---

## 핵심 요약

```text
입력:  PDF 룰북 1개 또는 이미지 폴더
출력:  workspaces/<project_id>/05_output/*_kor.txt
모델:  gemini-2.5-flash (기본)
플랫폼: Windows, macOS
```

```bash
pip install -e .
glk version   # → glk 1.0.0
```

---

## 주요 기능

### 통합 CLI

- Windows와 macOS에서 동일한 `glk` 명령
- 프로젝트별 독립 workspace와 `01_input` ~ `05_output` 단계별 폴더 구조
- 캐시·상태·내부 JSONL을 `.glk` 영역으로 분리
- `01_input`을 프로젝트의 유일한 원본 저장소로 사용
- 한쪽 입력 폴더에 원본이 있으면 `glk run`에서 자동 감지
- `glk projects`로 전체 프로젝트 목록과 진행 단계 조회
- 원문·용어·번역 검수를 `glk review source|glossary|translation`으로 통일

### PDF 원문 획득

- PDF 텍스트 fragment와 좌표 추출
- 페이지별 읽기 순서 판정 (1단·2단·3단 사전 지정 불필요)
- Gemini를 이용한 fragment 순서와 block 묶음 결정
- fragment 집합 검증 실패 시 해당 페이지 최대 2회 재시도 (최초 포함 총 3회)
- 재요청 실패 시 임의 보정 없이 partial 처리
- 페이지별 캐시와 선택 페이지 처리

### 이미지 폴더 OCR

- PNG, JPG, JPEG, WebP 지원
- 하위 폴더 재귀 탐색과 출력 구조 보존
- 공통 `ocr_prompt.txt`와 이미지별 `파일명.prompt.txt`
- 아이콘 30종 상세 prompt를 새 프로젝트에 자동 생성
- 아이콘을 `{HP}`, `{DEF}` 같은 token으로 기록하는 규칙 지원

### 원문 검수

- PDF와 이미지 결과를 같은 SourceBlock 구조로 정규화
- 자동 기준본 `draft.txt`와 사람이 수정하는 `review.txt` 분리
- localhost 브라우저 화면에서 원본 이미지와 추출문을 나란히 비교
- block 본문 수정, 순서 변경, 제외, 원본 bbox 지정 신규 block 추가
- 숫자·아이콘 token·판독 불가 표시와 OCR 혼동 후보의 로컬 QA
- marker, block 순서와 원문 hash 검증 후 최종 승인

### 용어집

- 승인 원문에서 로컬 규칙으로 반복 용어와 고유명사 후보 수집
- 수량 접두사·기능어·중첩 후보를 정제하는 v2 규칙
- `glk review glossary` localhost 검수 화면 (표 편집, 필터, 일괄 변경, 수동 추가)
- TSV hash 충돌 방지와 자동 후보 삭제 방지
- `approved`, `keep`, `rejected` 상태와 검증된 `termbase.json` 생성

### 초벌 번역

- 승인 원문 block ID를 유지하는 Gemini 번역
- hard rule → termbase → 프로젝트 지침 우선순위
- 현재 청크에 필요한 용어만 prompt에 포함
- 숫자, token, HTML tag, 확정 용어 검증과 실패 시 재요청
- 청크별 체크포인트와 `--resume`
- 사람 검토본을 자동으로 덮어쓰지 않는 stale 처리

### 번역 검수

- localhost 원문·번역 비교 화면
- block 검색, 오류·경고 필터, 번역 본문만 안전 저장
- QA ERROR block만 Gemini로 선택 재번역
- 정상 block과 초벌 draft 보존, revision 기록
- QA 코드는 영문 유지, HTML·보고서의 사유는 한글과 실제 차이값 표시
- 오류 0개일 때 최종 승인과 `05_output/*_kor.txt` 생성
- 최종 TXT에서 page·source 경계 유지, block·GLK marker 제거

---

## 구조와 보안

- 활성 코드와 문서를 `src/`, `tests/`, `docs/`로 정리
- 이전 코드와 PoC 자료인 `legacy/` 제거
- project workspace와 `.env`를 Git에서 제외
- API 키는 `.env` 또는 환경변수로만 관리, 추적 파일에서 키 패턴 미검출 확인
- localhost 검수 서버에 session token, Host·Origin 검사, 동시 편집 hash 검사 적용
- 외부 CDN·script 미사용, 선택 재번역 때만 Gemini 호출

---

## 검증 결과

### 자동 테스트

- 전체 104개 테스트 통과
- CLI 설치와 `glk version` 확인
- PDF·이미지 원문 획득과 검수 흐름 회귀 검사
- 용어 후보 생성·HTML 검수 저장·import 검증 회귀 검사
- 번역 prompt, 청크 저장과 재개 회귀 검사
- 원문·용어·번역 검수 서버의 저장·QA·승인, 원본 asset 제공과 요청 차단 검사
- Git 추적 파일의 API 키 패턴 검사

### End-to-end 검증

| 프로젝트 | 입력 | 결과 |
|---|---|---|
| PDF 룰북 샘플 | PDF 룰북 2페이지 (61 block, 2,742자) | 원문 추출 → QA → 검수 승인 → 용어 3개 확정 → 초벌 번역 → QA 통과 → 최종 TXT 생성 완료 |
| 이미지 OCR 샘플 | 카드 이미지 5장 (932×1270px, 아이콘 30종) | OCR → 검수 승인 → 용어 확정 → 번역 → 최종 TXT 생성 완료 |

두 프로젝트 모두 `glk init` → `glk run` → 검수 → 용어 → 번역 → 최종 승인의 전체 흐름을 사람 개입 포함해서 완주했습니다.

### 추가 확인

- Markdown 링크와 코드 블록 정상 렌더링 확인
- Git 추적 대상에 `legacy/`와 workspace가 없음을 확인
- macOS zsh에서 설치·실행 확인
- Windows PowerShell용 설치 명령과 크로스 플랫폼 경로 처리 테스트

---

## 설치

Python 3.10 이상이 필요합니다.

```bash
# macOS/Linux
cd game-localization-kit
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
glk version   # → glk 1.0.0

# Windows PowerShell
cd game-localization-kit
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
glk version   # → glk 1.0.0
```

---

## 알려진 제한사항

| 제한 | 대안 |
|---|---|
| 설치형 실행 파일과 데스크톱 GUI 없음 | CLI + 브라우저 검수 화면 사용 |
| 한국어 번역 전용 | 다른 언어 지원은 향후 검토 |
| Gemini API만 지원 | 다른 LLM provider 미지원 |
| 프로젝트당 PDF 1개 | 여러 PDF는 프로젝트를 나눠서 처리 |
| 스캔 PDF 직접 처리 불가 | 페이지를 이미지로 변환 후 이미지 OCR 흐름 사용 |
| 표·자유 배치의 읽기 순서 자동 복원 한계 | 원문 검수 단계에서 사람이 순서 조정 |

자세한 사용 방법과 대안은 [README](../README.md)를 참고합니다.
