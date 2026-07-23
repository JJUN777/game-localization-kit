# Game Localization Kit

PDF 룰북과 이미지 폴더에서 원문을 추출하고, 사람이 원문을 검수한 뒤 용어집·번역 단계로 연결하기 위한 크로스 플랫폼 CLI입니다.

현재 구현 범위는 **원문 획득 → 로컬 QA → 사람 검수 → 최종 원문 승인 → 용어 후보 검토 → termbase 생성 → ID 기반 초벌 번역 → 번역 QA와 최종 TXT 승인**까지입니다. QA 실패 segment 선택 재번역은 아직 구현 전입니다.

## 문서 안내

| 문서 | 용도 | 기준 |
|---|---|---|
| [문서 인덱스](docs/README.md) | 역할별 문서 구조와 기준 문서 안내 | 문서를 찾을 때 먼저 확인 |
| [전체 작업 흐름](docs/guides/workflow.md) | 프로젝트 생성부터 최종 번역 승인까지 실제 사용법 | 사용자 작업 순서의 단일 기준 |
| [아키텍처](docs/reference/architecture.md) | 코드 계층, 데이터 모델, 캐시와 승인 구조 | 현재 구현 구조의 단일 기준 |
| [용어집 검토 사양](docs/guides/glossary.md) | TSV 컬럼, 상태, 수동 용어, import 검증 규칙 | 용어 데이터 계약의 단일 기준 |
| [로드맵](docs/project/roadmap.md) | 미구현 작업과 우선순위 | 앞으로 할 작업의 단일 기준 |
| [인수인계](docs/project/handoff.md) | 현재 브랜치, 검증 상태와 바로 다음 작업 | 다른 컴퓨터·세션에서 재개할 때 사용 |
| [기록 보관소](docs/archive/README.md) | 초기 설계, PoC 결과와 과거 상세 체크리스트 | 참고용, 현재 동작 기준 아님 |

## 설치

Python 3.10 이상이 필요합니다. 모든 명령은 저장소 루트에서 실행합니다.

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

설치 확인:

```bash
glk --help
glk version
```

## API 키

저장소 루트에 추적되지 않는 `.env`를 만들고 Gemini API 키를 설정합니다. 셸이나 CI의 `GEMINI_API_KEY`가 `.env`보다 우선합니다.

```dotenv
GEMINI_API_KEY=your_api_key_here
```

키를 설정 JSON, 문서, 로그 또는 Git 이력에 넣지 않습니다.

## 첫 프로젝트 실행

프로젝트 이름은 사람이 읽는 이름이고, `project_id`는 경로와 CLI에서 사용하는 고정 식별자입니다.

```bash
glk init "Primal Rulebook" --project-id primal
glk run --project primal
```

`glk run`은 대화형으로 PDF 또는 이미지 폴더를 선택받고 다음 작업을 수행한 뒤 사람 검수 직전에 멈춥니다.

1. PDF fragment 추출·읽기 순서 복원 또는 이미지 폴더 OCR
2. PDF와 이미지 결과를 공통 source block으로 정규화
3. `draft/source.txt`와 `review/source.txt` 생성
4. LLM을 호출하지 않는 로컬 원문 QA 실행

비대화형 실행도 지원합니다.

```bash
glk run --project primal --input-type pdf --file rulebook.pdf
glk run --project cards --input-type images --folder card_images/
```

실행 후 `qa/source_qa.md`와 원본 PDF·이미지를 확인하면서 `review/source.txt`의 본문만 수정합니다. `draft/source.txt`는 자동 생성 기준본이므로 수정하지 않습니다.

```bash
glk review finalize --project primal --dry-run
glk review finalize --project primal
glk glossary build --project primal
# terminology/glossary_review.tsv를 검토한 다음
glk glossary import --project primal --file terminology/glossary_review.tsv
glk translate --project primal --dry-run
glk translate --project primal
# 브라우저에서 원문·번역 비교, 저장, QA와 최종 승인
glk translation review --project primal
glk status --project primal
```

상세한 파일 형식, 재실행과 stale 처리, 이미지 OCR prompt와 용어 검토 방법은 [전체 작업 흐름](docs/guides/workflow.md)을 따릅니다.

## 현재 명령 상태

실제 구현: `init`, `status`, `extract`, `ocr`, `run`, `segment`, `qa`, `review prepare`, `review finalize`, `glossary build`, `glossary import`, `translate`, `translation review`, `translation prepare`, `translation qa`, `translation finalize`

계획 상태: QA 실패 segment 선택 `retry`. 아직 연결되지 않은 명령은 성공으로 처리하지 않고 종료 코드 `3`을 반환합니다.

## 저장소 구조

기본 workspace는 `workspaces/<project_id>/`에 생성되고 Git에서 제외됩니다. 다른 루트를 사용하려면 관련 명령에 `--workspace-root PATH`를 동일하게 지정합니다.

```text
src/glk/        현재 사용하는 통합 CLI 코드
tests/          현재 CLI 회귀 테스트
docs/           가이드·아키텍처·로드맵·과거 기록
legacy/         통합 CLI 이전 코드와 PoC 자료
workspaces/     신규 프로젝트 작업 데이터 (Git 제외)
local-data/     예전 대용량 입력·출력 자료 (Git 제외)
```

신규 작업에서는 `src/glk/`와 `glk` 명령만 사용합니다. 예전 단일 스크립트의 범위와 주의사항은 [레거시 안내](legacy/README.md)를 확인합니다.
