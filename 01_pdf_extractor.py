import os
import logging
from PyPDF2 import PdfReader
from common import init_config_only, get_absolute_path


def extract_text_from_pdfs(config):
    """Extracts text from all PDF files in the source directory."""
    pdf_source_dir = get_absolute_path(config["paths"]["pdf_source_dir"])
    english_txt_dir = get_absolute_path(config["paths"]["english_txt_dir"])

    os.makedirs(english_txt_dir, exist_ok=True)

    logging.info(f"Starting PDF text extraction from '{pdf_source_dir}'")

    if not os.path.isdir(pdf_source_dir):
        logging.error(f"Source directory not found: '{pdf_source_dir}'")
        return

    for filename in os.listdir(pdf_source_dir):
        if filename.lower().endswith(".pdf"):
            pdf_path = os.path.join(pdf_source_dir, filename)
            txt_filename = os.path.splitext(filename)[0] + ".txt"
            txt_path = os.path.join(english_txt_dir, txt_filename)

            if os.path.exists(txt_path):
                logging.info(f"Skipping '{filename}': Output file already exists.")
                continue

            try:
                reader = PdfReader(pdf_path)
                text = ""
                for i, page in enumerate(reader.pages):
                    page_text = page.extract_text() or ""
                    text += f"[PAGE {i + 1}]\n{page_text}\n\n"

                with open(txt_path, 'w', encoding='utf-8') as f:
                    f.write(text)
                logging.info(f"Successfully extracted text from '{filename}'")

            except Exception as e:
                logging.error(f"Failed to process '{filename}': {e}")


if __name__ == "__main__":
    config = init_config_only()
    if config:
        extract_text_from_pdfs(config)