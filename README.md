# 게임 로컬라이제이션 키트

보드게임용 PDF와 카드 이미지를 한국어로 현지화하기 위한 파이썬 스크립트 모음입니다. 기본 구성은 *Old King's Crown / Dragon Eclipse* 자료를 대상으로 하지만 `00_config.json`을 수정하면 다른 프로젝트에도 적용할 수 있습니다.

## 프로젝트 구조

```
01_pdf_extractor.py      # PDF → 기본 텍스트 추출
02_text_translator.py    # 텍스트 → 한국어 번역 (Google GenAI)
03_image_translator.py   # 카드 이미지 텍스트 교체
04_image_ocr.py          # 번역된 이미지에서 OCR 수행
05_pdf_formatter.py      # 번역 텍스트 레이아웃 포맷팅
90_pdfOrg/               # 원본 PDF
91_pdf_extracted/        # 1단계 출력 텍스트
92_txt_translated/       # 2단계 번역 결과
93_1_imgOrg/             # 원본 카드 이미지
93_2_img_translated/     # 3단계 결과 이미지
94_imgOcrTxt/            # 4단계 OCR 텍스트
95_pdf_formatted/        # 5단계 포맷팅 결과
common.py                # 공통 유틸 (설정, 로깅, 경로, API 키, retry, 프롬프트 변환)
```

### 전체 디렉터리 트리

```
.
├── 00_config.json
├── 01_pdf_extractor.py
├── 02_text_translator.py
├── 03_image_translator.py
├── 04_image_ocr.py
├── 05_pdf_formatter.py
├── 90_pdfOrg/
│   └── Primal The Awakening - Rulebook.pdf
├── 91_pdf_extracted/
│   └── Primal The Awakening - Rulebook.txt
├── 92_txt_translated/
│   └── Primal The Awakening - Rulebook_ko.txt
├── 93_1_imgOrg/
├── 93_2_img_translated/
├── 94_imgOcrTxt/
├── 95_pdf_formatted/
│   └── Primal The Awakening - Rulebook_formatted.txt
├── README.md
├── common.py
├── __pycache__/
├── .idea/
└── .venv/
```

> 참고: 위 트리는 현재 저장소 상태 기준이며, 추가 자원이나 산출물이 생기면 그대로 반영됩니다.

## 선행 조건

