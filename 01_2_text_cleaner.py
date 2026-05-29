import argparse
import os
import re
import json

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_FILENAME = "00_config.json"


def load_config(config_filename=DEFAULT_CONFIG_FILENAME):
    config_path = os.path.join(PROJECT_ROOT, config_filename)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_absolute_path(relative_path):
    return os.path.join(PROJECT_ROOT, relative_path)


def _should_merge_lines(curr: str, nxt: str) -> bool:
    c = curr.strip()
    n = nxt.strip()
    if not c or not n:
        return False
    if not re.fullmatch(r"[A-Za-z]{1,20}", c):
        return False
    if not re.match(r"^[a-z][A-Za-z'\-]*", n):
        return False

    # Strong split cases: "I\nntroduction", "Cooldo\nwn"
    if len(c) == 1 and c.isalpha():
        return True
    if len(c) >= 4 and not c.isupper():
        return True
    return False


def clean_text(text: str) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\ufb01", "fi").replace("\ufb02", "fl")

    # Remove obvious replacement-char noise and OCR separators.
    t = re.sub(r"\uFFFD+", " ", t)
    t = re.sub(r"[•·●■□▪▫]+", " ", t)
    t = re.sub(r"[._]{5,}", " ", t)

    # Fix OCR artifact like /T_his -> This
    t = re.sub(r"/([A-Za-z])_", r"\1", t)

    # Add missing space between glued words (e.g., EraTable -> Era Table)
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)

    # Line-based merge for broken words.
    lines = t.split("\n")
    merged = []
    i = 0
    while i < len(lines):
        curr = lines[i]
        if i + 1 < len(lines) and _should_merge_lines(curr, lines[i + 1]):
            merged.append(curr.strip() + lines[i + 1].lstrip())
            i += 2
            continue
        merged.append(curr)
        i += 1

    t = "\n".join(merged)

    # Fix split token like "T rack" -> "Track" in the middle of lines.
    t = re.sub(r"\b([A-Z])\s([a-z]{2,})\b", r"\1\2", t)

    # Normalize whitespace while preserving line structure.
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)

    # Trim spaces around line breaks.
    t = re.sub(r" *\n *", "\n", t)

    return t.strip() + "\n"


def process_file(input_path: str, output_path: str):
    with open(input_path, "r", encoding="utf-8") as f:
        raw = f.read()

    cleaned = clean_text(raw)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(cleaned)


def main():
    parser = argparse.ArgumentParser(description="Clean extracted PDF text for translation readiness.")
    parser.add_argument("--file", help="Single txt filename under english_txt_dir (e.g., TES_Rulebook.txt)")
    parser.add_argument("--suffix", default="_clean", help="Output suffix before .txt")
    parser.add_argument("--inplace", action="store_true", help="Overwrite original files")
    args = parser.parse_args()

    config = load_config()
    english_txt_dir = get_absolute_path(config["paths"]["english_txt_dir"])

    if args.file:
        filenames = [args.file]
    else:
        filenames = [fn for fn in os.listdir(english_txt_dir) if fn.lower().endswith(".txt")]

    for filename in filenames:
        input_path = os.path.join(english_txt_dir, filename)
        if not os.path.exists(input_path):
            print(f"Skip (not found): {input_path}")
            continue

        if args.inplace:
            output_path = input_path
        else:
            stem, ext = os.path.splitext(filename)
            output_path = os.path.join(english_txt_dir, f"{stem}{args.suffix}{ext}")

        process_file(input_path, output_path)
        print(f"Cleaned: {filename}")
        print(f" -> {output_path}")


if __name__ == "__main__":
    main()
