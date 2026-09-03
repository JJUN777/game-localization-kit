# 용어집 검토 사양

이 문서는 승인된 원문에서 용어 후보를 만들고, 사람이 검토해 termbase를 확정하는 과정의 파일 형식과 검증 규칙을 정리합니다.

**대상 독자**: 용어 TSV를 직접 편집하거나, glossary import가 차단되는 원인을 파악하려는 사용자

기본 화면 사용법은 [GUI 사용 가이드](GUI.md#6-용어-후보와-용어-검수),
CLI 순서는 [전체 작업 흐름](WORKFLOW.md#6-용어-후보-검토)을 참고합니다.
여기서는 TSV 컬럼 규칙, import 검증 조건, 수동 용어 추가 방법을 상세히
다룹니다.

---

## 처리 흐름

```text
.glk/segments/approved_source.jsonl   ← 최종 승인된 원문
        ↓ glk glossary build
03_terminology/glossary_review.tsv    ← 사람이 검토하는 파일
        ↓ glk review glossary (브라우저) 또는 스프레드시트
        ↓ glk glossary import (또는 브라우저의 "검증 및 termbase 생성")
03_terminology/termbase.json          ← 번역에 사용하는 확정 용어
```

`approved_source.jsonl`이 없거나 stale이면 후보 생성을 시작하지 않습니다.

---

## 용어 후보 생성 규칙

`glk glossary build`는 AI API를 호출하지 않고 로컬 규칙으로 후보를 수집합니다.

**수집 대상:**
- 제목·고유명사 (대문자로 시작하는 반복 표현)
- 정의형 label과 list prefix (예: `Behavior deck:`)
- 1~4단어로 반복되는 게임 구성요소 표현

**자동 제외 (검토량을 줄이기 위한 정제):**
- `4x Fire terrain token` → 수량 접두사 제거, `Fire terrain token`만 후보화
- `Campaign and`, `and Expedition` → 문장 중간에서 잘린 단발성 대문자 조각
- `display`, `following`, `used` → 일반 기능어·동사
- 단독 로마 숫자, `the monster` 같은 관사 시작 조각
- `Bonus`와 `Bonus damage`가 같은 위치에서만 겹치면 → 가장 긴 후보만 유지
- 단, `token`, `cards`처럼 긴 후보 밖에서도 독립적으로 쓰이는 일반 용어는 유지

정제는 원문을 수정하지 않고 자동 후보 목록만 줄입니다.

---

## TSV 파일 형식

사람이 수정하는 파일은 UTF-8 TSV(탭 구분)입니다.

```text
03_terminology/glossary_review.tsv
```

CSV 대신 TSV를 사용하는 이유: 예문에 포함된 쉼표 때문에 셀 경계가 복잡해지는 문제를 피합니다. Excel, Numbers, LibreOffice와 일반 텍스트 편집기에서 모두 열 수 있습니다.

### 컬럼 구조

아래에서 `→`는 탭(Tab) 한 칸을 나타냅니다.

```text
status → source_term → translation → category → note → variants → occurrences → locations → example → candidate_id
```

실제 TSV 예시 (탭으로 구분된 한 행):

```
review	Hunter		term		Hunter | Hunters	28	p2,p4,p8	Each Hunter gains 2 Stamina.	term-a81c02
```

| 컬럼 | 사람이 수정 | 설명 |
|---|:---:|---|
| `status` | O | 후보 처리 상태 (아래 참고) |
| `source_term` | 필요시 | 기준 원문 용어 |
| `translation` | O | 확정 한국어 번역어 |
| `category` | O | 용어 유형 (아래 참고) |
| `note` | TSV 직접 편집 | 이전 버전과 외부 편집기의 번역 원칙·메모 보존 |
| `variants` | — | 발견된 대소문자·복수형 변형 (자동 계산) |
| `occurrences` | — | 승인 원문 출현 횟수 (자동 계산) |
| `locations` | — | 원본 페이지·파일 위치 (자동 계산) |
| `example` | — | 실제 사용 예문 (자동 계산) |
| `candidate_id` | — | 후보 식별용 안정 ID (자동 생성) |

> `variants`~`candidate_id`는 사람이 실수로 수정하더라도 import 시 승인 원문을 기준으로 다시 계산합니다.

---

## 상태(status)와 카테고리(category)

### 허용 상태

| status | 의미 | 번역어 필수 |
|---|---|:---:|
| `review` | 아직 확인하지 않음 | — |
| `approved` | 지정한 번역어를 일관되게 사용 | O |
| `keep` | 번역하지 않고 원문 표기를 유지 | — |
| `rejected` | 용어집에서 제외 (이력으로 보존) | — |

`review`가 하나라도 남아 있으면 termbase를 만들 수 없습니다.

### 허용 카테고리

| category | 의미 | 예시 |
|---|---|---|
| `term` | 게임 규칙 용어 | Attack, Defense |
| `proper_noun` | 인물·지역·세력명 | Eldoria, Black Fang |
| `ability` | 능력·행동명 | Charge, Backstab |
| `component` | 카드·토큰·보드 구성물 | Health token, Dice |
| `ui` | UI와 상태 표기 | Ready, Exhausted |
| `phrase` | 일관된 번역이 필요한 문구 | End of turn |

---

## 브라우저 검토 화면

```bash
glk review glossary --project sample_rulebook
```

HTML 표에서 지원하는 기능:

- 상태 4종과 카테고리 6종 드롭다운
- 원문 용어·번역어·출현 문맥 검색과 상태·카테고리 필터
- 체크박스로 여러 후보를 선택한 뒤 상태 일괄 변경
- 실제 출현 위치, 표기 변형과 예문 펼쳐보기
- 자동 후보를 보존한 채 수동 용어 행 추가·삭제
- 현재 TSV hash를 이용한 동시 편집 충돌 방지
- `TSV 저장` 후 `검증 및 termbase 생성` 연속 실행

HTML은 별도 데이터베이스를 만들지 않습니다. 브라우저에서 저장한 내용은 `glossary_review.tsv`에 기록되므로 스프레드시트와 병행할 수 있습니다. 표는 추천 순서 외에 첫 등장 위치, 출현 횟수, 원문 용어와 상태 기준으로 정렬할 수 있으며 정렬은 TSV 행 순서를 변경하지 않습니다. 다른 프로그램에서 TSV를 바꾼 뒤 브라우저에서 저장하려 하면 충돌을 알리고 새로고침을 요구합니다.

대시보드에서 검수 화면을 연 경우 termbase 생성 성공 뒤 완료 모달에서 현재
화면에 머물거나 대시보드로 돌아갈 수 있습니다. CLI에서 직접 연 독립 검수
화면에는 대시보드 복귀 버튼이 나타나지 않습니다.

---

## 수동 용어 추가

자동 후보에 없는 용어는 두 가지 방법으로 추가합니다.

### 방법 1: 브라우저의 `+ 수동 용어` 버튼

화면에서 source term, translation, status, category를 입력합니다. `candidate_id`와 근거 컬럼은 비워둡니다.

### 방법 2: TSV 마지막에 직접 추가

아래에서 `⇥`는 탭 한 칸입니다. 근거 필드(`variants`~`candidate_id`)는 모두 비워둡니다.

```text
approved⇥Critical Hit⇥치명타⇥term⇥항상 치명타로 번역⇥⇥⇥⇥⇥
```

### import 시 자동 처리

`glk glossary import`는 빈 `candidate_id`를 수동 추가 행으로 판정하고:

1. 승인 원문에서 `source_term`을 검색합니다.
2. 대소문자와 보수적인 단수·복수 변형을 탐색합니다.
   - 예: `Hunter`를 추가하면 `Hunters`도 같은 후보군으로 자동 병합
3. 출현 횟수, block ID, 페이지·이미지 위치와 예문을 수집합니다.
4. 안정적인 `candidate_id`를 생성합니다.
5. 최종 termbase에 `origin: manual`로 기록합니다.

검증에 성공하면 계산한 근거를 원래 `glossary_review.tsv`에도 다시 기록합니다.

> "보수적인 단수·복수 변형"이란: `Hunter` → `Hunters`처럼 영어의 일반적인 복수형 규칙(`-s`, `-es`, `-ies`)만 자동 검색합니다. 의미가 달라질 수 있는 불규칙 변형은 합치지 않고 별도 후보로 둡니다.

---

## Import 검증 규칙

`glk glossary import`는 다음 조건에서 차단됩니다.

| 차단 조건 | 해결 방법 |
|---|---|
| `review` 상태가 남아 있음 | 모든 용어를 `approved`, `keep`, `rejected` 중 하나로 결정 |
| `approved`인데 번역어가 비어 있음 | 번역어를 입력하거나 `keep`으로 변경 |
| 허용되지 않은 status 또는 category 값 | 위 허용 목록의 값만 사용 |
| 같은 용어가 중복됨 | 대소문자·단수·복수가 같은 후보군을 하나로 합침 |
| 자동 후보의 `candidate_id`가 삭제·변경·중복됨 | 삭제하지 말고 `rejected`로 처리 |
| 수동 용어가 승인 원문에서 발견되지 않음 | 원문 확인 후 철자 수정, 또는 `--allow-missing-terms` 사용 |
| `[HP]`, `[DEF]` 같은 보호 token을 용어로 등록 | 보호 token은 용어집이 아니라 OCR prompt에서 관리 |

### 원문에 없는 용어를 의도적으로 등록할 때

확장판 용어처럼 현재 원문에는 없지만 미리 등록하고 싶은 경우:

```bash
glk glossary import \
  --project sample_rulebook \
  --file 03_terminology/glossary_review.tsv \
  --allow-missing-terms
```

이 항목은 termbase에 `origin: manual`, `source_verified: false`로 기록되고 경고가 출력됩니다.

브라우저에서는 화면 아래의 `승인 원문에서 찾을 수 없는 수동 용어도 명시적으로 허용`을 선택한 경우에만 같은 동작을 합니다.

---

## TSV 파일 보존 정책

- `glk glossary build`를 다시 실행해도 기존 TSV를 덮어쓰지 않습니다.
- 승인 원문 또는 추출 설정이 변경되면 TSV를 `stale`로 표시합니다.
- `--force`는 기존 편집 내용을 모두 버리고 새 후보로 초기화합니다. 백업과 비교 후에만 사용합니다.

```bash
# 후보 수를 조정하거나 결과만 미리 확인
glk glossary build --project sample_rulebook --min-frequency 2 --max-words 4 --max-candidates 500
glk glossary build --project sample_rulebook --dry-run
```

---

## 최종 출력: termbase.json

```text
03_terminology/termbase.json
```

termbase entry에 저장되는 정보:

| 필드 | 설명 |
|---|---|
| source_term | 기준 원문 용어 |
| translation | 확정 번역어 (`keep`이면 원문과 동일) |
| category | 용어 유형 |
| status | `approved` 또는 `keep` |
| note | 번역 원칙·메모 |
| variants | 대소문자·복수형 변형 |
| occurrences | 승인 원문 출현 횟수 |
| block_ids | 출현 block ID 목록 |
| locations | 페이지·이미지 위치 |
| example | 실제 사용 예문 |
| origin | `automatic` 또는 `manual` |
| source_verified | 승인 원문에서 근거를 확인했는지 여부 |

번역 prompt에는 `approved`와 `keep`만 전달합니다. `rejected`는 "이 용어는 의도적으로 제외했다"는 이력으로 보존됩니다.

### 상태 판정

`glk status`는 승인 원문, 검토 TSV와 termbase의 저장 hash가 모두 일치할 때만 `Termbase: current`로 표시합니다. 셋 중 하나가 바뀌면 `stale`로 표시합니다.

---

## `--file` 경로 규칙

`glk glossary import --file`이 상대 경로이면:

1. project workspace 내부를 먼저 찾습니다.
2. 없으면 현재 작업 디렉터리를 확인합니다.

workspace 외부의 TSV를 지정해도 검증 성공 후 정규화된 결과는 프로젝트의 `03_terminology/glossary_review.tsv`에 기록됩니다.
