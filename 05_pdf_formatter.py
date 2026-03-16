import os
import re
import logging
import time
from PyPDF2 import PdfReader
from common import (
    get_absolute_path, build_prompt_string,
    call_gemini_with_retry, init_pipeline,
)


PAGE_HEADER_PATTERN = re.compile(r'^\[PAGE (\d+)\]$', re.MULTILINE)


def parse_pages_from_text(text):
    """
    [PAGE n] 구분자로 텍스트를 파싱하여 {페이지번호: 내용} 딕셔너리를 반환합니다.
    re.findall + 위치 기반 슬라이싱으로 내용 안에 [PAGE n] 패턴이 있어도 안전합니다.
    """
    pages = {}
    matches = list(PAGE_HEADER_PATTERN.finditer(text))
    for idx, match in enumerate(matches):
        page_num = int(match.group(1))
        content_start = match.end() + 1  # \n 건너뛰기
        content_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        pages[page_num] = text[content_start:content_end].strip()
    return pages


def build_format_prompt(original_page_text, translated_page_text, prompt_template):
    """Builds the prompt for the Gemini API call."""
    prompt_str = build_prompt_string(prompt_template)
    return prompt_str.format(
        original_layout=original_page_text,
        translated_text=translated_page_text
    )


def format_single_pdf(client, model_name, prompt_template, translated_txt_path, pdf_source_dir, output_dir):
    """
    Processes a single translated text file against its original PDF to format the output.
    """
    base_filename = os.path.basename(translated_txt_path).replace('_ko.txt', '')
    original_pdf_path = os.path.join(pdf_source_dir, base_filename + '.pdf')

    output_filename = base_filename + '_formatted.txt'
    output_path = os.path.join(output_dir, output_filename)
    tmp_output_path = output_path + '.tmp'

    if os.path.exists(output_path):
        logging.info(f"Skipping '{base_filename}': Formatted file already exists.")
        return

    if not os.path.exists(original_pdf_path):
        logging.error(f"Original PDF not found for '{translated_txt_path}'. Searched at: {original_pdf_path}")
        return

    try:
        # 1. 원본 PDF와 번역된 텍스트 파일 읽기
        pdf_reader = PdfReader(original_pdf_path)
        num_pages = len(pdf_reader.pages)

        with open(translated_txt_path, 'r', encoding='utf-8') as f:
            translated_content = f.read()

        # 2. 번역된 텍스트를 페이지별로 분리 (견고한 파서 사용)
        translated_pages_dict = parse_pages_from_text(translated_content)

        # 3. 임시 파일이 있으면 진행 상황 복구
        processed_pages = {}
        if os.path.exists(tmp_output_path):
            logging.info(f"Resuming from temporary file: {tmp_output_path}")
            with open(tmp_output_path, 'r', encoding='utf-8') as f:
                tmp_content = f.read()
            processed_pages = parse_pages_from_text(tmp_content)

    except Exception as e:
        logging.error(f"Error preparing files for '{base_filename}': {e}")
        return

    # 4. 페이지 단위로 순회하며 포맷팅 수행
    with open(tmp_output_path, 'w', encoding='utf-8') as f_tmp:
        # 복구된 내용 먼저 쓰기
        for pg in sorted(processed_pages.keys()):
            f_tmp.write(f"[PAGE {pg}]\n{processed_pages[pg]}\n\n")

        for i in range(num_pages):
            page_num = i + 1
            if page_num in processed_pages:
                logging.info(f"Page {page_num} already processed. Skipping.")
                continue

            logging.info(f"Formatting page {page_num}/{num_pages} for '{base_filename}'...")

            try:
                original_page_text = pdf_reader.pages[i].extract_text() or ""
                translated_page_text = translated_pages_dict.get(page_num, "").strip()

                if not translated_page_text:
                    logging.warning(f"No translated text found for page {page_num}. Writing empty.")
                    formatted_text = ""
                else:
                    prompt = build_format_prompt(original_page_text, translated_page_text, prompt_template)
                    response = call_gemini_with_retry(client, model_name, prompt)
                    formatted_text = response.text.strip()

                f_tmp.write(f"[PAGE {page_num}]\n{formatted_text}\n\n")
                f_tmp.flush()
                logging.info(f"Successfully formatted page {page_num}.")
                time.sleep(1)

            except Exception as e:
                logging.error(f"Failed to format page {page_num} for '{base_filename}': {e}. Skipping page.")
                f_tmp.write(f"[PAGE {page_num}]\n[FORMAT ERROR: {e}]\n\n")
                f_tmp.flush()
                continue

    # 5. 모든 페이지 처리가 완료되면 임시 파일의 이름을 최종 파일명으로 변경
    os.rename(tmp_output_path, output_path)
    logging.info(f"Successfully created formatted file: {output_path}")


def main():
    config, client = init_pipeline("05_formatter.log")
    if not config:
        return

    model_name = config.get('translation', {}).get('model_name', 'gemini-1.5-flash-latest')
    prompt_template = config.get('prompts', {}).get('pdf_format')
    if not prompt_template:
        logging.error("Prompt 'pdf_format' not found in config.json. Please add it.")
        return

    # 경로 설정
    pdf_source_dir = get_absolute_path(config['paths']['pdf_source_dir'])
    translated_txt_dir = get_absolute_path(config['paths']['translated_txt_dir'])
    output_dir = get_absolute_path(config['paths']['pdf_formatted_dir'])

    os.makedirs(output_dir, exist_ok=True)
    logging.info(f"Formatted text output directory: {output_dir}")

    if not os.path.exists(translated_txt_dir):
        logging.error(f"Translated text directory not found: {translated_txt_dir}")
        return

    translated_files = [f for f in os.listdir(translated_txt_dir) if f.endswith("_ko.txt")]
    if not translated_files:
        logging.warning(f"No translated text files (*_ko.txt) found in {translated_txt_dir}")
        return

    logging.info(f"Found {len(translated_files)} translated files to format.")

    for translated_file in translated_files:
        translated_txt_path = os.path.join(translated_txt_dir, translated_file)
        format_single_pdf(client, model_name, prompt_template, translated_txt_path, pdf_source_dir, output_dir)

    logging.info("PDF formatting process completed.")


if __name__ == "__main__":
    main()
