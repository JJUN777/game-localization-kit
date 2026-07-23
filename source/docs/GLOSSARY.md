# 용어집 검토 사양

이 문서는 최종 공통 원문에서 용어 후보를 만들고 사람이 검토하는 파일 형식의 확정 사양입니다. 구현 시 필드명과 검증 규칙을 변경하려면 [전체 작업 흐름도](WORKFLOW.md)와 이 문서를 함께 갱신합니다.

## 처리 흐름

```text
segments/approved_source.jsonl
→ glk glossary build
→ terminology/glossary_review.tsv
→ 사람이 translation/status/category/note 수정 및 누락 용어 행 추가
→ glk glossary import
→ terminology/termbase.json
```

`segments/approved_source.jsonl`이 없거나 현재 원문 기준으로 stale이면 후보 생성을 시작하지 않습니다.

`glk glossary build`와 `glk glossary import`는 구현 완료됐습니다. 첫 후보 생성은 API를 호출하지 않고 제목·고유명사와 반복 1~4단어 표현을 로컬 규칙으로 수집합니다. LLM을 이용한 후보 의미 분류는 현재 범위에 포함하지 않습니다.

## 검토 파일

사람이 수정하는 기본 파일은 UTF-8 TSV입니다.

```text
terminology/glossary_review.tsv
```

CSV 대신 TSV를 사용해 예문에 포함된 쉼표 때문에 셀이 복잡해지는 문제를 피합니다. Excel, Numbers, LibreOffice와 일반 텍스트 편집기에서 열 수 있어야 합니다.

컬럼 순서는 다음으로 고정합니다.

```tsv
status	source_term	translation	category	note	variants	occurrences	locations	example	candidate_id
review	Hunter		term		Hunter | Hunters	28	p2,p4,p8	Each Hunter gains 2 Stamina.	term-a81c02
```

| 컬럼 | 사람 수정 | 설명 |
|---|---:|---|
| `status` | O | 후보 처리 상태 |
| `source_term` | 필요시 | 기준 원문 용어. 자동 후보에서는 보통 유지 |
| `translation` | O | 확정 번역어 |
| `category` | O | 용어 유형 |
| `note` | O | 번역 원칙과 예외 |
| `variants` | X | 발견된 대소문자·복수형 등의 변형 |
| `occurrences` | X | 최종 공통 원문의 출현 횟수 |
| `locations` | X | 원본 페이지·파일 위치 |
| `example` | X | 실제 사용 예문 |
| `candidate_id` | X | 정렬 후에도 후보를 식별하는 안정적 ID |

뒤쪽 근거 컬럼은 사람이 실수로 수정하더라도 import 시 최종 공통 원문을 기준으로 다시 계산합니다.

기존 TSV는 `glk glossary build`를 다시 실행해도 덮어쓰지 않습니다. 승인 원문 또는 추출 설정이 변경되면 TSV를 stale로 표시하고, 사람이 기존 편집 내용을 비교한 뒤 `--force`를 명시한 경우에만 새 후보로 초기화합니다.

후보 수집 범위를 조정하거나 파일을 쓰지 않고 결과만 확인할 때는 다음 옵션을 사용합니다.

```bash
glk glossary build --project primal --min-frequency 2 --max-words 4 --max-candidates 500
glk glossary build --project primal --dry-run
```

실행 기준은 `state/glossary_build.json`에 기록합니다. 승인 원문 hash와 후보 생성 설정이 같을 때 기존 TSV를 현재 결과로 인정하며, 설정이나 승인 원문이 달라지면 `glk status`에서 `stale`로 표시합니다. `--force`는 사람이 수정한 TSV를 초기화하므로 기존 파일을 비교·백업한 뒤에만 사용합니다.

## 상태와 카테고리

허용 상태:

- `review`: 아직 확인하지 않음
- `approved`: 지정한 번역어를 일관되게 사용
- `keep`: 번역하지 않고 원문을 유지
- `rejected`: 용어집에서 제외

허용 카테고리:

- `term`: 게임 규칙 용어
- `proper_noun`: 인물·지역·세력명
- `ability`: 능력·행동명
- `component`: 카드·토큰·보드 구성물
- `ui`: UI와 상태 표기
- `phrase`: 일관된 번역이 필요한 문구

`approved` 상태에는 비어 있지 않은 `translation`이 필요합니다. `keep`은 원문 표기를 그대로 사용합니다.

## 사람이 누락 용어 추가

자동 후보에 없는 용어는 TSV 마지막에 새 행으로 추가합니다. 사람은 앞쪽 필드만 작성하고 `candidate_id`와 근거 컬럼은 비워둡니다.

아래 예시의 `⇥`는 tab 한 칸이며, `note` 뒤의 근거 필드는 비워둡니다.

```text
approved⇥Critical Hit⇥치명타⇥term⇥항상 치명타로 번역⇥⇥⇥⇥⇥
```

`glk glossary import`는 빈 `candidate_id`를 수동 추가 행으로 판정하고 다음을 자동 처리합니다.

1. 최종 공통 원문에서 `source_term` 검색
2. 대소문자와 보수적으로 판정 가능한 복수형 변형 탐색
3. 출현 횟수, block ID, 페이지·이미지 위치와 예문 수집
4. 안정적인 `candidate_id` 생성
5. 최종 termbase에 `origin: manual` 기록

검증에 성공하면 계산한 `candidate_id`, variants, occurrences, locations와 example을 원래 `glossary_review.tsv`에도 다시 기록합니다. 이후 같은 파일을 다시 import하면 입력 hash와 termbase hash가 일치할 때 현재 결과를 재사용합니다.

자동 변형 병합이 의미를 바꿀 가능성이 있으면 합치지 않고 사람이 확인할 별도 후보로 둡니다.

## Import 검증

다음 조건은 import를 차단합니다.

- `review` 상태가 남아 있음
- `approved`인데 번역어가 비어 있음
- 허용되지 않은 status 또는 category
- 같은 `source_term` 또는 보수적으로 같은 대소문자·단수·복수 후보군이 중복됨
- 기존 `candidate_id`가 삭제·변경·중복됨
- 수동 추가 `source_term`이 최종 공통 원문에서 발견되지 않음
- `{HP}`, `{DEF}` 같은 보호 token을 일반 용어로 등록함

원문에 아직 없지만 향후 사용할 용어를 의도적으로 선등록할 때만 다음 옵션을 허용합니다.

```bash
glk glossary import \
  --project primal \
  --file terminology/glossary_review.tsv \
  --allow-missing-terms
```

이 경우 termbase에 `origin: manual`, `source_verified: false`를 기록하고 경고를 남깁니다.

`--file`이 상대 경로이면 project workspace 내부를 먼저 찾고, 없으면 현재 작업 디렉터리를 확인합니다. workspace 외부의 검토 TSV를 지정해도 검증 성공 후 정규화된 결과는 프로젝트의 `terminology/glossary_review.tsv`에 기록됩니다.

## 최종 출력

```text
terminology/termbase.json
```

최종 termbase에는 source term, translation, category, status, note, variants, 근거 위치, origin과 source 검증 여부를 저장합니다. 이후 번역 prompt에는 `approved`와 `keep` 항목만 전달하고, 번역 QA는 확정 용어와 보호 token의 일관성을 검사합니다.

`glk status`는 승인 원문, 검토 TSV와 termbase의 저장 hash가 모두 일치할 때만 `Termbase: current`로 표시합니다. 셋 중 하나가 바뀌면 기존 termbase는 삭제하지 않고 `stale`로 표시합니다.
