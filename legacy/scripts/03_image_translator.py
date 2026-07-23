import os
import logging
import time
from PIL import Image
from io import BytesIO
from common import (
    get_absolute_path, build_prompt_string,
    call_gemini_with_retry, init_pipeline, list_image_files,
)


def save_image_corrected_size(image_bytes, original_image_path, output_path):
    """
    Gemini가 생성한 이미지를 원본 이미지 크기에 맞춰 저장합니다.
    """
    try:
        with Image.open(original_image_path) as original_img:
            original_width, original_height = original_img.size

        generated_img = Image.open(BytesIO(image_bytes))
        resized_img = generated_img.resize((original_width, original_height), Image.Resampling.LANCZOS)
        resized_img.save(output_path, quality=95)
        logging.info(f"Saved resized image to {output_path} ({original_width}x{original_height})")
        return True
    except Exception as e:
        logging.error(f"Error resizing/saving image: {e}")
        try:
            with open(output_path, "wb") as f:
                f.write(image_bytes)
            logging.warning(f"Saved original size generated image (resize failed) to {output_path}")
        except Exception as save_e:
            logging.error(f"Could not even save the raw bytes: {save_e}")
        return False


def _extract_image_from_response(response):
    """
    Gemini 응답에서 이미지 데이터를 추출합니다.
    Returns (image_bytes, error_reason). 성공 시 error_reason은 None.
    """
    if not response.parts:
        prompt_feedback = response.prompt_feedback
        if prompt_feedback and prompt_feedback.block_reason:
            return None, f"blocked: {prompt_feedback.block_reason.name}"
        return None, "empty response"

    for part in response.parts:
        inline_data = getattr(part, "inline_data", None)
        if inline_data and inline_data.mime_type and inline_data.mime_type.startswith("image/"):
            if not inline_data.data:
                return None, "empty image data"
            return inline_data.data, None

    return None, "no image in response"


def _process_single_image(client, model_name, prompt, input_path, output_path, filename):
    """단일 이미지를 번역 처리합니다. Returns error_reason or None on success."""
    img = Image.open(input_path)
    try:
        response = call_gemini_with_retry(client, model_name, [prompt, img])
    finally:
        img.close()

    img_data, error_reason = _extract_image_from_response(response)
    if error_reason:
        logging.warning(f"{error_reason} for {filename}")
        return error_reason

    save_image_corrected_size(img_data, input_path, output_path)
    return None


def main():
    config, client = init_pipeline()
    if not config:
        return

    model_name = config['translation']['model_name']

    image_source_dir = get_absolute_path(config['paths']['image_source_dir'])
    image_output_dir = get_absolute_path(config['paths']['image_output_dir'])

    keep_terms_list = config.get('keep_terms', [])
    keep_terms_str = ", ".join(keep_terms_list)

    prompt_template = build_prompt_string(config['prompts']['image_translation'])
    prompt = prompt_template.format(keep_terms=keep_terms_str)

    os.makedirs(image_output_dir, exist_ok=True)

    image_files = list_image_files(image_source_dir, config)
    total_files = len(image_files)
    failed_files = []

    logging.info(f"Found {total_files} images in {image_source_dir}")
    logging.info(f"Output folder: {image_output_dir}")
    logging.info(f"Model: {model_name}")

    for idx, filename in enumerate(image_files):
        input_path = os.path.join(image_source_dir, filename)
        output_path = os.path.join(image_output_dir, filename)

        if os.path.exists(output_path):
            logging.info(f"[{idx + 1}/{total_files}] Skipping {filename} (already exists)")
            continue

        logging.info(f"[{idx + 1}/{total_files}] Processing {filename}...")

        try:
            error_reason = _process_single_image(client, model_name, prompt, input_path, output_path, filename)
            if error_reason:
                failed_files.append((filename, error_reason))
        except Exception as e:
            logging.error(f"Failed to process {filename}: {e}")
            failed_files.append((filename, str(e)))

        time.sleep(2)

    # 실패 목록 리포트
    if failed_files:
        logging.warning(f"\n{'='*50}")
        logging.warning(f"Failed files summary ({len(failed_files)}/{total_files}):")
        for fname, reason in failed_files:
            logging.warning(f"  - {fname}: {reason}")
        logging.warning(f"{'='*50}")
    else:
        logging.info("All images processed successfully.")


if __name__ == "__main__":
    main()
