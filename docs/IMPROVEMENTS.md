# 게임 로컬라이제이션 키트 개선 로드맵

이 문서는 현재 코드베이스에서 확인된 개선 작업을 우선순위와 구현 단계별로 정리한 체크리스트입니다.

통합 CLI와 초벌 번역 개선 흐름의 설계는 [번역 자동화 파이프라인 설계 초안](TRANSLATION_AUTOMATION_DESIGN.md)을 참고합니다.
PDF 다단·줄바꿈 복원 실험은 [PDF 레이아웃 복원 PoC](LAYOUT_RECONSTRUCTION_POC.md)를 참고합니다.

## 완료된 작업

- [x] 개선 작업용 `improvement/pipeline-hardening` 브랜치 생성
- [x] Git 이력 정리 및 원격 `main` 재구성
- [x] `00_config.json`에서 API 키 제거
- [x] 로컬 `.env`의 `GEMINI_API_KEY` 사용
- [x] 셸/CI 환경변수가 `.env`보다 우선하도록 구성
- [x] `.env`, 가상환경, 로그, 임시 파일 등을 `.gitignore`에 추가
- [x] `python-dotenv` 의존성 및 README 설정 방법 추가
- [x] PDF 좌표 기반 fragment 추출 및 페이지 렌더링 PoC
- [x] 로컬 recursive XY-cut과 이미지 기반 layout 판정 비교 PoC
- [x] fragment 누락·중복 방지 검증 및 시각적 줄바꿈 결합 PoC
- [x] 이미지 폴더 Gemini OCR PoC 및 7장 샘플 검증
- [x] 아이콘 참조 이미지를 반복 전송하지 않는 텍스트 설명형 OCR 프롬프트
- [x] 이미지별 TXT와 구분선 기반 통합 TXT 생성
- [x] `glk ocr` 프로젝트 서비스, 원본 등록, 결과 캐시 및 상태 기록

## P0: 원문 획득 및 OCR

- [ ] PDF를 페이지 이미지로 렌더링하고 원본 페이지 순서를 보존한다.
- [ ] 페이지별로 내장 텍스트, OCR, hybrid 처리 여부를 자동 판정한다.
- [ ] 텍스트 레이어가 없는 스캔 PDF를 자동으로 OCR 대상으로 전환한다.
- [x] 여러 이미지 파일을 하나의 문서로 ingest하고 자연 정렬 순서를 보존한다.
- [x] 이미지 OCR 전에 EXIF 방향을 반영한다.
- [ ] 자동 회전, 기울기, 여백, 대비를 OCR 전에 보정한다.
- [x] 이미지 OCR 결과를 block/bbox/reading order 구조로 저장한다.
- [x] 이미지 OCR provider를 교체할 수 있는 공통 인터페이스를 만든다.
- [ ] 내장 텍스트와 OCR 결과가 크게 다른 페이지를 표시한다.
- [ ] 다단 편집, 표, 사이드바, 캡션의 읽기 순서 오류를 탐지한다.
- [ ] 반복 머리말·꼬리말과 페이지 번호를 본문과 구분한다.
- [ ] `0/O`, `1/I/l`, `5/S` 등 OCR 혼동 후보를 탐지한다.
- [ ] 숫자, 부정어, 카드·지도 참조의 OCR 의심 항목을 높은 심각도로 분류한다.
- [x] 이미지 원본과 raw OCR JSON 및 파생 TXT를 함께 보존한다.
- [ ] corrected text와 원문 수정 이력을 추가로 보존한다.
- [ ] 원문 검수용 HTML 리포트를 생성한다.
- [ ] 원문 수정 이력과 승인 상태를 기록한다.
- [ ] 승인되지 않은 원문 block이 번역 단계로 넘어가지 않게 한다.
- [ ] 텍스트 PDF, 스캔 PDF, hybrid PDF, 이미지 폴더 fixture를 추가한다.

완료 조건:

- 어떤 번역 문장도 원본 페이지와 OCR block 위치를 역추적할 수 있다.
- OCR 오류와 읽기 순서가 검수되기 전에는 번역이 시작되지 않는다.

## P0: 파이프라인 정확성

