import argparse
import csv
import json
import os
import re
from collections import Counter

COMMON_STOPWORDS = {
    "A", "An", "And", "Any", "Are", "As", "At", "Be", "By", "Can", "Each", "For", "From",
    "Has", "Have", "If", "In", "Into", "Is", "It", "Its", "May", "Must", "No", "Not", "Of",
    "On", "Or", "That", "The", "Their", "Then", "This", "To", "Up", "When", "With", "You",
    "Your", "Page", "Rulebook", "Version", "Table", "Contents", "Chapter", "Step", "Steps", "Phase",
    "Phases", "Round", "Turn", "Turns", "Example", "Examples", "Setup", "Rules", "Game", "Games",
    "He", "She", "They", "Them", "There", "These", "This", "Those", "Some", "Any", "All", "Each",
    "After", "Before", "During", "While", "When", "Then", "However", "Since", "Also", "Note", "See",
    "Place", "Set", "Start", "Choose", "Counter", "Draw", "Resolve", "Gain", "Save", "Saved",
    "Successfully", "If", "To", "From", "With", "Without", "Into", "Over", "Under",
}

LOWER_CONNECTORS = {"of", "the", "and", "for", "to", "in", "on", "from", "with", "&"}
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def load_config(config_filename="00_config.json"):
    config_path = os.path.join(PROJECT_ROOT, config_filename)
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_absolute_path(relative_path):
    return os.path.join(PROJECT_ROOT, relative_path)


def normalize_text(text):
    text = text.replace("\ufb01", "fi").replace("\ufb02", "fl")

    # Remove page markers and obvious OCR separators.
    text = re.sub(r"\[PAGE\s+\d+\]", " ", text)
    text = re.sub(r"[\uFFFD]+", " ", text)
    text = re.sub(r"[•·●■□▪▫]+", " ", text)
    text = re.sub(r"[._]{3,}", " ", text)

    # Fix common OCR pattern like /T_his, /f_irst -> This, first
    text = re.sub(r"/([A-Za-z])_", r"\1", text)

    # Remove most punctuation except apostrophes/hyphens for names.
    text = re.sub(r"[^A-Za-z0-9\n\-': ]+", " ", text)

    # Compress whitespace but keep line boundaries for optional future tuning.
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _is_title_like(token):
    if not token:
        return False
    if token.isupper() and len(token) >= 2:
        return True
    if token[0].isupper() and any(ch.isalpha() for ch in token[1:]):
        return True
    return False


def _clean_token(token):
    token = token.strip(" -:'")
    return token


def _non_sentence_start_ratio(term, source_text, ignore_case=False):
    flags = re.IGNORECASE if ignore_case else 0
    pattern = re.compile(rf"\b{re.escape(term)}\b", flags)
    total = 0
    non_start = 0

    for match in pattern.finditer(source_text):
        total += 1
        idx = match.start()
        j = idx - 1
        while j >= 0 and source_text[j].isspace():
            j -= 1
        if j < 0:
            continue
        if source_text[j] not in ".!?\n:":
            non_start += 1

    if total == 0:
        return 0.0
    return non_start / total


def _count_term_case_insensitive(term, source_text):
    parts = [re.escape(p) for p in term.split()]
    if not parts:
        return 0
    pattern = re.compile(r"\b" + r"\s+".join(parts) + r"\b", re.IGNORECASE)
    return len(pattern.findall(source_text))


def _has_capitalized_variant(term, source_text):
    if not term:
        return False
    cap = term[0].upper() + term[1:]
    pattern = re.compile(rf"\b{re.escape(cap)}\b")
    return bool(pattern.search(source_text))


def _has_non_sentence_cap_variant(term, source_text):
    if not term:
        return False
    cap = term[0].upper() + term[1:]
    pattern = re.compile(rf"\b{re.escape(cap)}\b")
    for match in pattern.finditer(source_text):
        idx = match.start()
        j = idx - 1
        while j >= 0 and source_text[j].isspace():
            j -= 1
        if j < 0:
            continue
        if source_text[j] not in ".!?\n:":
            return True
    return False


