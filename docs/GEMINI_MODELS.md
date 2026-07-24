# Gemini 모델 목록 관리

대시보드 `AI 설정` 드롭다운의 모델 목록과 업데이트 기준을 기록합니다.

## 관리 파일

실제 드롭다운 데이터의 단일 기준은
[`src/glk/data/gemini_models.json`](../src/glk/data/gemini_models.json)입니다.
대시보드 서버가 이 JSON을 읽어 UI에 전달하므로 HTML이나 Python 코드에 모델
ID를 중복해서 추가하지 않습니다.

JSON에는 다음 정보를 기록합니다.

- `last_verified`: 공식 문서를 마지막으로 확인한 날짜
- `source_url`: 확인한 Gemini API 공식 모델 문서
- `models[].id`: Gemini API 요청에 그대로 사용하는 모델 ID
- `models[].description_ko`: 대시보드에 표시할 짧은 설명
- `models[].recommended`: 기본 권장 모델 여부

## 초기 모델 목록

2026-07-24 기준으로 현재 GLK의 텍스트·이미지·PDF 입력과 구조화 출력 흐름에
사용할 안정 모델만 포함합니다.

| API 모델 ID | 용도 |
|---|---|
| `gemini-3.5-flash` | 복잡한 문서와 멀티모달 작업을 위한 최신 안정 Flash 모델 |
| `gemini-3.1-flash-lite` | 대량 추출과 저비용 처리를 위한 3.x 안정 모델 |
| `gemini-2.5-flash` | 속도와 품질의 균형이 좋은 기본 모델 |
| `gemini-2.5-pro` | 복잡한 문서와 추론 작업에 적합한 고성능 모델 |
| `gemini-2.5-flash-lite` | 단순 추출과 대량 처리에 적합한 저비용 모델 |

공식 기준은 [Gemini API 모델 문서](https://ai.google.dev/gemini-api/docs/models)를
확인합니다. 목록에 없는 API 모델은 대시보드의 `직접 입력`으로 사용할 수
있습니다.

`gemini-3.6-flash`와 `gemini-3.5-flash-lite`부터 sampling parameter 규칙이
바뀌므로 GLK의 PDF layout, 이미지 OCR, 번역 provider에서 `temperature` 설정을
제거하거나 모델별로 분기한 뒤 기본 목록에 추가합니다.

## 업데이트 절차

1. 공식 모델 문서와 지원 중단 문서를 확인합니다.
2. GLK가 사용하는 `generateContent`, 이미지·PDF 입력, 구조화 출력과 생성
   옵션을 지원하는지 확인합니다.
3. 안정 모델을 우선해서 JSON의 `models`를 수정합니다.
4. `last_verified`를 확인 날짜로 바꿉니다.
5. 전체 테스트와 Orca 대시보드 드롭다운을 확인합니다.

Preview·experimental 모델은 짧은 기간에 이름이나 동작이 바뀔 수 있으므로
기본 목록에 넣기보다 `직접 입력`을 우선합니다.
