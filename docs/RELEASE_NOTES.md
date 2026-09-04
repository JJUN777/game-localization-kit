# 릴리즈 노트

사용자에게 영향을 주는 기능, 동작 변경과 주요 안정성 개선을 버전별로
기록합니다.

## 다음 릴리즈

### 추가

- Gemini와 OpenAI 중 사용할 AI 제공자를 선택하는 대시보드 설정
- `GLK_AI_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_MODEL` `.env` 설정
- OpenAI Responses API 기반 PDF 레이아웃 분석, 이미지 OCR, 초벌 번역과
  QA 오류 문장 선택 재번역
- 제공자별 모델 목록, 안전한 오류 안내와 OpenAI adapter 자동 테스트
- Gemini 3.8 Flash를 기본 권장 모델로 추가하고 Gemini 2.x는 새 프로젝트용
  모델 목록에서 제거
- PDF 원문 block을 골라 원본 crop과 AI 제안을 비교·적용하는 `아이콘 검사(AI)`
- 아직 검토하지 않은 자동 용어 후보를 상태·번역어·분류·근거와 신뢰도로
  추천하고, 예상 비용 확인·미리보기·선택 반영·1회 취소를 제공하는
  `후보 1차 정리(AI)`
- 용어 후보 AI 정리의 청크별 후보·API 요청 진행률, 현재 단계와 실시간 경과 시간
- 동일 후보·언어·provider·model·prompt version의 AI 용어 추천을 다시 호출하지
  않는 후보별 fingerprint cache
- 프로젝트명·보드게임 규칙서 문맥과 `합니다체` 설명문 기준을 바탕으로 승인 원문의
  대표 block에서 번역 문체 지침을 제안하고, 호출 전 예상 token·비용과 호출 후
  실제 사용량, 게임 맥락 첫 문장, 명시적 미리보기·반영과 fingerprint cache를
  제공하는 `AI로 초안 만들기`
- 프로젝트별 AI 사용량 원장과 원문 추출·검수·용어 정리·번역·번역 검수의
  단계별 누적 예상 비용
- PDF 일부 페이지 처리 실패 시 성공 결과를 보존하고 재시도, `PDF 교체` 또는
  `검수에서 직접 수정`을 선택하는 복구 흐름

### 변경

- 대시보드 카드에는 현재의 다음 작업 하나만 강조하고, 이전 검수는 파이프라인
  단계에서 다시 열며 원문 다운로드·프로젝트 설정은 보조 메뉴로 분리
- 사용자가 바로 이해하고 수정할 수 있도록 기본 번역 문체 프롬프트를 한국어로 변경
- 용어 검수의 상시 수동 용어 허용 옵션을 없애고, 원문에서 찾을 수 없는 수동
  용어가 있을 때만 목록을 확인한 뒤 포함 여부를 결정하도록 확정 흐름을 단순화
- 용어 검수의 수동 용어 추가를 필터 줄로 분리하고, 일괄 상태 변경 도구는 행을
  선택했을 때 필터 줄을 같은 높이로 교체해 선택 개수·선택 해제와 함께 표시
- 용어 검수 헤더의 상태 통계를 클릭 가능한 필터 버튼으로 옮겨 프로젝트명과
  저장·확정 작업의 가독성을 개선
- 번역 검수의 상태 통계를 왼쪽 필터에 통합하고 상단에는 용어집·저장·현재 단계
  버튼만 유지하며, 오류 재번역·예외 승인은 조건부 더보기 메뉴로 정리
- 원문 검수를 `텍스트 편집`과 `블록 편집`으로 분리하고 Shift+클릭 범위 선택,
  일괄 제외·복원, 선택 묶음 drag 재정렬과 기존 `↑`·`↓` 이동을 함께 제공
- PDF와 block 카드에 페이지마다 1부터 다시 시작하는 작업 번호를 표시하고,
  PDF block 선택 시 대응하는 오른쪽 카드로 자동 이동
- 누락 문단 추가와 아이콘 검사는 모드별 상단 도구로, 저장·검증·최종 승인은
  별도 작업 줄로 정리
- 대시보드 단계명은 밑줄 친 검수 화면 링크로 표시하고, 비용 영역에만
  모델·요청·token hover 도움말을 제공하며 실행 중 소요 시간은 초 단위로 갱신
- 대시보드와 원문·용어·번역 검수 화면을 공통 디자인 token 기반의 일관된
  비주얼로 정리하고 운영체제 설정을 따르는 자동 다크 모드 적용
- OpenAI 모델 목록을 flagship `gpt-5.6-sol`, 균형형 `gpt-5.6-terra`, 대량
  처리형 `gpt-5.6-luna` 순서로 정렬
- Gemini 3.8에서 폐기된 sampling parameter를 제거하고 `google-genai` 최소
  버전을 2.22로 갱신

### 안정성

