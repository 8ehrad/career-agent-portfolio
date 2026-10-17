"""Lossless advert input plus a structure-preserving view. No network access."""
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
import re

VERSION = "advert-document-v2-structure"
BLOCK_TAGS = {"p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "ul", "ol"}


def fingerprint(value):
    # Keep the existing canonical hash format without importing offline retrieval into ingestion.
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class AdvertHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in BLOCK_TAGS | {"br", "hr"}:
            self.parts.append("\n")
        if tag == "li" and not self.hidden:
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in BLOCK_TAGS:
            self.parts.append("\n")
        if not self.hidden and tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def prepare_advert(raw, *, source="", upstream_partial=False):
    original = str(raw)
    markup = unescape(original)
    html = bool(re.search(r"<[a-z][a-z0-9:-]*(?:\s[^<>]*|/?)>", markup, re.I))
    if html:
        parser = AdvertHTML()
        parser.feed(markup)
        text = "".join(parser.parts)
    else:
        text = original
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    flags = []
    if not text:
        flags.append("empty")
    if upstream_partial:
        flags.append("upstream_partial")
    if re.search(r"(?:\.\.\.|…)\s*$", text):
        flags.append("trailing_ellipsis")
    if source == "adzuna" and 490 <= len(original) <= 500:
        flags.append("likely_source_snippet")
    if text and "\n" not in text:
        flags.append("flattened_structure")
    return {"version": VERSION, "original": original, "original_sha256": fingerprint(original),
            "text": text, "text_sha256": fingerprint(text), "source": source,
            "quality_flags": flags, "possibly_incomplete": any(f != "flattened_structure" for f in flags)}


def select_existing_version(job, alternatives=()):
    """Only accept explicitly identity-verified alternate descriptions, never title/company guesses."""
    candidates = [job]
    identity = job.get("verified_vacancy_key")
    if identity:
        candidates.extend(a for a in alternatives if a.get("verified_vacancy_key") == identity)
    documents = []
    for candidate in candidates:
        stored = candidate.get("advert_document")
        if stored is not None:
            if not isinstance(stored, dict) or fingerprint(stored.get("original")) != stored.get("original_sha256"):
                raise ValueError("Stored advert integrity failure")
            original = stored["original"]
        else:
            original = candidate.get("description_original") or candidate.get("description", "")
        partial = candidate.get("description_partial", False) or bool(stored and "upstream_partial" in stored["quality_flags"])
        documents.append((candidate, prepare_advert(original, source=candidate.get("source", ""), upstream_partial=partial)))
    chosen, document = max(documents, key=lambda pair: (not pair[1]["possibly_incomplete"], len(pair[1]["text"])))
    return {**document, "selected_job_id": chosen.get("id"),
            "selection_reason": "original" if chosen is job else "verified_identity_fuller_existing_advert"}
