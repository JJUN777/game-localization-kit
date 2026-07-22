# PDF 레이아웃 복원 PoC

> 상태: PoC 완료, 통합 CLI extraction service로 이전됨  
> 입력: `9999. final/PoC.pdf` 4페이지  
> 구현: `experiments/layout_reconstructor_poc.py`

## 목적

PDF에서 원문을 가져올 때 발생하는 두 문제를 검증합니다.

1. 2단·3단·패널 구성에서 TXT 읽기 순서가 섞이는 문제
2. 시각적 줄바꿈 때문에 한 문장이 여러 줄로 나뉘는 문제

텍스트 레이어가 있는 PDF는 OCR로 다시 읽지 않습니다. PyMuPDF로 원문과 좌표를 함께 추출하고, 원문 문자열은 그대로 둔 채 fragment의 순서와 묶음만 결정합니다.

## 두 가지 실행 방식

### 로컬 규칙만 사용

```bash
python experiments/layout_reconstructor_poc.py \
  --file "9999. final/PoC.pdf" \
  --pages 1-4 \
  --local-only
```

외부 API를 호출하지 않습니다. PDF가 제공한 text block을 복원한 뒤, 페이지의 큰 수평·수직 빈 공간을 재귀적으로 잘라 상→하, 좌→우 읽기 순서를 결정합니다. 문단의 시각적 줄바꿈과 열 경계를 넘어 이어지는 문장을 로컬 규칙으로 결합합니다.

### 로컬 결과와 이미지 기반 판정 함께 생성

```bash
python experiments/layout_reconstructor_poc.py \
  --file "9999. final/PoC.pdf" \
  --pages 1-4
```

로컬 결과를 먼저 만들고 Gemini에는 해당 PDF 페이지 이미지와 그 페이지의 fragment ID·텍스트·좌표만 전달합니다. `.env`, config, 다른 프로젝트 파일은 전달하지 않습니다. 모델은 원문을 번역하거나 다시 작성하지 않고 block type, fragment 순서, 묶음, 본문 포함 여부만 반환합니다.

기존 모델 결과가 있으면 재사용합니다. 다시 판정하려면 `--force`를 추가합니다.

## 원문 무결성 장치

- 모델 응답에는 fragment ID만 허용합니다.
- 추출된 모든 ID가 정확히 한 번 반환돼야 합니다.
- 누락, 알 수 없는 ID, 중복이 하나라도 있으면 해당 페이지를 실패 처리합니다.
- 최종 텍스트는 모델 출력 문자열이 아니라 PDF에서 추출한 원문 fragment로 조립합니다.
- 파일은 임시 파일 기록과 `fsync()` 후 `os.replace()`로 교체합니다.

## 4페이지 결과

| 페이지 | 구성 | 로컬 규칙 | 이미지 기반 판정 |
|---|---|---|---|
| 1 | 전폭 본문 + 좌우 독립 패널 | Campaign 전체 후 Expedition 전체로 정상 정렬, 줄바꿈 정상 결합 | 정상 정렬 |
| 2 | 일반 2단 본문 | 왼쪽 단을 끝낸 뒤 오른쪽 단으로 이동, 열 경계의 `the` + `sky.` 문장도 정상 결합 | 정상 정렬 |
| 3 | 도표 + 2단 번호 목록 | 본문·번호 목록은 정상, 도표 callout 숫자의 의미 순서는 불안정 | 도표 label과 artifact를 더 정확히 구분 |
| 4 | 자유 배치 컴포넌트 그리드 + 규칙 | 규칙 본문은 정상, 아이콘 숫자와 컴포넌트 label 일부가 잘못 묶임 | 컴포넌트 label을 의미 단위로 더 안정적으로 묶음 |

모든 방식에서 네 페이지의 fragment 검증은 통과했습니다.

| 페이지 | fragment 수 | 누락 | 중복 | 알 수 없는 ID |
|---|---:|---:|---:|---:|
| 1 | 53 | 0 | 0 | 0 |
| 2 | 80 | 0 | 0 | 0 |
| 3 | 57 | 0 | 0 | 0 |
| 4 | 108 | 0 | 0 | 0 |

## 생성 파일

기본 출력 디렉터리는 git에서 제외된 `97_layout_poc/`입니다.

- `page_NNN.png`: 판정에 사용한 페이지 렌더링
- `page_NNN_fragments.json`: PDF 원문 fragment와 좌표
- `page_NNN_local_result.json`: 로컬 block 구조와 검증 결과
- `page_NNN_local.txt`: 로컬 재구성 TXT
- `page_NNN_result.json`: 이미지 기반 block 구조와 검증 결과
- `page_NNN_reconstructed.txt`: 이미지 기반 재구성 TXT
- `local_reconstructed.txt`: 로컬 전체 합본
- `reconstructed.txt`: 이미지 기반 전체 합본

## 결론과 다음 구현 방향

정식 추출 파이프라인에서는 텍스트 레이어가 정상인 모든 페이지에 이미지 기반 레이아웃 판정을 적용합니다.

- 일반 본문, 단순 다단, 좌우 패널도 동일한 LLM 제약과 검증 절차 적용
- 도표, 카드 그리드, 자유 배치 label 역시 fragment ID만 재배열
- 로컬 XY-cut 결과는 비교·진단·테스트 기준선으로만 유지
- LLM 실패 시 로컬 결과로 조용히 fallback하지 않고 해당 페이지를 실패 처리
- 텍스트 레이어가 없거나 깨진 페이지만 별도 OCR
- 최종적으로 fragment ID 무결성 검사와 원문 QA를 통과한 결과만 번역 단계로 전달

PoC 이전 작업은 `glk extract`에 반영되었습니다. 입력 PDF SHA-256, fragment SHA-256, 모델명, 프롬프트 버전을 기준으로 페이지 결과를 캐시하며, 정식 결과는 project workspace의 `source/pages`, `source/fragments`, `source/layouts`, `source/document.json`, `source/extracted.txt`에 저장됩니다.

## 알려진 한계

- 좌표만으로는 그림 안 숫자가 어느 설명을 가리키는지 알 수 없습니다.
- PDF 제작 방식이 좋지 않으면 서로 다른 label이 하나의 native block으로 저장될 수 있습니다.
- 아이콘이 글꼴 glyph로 저장되면 텍스트 추출 단계에서 의미가 유실될 수 있습니다.
- 로컬 문장 결합은 소문자 시작과 문장부호를 이용하므로 약어, 코드, 고유 형식에는 추가 규칙이 필요합니다.
- 표는 평문 순서보다 행·열 구조 보존이 중요하므로 별도 table block 모델이 필요합니다.
