# 로드맵

이 문서는 아직 구현하지 않은 작업과 우선순위만 관리합니다. 완료 기능의 사용법은 [전체 작업 흐름](../guides/workflow.md), 현재 구현 구조는 [아키텍처](../reference/architecture.md), 최근 검증 상태는 [인수인계](handoff.md)를 따릅니다.

## 현재 우선순위: 기존 번역 정렬

- [ ] 기존 초벌 번역을 page/key/text 기준으로 정렬
- [ ] 낮은 정렬 신뢰도는 자동 병합하지 않고 검토 대상으로 표시

완료 조건:

- 기존 번역 정렬의 불확실한 결과는 자동으로 승인 데이터에 섞이지 않습니다.

## 번역 QA와 선택 재번역

- [ ] QA 실패 segment만 선택적으로 재번역
- [ ] 승인·locked 번역을 자동 수정 대상에서 제외
- [ ] 수정 전후와 사용 prompt/model을 revision으로 기록

## 원문 획득 보강

- [ ] 텍스트 레이어가 없는 스캔 PDF의 OCR fallback
- [ ] 텍스트·스캔이 섞인 hybrid PDF 페이지별 처리
- [ ] 자동 회전, 기울기, 여백과 대비 보정
- [ ] 내장 텍스트와 OCR 결과가 크게 다른 페이지 표시
- [ ] 표, 사이드바, 캡션과 반복 머리말·꼬리말 탐지
- [ ] 문맥 오탐을 줄인 `5/S`, `8/B` OCR 혼동 규칙
- [ ] 원문 검수용 HTML 리포트
- [ ] 텍스트 PDF, 스캔 PDF, hybrid PDF와 이미지 폴더 fixture

## 운영 안정성과 배포

- [ ] `Retry-After`, exponential backoff jitter와 호출 동시성 제한
- [ ] 단계별 처리 시간, 성공·실패·재시도와 사용량 기록
- [ ] 로그의 API 키·전체 prompt 노출 방지 회귀 검사
- [ ] `pytest` 또는 현재 unittest 기준의 CI 구성
- [ ] `ruff`, secret scan과 지원 Python 버전 matrix
- [ ] 실패 결과가 정상 출력으로 승격되지 않는 통합 테스트
- [ ] 설치형 실행 파일 또는 GUI 필요성은 CLI workflow가 안정된 뒤 재평가

## 완료된 기반

현재 완료된 큰 범위만 기록합니다.

- [x] 크로스 플랫폼 `glk` CLI와 project workspace
- [x] PDF fragment 추출과 Gemini 읽기 순서 복원
- [x] 재귀 이미지 폴더 OCR, 공통·개별 prompt, 개별·통합 TXT
- [x] provider 독립 SourceBlock과 안정적인 ID·bbox·source hash
- [x] 로컬 원문 QA와 사람이 편집하는 draft/review TXT
- [x] 최종 원문 승인 gate와 approved JSONL
- [x] 승인 원문 기반 로컬 용어 후보 TSV, 편집 보존과 stale 감지
- [x] 검토 TSV 검증, 수동 용어 근거 보충과 versioned termbase import
- [x] ID 기반 번역 segment, prompt 우선순위 compiler와 관련 용어 선별
- [x] Gemini JSON 초벌 번역, 즉시 숫자·token·용어 검증과 청크 재요청
- [x] 입력 hash 체크포인트, partial 보존과 `--resume`
- [x] 번역 review marker·source·block 순서 보호와 stale 판정
- [x] 숫자·token·HTML·확정 용어 로컬 QA와 JSON/Markdown 리포트
- [x] 사람 수정만 분리 보존하는 approved translation과 최종 TXT
- [x] localhost HTML 원문·번역 대조, 검색과 오류·경고·수정 필터
- [x] block ID 기반 안전 저장, 로컬 QA와 최종 승인 UI
- [x] session token·Origin·hash 충돌 검사를 적용한 로컬 전용 서버

## 범위 관리 규칙

- 완료 항목의 상세 설명을 이 문서에 계속 누적하지 않습니다.
- 사용자가 실행해야 하는 새 단계는 [전체 작업 흐름](../guides/workflow.md)에 반영합니다.
- 데이터 계약이나 계층이 바뀌면 [아키텍처](../reference/architecture.md) 또는 해당 사양 문서를 갱신합니다.
- 우선순위가 바뀌면 이 문서와 [인수인계](handoff.md)의 바로 다음 작업을 함께 갱신합니다.
