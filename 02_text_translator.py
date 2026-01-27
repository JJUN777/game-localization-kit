import os
import json
import time
import logging
import google.genai as genai  # 최신 Google GenAI SDK
from common import load_config, setup_logging, get_absolute_path, get_api_key


class TranslationState:
    def __init__(self, state_path):
        self.state_path = state_path
        self.last_completed_idx = -1
        self.total_chunks = 0
        self.load()

    def load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.last_completed_idx = data.get('last_completed_idx', -1)
                    self.total_chunks = data.get('total_chunks', 0)
            except Exception as e:
                logging.error(f"Failed to load state file: {e}")

    def save(self):
        data = {
            'last_completed_idx': self.last_completed_idx,
            'total_chunks': self.total_chunks
        }
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)


def smart_chunk_text(text, max_chunk_size=2000):
    chunks = []
    current_chunk = []
    current_length = 0
    paragraphs = text.split('\n\n')
    for paragraph in paragraphs:
        if len(paragraph) > max_chunk_size:
            sentences = paragraph.replace('. ', '.\n').split('\n')
            for sentence in sentences:
                if current_length + len(sentence) + 1 > max_chunk_size:
                    chunks.append('\n\n'.join(current_chunk))
                    current_chunk = [sentence]
                    current_length = len(sentence)
                else:
                    current_chunk.append(sentence)
                    current_length += len(sentence) + 1
        else:
            if current_length + len(paragraph) + 2 > max_chunk_size:
                chunks.append('\n\n'.join(current_chunk))
                current_chunk = [paragraph]
                current_length = len(paragraph)
            else:
                current_chunk.append(paragraph)
                current_length += len(paragraph) + 2
    if current_chunk:
        chunks.append('\n\n'.join(current_chunk))
    return chunks


def translate_file_with_checkpointing(
    input_path,
    output_path,
    client,
    model_name,
    prompt_template,
    keep_terms_str,
    glossary_str,
):
    temp_output_path = output_path + ".tmp"
    state_path = output_path + ".meta.json"

    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logging.error(f"Failed to read {input_path}: {e}")
        return

    state = TranslationState(state_path)
    chunks = smart_chunk_text(content)
    state.total_chunks = len(chunks)
    state.save()

    logging.info(f"Processing {os.path.basename(input_path)}: {len(chunks)} chunks")

    if state.last_completed_idx >= 0:
        if not os.path.exists(temp_output_path):
            logging.warning("State file exists but temp output missing. Restarting.")
            state.last_completed_idx = -1
            state.save()
        else:
            logging.info(f"Resuming from chunk {state.last_completed_idx + 1}")

    for i, chunk in enumerate(chunks):
        if i <= state.last_completed_idx:
            continue

        try:
            prompt = prompt_template.format(text=chunk, keep_terms=keep_terms_str, glossary=glossary_str)
        except KeyError:
            prompt = prompt_template.replace("{text}", chunk)

        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            translated_text = response.text or ""

            with open(temp_output_path, 'a', encoding='utf-8') as f:
                f.write(translated_text + "\n\n")

            state.last_completed_idx = i
            state.save()

            logging.info(f"Translated chunk {i + 1}/{len(chunks)}")
            time.sleep(1)

        except Exception as e:
            logging.error(f"Error translating chunk {i + 1}: {e}")
            return

    if os.path.exists(output_path):
        os.remove(output_path)
    os.rename(temp_output_path, output_path)

    if os.path.exists(state_path):
        os.remove(state_path)

    logging.info(f"Completed: {os.path.basename(output_path)}")


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
    model_name = config["translation"]["model_name"]

    prompt_raw = config["prompts"].get("text_translation", "")
    prompt_template = "\n".join(prompt_raw) if isinstance(prompt_raw, list) else prompt_raw

    keep_terms = config.get("keep_terms", [])
    keep_terms_str = ", ".join(keep_terms) if isinstance(keep_terms, list) else str(keep_terms)

    glossary = config.get("glossary", {})
    glossary_lines = [f"- {k}: {v}" for k, v in glossary.items()]
    glossary_str = "\n".join(glossary_lines)

    english_txt_dir = get_absolute_path(config["paths"]["english_txt_dir"])
    translated_txt_dir = get_absolute_path(config["paths"]["translated_txt_dir"])

    os.makedirs(translated_txt_dir, exist_ok=True)

    if not os.path.exists(english_txt_dir):
        logging.error(f"Source directory not found: {english_txt_dir}")
        return

    for filename in os.listdir(english_txt_dir):
        if filename.endswith(".txt"):
            input_path = os.path.join(english_txt_dir, filename)
            filename_no_ext = os.path.splitext(filename)[0]
            output_filename = f"{filename_no_ext}_ko.txt"
            output_path = os.path.join(translated_txt_dir, output_filename)

            if os.path.exists(output_path):
                logging.info(f"Skipping {filename}: Output already exists.")
                continue

            translate_file_with_checkpointing(
                input_path,
                output_path,
                client,
                model_name,
                prompt_template,
                keep_terms_str,
                glossary_str,
            )


if __name__ == "__main__":
    main()
