# 게임 로컬라이제이션 키트

보드게임 룰북/PDF/카드 이미지를 한국어로 현지화하기 위한 스크립트 모음입니다.  
현재 워크플로우는 `The Elder Scrolls` 룰북 번역 흐름(텍스트 정제 + 용어집 구축 + 번역)에 맞춰 업데이트되어 있습니다.

## 문서

- [번역 자동화 파이프라인 설계 초안](docs/TRANSLATION_AUTOMATION_DESIGN.md)
- [개선 로드맵](docs/IMPROVEMENTS.md)
- [PDF 다단·줄바꿈 복원 PoC](docs/LAYOUT_RECONSTRUCTION_POC.md)
- [이미지 폴더 OCR 설계 및 검증 결과](docs/IMAGE_OCR.md)

## 빠른 시작

모든 명령은 **프로젝트 루트**에서 실행하세요.

```bash
cd /Users/jjun_mac/Documents/GitHub/game-localization-kit
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
touch .env
```

Windows PowerShell에서는 가상환경을 다음과 같이 활성화합니다.

```powershell
.venv\Scripts\Activate.ps1
pip install -e .
```

설치 후 통합 CLI 진입점을 확인할 수 있습니다.

```bash
glk --help
glk version
```

프로젝트 작업공간을 만들고 상태를 확인할 수 있습니다.

```bash
glk init "Primal Rulebook" --profile primal
glk status --project primal_rulebook
```

기본 생성 위치는 `workspaces/<project_id>/`이며 Git에서 제외됩니다. 다른 위치를 사용하려면 두 명령에 `--workspace-root PATH`를 지정합니다.

PDF 텍스트 레이어를 추출하고 LLM으로 읽기 순서를 복원할 수 있습니다.

```bash
glk extract \
  --project primal_rulebook \
  --file "rulebook.pdf" \
  --pages 1-10
```

첫 실행은 PDF를 `source/original.pdf`로 등록하고 Gemini 레이아웃 판정을 수행합니다. 이후에는 `--file`을 생략할 수 있으며, PDF·fragment·모델·프롬프트가 같으면 검증된 페이지 캐시를 재사용합니다. 강제로 다시 판정하려면 `--force`를 사용합니다.

이미지 폴더에서 원문을 OCR할 수 있습니다. 폴더의 `ocr_prompt.txt`는 공통 지침으로, `파일명.jpg.prompt.txt`는 해당 이미지 전용 추가 지침으로 사용합니다.

```bash
glk init "Card Set" --project-id card_set
glk ocr --project card_set --folder samples/image_ocr
```

원본은 `source/images/`, 개별 TXT는 `source/ocr/individual/`, 통합본은 `source/ocr/combined.txt`에 저장됩니다. 같은 이미지·공통 프롬프트·개별 프롬프트·모델·프롬프트 버전이면 이후에는 `--folder`를 생략하고 검증된 캐시를 재사용할 수 있습니다.

```bash
glk ocr --project card_set
glk ocr --project card_set --force
```

현재 `init`, `status`, `extract`, `ocr`가 실제 application service에 연결된 상태입니다. `run`, `translate` 등 아직 연결되지 않은 명령은 성공으로 처리하지 않고 종료 코드 `3`을 반환합니다. 텍스트 레이어가 없는 PDF 페이지의 OCR fallback은 다음 구현 단계에 포함됩니다.

생성한 `.env`에 Gemini API 키를 입력하세요. `.env`는 Git에서 제외되며,
셸이나 CI에 설정된 `GEMINI_API_KEY`가 있으면 그 값을 우선 사용합니다.

```dotenv
GEMINI_API_KEY=your_api_key_here
```

## 주요 파일

```text
00_config.json                   # 경로/모델/프롬프트/용어집 설정
01_pdf_extractor.py              # PDF -> TXT 추출
01_2_text_cleaner.py             # 추출 TXT 정제(_clean.txt)
02_text_translator.py            # TXT 번역
03_image_translator.py           # 이미지 번역 편집
04_image_ocr.py                  # 이미지 OCR
05_pdf_formatter.py              # 번역 텍스트 레이아웃 정리
06_glossary_builder.py           # 용어 후보 CSV/JSON 생성
common.py                        # 공통 유틸
90_pdfOrg/                       # 원본 PDF
91_pdf_extracted/                # 추출/정제 TXT
92_txt_translated/               # 번역 TXT
96_glossary_candidates/          # 용어 후보 CSV
```

## 설정 (`00_config.json`)

- `paths`: 입력/출력 디렉터리
- `translation.model_name`: Gemini 모델명
- `GEMINI_API_KEY`: `.env` 또는 실행 환경에서 주입하는 API 키
- `translation.chunk_size`: 텍스트 번역 청크 크기
- `keep_terms`: 절대 번역하지 않을 용어
- `glossary`: 고정 번역 용어집
- `prompts`: 텍스트/이미지/OCR/포맷팅 프롬프트

## 권장 워크플로우

1. PDF 텍스트 추출
```bash
python3 01_pdf_extractor.py
```

2. 추출 텍스트 정제
```bash
python3 01_2_text_cleaner.py --file "TES_Rulebook.txt"
```
- 기본 출력: `91_pdf_extracted/TES_Rulebook_clean.txt`
- 원본 덮어쓰기: `--inplace`

3. 용어 후보 생성
```bash
python3 06_glossary_builder.py --file "TES_Rulebook.txt" --min-freq 4
```
- `_clean.txt`가 있으면 자동으로 우선 사용합니다.
- 출력: `96_glossary_candidates/*_candidates.csv`, `*_glossary_template.json`

4. CSV 검수 후 `00_config.json` 반영
- `korean` 컬럼 확정값을 `glossary`로 반영
- `HP`, `XP`, `EP`, `NPC` 같이 영문 유지할 용어는 `keep_terms`에 반영

5. 텍스트 번역 실행
```bash
python3 02_text_translator.py
```

6. 필요 시 이미지 번역/OCR/PDF 포맷팅 실행
```bash
python3 03_image_translator.py
python3 04_image_ocr.py
python3 05_pdf_formatter.py
```

## 참고 사항

- `02_text_translator.py`는 체크포인트(`.tmp`, `.meta.json`) 기반으로 재시작을 지원합니다.
- API 호출은 `common.call_gemini_with_retry`로 재시도 로직이 적용됩니다.
- 기존 출력 파일이 있으면 단계별로 건너뛰는 로직이 있으니, 재생성이 필요하면 해당 결과 파일을 먼저 삭제하세요.
