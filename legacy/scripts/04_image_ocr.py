import os
import logging
from PIL import Image
from common import (
    get_absolute_path, build_prompt_string,
    call_gemini_with_retry, init_pipeline, list_image_files,
)


def perform_gemini_ocr(client, model_name, image_path, prompt):
    """Use Gemini model to extract text from an image."""
    try:
        with Image.open(image_path) as img:
            response = call_gemini_with_retry(client, model_name, [prompt, img])

        if response.text:
            return response.text.strip()

        prompt_feedback = getattr(response, "prompt_feedback", None)
        if prompt_feedback and prompt_feedback.block_reason:
            logging.error(
                f"OCR blocked for {image_path}: {prompt_feedback.block_reason.name}"
            )
        else:
            logging.warning(f"OCR returned no text for {image_path}")
        return None
    except FileNotFoundError:
        logging.error(f"OCR Error: Image file not found at {image_path}")
        return None
    except Exception as e:
        logging.error(f"Gemini OCR Error processing {image_path}: {e}")
        return None


def main():
    config, client = init_pipeline()
    if not config:
        return

    model_name = config.get('translation', {}).get('model_name', 'gemini-pro-vision')

    prompt_raw = config.get('prompts', {}).get('image_ocr')
    if not prompt_raw:
        logging.error("Prompt 'image_ocr' not found in config.json")
        return

    prompt = build_prompt_string(prompt_raw)

    image_source_dir = get_absolute_path(config['paths']['image_source_dir'])
    ocr_output_dir = get_absolute_path(config['paths']['image_ocr_dir'])

    os.makedirs(ocr_output_dir, exist_ok=True)
    logging.info(f"Image source directory: {image_source_dir}")
    logging.info(f"OCR text output directory: {ocr_output_dir}")

    if not os.path.exists(image_source_dir):
        logging.error(f"Error: Image source directory not found: {image_source_dir}")
        return

    image_files = list_image_files(image_source_dir, config)

    if not image_files:
        logging.warning(f"No image files found in {image_source_dir}")
        return

    logging.info(f"Found {len(image_files)} image files to process.")

    for image_file in image_files:
        image_path = os.path.join(image_source_dir, image_file)
        txt_filename = os.path.splitext(image_file)[0] + '.txt'
        output_txt_path = os.path.join(ocr_output_dir, txt_filename)

        if os.path.exists(output_txt_path):
            logging.info(f"Skipping {image_file}: Output text file already exists.")
            continue

        logging.info(f"Processing {image_file} with Gemini...")
        extracted_text = perform_gemini_ocr(client, model_name, image_path, prompt)

        if extracted_text:
            with open(output_txt_path, 'w', encoding='utf-8') as f:
                f.write(extracted_text.strip())
            logging.info(f"Successfully extracted text from {image_file} and saved to {output_txt_path}")
        else:
            logging.warning(f"No significant text extracted from {image_file}. Skipping saving empty file.")

    logging.info("Gemini OCR process completed.")


if __name__ == "__main__":
    main()
