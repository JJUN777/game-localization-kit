## Game Localization Kit

PDF와 카드 이미지 한글화를 Gemini 기반으로 자동화하기 위한 파이썬 스크립트 모음입니다. 

`00_config.json`을 수정하면 다른 프로젝트에도 적용할 수 있습니다.

## 프로젝트 구조

```
"소스 코드 구성"
00_config.json           # 기본 설정이 들어가 있는 json 설정 파일
01_pdf_extractor.py      # PDF → 기본 텍스트 추출
02_text_translator.py    # 텍스트 → 한국어 번역 (Google GenAI)
03_image_translator.py   # 카드 이미지 텍스트 번역
04_image_ocr.py          # 카드 이미지에서 OCR을 통해서 텍스트 추출
common.py                # 공통 유틸 (설정, 로깅, 경로, API 키)

" 프로젝트 상위 폴더 구성"
파일이 저장될 위치 이기 때문에 입맛에 맞게 수정 후 config 파일 수정이 필요합니다.

90_pdfOrg/               # 원본 PDF
91_pdf_extracted/        # 1단계 출력 텍스트
92_txt_translated/       # 2단계 번역 결과
93_1_imgOrg/             # 원본 카드 이미지
93_2_img_translated/     # 3단계 결과 이미지
94_imgOcrTxt/            # 4단계 OCR 텍스트
```

## 전체 디렉토리 구조

```
.
├── 00_config.json
├── 01_pdf_extractor.py
├── 02_text_translator.py
├── 03_image_translator.py
├── 04_image_ocr.py
├── 90_pdfOrg/
├── 91_pdf_extracted/
├── 92_txt_translated/
├── 93_1_imgOrg/
├── 93_2_img_translated/
├── 94_imgOcrTxt/
├── README.md
├── common.py
.
```


## 선행 조건

- Python 3.10 이상과 `venv`.
- Google GenAI SDK(`google-genai`) 및 `Pillow`, `PyPDF2` 등 의존성. 가상환경에 설치하세요:
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install google-genai Pillow PyPDF2
  ```
- Gemini API 키는 미리 Google AI Studio를 통해서 생성이 필요합니다.

## 설정 (`00_config.json`)

- `paths`: 입력/출력 폴더. `common.get_absolute_path`로 루트 기준 절대 경로로 변환되므로 내부 경로만 기입하세요.
- `model_name`: 번역에 사용할 Gemini 모델(예: `gemini-2.5-flash`, `gemini-3-pro-image-preview`).
- `api_key_env_var`: 현재는 직접 API 키 문자열을 저장하고 `get_api_key`가 그대로 반환합니다. 배포 전 본인의 Gemini API 키로 교체하세요.
- `keep_terms`, `glossary`: 번역 시 유지해야 할 단어와 용어집 정의.
- `prompts`: 텍스트 번역/이미지 번역/OCR에 쓰이는 프롬프트. 리스트 또는 단일 문자열 모두 허용되며 리스트는 줄바꿈으로 이어집니다.

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
   - Gemini에 청크 단위로 전송하여 번역합니다.
   - 청크 사이즈 변경이 필요하다면 `translation`의 `chunk_size`를 변경하면 됩니다.
   - 진행 상황은 `*.meta.json`과 `*.tmp`로 체크포인트를 유지하고 중간 과정을 저장하게 되어있어서, 외부요인으로 작업이 멈추더라도 이어서 작업할 수 있습니다.
   - 결과 파일은 `{원본}_ko.txt` 형태로 `paths.translated_txt_dir`에 기록됩니다.

3. **카드 이미지 번역**
   ```bash
   .venv/bin/python 03_image_translator.py
   ```
   - `paths.image_source_dir`의 이미지를 불러와 `prompts.image_translation` 지침과 함께 Gemini 비전 모델에 전송합니다.
   - 모델 응답에서 이미지 데이터를 추출한 뒤 원본 해상도에 맞춰 리사이즈해 `paths.image_output_dir`에 저장합니다.
   - 이미지 번역 및 인식의 경우 Gemini의 Nanobanana 모델인 `gemini-3-pro-image-preview` 사용을 권장합니다.

4. **카드 이미지 OCR**
   ```bash
   .venv/bin/python 04_image_ocr.py
   ```
   - `prompts.image_ocr`를 사용해 이미지에서 텍스트를 추출합니다.
   - 결과는 이미지별 `.txt` 파일로 `paths.image_ocr_dir`에 기록되며, 기존 파일은 건너뜁니다.

## 사용 팁

- 디렉토리 이름을 바꿀 때는 실제 폴더와 `00_config.json`을 동시에 업데이트하세요.
- AI를 기반으로 한 번역이기 때문에, 프롬프트를 아주 상세하고 구체적이게 작성하는게 중요합니다.
- API 속도 제한에 맞추기 위해 `02_text_translator.py`는 청크 사이에 `time.sleep(1)`, `03_image_translator.py`는 요청마다 `time.sleep(2)`를 둡니다. 필요 시 조정하세요.

