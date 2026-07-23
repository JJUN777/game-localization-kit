# 작업 히스토리와 인수인계

이 문서는 다른 컴퓨터나 새 세션에서 현재 구현 상태를 빠르게 확인하고 같은 지점부터 작업하기 위한 인수인계 기록입니다. 현재 실행 순서는 [전체 작업 흐름도](WORKFLOW.md), 세부 설계는 [번역 자동화 파이프라인 설계](TRANSLATION_AUTOMATION_DESIGN.md), 남은 체크리스트는 [개선 로드맵](IMPROVEMENTS.md)을 함께 참고합니다. 파이프라인 단계·명령·출력이 바뀌면 전체 작업 흐름도를 반드시 함께 갱신합니다.

## 현재 작업 기준

- 기록일: 2026-07-23
- 작업 브랜치: `improvement/local-source-20260723`
- 분기 기준 커밋: `fca2fb8` (`md 업데이트`)
- 분기 목적: 현재 로컬 소스를 유지하고, 마음에 들지 않았던 원격 `improvement/pipeline-hardening`의 후속 커밋을 자동 merge/rebase하지 않기 위함
- 로컬 `00_config.json` 변경은 개인 설정이므로 작업 커밋에서 제외
- API 키는 저장소에 기록하지 않고 각 컴퓨터의 `.env` 또는 `GEMINI_API_KEY` 환경변수로 설정

새 브랜치는 이 문서를 작성한 시점에는 로컬 브랜치입니다. 다른 컴퓨터에서 사용하려면 현재 변경을 커밋한 뒤 해당 브랜치만 명시적으로 push해야 합니다. 백업 브랜치를 포함한 `git push --all`은 사용하지 않습니다.

```bash
git push -u origin improvement/local-source-20260723
```

## 지금까지 완료한 작업

### 저장소와 보안

- 유출된 API 키가 포함됐던 Git 이력을 정리하고 원격 `main`을 안전한 스냅샷으로 재구성
- `00_config.json`에서 API 키 제거
- `.env`와 셸 환경변수 기반 `GEMINI_API_KEY` 로딩
- `.env`, workspace, PoC 출력, 가상환경과 임시 파일 Git 제외

### 통합 CLI와 프로젝트 workspace

- `pyproject.toml`과 `src/glk` 패키지 구성
- `glk` console script 제공
- `glk init`, `glk status` 구현
- project manifest와 `workspaces/<project_id>/` 구조 생성
- Windows/macOS 공통 경로 처리를 위해 `pathlib.Path` 사용

### PDF 원문 추출

- `glk extract` 구현
- PDF 원본 등록과 페이지 이미지 렌더링
- 좌표가 있는 text fragment 추출
- Gemini에는 원문 생성이 아닌 fragment ID의 읽기 순서와 block 묶음만 요청
- fragment 누락·중복 검증 후 코드가 원문을 재조립
- 입력 PDF, fragment, 모델, 프롬프트 버전 기반 페이지 캐시
- 다단 PDF와 시각적 줄바꿈 복원 PoC 완료

### 이미지 폴더 OCR

- `glk ocr` 구현
- PNG, JPEG, WebP 루트 폴더와 하위 폴더 재귀 탐색
- 하위 폴더 상대 경로를 원본과 개별 TXT 출력에 유지
- EXIF 방향 반영
- 공통 `ocr_prompt.txt`와 `이미지파일명.prompt.txt` sidecar 지원
- 요청마다 OCR 대상 이미지 한 장만 Gemini에 전달
- 참조 아이콘 이미지는 반복 전송하지 않고 시각적 설명과 `{TOKEN}` 매핑을 공통 prompt에 기록
- block, bbox, legibility, warnings 구조화 JSON 저장
- 이미지별 TXT와 `[파일명.txt]`/구분선 형식의 `combined.txt` 생성
- 이미지·공통 prompt·개별 prompt·모델·prompt version 기반 캐시
- 샘플 이미지 7장: 7/7 성공, 두 번째 실행 7/7 캐시 재사용

### 검수 직전까지 통합 실행