### 파일 매칭

- [ ] `_clean_ko.txt`에서 원본 PDF 이름을 올바르게 복원한다.
- [ ] 파일명 문자열 치환 대신 입력과 출력 관계를 manifest로 관리한다.
- [ ] 원본 파일과 `_clean.txt`가 동시에 번역되는 중복 처리를 방지한다.
- [ ] 모든 처리 스크립트에 공통 `--file` 선택 옵션을 제공한다.
- [ ] 입력 파일이 없거나 이름이 맞지 않을 때 가능한 후보 경로를 오류에 표시한다.

완료 조건:

- `TES_Rulebook.pdf` → `TES_Rulebook_clean_ko.txt` → `TES_Rulebook_formatted.txt` 흐름이 자동으로 연결된다.
- 원본과 정제본 중 하나만 번역 대상으로 선택된다.

### 실패 처리

- [ ] 포맷팅 실패 페이지를 `[FORMAT ERROR]`와 함께 최종 파일로 확정하지 않는다.
- [ ] 한 페이지라도 실패하면 `.tmp`를 유지하고 실패 종료 코드를 반환한다.
- [ ] 빈 Gemini 응답을 성공으로 처리하지 않는다.
- [ ] 이미지 저장 함수의 실패 반환값을 전체 실패 목록에 반영한다.
- [ ] 이미지 입력 디렉터리를 `os.listdir()` 호출 전에 검사한다.
- [ ] 부분 성공, 전체 성공, 전체 실패를 서로 다른 종료 코드로 구분한다.

완료 조건:

- 실패 결과가 정상 출력 파일로 오인되지 않는다.
- 실패한 항목만 다시 실행할 수 있다.

## P0: 번역 결과 무결성

- [ ] 입력과 출력의 `[PAGE n]` 집합을 비교한다.
- [ ] identifier/key 라인의 누락, 추가, 순서 변경을 검사한다.
- [ ] HTML 및 rich text 태그 보존 여부를 검사한다.
- [ ] `[NEWLINE]`, `[hp]`, `[time]` 등의 bracket token을 검사한다.
- [ ] 숫자, 카드 번호, Script/Secret/Map 참조 변경을 검사한다.
- [ ] 입력과 출력의 줄 수 및 빈 줄 구조 차이를 리포트한다.
- [ ] `keep_terms`가 번역되지 않았는지 검사한다.
- [ ] glossary가 일관되게 적용됐는지 검사한다.
- [ ] QA 실패 청크만 선택적으로 재번역한다.
- [ ] 최종 결과와 함께 `*_qa_report.json`을 생성한다.

완료 조건:

- 구조 또는 보호 토큰이 손상된 결과는 최종 파일로 승격되지 않는다.
- QA 리포트만 보고 누락된 키와 변경된 토큰을 찾을 수 있다.

## P1: 체크포인트와 재실행 안정성

- [ ] 체크포인트에 입력 파일 SHA-256을 저장한다.
- [ ] 프롬프트, glossary, keep terms, 모델명, 청크 크기의 해시를 저장한다.
- [ ] 이전 실행과 설정이 다르면 자동 재개하지 않는다.
- [ ] `.meta.json` 없이 `.tmp`만 남아 있으면 append하지 않는다.
- [ ] 청크별 원문 해시와 처리 상태를 기록한다.
- [ ] 임시 파일 기록 후 `flush()`와 `fsync()`를 수행한다.
- [ ] 최종 파일 전환에 `os.replace()`를 사용한다.
- [ ] `--resume`, `--force`, `--dry-run` 동작을 모든 단계에서 통일한다.
- [ ] 입력이 변경되지 않은 경우에만 기존 결과를 재사용한다.

완료 조건:

- 원문이나 설정이 바뀐 뒤 이전 번역과 새 번역이 섞이지 않는다.
- 프로세스를 강제 종료한 뒤에도 마지막 완료 청크부터 안전하게 재개한다.

## P1: 청크 분할

