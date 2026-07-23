# 레거시 코드

이 폴더는 통합 `glk` CLI 이전의 단일 실행 스크립트와 PoC 자료를 보존합니다.

- `scripts/`: `01_*`~`06_*` 방식의 예전 실행 스크립트
- `experiments/`: PDF 레이아웃·이미지 OCR PoC 코드
- `docs/`: 과거 설계, PoC 결과와 상세 작업 기록
- `samples/`: PoC prompt와 아이콘 자료
- `data/`: 예전 `90_*`~`97_*`, `9999. final` 입력·출력 자료
- `requirements.txt`: 예전 스크립트 환경의 의존성 기록

신규 작업과 유지보수 대상은 `source/src/glk/`와 `source/tests/`입니다. 이 폴더의 코드는 현재 CLI 회귀 테스트 대상이 아니며, 별도 수정 없이 정상 동작한다고 보장하지 않습니다.

개인용 예전 설정은 Git에서 제외된 `legacy/00_config.json`에만 남아 있습니다. API 키는 `source/.env` 또는 환경변수로 관리합니다. `data/`의 대용량 자료도 Git에서 제외되므로 다른 컴퓨터에 자동 동기화되지 않습니다.