- Python 3.10 이상과 `venv`.
- Google GenAI SDK(`google-genai`) 및 `Pillow`, `PyPDF2` 등 의존성. 가상환경에 설치하세요:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install google-genai Pillow PyPDF2
  ```
- 이미지 모델을 호출할 수 있는 Gemini API 키.

## 설정 (`00_config.json`)

- `paths`: 입력/출력 폴더. `common.get_absolute_path`로 루트 기준 절대 경로로 변환되므로 리포지토리 내부 경로만 기입하세요.
- `translation.model_name`: 전반에 사용할 Gemini 모델(예: `gemini-3-pro-image-preview`).
- `translation.api_key`: API 키 문자열을 직접 저장합니다. 배포 전 본인 키로 교체하세요.
- `translation.chunk_size`: 텍스트 번역 시 청크 분할 크기(문자 수). 기본값 3000.
- `supported_image_extensions`: 이미지 처리 시 인식할 확장자 목록(예: `[".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]`). 모든 이미지 관련 스크립트가 이 설정을 공유합니다.
- `keep_terms`, `glossary`: 번역 시 유지해야 할 단어와 용어집 정의.
- `prompts`: 텍스트 번역/이미지 번역/OCR/PDF 포맷팅에 쓰이는 프롬프트. 리스트 또는 단일 문자열 모두 허용되며 리스트는 줄바꿈으로 이어집니다.

## 공통 모듈 (`common.py`)

모든 스크립트가 의존하는 공통 유틸리티입니다.

- `init_pipeline(log_filename)`: 로깅 설정 → config 로드 → Gemini 클라이언트 생성을 한 번에 처리합니다. `(config, client)` 튜플을 반환합니다.
- `call_gemini_with_retry(client, model_name, contents)`: Gemini API 호출을 exponential backoff(최대 3회)로 재시도합니다. 일시적 네트워크 오류나 429 rate limit에 대응합니다.
- `build_prompt_string(raw_prompt)`: 리스트 또는 문자열 형태의 프롬프트를 단일 문자열로 변환합니다.
- `list_image_files(directory, config)`: config의 `supported_image_extensions`를 참조하여 디렉토리 내 이미지 파일 목록을 반환합니다.
- `get_absolute_path(relative_path)`: 프로젝트 루트 기준 절대 경로 변환.
- `get_api_key(config)`: config에서 `translation.api_key` 값을 읽어 반환합니다.

## 사용 순서

1. **PDF 텍스트 추출**
   ```bash
   .venv/bin/python 01_pdf_extractor.py
   ```
   `paths.pdf_source_dir`의 PDF를 읽어 각 페이지를 `[PAGE n]` 헤더와 함께 `paths.english_txt_dir`에 저장합니다.

2. **텍스트 번역**
   ```bash
   .venv/bin/python 02_text_translator.py
   ```
   - Gemini에 `translation.chunk_size` 단위로 분할 전송하여 번역합니다.
   - 진행 상황은 `*.meta.json`과 `*.tmp`로 체크포인트를 유지하여 중단 후 재개할 수 있습니다.
   - API 호출 실패 시 exponential backoff로 자동 재시도합니다.
   - 결과 파일은 `{원본}_ko.txt` 형태로 `paths.translated_txt_dir`에 기록됩니다.

3. **카드 이미지 번역**
   ```bash
   .venv/bin/python 03_image_translator.py
   ```
   - `paths.image_source_dir`의 이미지를 불러와 `prompts.image_translation` 지침과 함께 Gemini 비전 모델에 전송합니다.
   - 모델 응답에서 이미지 데이터를 추출한 뒤 원본 해상도에 맞춰 리사이즈해 `paths.image_output_dir`에 저장합니다.
   - 처리 완료 후 실패한 파일 목록을 요약 리포트로 출력합니다.

4. **번역 이미지 OCR**
   ```bash
   .venv/bin/python 04_image_ocr.py
   ```
   - `prompts.image_ocr`를 사용해 이미지에서 텍스트를 재추출합니다.
   - 결과는 이미지별 `.txt` 파일로 `paths.image_ocr_dir`에 기록되며, 기존 파일은 건너뜁니다.

5. **번역 텍스트 포맷팅**
   ```bash
   .venv/bin/python 05_pdf_formatter.py
   ```
   - 원본 PDF 레이아웃을 참조하여 번역된 텍스트의 줄바꿈/문단 간격을 Gemini로 재정리합니다.
   - 페이지 단위로 처리하며, 개별 페이지 실패 시 `[FORMAT ERROR]` 마커를 남기고 나머지 페이지를 계속 처리합니다.
   - 임시 파일 기반 복구를 지원하여 중단 후 재개가 가능합니다.
   - 결과는 `paths.pdf_formatted_dir`에 `{원본}_formatted.txt` 형태로 저장됩니다.

## 로깅 및 진단

- 모든 스크립트가 `common.init_pipeline`을 통해 로깅을 초기화합니다. 인자로 로그 파일명을 넘기면 `<repo>/<파일명>`에 동시에 기록되고, 인자를 생략하면 콘솔 출력만 남습니다.
- 각 단계는 출력물이 이미 존재하면 자동으로 건너뛰므로, 재생성하려면 해당 파일을 삭제하세요.

## 운영 팁

- 디렉터리 이름을 바꿀 때는 실제 폴더와 `00_config.json`을 동시에 업데이트하세요.
- 본인의 게임 톤과 스타일에 맞게 프롬프트를 먼저 조정한 뒤 대량 작업을 실행하는 것이 좋습니다.
- API 속도 제한에 맞추기 위해 `02_text_translator.py`는 청크 사이에 `time.sleep(1)`, `03_image_translator.py`는 요청마다 `time.sleep(2)`를 둡니다. 필요 시 조정하세요.
- 모든 Gemini API 호출은 `call_gemini_with_retry`를 통해 일시적 오류 시 최대 3회 자동 재시도됩니다.
