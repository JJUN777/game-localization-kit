# 이미지 폴더 OCR 설계 및 검증 결과

## 목적

여러 이미지에서 원문을 자동 추출하면서 프로젝트별 아이콘 표기를 `{TOKEN}` 형식으로 보존하고, 이미지별 TXT와 하나의 통합 TXT를 동시에 생성합니다. Windows와 macOS에서 같은 `glk` 명령을 사용하며 핵심 처리는 CLI가 아닌 application service에 둡니다.

## 확정된 입력 방식

이미지 폴더에는 PNG, JPEG, WebP 파일을 넣습니다. 하위 폴더를 재귀 탐색하고 숫자가 포함된 파일명은 자연 정렬합니다.

```text
card_images/
├── ocr_prompt.txt
├── card-1.jpg
├── card-1.jpg.prompt.txt
└── card-2.jpg
```

- `ocr_prompt.txt`: 모든 이미지에 적용할 공통 지침
- `이미지파일명.prompt.txt`: 해당 이미지에만 적용할 추가 지침
- prompt sidecar가 없으면 공통 지침만 사용

## 아이콘 처리 정책

아이콘 참조 이미지를 Gemini 요청에 포함하지 않습니다. 참조 아이콘이 많거나 OCR 대상이 100장 이상일 때 같은 이미지를 매번 다시 전송하는 비용을 피하기 위해, 아이콘의 핵심 실루엣과 정확한 출력 토큰을 `ocr_prompt.txt`에 글로 설명합니다.

현재 샘플 프롬프트는 [samples/image_ocr/ocr_prompt.txt](../samples/image_ocr/ocr_prompt.txt)에 있으며 다음 계열을 포함합니다.

- 능력치·규칙: `DEF`, `DMG`, `DMGR`, `HP`, `PWR`, `STR`, `SWT`, `WILL`
- 흑백 기호: `ARCH`, `CLOVER`, `Non`, `SPADE`
- 기본 속성: `Air`, `Dark`, `Earth`, `Fire`, `Light`, `Water`
- e 접두 속성: `eAir`, `eDark`, `eEarth`, `eFire`, `eLight`, `eWater`
- t 접두 속성: `tAir`, `tDark`, `tEarth`, `tFire`, `tLight`, `tWater`

확실하게 일치하면 `{Air}`, `{DMGR}`처럼 기록합니다. 설명만으로 확신할 수 없는 아이콘은 잘못된 토큰을 추측하지 않고 `[ICON: ...]`과 warning을 남깁니다.

## CLI 사용법

```bash
glk init "Card Set" --project-id card_set
glk ocr --project card_set --folder card_images/
```

`--prompt`를 지정하지 않으면 입력 폴더의 `ocr_prompt.txt`를 자동으로 사용합니다.

```bash
glk ocr \
  --project card_set \
  --folder card_images/ \
  --prompt card_images/ocr_prompt.txt
```

첫 실행 후에는 원본과 프롬프트가 workspace에 등록되므로 폴더를 생략할 수 있습니다.

```bash
glk ocr --project card_set
```

입력 이미지나 프롬프트가 바뀌지 않으면 검증된 결과를 캐시에서 재사용합니다. 다시 OCR하려면 `--force`를 사용합니다.

## 결과 구조

```text
workspaces/card_set/
├── project.json
├── source/
│   ├── images/
│   ├── ocr_prompt.txt
│   └── ocr/
│       ├── individual/
│       │   ├── card-1.txt
│       │   └── card-2.txt
│       ├── results/
│       │   ├── card-1.json
│       │   └── card-2.json
│       ├── combined.txt
│       └── run_summary.json
└── state/image_ocr.json
```

통합 파일 형식은 다음과 같습니다.

```text
[card-1.txt]
OCR 내용

======================

[card-2.txt]
OCR 내용

======================
```

개별 JSON에는 원본 이미지 경로와 해시, 공통·개별 프롬프트 해시, 모델명, 프롬프트 버전, block type, bbox, legibility, warnings를 기록합니다. 일부 이미지가 실패하면 빈 섹션을 유지한 `combined.partial.txt`와 실패 목록을 생성하고 CLI는 부분 성공 종료 코드를 반환합니다.

## 검증 결과

2026-07-22에 샘플 이미지 7장으로 정식 `glk ocr` 명령을 검증했습니다.

- 첫 실행: 7/7 성공, 실패 0, needs review 0
- 개별 TXT 7개와 `combined.txt` 생성
- `{Air}`, `{CLOVER}`, `{SPADE}`, `{DMG}`, `{DMGR}`, `{DEF}`, `{WILL}`, `{PWR}`, `{STR}` 등 아이콘 토큰 출력 확인
- 두 번째 실행: 7/7 이미지 캐시 재사용, Gemini 재호출 없음
- 전체 자동 테스트: 23개 통과

검증 workspace는 `workspaces/image_ocr_poc/`이며 workspace와 원본 샘플 이미지는 Git에서 제외됩니다.

## 현재 제한사항

- 아이콘 설명만으로 판별하므로 작거나 흐린 아이콘은 토큰이 흔들릴 수 있습니다.
- `e*` 계열은 실루엣이 같고 색상이 핵심이므로 흑백 또는 색 손상 이미지에서는 확정하지 않습니다.
- 자동 회전, 기울기 보정, 대비 향상은 아직 구현하지 않았습니다. EXIF 방향만 반영합니다.
- warning을 활용한 원문 QA와 사람 승인 단계는 후속 작업입니다.
- 스캔 PDF 페이지를 이 이미지 OCR 서비스로 자동 전환하는 fallback은 아직 연결하지 않았습니다.