- 동일한 원문·영역·페이지 이미지·아이콘 정의·provider·model·prompt version의
  아이콘 검사 결과를 재사용해 중복 API 호출과 비용 기록 방지
- 검수 작업물이 없는 부분 실패 상태에서만 PDF 교체를 허용하고, 사람이 만든
  원문 block 또는 후속 결과가 있으면 원본 교체를 차단
- background job polling 중 카드 전체를 다시 만들지 않고 진행 표시만 부분
  갱신해 카드 내부 hover와 조작 상태 유지

### 호환성

- 제공자를 지정하지 않은 기존 `.env`는 Gemini를 기본값으로 계속 사용
- Gemini와 OpenAI 키·모델을 분리 보존해 제공자 전환 시 기존 설정 유지
- 기존 설정에 저장된 Gemini 2.x 모델 ID는 사용자 지정 모델로 계속 사용 가능

## v2.0.0 — 2026-07-26

CLI 중심이던 1.x 흐름에 로컬 웹 대시보드를 추가하고, 프로젝트 생성부터 원문
준비·용어 검수·번역 검수·결과 저장까지 하나의 GUI에서 진행할 수 있게 한
릴리즈입니다.

### 추가

- `glk ui` 로컬 대시보드와 프로젝트 검색·상태 필터·진행 단계 표시
- 프로젝트 생성, 휴지통 삭제, PDF 한 개 또는 이미지 여러 장 등록
- 원문 처리 시작 전 원본 교체와 OCR 프롬프트 독립 저장·수정
- API 키 설정 여부, Gemini 모델 목록·직접 입력을 제공하는 AI 설정
- 원문 준비, 용어 후보 생성, 초벌 번역의 background job과 진행률 복원
- 원문 검수의 PDF·이미지 원본 비교, block 이동·제외·수동 추가와 최종 승인
- 용어 검수의 검색 범위, 필터·정렬·상태 일괄 변경과 수동 용어 추가
- 번역 프롬프트 사전 편집, 청크 이어하기와 기존 결과를 보존하는 전체 재번역
- 번역 검수의 적용 용어 확인, 수정 가능한 QA 경고와 오류 block 선택 재번역
- 최종 승인된 PDF TXT, 이미지 통합본 TXT와 이미지별 결과 ZIP 저장

### 변경

- 번역의 내용 검증 문제는 결과를 폐기하지 않고 번역 검수 단계로 전달
- `keep` 용어와 승인 번역어를 Gemini prompt에 명시하고 로컬 QA와 같은 규칙 적용
- 이미지별 다운로드 버튼을 나열하지 않고 통합본·전체 ZIP 두 버튼으로 단순화
- 이미지 통합본의 원본 경계를 내부 경로 없이 `[원본 파일명]`으로 표시
- 지원 브라우저에서는 저장 위치를 먼저 선택하고 Safari 등 미지원 브라우저는
  기본 다운로드 방식으로 전환
- `glk ui` 기본 주소를 `http://127.0.0.1:8765/`로 고정하고 포트 충돌 시
  다른 포트 지정 방법 안내
- Gemini provider의 환경 로딩, timeout·재시도와 오류 분류를 공통 기반으로 통합
- localhost 검수 서버와 대시보드의 인증·보안 헤더·라우팅 기반을 통합
- 중복된 작업 문서를 현재 사양과 버전별 릴리즈 기록으로 정리하고 수동 smoke
  test 원본을 `examples/smoke/`로 이동

### 안정성

- 원본 교체 실패 시 백업 보존과 rollback 경로 안내
- 프로젝트 mutation과 background job 시작 사이의 경합 제거
- 번역 청크 durable append·checkpoint 복구와 용어 후보 생성 중단 복구
- 승인 파일의 경로와 SHA-256을 표시·전송·ZIP 생성 직전에 재검증
- Windows 임시 파일 정리, 경로 종류와 background job 종료 경합 보완
- 모달이 열린 상태에서도 오류 토스트가 가려지지 않도록 표시 계층 보완
- PDF·OCR·번역·검수의 책임 분리와 명시적 오류 코드 기반 사용자 안내

### 호환성과 검증 기준

- Python 3.10 이상
- Windows와 macOS의 Python 3.10·3.14 CI
- 전체 unittest 303개
- 프로젝트 설정 범위 mypy 49개 파일
- 전체 Python 소스 Ruff 검사
- 실제 Gemini API 호출 없이 자동 검증하며 수동 API 확인은
  `examples/smoke/` 원본 사용

### 알려진 제한사항

- 영어 원문에서 한국어로 번역하는 Gemini API 흐름만 지원
- 프로젝트당 PDF 한 개
- 스캔 PDF 직접 OCR과 background job 취소 미지원
- 네이티브 데스크톱 앱과 설치형 실행 파일 미제공
- 저장소 라이선스는 배포·공개 범위를 결정한 뒤 추가

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
- 아이콘을 `[HP]`, `[DEF]` 같은 token으로 기록하는 규칙 지원

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
