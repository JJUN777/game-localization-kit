# 수동 smoke test 원본

새 설치 환경의 대시보드에서 PDF 추출과 이미지 OCR 흐름을 직접 확인하기 위한
작은 영문 룰북 샘플입니다. 자동 테스트 fixture가 아니며, 실제 Gemini 호출 시
사용량과 비용이 발생할 수 있습니다.

| 파일 | 용도 |
|---|---|
| `pdf_rulebook_2pages.pdf` | 2페이지 PDF의 텍스트 추출과 읽기 순서 재구성 확인 |
| `ocr_rulebook_01.png` | 여러 이미지 OCR의 첫 번째 페이지 |
| `ocr_rulebook_02.png` | 여러 이미지 OCR의 두 번째 페이지 |

PDF 흐름은 새 프로젝트에서 `pdf_rulebook_2pages.pdf` 하나를 등록합니다.
이미지 흐름은 별도 프로젝트에서 `ocr_rulebook_01.png`와
`ocr_rulebook_02.png`를 함께 등록합니다. 파일명 순서가 페이지 순서가 되므로
두 이미지의 숫자 접미사는 유지합니다.

등록한 원본은 프로젝트 workspace로 복사되며 이 폴더의 샘플은 변경되지
않습니다.
