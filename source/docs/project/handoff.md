# 현재 작업 인수인계

이 문서는 다른 컴퓨터나 새 세션에서 같은 지점부터 작업하기 위한 짧은 현재 상태 기록입니다. 상세 사용법은 [전체 작업 흐름](../guides/workflow.md), 미구현 목록은 [로드맵](roadmap.md)을 따릅니다.

## 작업 기준

- 갱신일: 2026-07-23
- 브랜치: `improvement/local-source-20260723`
- 원격 추적: `origin/improvement/local-source-20260723`
- 현재 정리 기준 커밋: `8e6e218` (`v1.0 버전 업데이트`)
- 목적: 현재 로컬 소스를 기준으로 작업하고 이전 `improvement/pipeline-hardening` 후속 변경을 자동 병합하지 않음
- API 키는 Git에 넣지 않고 각 컴퓨터의 `.env` 또는 환경변수로 설정

최상위 폴더 재구성과 관련 경로 변경은 이 문서 갱신 시점에 아직 커밋·push하지 않았습니다. 이동 전에 관련 변경을 검토해 커밋하고 현재 브랜치만 push합니다. `git push --all`은 사용하지 않습니다.

## 저장소 정리 상태

```text
source/
├── README.md
├── pyproject.toml
├── src/glk/      현재 사용하는 통합 CLI 코드
├── tests/        현재 CLI 회귀 테스트
├── docs/         현재 가이드·아키텍처·로드맵
└── workspaces/   신규 프로젝트 데이터 (Git 제외)
```

활성 README·코드·테스트·문서는 모두 `source/`에 있습니다.

## 현재 구현 상태

구현된 CLI:

```text
init, status, extract, ocr, run, segment, qa,
review prepare, review finalize, glossary build, glossary import, translate,
translation review, translation prepare, translation qa, translation finalize
```

현재 파이프라인은 다음 지점까지 동작합니다.

```text
PDF/이미지 원문 획득
→ 공통 SourceBlock
→ 로컬 원문 QA
→ 파일 기반 사람 검수
→ 최종 공통 원문 승인
→ 검토용 용어 후보 TSV
→ 검증된 termbase
→ ID 기반 Gemini 초벌 번역과 draft/review TXT
→ localhost HTML 원문·번역 대조와 편집
→ 로컬 번역 QA
→ 사람 수정이 분리된 승인 번역 JSONL과 최종 TXT
```

기존 번역 정렬과 QA 실패 segment 선택 재번역 명령은 아직 구현 전입니다. 최종 산출물은 `final/translation.txt`로 충분하므로 별도 export 단계는 범위에서 제외했습니다.

## 검증 상태

2026-07-23 기준 전체 80개 테스트가 통과합니다.

```bash
.venv/bin/python -m unittest discover -s tests
```

확인된 주요 회귀 범위:

- PDF·이미지 원문 획득과 block 정규화
- 반복 실행 시 layout, OCR, segmentation과 QA 캐시
- QA issue와 review pending/stale/approved 판정
- marker·token·hash를 검사하는 review finalize
- 승인되지 않은 원문의 glossary build 차단
- Hunter/Hunters 같은 보수적 단수·복수 후보 병합
- glossary TSV 사람 편집 보존, 설정 변경 stale와 `--force` 초기화
- 검토 미완료·필드 오류·자동 candidate ID 삭제·변조 import 차단
- 수동 용어 근거 보충, 미검증 선등록 예외와 기존 termbase 보존
- 승인 원문·TSV·termbase hash 기반 current/stale 상태 판정
- hard rules → termbase → project prompt 우선순위와 관련 용어 선별
- 번역 응답 ID·순서·숫자·token·HTML·용어 검증과 재요청
- 청크별 원자적 체크포인트, partial 상태와 `--resume`
- source block ID 기반 translation JSONL과 draft/review TXT 생성
- 강제 재번역 시 기존 사람 review 보존과 stale 판정
- 번역 review의 marker·source·ID·순서 변조 차단
- 숫자·token·HTML·확정 용어 번역 QA와 리포트 hash 판정
- 초벌 번역 보존, 사람 수정 분리와 오류 없는 최종 번역 승인
- localhost HTML 로드, block별 저장, QA와 최종 승인 API
- 외부 Origin·무인증 요청·알 수 없는 block·동시 편집 충돌 차단
- Orca 내장 브라우저에서 수정 표시·필터·QA 통과·최종 승인까지 실제 조작 확인

workspace와 로컬 원본은 Git에서 제외되므로 다른 컴퓨터에 자동 전달되지 않습니다.

## 바로 다음 작업

사용자가 보유한 기존 초벌 번역을 현재 source block에 안전하게 정렬하는 흐름을 구현합니다. 세부 체크리스트는 [로드맵](roadmap.md)에 있습니다.

권장 구현 순서:

1. 가져올 기존 번역의 입력 형식과 page/key/text 식별자 정의
2. source block과 exact·normalized·fuzzy 후보 정렬
3. confidence와 근거를 포함한 검토 리포트 생성
4. 낮은 confidence 결과의 자동 병합 차단
5. 사람이 확정한 정렬만 review translation에 반영

## 다른 컴퓨터에서 재개

```bash
git fetch origin
git switch improvement/local-source-20260723
cd source
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m unittest discover -s tests
glk status --project <project_id>
git status --short --branch
```

Windows PowerShell에서는 가상환경만 다음과 같이 활성화합니다.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

각 컴퓨터의 `source/`에 추적되지 않는 `.env`를 별도로 만듭니다.

```dotenv
GEMINI_API_KEY=your_api_key_here
```

작업을 재개하기 전에 `git status --short --branch`로 사용자 파일을 확인하고, 관련 없는 로컬 변경을 덮어쓰지 않습니다.