- `glk run`으로 원문 획득부터 검수용 TXT와 로컬 QA까지 통합 실행
- 대화형 실행에서 PDF 또는 이미지 폴더 선택
- PDF는 파일 경로만 질문하고 전체 페이지를 기본 처리
- 이미지 입력은 루트 폴더를 질문하고 하위 폴더를 재귀 처리
- 페이지 범위와 별도 OCR prompt는 각각 `--pages`, `--prompt` 선택 옵션으로만 제공
- 비대화형 실행에서는 `--input-type pdf|images` 지원
- `--file`만 있으면 PDF, `--folder`만 있으면 이미지 입력으로 자동 판정
- 이미 source가 등록된 프로젝트는 입력 유형과 경로를 manifest에서 자동 재사용
- PDF와 이미지 로직을 복제하지 않고 기존 `extract_project_pdf()`와 `ocr_project_images()` service 호출
- 획득 성공 후 기존 `segment_project_source()`와 `run_project_source_qa()`를 순서대로 호출
- 획득이 partial이면 후속 단계 중단
- 성공 시 `review/source.txt`, `qa/source_qa.md`와 다음 사람 작업 안내
- JSON 모드에서는 획득·정규화·QA 결과를 하나의 JSON object로 출력
- `glk status`에서 획득 여부, QA 상태와 issue 수, 검수 pending/stale/approved 상태 표시
- approved 상태는 review, final TXT와 approved JSONL의 저장된 hash가 모두 일치할 때만 인정
- 획득 summary의 `updated_at`, `cached_pages`, `cached_images`는 segmentation 입력 hash에서 제외해 반복 `glk run`의 중간 단계 캐시 유지

### 검수용 중간 source block

- `glk segment` 구현
- PDF `source/layouts/*.json`과 이미지 `source/ocr/results/*.json`을 같은 `SourceBlock` 모델로 변환
- 검수 전 중간 결과를 `segments/source.jsonl`에 JSONL로 저장
- PDF fragment 실측 bbox를 이미지 OCR과 같은 0~1000 범위로 정규화
- raw text, corrected text, 원본 파일, 페이지, 읽기 순서, block type, legibility, warnings와 원문 참조 저장
- ID는 원본 위치와 block 순서를 기준으로 생성해 text 수정만으로 변경되지 않음
- `source_hash`는 raw text 변경 감지에 사용
- 원문 획득 상태가 partial이면 segmentation을 거부
- 입력 JSON과 segmentation version 기반 캐시 및 원자적 파일 저장

### 로컬 원문 QA

- `glk qa` 구현
- LLM 또는 추가 API 호출 없이 `segments/source.jsonl` 전체 검사
- `[ILLEGIBLE]`, `[ICON: ...]`, replacement character 탐지
- malformed token, OCR prompt에 없는 token과 아이콘 파일명 평문 탐지
- 숫자와 같은 문자열에 섞인 `O/0`, `I/l/1` 후보 탐지
- identifier 형식과 중복 검사
- provider warning, uncertain legibility와 source hash 불일치 검사
- issue에 block ID, severity, evidence, 원본 파일, 페이지와 bbox 저장
- 원문 자동 수정 없음, 모든 `auto_fixable=false`
- 프로그램용 결과는 `qa/source_qa.json`, 사람이 읽는 결과는 `qa/source_qa.md`, 실행 상태는 `state/source_qa.json`에 저장
- source JSONL, OCR prompt와 QA version 기반 캐시
- 의미 판단이나 LLM 원본 재확인 단계는 현재 범위에서 제외

### 파일 기반 원문 검수와 최종화

- `glk segment` 실행 시 `draft/source.txt`와 `review/source.txt`를 같은 내용으로 생성
- draft는 자동 생성 기준본, review는 사람이 PDF·이미지를 보면서 일반 편집기로 수정하는 작업본
- PDF는 `[PAGE ...]`, 이미지는 `[SOURCE ...]` marker로 원본 위치 표시
- 모든 본문에 안정적인 `[BLOCK ...]` marker를 넣어 QA 보고서와 연결
- segmentation을 다시 실행해도 기존 review를 자동으로 덮어쓰지 않음
- 원문 hash가 변경되면 새 draft만 갱신하고 review를 stale로 표시해 최종화 차단
- `glk review prepare --force`를 명시한 경우에만 review를 현재 draft로 초기화
- `glk review finalize`에서 block 순서·marker·빈 본문·미해결 OCR 표시·token 구조 검증
- 아이콘 token 변경은 `--allow-token-changes`를 명시해야 허용
- `final/source.txt`와 최종 공통 원문 `segments/approved_source.jsonl` 생성
- approved JSONL은 raw text를 보존하고 실제 변경문만 `corrected_text`에 저장
- Windows CRLF 검토 파일도 동일하게 처리

## 현재 주요 명령

```bash
glk init "Rulebook" --project-id rulebook
glk status --project rulebook

# 진단·부분 재실행용 개별 단계
glk extract --project rulebook --file rulebook.pdf
glk ocr --project cards --folder card_images/

# 대화형 시작
glk run --project rulebook

# 비대화형 시작
glk run --project rulebook --input-type pdf --file rulebook.pdf
glk run --project cards --input-type images --folder card_images/

# 아래 두 명령은 glk run에 포함되며 필요할 때만 개별 실행
glk segment --project rulebook
glk qa --project rulebook

# review/source.txt를 일반 편집기로 수정하기 전/후
glk review prepare --project rulebook
glk review finalize --project rulebook --dry-run
glk review finalize --project rulebook

# 의도적으로 아이콘 token을 수정한 경우에만
glk review finalize --project rulebook --allow-token-changes

# 선택 옵션
glk run --project rulebook --input-type pdf --file rulebook.pdf --pages 1-10
glk run --project cards --input-type images --folder card_images/ --prompt prompts/icons.txt
```