def extract_candidates(text, min_freq=2, max_words=6):
    tokens = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    candidates = Counter()

    i = 0
    n = len(tokens)
    while i < n:
        tok = _clean_token(tokens[i])
        if not tok:
            i += 1
            continue

        if _is_title_like(tok) and tok not in COMMON_STOPWORDS:
            phrase = [tok]
            j = i + 1
            while j < n and len(phrase) < max_words:
                nxt = _clean_token(tokens[j])
                if not nxt:
                    break
                if _is_title_like(nxt):
                    phrase.append(nxt)
                    j += 1
                    continue
                if nxt.lower() in LOWER_CONNECTORS:
                    if j + 1 < n and _is_title_like(_clean_token(tokens[j + 1])):
                        phrase.append(nxt)
                        j += 1
                        continue
                break

            # Register only the full phrase to reduce prefix noise.
            joined = " ".join(phrase)
            candidates[joined] += 1

            i = j
        else:
            i += 1

    # Conservative lowercase pass: include only frequent terms with capitalized evidence.
    lower_tokens = re.findall(r"\b[a-z][a-z'\-]{5,}\b", text)
    lower_counts = Counter(tok.strip(" -:'") for tok in lower_tokens)
    for tok, freq in lower_counts.items():
        if freq < min_freq:
            continue
        if tok in LOWER_CONNECTORS:
            continue
        if _non_sentence_start_ratio(tok, text, ignore_case=True) < 0.25:
            continue
        if not _has_capitalized_variant(tok, text):
            continue
        if not _has_non_sentence_cap_variant(tok, text):
            continue
        if not any(existing.lower() == tok for existing in candidates.keys()):
            candidates[tok] = freq

    filtered = {}
    allowed_short = {"HP", "XP", "EP", "NPC"}
    for term, freq in candidates.items():
        effective_freq = _count_term_case_insensitive(term, text)
        if effective_freq < min_freq:
            continue
        if len(term) <= 1:
            continue
        # Remove lines that are mostly generic headings.
        if term in COMMON_STOPWORDS:
            continue
        words = term.split()
        if any(w in COMMON_STOPWORDS for w in words):
            continue
        if len(words) == 1 and len(words[0]) < 4 and words[0] not in allowed_short:
            continue
        if words[0].lower() in LOWER_CONNECTORS or words[-1].lower() in LOWER_CONNECTORS:
            continue
        if any(len(w) == 1 for w in words):
            continue
        if len(words) == 1 and words[0] not in allowed_short:
            if _non_sentence_start_ratio(words[0], text, ignore_case=True) < 0.25:
                continue
        filtered[term] = effective_freq

    # Sort by frequency desc, then term length desc.
    return sorted(filtered.items(), key=lambda x: (-x[1], -len(x[0]), x[0]))


def write_outputs(candidates, output_base):
    csv_path = output_base + "_candidates.csv"
    json_path = output_base + "_glossary_template.json"

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["term", "count", "recommended_action", "korean"])
        for term, count in candidates:
            action = "keep_or_define"
            writer.writerow([term, count, action, ""])

    glossary_obj = {term: "" for term, _ in candidates}
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(glossary_obj, f, ensure_ascii=False, indent=2)

    return csv_path, json_path


def process_file(input_path, output_dir, min_freq):
    with open(input_path, "r", encoding="utf-8") as f:
        text = f.read()

    normalized = normalize_text(text)
    candidates = extract_candidates(normalized, min_freq=min_freq)

    stem = os.path.splitext(os.path.basename(input_path))[0]
    output_base = os.path.join(output_dir, stem)
    csv_path, json_path = write_outputs(candidates, output_base)
    return csv_path, json_path, len(candidates)


def _prefer_clean_path(path):
    base, ext = os.path.splitext(path)
    if base.endswith("_clean"):
        return path
    clean_path = f"{base}_clean{ext}"
    if os.path.exists(clean_path):
        return clean_path
    return path


def main():
    parser = argparse.ArgumentParser(description="Build glossary candidates from extracted English TXT files.")
    parser.add_argument("--file", help="Single txt filename under english_txt_dir (e.g., TES_Rulebook.txt)")
    parser.add_argument("--min-freq", type=int, default=3, help="Minimum frequency to keep a term")
    parser.add_argument("--output-dir", default="96_glossary_candidates", help="Output directory")
    args = parser.parse_args()

    config = load_config()

    english_txt_dir = get_absolute_path(config["paths"]["english_txt_dir"])
    output_dir = get_absolute_path(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    targets = []
    if args.file:
        targets = [_prefer_clean_path(os.path.join(english_txt_dir, args.file))]
    else:
        raw_targets = [
            os.path.join(english_txt_dir, fn)
            for fn in os.listdir(english_txt_dir)
            if fn.lower().endswith(".txt")
        ]
        # If both raw and _clean exist, keep only _clean target.
        dedup = {}
        for path in raw_targets:
            p = _prefer_clean_path(path)
            key = re.sub(r"_clean(?=\.txt$)", "", os.path.basename(p), flags=re.IGNORECASE)
            dedup[key] = p
        targets = sorted(dedup.values())

    if not targets:
        print("No txt files found.")
        return

    for path in targets:
        if not os.path.exists(path):
            print(f"Skip (not found): {path}")
            continue

        csv_path, json_path, count = process_file(path, output_dir, args.min_freq)
        print(f"Processed: {os.path.basename(path)}")
        print(f" - candidates: {count}")
        print(f" - csv: {csv_path}")
        print(f" - json: {json_path}")


if __name__ == "__main__":
    main()
