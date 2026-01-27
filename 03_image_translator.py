import os
import logging
import time
from PIL import Image
from io import BytesIO
import google.genai as genai  # 최신 Google GenAI SDK
from common import load_config, setup_logging, get_absolute_path, get_api_key


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


def main():
    # 인자 없이 setup_logging()을 호출하도록 수정
    setup_logging()
    config = load_config()
    if not config:
        return

    api_key = get_api_key(config)
    if not api_key:
        return

    client = genai.Client(api_key=api_key)
    model_name = config['translation']['model_name']

    image_source_dir = get_absolute_path(config['paths']['image_source_dir'])
    image_output_dir = get_absolute_path(config['paths']['image_output_dir'])

    keep_terms_list = config.get('keep_terms', [])
    keep_terms_str = ", ".join(keep_terms_list)

    prompt_template = config['prompts']['image_translation']
    if isinstance(prompt_template, list):
        prompt_template = "\n".join(prompt_template)

    prompt = prompt_template.format(keep_terms=keep_terms_str)

    os.makedirs(image_output_dir, exist_ok=True)

    image_files = [f for f in os.listdir(image_source_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))]
    total_files = len(image_files)

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
            img = Image.open(input_path)
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[prompt, img],
                )
            finally:
                img.close()

            if response.parts:
                for part in response.parts:
                    inline_data = getattr(part, "inline_data", None)
                    if inline_data and inline_data.mime_type and inline_data.mime_type.startswith("image/"):
                        img_data = inline_data.data
                        if not img_data:
                            logging.warning(f"Image data empty in response for {filename}")
                            continue
                        save_image_corrected_size(img_data, input_path, output_path)
                        break
                else:
                    logging.warning(f"No image data found in response for {filename}")
            else:
                # Check for blocked response
                prompt_feedback = response.prompt_feedback
                if prompt_feedback and prompt_feedback.block_reason:
                    logging.error(f"Request for {filename} was blocked: {prompt_feedback.block_reason.name}")
                else:
                    logging.warning(f"Empty response for {filename}. Text part: {response.text}")

        except Exception as e:
            logging.error(f"Failed to process {filename}: {e}")
            time.sleep(2)

        time.sleep(2)


if __name__ == "__main__":
    main()
