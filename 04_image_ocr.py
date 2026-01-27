import os
import logging
from PIL import Image
import google.generativeai as genai
from common import load_config, setup_logging, get_absolute_path, get_api_key

def perform_gemini_ocr(model, image_path, prompt):
    """Gemini 모델을 사용하여 이미지에서 텍스트를 추출합니다."""
    try:
        img = Image.open(image_path)
        response = model.generate_content([prompt, img])
        return response.text
    except FileNotFoundError:
        logging.error(f"OCR Error: Image file not found at {image_path}")
        return None
    except Exception as e:
        logging.error(f"Gemini OCR Error processing {image_path}: {e}")
        return None

def main():
    setup_logging('04_image_ocr.log')
    config = load_config()
    if not config:
        return

    api_key = get_api_key(config)
    if not api_key:
        return
    genai.configure(api_key=api_key)

    model_name = config.get('translation', {}).get('model_name', 'gemini-pro-vision')
    model = genai.GenerativeModel(model_name)
    
    prompt_template = config.get('prompts', {}).get('image_ocr')
    if not prompt_template:
        logging.error("Prompt 'image_ocr' not found in config.json")
        return
        
    prompt = "\\n".join(prompt_template) if isinstance(prompt_template, list) else prompt_template

    image_source_dir = get_absolute_path(config['paths']['image_source_dir'])
    ocr_output_dir = get_absolute_path(config['paths']['image_ocr_dir'])

    os.makedirs(ocr_output_dir, exist_ok=True)
    logging.info(f"Image source directory: {image_source_dir}")
    logging.info(f"OCR text output directory: {ocr_output_dir}")

    if not os.path.exists(image_source_dir):
        logging.error(f"Error: Image source directory not found: {image_source_dir}")
        return

    image_files = [f for f in os.listdir(image_source_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp'))]
    
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
        extracted_text = perform_gemini_ocr(model, image_path, prompt)

        if extracted_text:
            with open(output_txt_path, 'w', encoding='utf-8') as f:
                f.write(extracted_text.strip())
            logging.info(f"Successfully extracted text from {image_file} and saved to {output_txt_path}")
        else:
            logging.warning(f"No significant text extracted from {image_file}. Skipping saving empty file.")

    logging.info("Gemini OCR process completed.")

if __name__ == "__main__":
    main()