- [ ] 청크 크기를 넘는 단일 문장을 추가로 분할한다.
- [ ] 문자 수 대신 모델 토큰 수를 기준으로 제한한다.
- [ ] key 라인과 연결된 번역 문장이 서로 다른 청크로 갈라지지 않게 한다.
- [ ] `[PAGE n]`을 우선 청크 경계로 사용한다.
- [ ] HTML 태그와 bracket token 내부에서 자르지 않는다.
- [ ] 문장 분할 중 원문의 줄바꿈을 임의로 변경하지 않는다.
- [ ] 필요할 경우 이전 청크 문맥을 출력 제외 컨텍스트로 제공한다.

완료 조건:

- 설정한 최대 크기를 넘는 청크가 생성되지 않는다.
- 청크를 다시 합쳤을 때 번역 전 구조와 동일한 경계를 복원할 수 있다.

## P1: 설정과 게임 프로필

- [ ] 텍스트 번역, 이미지 번역, OCR, PDF 포맷팅 모델을 각각 설정할 수 있게 한다.
- [ ] The Elder Scrolls, Dragon Eclipse 등 게임별 설정을 분리한다.
- [ ] 공통 설정과 게임별 설정을 합성하는 로더를 구현한다.
- [ ] 프롬프트를 JSON에서 별도 텍스트 파일로 분리한다.
- [ ] glossary와 keep terms를 별도 파일로 분리한다.
- [ ] 필수 설정, 자료형, 경로, 청크 크기를 실행 전에 검증한다.
- [ ] 모델명과 작업 유형의 호환성을 실행 전에 검사한다.
- [ ] README와 실제 기본 프로필의 대상 게임을 일치시킨다.

예시 구조:

```text
configs/
├── base.json
├── dragon_eclipse.json
└── elder_scrolls.json

prompts/
├── text_translation.txt
├── image_translation.txt
├── image_ocr.txt
└── pdf_format.txt

glossaries/
├── dragon_eclipse.json
└── elder_scrolls.json
```

## P1: API 안정성 및 비용

- [ ] API 요청 timeout을 설정한다.
- [ ] SDK 예외 타입과 HTTP 상태 코드로 재시도 여부를 판별한다.
- [ ] 429 응답의 `Retry-After`를 반영한다.
- [ ] exponential backoff에 random jitter를 추가한다.
- [ ] 단계별 최대 재시도 횟수와 대기 시간을 설정으로 이동한다.
- [ ] 처리 시간, 성공 수, 실패 수, 재시도 수를 기록한다.
- [ ] 가능하면 토큰 사용량과 예상 비용을 리포트한다.
- [ ] 동시 API 호출 개수를 제한한 병렬 처리를 지원한다.
- [ ] `Ctrl+C` 종료 시 체크포인트를 안전하게 저장한다.

## P2: 용어집 워크플로우

- [ ] 검수 완료 CSV를 glossary JSON으로 변환하는 명령을 제공한다.
- [ ] config 또는 프로필 glossary로 안전하게 병합하는 명령을 제공한다.
- [ ] 대소문자 변형과 단수·복수형을 같은 후보군으로 묶는다.
- [ ] 후보에 `keep`, `translate`, `review`, `ignore` 상태를 부여한다.
- [ ] 후보 CSV에 출현 위치와 짧은 예문을 추가한다.
- [ ] 기존 glossary와 keep terms에 등록된 항목을 후보에서 제외한다.
- [ ] 같은 영문 용어에 여러 한국어 번역이 지정된 충돌을 탐지한다.
- [ ] 번역 결과의 용어 일관성 통계를 생성한다.

## P2: 이미지 처리

- [ ] Gemini 응답 MIME 형식과 출력 확장자를 일치시킨다.
- [ ] GIF 다중 프레임을 지원하거나 명시적으로 제외한다.
- [x] EXIF 방향을 반영한 뒤 이미지를 처리한다.
- [ ] 크기 보정 실패 시 raw bytes 저장을 성공으로 처리하지 않는다.
- [ ] 원본과 생성 이미지의 크기 및 종횡비를 검증한다.
- [ ] 카드 제목, 프레임, 아이콘 영역의 과도한 변경을 탐지한다.
- [ ] 실패 이미지 목록을 JSON 또는 CSV로 저장한다.
- [ ] 입력과 출력 경로가 같아 원본을 덮어쓰는 상황을 차단한다.