## 출력 위치

PDF 프로젝트:

```text
workspaces/<project_id>/source/
├── original.pdf
├── pages/
├── fragments/
├── layouts/
├── document.json
└── extracted.txt
```

이미지 프로젝트:

```text
workspaces/<project_id>/source/
├── images/
├── ocr_prompt.txt
└── ocr/
    ├── individual/
    ├── results/
    ├── combined.txt
    └── run_summary.json
```

검수 및 최종 출력:

```text
workspaces/<project_id>/
├── segments/
│   ├── source.jsonl
│   └── approved_source.jsonl
├── draft/
│   └── source.txt
├── review/
│   └── source.txt
├── qa/
│   ├── source_qa.json
│   └── source_qa.md
└── final/
    └── source.txt
```

## 검증 상태

2026-07-23 기준 전체 49개 테스트가 통과합니다.

```bash
python -m unittest discover -s tests
```

실제 workspace dry-run 검증:

- 등록된 PDF 프로젝트에서 `glk run --project <id> --dry-run --json`이 PDF 유형과 전체 페이지를 자동 판정
- 등록된 이미지 프로젝트에서 같은 명령이 이미지 유형과 등록된 7장을 자동 판정
- API 키 없이도 `--dry-run` 가능
- 실제 PDF 4페이지를 검수용 중간 block 115개로 변환: ID 중복 0, bbox 범위 오류 0
- 실제 이미지 7장을 검수용 중간 block 60개로 변환: ID 중복 0, bbox 범위 오류 0, 아이콘 토큰 포함 block 28개
- PDF와 이미지 프로젝트 모두 두 번째 `glk segment` 실행에서 캐시 재사용
- 실제 PDF source block 115개 로컬 QA: issue 0개
- 실제 이미지 source block 60개와 허용 아이콘 token 30개 로컬 QA: issue 0개
- 단독 token block을 identifier로 오인한 초기 오탐 2개를 회귀 테스트와 함께 보정
- 두 프로젝트 모두 두 번째 `glk qa` 실행에서 캐시 재사용
- 실제 PDF 115개 block의 draft/review 동일 생성과 finalize 사전검증 통과
- 실제 이미지 60개 block의 draft/review 동일 생성과 finalize 사전검증 통과
- 실제 검토를 하지 않은 PoC에는 승인 결과를 쓰지 않고 `--dry-run`으로만 검증
- 통합 `glk run`의 획득 → 중간 정규화 → QA 순차 호출과 partial 중단 회귀 테스트 통과
- `glk status`의 pending, approved, stale 상태 판정 회귀 테스트 통과
- 실제 PDF 4페이지와 이미지 7장에서 통합 `glk run` 실행 성공, 각각 QA issue 0개와 사람 검수 pending 확인
- 실제 PDF 통합 명령 재실행에서 layout, segmentation, QA 캐시 재사용 확인

## 다른 컴퓨터에서 이어서 작업하기

현재 변경을 커밋·push한 다음 다른 컴퓨터에서 다음 순서로 준비합니다.

```bash
git fetch origin
git switch improvement/local-source-20260723
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

각 컴퓨터에서 추적되지 않는 `.env`를 만들고 키를 설정합니다.

```dotenv
GEMINI_API_KEY=your_api_key_here
```

그다음 다음 명령으로 기준 상태를 확인합니다.

```bash
python -m unittest discover -s tests
glk --help
glk run --help
glk review --help
git status --short --branch
```

`workspaces/`, 샘플 이미지 바이너리와 PoC 결과는 Git에서 제외되므로 다른 컴퓨터로 자동 전달되지 않습니다. 실제 원본 자료가 필요하면 Git 이외의 안전한 경로로 별도 복사해야 합니다.

## 바로 다음 작업

PDF와 이미지 OCR 결과의 검수용 중간 정규화, 로컬 QA와 파일 기반 사람 검수가 완료됐습니다. 다음 구현은 최종 공통 원문을 실제 용어 분석·번역 단계로 연결하는 것입니다.

1. `segments/approved_source.jsonl`이 없으면 용어 분석·번역을 거부
2. 승인 원문 기반 용어·고유명사·반복 표현 후보 추출
3. 사람이 확정한 용어집 파일 형식과 import 흐름 구현
4. 원문 block ID를 유지한 번역 세그먼트 생성
5. 용어집·숫자·아이콘 token을 검사하는 번역 QA 연결

아직 구현하지 않은 전체 번역, 용어 분석, 번역 QA와 export 명령은 성공한 것처럼 처리하지 않고 종료 코드 `3`을 반환합니다.