## P2: 테스트와 CI

- [ ] `pytest`를 도입한다.
- [ ] 텍스트 정제 규칙 단위 테스트를 작성한다.
- [ ] 청크 최대 크기와 경계 보존 테스트를 작성한다.
- [ ] 페이지 파서 테스트를 작성한다.
- [ ] 파일명 매칭 테스트를 작성한다.
- [ ] 체크포인트 생성 및 재개 테스트를 작성한다.
- [ ] 토큰, 태그, key 보존 QA 테스트를 작성한다.
- [ ] Gemini 클라이언트를 mock 처리한 통합 테스트를 작성한다.
- [ ] 작은 PDF, TXT, 이미지 fixture를 제공한다.
- [ ] GitHub Actions에서 테스트, lint, secret scan을 실행한다.
- [ ] `ruff`를 적용해 스타일과 기본 오류를 검사한다.

## P2: 저장소 관리

- [ ] 현재 추적 중인 `.DS_Store`, `.idea`, `__pycache__`를 Git에서 제거한다.
- [ ] 50MB 이상 PDF를 Git LFS로 전환하거나 릴리스 자산으로 분리한다.
- [ ] 소스 코드와 완성 번역 산출물의 보관 정책을 정한다.
- [ ] `requirements.txt` 버전을 고정하거나 `pyproject.toml`로 전환한다.
- [ ] 지원 Python 버전을 README에 명시한다.
- [ ] 라이선스 파일을 추가한다.

주의:

- Git LFS 전환은 원격 이력을 다시 변경할 수 있으므로 별도 작업으로 수행한다.
- 로컬 백업 브랜치를 `git push --all`로 원격에 올리지 않는다.

## P3: 통합 CLI와 사용성

- [x] `pyproject.toml`, `src/glk` 패키지, `glk` console script 뼈대를 만든다.
- [x] `glk init`과 `glk status`에 project manifest 및 workspace service를 연결한다.
- [x] `glk extract`에 PDF 등록, 렌더링, fragment 추출, LLM 레이아웃 복원과 캐시를 연결한다.
- [x] `glk ocr`에 이미지 폴더 등록, 공통·개별 프롬프트, 구조화 OCR, 개별·통합 TXT와 캐시를 연결한다.
- [ ] 번호 기반 스크립트를 하나의 CLI 진입점으로 통합한다.
- [ ] 전체 파이프라인을 한 번에 실행하는 명령을 제공한다.
- [ ] 단계별 실행과 전체 실행이 같은 내부 함수를 사용하게 한다.
- [ ] 진행률과 예상 남은 시간을 표시한다.
- [ ] 실행 ID별 출력 디렉터리와 결과 리포트를 생성한다.
- [ ] 모델명, 프롬프트 버전, glossary 버전을 결과 메타데이터에 기록한다.
- [ ] 로그에 API 키와 전체 프롬프트가 기록되지 않게 한다.
- [ ] 성공, 부분 성공, 실패를 CLI 종료 코드로 구분한다.

목표 CLI 예시:

```bash
glk extract --file TES_Rulebook.pdf
glk ocr --project card_set --folder card_images/
glk clean --file TES_Rulebook.txt
glk glossary --file TES_Rulebook_clean.txt
glk translate --file TES_Rulebook_clean.txt --resume
glk format --file TES_Rulebook_clean_ko.txt
glk run --profile elder_scrolls --file TES_Rulebook.pdf
```

## 권장 구현 순서

1. `pyproject.toml`과 통합 CLI 진입점
2. project manifest와 workspace 상태 관리
3. PDF fragment 추출·렌더링·LLM 레이아웃 복원
4. 이미지 폴더 등록과 Gemini OCR
5. 파일명 매칭 및 실패 처리
6. 체크포인트 해시와 원자적 파일 저장
7. 원문 QA, 수정 이력, 승인 gate
8. 번역 결과 QA 검사
9. 청크 분할 개선
10. 단위 테스트와 Gemini mock 통합 테스트
11. 게임별 설정 및 모델 분리
12. 용어집 자동 반영과 저장소 대형 파일 정리
