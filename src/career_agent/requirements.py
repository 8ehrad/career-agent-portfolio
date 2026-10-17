"""Conservative, source-cited requirement extraction and offline assessment policy."""
from __future__ import annotations

from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
import re

from .evidence_selection import SelectionLimits, fingerprint, select_evidence, serialized

REQUIREMENT_VERSION = "requirements-v1"
ESSENTIAL = re.compile(r"\b(must|required|essential|mandatory|minimum|need to have)\b", re.I)
DESIRABLE = re.compile(r"\b(preferred|desirable|ideally|bonus|nice[- ]to[- ]have|a plus|advantageous|beneficial|optional)\b", re.I)
OPTIONAL = re.compile(r"\b(not (?:strictly )?(?:required|essential|necessary)|no .{0,60} required)\b", re.I)
ESSENTIAL_HEADINGS = {"essential", "essential requirements", "essential skills", "required skills",
                      "minimum qualifications", "minimum requirements", "must haves", "must-haves",
                      "required qualifications", "required", "what you need"}
DESIRABLE_HEADINGS = {"desirable", "desirable skills", "desirable requirements", "preferred qualifications",
                      "preferred skills", "nice to have", "nice-to-have", "nice to haves", "bonus points"}
GENERAL_HEADINGS = {"requirements", "qualifications", "skills", "about you", "what you bring",
                      "what you'll bring", "what we are looking for", "what we're looking for"}
OTHER_HEADINGS = {"benefits", "what we offer", "about us", "about the company", "responsibilities",
                  "the role", "your responsibilities", "what you'll do", "equal opportunities", "how to apply"}

RECOMMENDATION_POLICY_VERSION = "recommendation-requirements-v1-no-behaviour"


def recommendation_requirement(row):
    """Soft skills/interests are application context, never recommendation evidence."""
    if row.get("category") == "behaviour":
        return False
    text = str(row.get("text", "")).strip()
    # Protect against interest statements misclassified as technical/experience.
    return not bool(re.match(r"(?i)^(?:(?:strong|genuine|keen|excellent|effective)\s+)?(?:interest\s+in|passion\s+for|"
                             r"enthusiasm\s+for|curiosity\b|willingness\s+to\s+learn|problem.solving\s+mindset|"
                             r"(?:written and verbal )?communication\s+skills|interpersonal\s+skills|teamwork\b|team player\b|"
                             r"(?:able|ability) to (?:explain|communicate)\b|comfortable working in a collaborative)", text))


def operational_diagnosis(text):
    return bool(re.search(r"\b(?:troubleshoot\w*|diagnos\w*|debug\w*|incident\w*|root cause|"
                         r"when (?:they|things|systems) break|behav\w* when .*break)\b", text, re.I))


def recommendation_extraction_review(document):
    if "issues" not in document or "concerns" not in document:
        return document.get("requires_extraction_review", False)
    # Incomplete input/overflow and substantive ambiguity still block. An issue confined
    # to a behavioural row must not reject otherwise clear substantive requirements.
    issue_reasons = {i["reason"] for i in document["issues"]}
    if any(c not in issue_reasons for c in document["concerns"]):
        return True
    for issue in document["issues"]:
        matches = [r for r in document["requirements"] if issue["quote"] in r["quote"]]
        if not matches or any(recommendation_requirement(r) for r in matches):
            return True
    return False


class _AdvertHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "li", "br", "div", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "li", "div", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clauses(text):
    for sentence in re.split(r";|(?<=[.!?])\s+(?=[A-Z])", text):
        start = 0
        for delimiter in re.finditer(r",\s*(?:but\s+|and\s+)?|\s+(?:but|and)\s+", sentence):
            left, right = sentence[start:delimiter.start()], sentence[delimiter.end():]
            if OPTIONAL.search(left) and re.match(r"(?:is|it|would|can|will)\b", right.strip(), re.I):
                continue
            # Separate explicitly mixed modalities, not comma-separated skills or degree alternatives.
            if (ESSENTIAL.search(left) or DESIRABLE.search(left)) and (ESSENTIAL.search(right) or DESIRABLE.search(right)):
                yield left
                start = delimiter.end()
        yield sentence[start:]


def extract_requirements(description: str, max_requirements: int = 40) -> dict:
    if max_requirements < 1:
        raise ValueError("Requirement limit must be positive")
    text = description
    if re.search(r"<(?:p|li|div|h[1-6]|br|ul)\b", text, re.I):
        parser = _AdvertHTML()
        parser.feed(text)
        text = "".join(parser.parts)
    text = unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    # Offsets cite this retained normalized view, not positions in raw HTML.
    text = re.sub(r"[ \t]+", " ", text)
    section, inherited = None, None
    rows, excluded = [], []
    offset = 0
    all_headings = ESSENTIAL_HEADINGS | DESIRABLE_HEADINGS | GENERAL_HEADINGS | OTHER_HEADINGS
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        clean = re.sub(r"^[\s#*•\-–]+", "", line).strip().strip("*")
        heading, separator, tail = clean.partition(":")
        heading = heading.strip().lower().replace("’", "'")
        bare = clean.rstrip(":").lower().replace("’", "'")
        label = heading if separator and heading in all_headings else bare
        if label in all_headings:
            section = label
            inherited = "essential" if label in ESSENTIAL_HEADINGS else (
                "desirable" if label in DESIRABLE_HEADINGS else "essential" if label in GENERAL_HEADINGS else None)
            if not separator or not tail.strip():
                offset += len(line)
                continue
            clean = tail.strip()
        elif re.match(r"^\s*#{1,6}\s", line) or (clean.endswith(":") and len(clean) < 100):
            # Unknown headings must not inherit the previous mandatory section.
            section, inherited = clean.rstrip(":"), None
            offset += len(line)
            continue
        if not clean:
            offset += len(line)
            continue
        # Keep AND/OR/equivalent qualifications intact; split only clear sentence/clause boundaries.
        pieces = _clauses(clean)
        cursor = offset
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            start = text.find(piece, cursor, offset + len(line))
            if start < 0:
                raise ValueError("Requirement lost source alignment")
            cursor = start + len(piece)
            explicit_e = bool(ESSENTIAL.search(piece))
            explicit_d = bool(DESIRABLE.search(piece))
            optional = bool(OPTIONAL.search(piece))
            is_candidate = inherited is not None or explicit_e or explicit_d or bool(re.search(
                r"\b(experience|proficien\w*|degree|Ph\.?D|qualification|competenc\w*)\b", piece, re.I))
            source = {"start": start, "end": cursor, "line": line_number, "section": section}
            if not is_candidate or section in OTHER_HEADINGS:
                excluded.append({"text": piece, "source": source, "reason": "not_identified_as_candidate_requirement"})
                continue
            if optional and not explicit_d:
                excluded.append({"text": piece, "source": source, "reason": "explicitly_not_required"})
                continue
            if explicit_d and (not explicit_e or optional):
                priority, reason = "desirable", "explicit_optional_or_preference_wording_overrides_heading"
            elif explicit_e and explicit_d:
                priority, reason = "essential", "mandatory_wording_retained_in_mixed_clause"
            elif explicit_e:
                priority, reason = "essential", "explicit_requirement_wording"
            else:
                priority, reason = inherited or "essential", "section_heading" if inherited else "stated_requirement_defaults_to_essential"
            rows.append({"id": "R-" + fingerprint([start, cursor, piece])[:12], "text": piece,
                         "priority": priority, "classification_reason": reason, "source": source,
                         "polarity": "negative" if re.search(r"\bmust not\b", piece, re.I) else "positive",
                         "logical_form": "alternatives_preserved" if re.search(r"\b(or|equivalent)\b", piece, re.I)
                         else "conjunction_preserved" if re.search(r"\band\b", piece, re.I) else "single_or_unspecified"})
        offset += len(line)
    return {"version": REQUIREMENT_VERSION, "source_sha256": fingerprint(description), "normalized_text": text,
            "requirements": rows[:max_requirements], "excluded": excluded,
            "overflow_count": max(0, len(rows) - max_requirements),
            "requires_extraction_review": not rows or len(rows) > max_requirements,
            "notice": "Two tiers only: stated requirements default to essential unless explicitly preferred/optional. Alternatives and negation remain intact. Unknown candidate fit is a separate assessment state, not a requirement tier."}


def select_requirement_evidence(job, draft, rules, baseline, limits=SelectionLimits()):
    document = extract_requirements(str(job.get("description", "")))
    base = select_evidence({"title": "", "description": ""}, draft, rules, baseline, limits)["context"]
    base["selection_policy"]["requirement_version"] = REQUIREMENT_VERSION
    background_by_requirement = {}
    claims_by_id = {c["id"]: c for c in draft["claims"]}
    for requirement in document["requirements"]:
        text = requirement["text"]
        prefixes = []
        if re.search(r"\b(?:years?|yrs?)\b.*\bexperience\b", text, re.I):
            prefixes.append("ROLE-")
        if re.search(r"\b(?:degree|Ph\.?D|BSc|MSc|bachelor|master|doctorate|equivalent)\b", text, re.I):
            prefixes.append("EDU-")
        if re.search(r"\b(?:sponsorship|right to work|work authori[sz]ation)\b", text, re.I):
            prefixes.append("WORK-AUTHORIZATION")
        background_by_requirement[requirement["id"]] = []
        for entity in draft["entities"]:
            if not any(entity["id"].startswith(prefix) for prefix in prefixes):
                continue
            key = "BACKGROUND-" + entity["id"]
            background_by_requirement[requirement["id"]].append(key)
            if any(c["id"] == key for c in base["evidence"]):
                continue
            references = []
            if entity.get("role_or_degree"):
                description = entity["role_or_degree"]["value"] + ", " + entity["organization"]["value"]
                dates = entity["dates"]
                description += f"; {dates['start']} to {dates['end'] or 'present as of '+draft['as_of']}; month precision."
                for field in ("role_or_degree", "organization", "dates"):
                    references.extend(entity[field]["citations"])
            else:
                source_claims = [claims_by_id[c] for c in entity["evidence"]["eligibility"]]
                description = " ".join(c["statement"] for c in source_claims)
                references = [ref for c in source_claims for ref in c["citations"]]
            base["evidence"].append({"id": key, "entity_id": entity["id"], "category": entity["category"],
                                     "claim": description, "kind": "background_fact", "skills": [],
                                     "citations": sorted(set(references)), "attestation": "source_reported"})
            constraints = [deepcopy(claims_by_id[c]) for c in entity["evidence"]["limitation"]]
            base.setdefault("background_constraints", []).append({"entity_id": entity["id"], "limitations": constraints})
            references.extend(ref for c in constraints for ref in c["citations"])
            for ref in references:
                base["citations"][ref] = {k: v for k, v in draft["citations"][ref].items()
                                           if k not in {"excerpt", "headings", "field"}}
    if any(background_by_requirement.values()):
        base["selection_policy"]["background_scope"] = (
            "Dates do not establish years using a particular skill. Distinguish research, placement and employment, "
            "avoid double-counting overlaps, and do not infer absence of another qualification from this summary.")
    if len(serialized(base).encode()) > limits.max_context_bytes:
        raise ValueError("Budget cannot fit mandatory requirement policy")
    retrieved = {}
    for requirement in document["requirements"]:
        retrieved[requirement["id"]] = select_evidence(
            {"title": "", "description": requirement["text"]}, draft, rules, baseline, limits)
    # Role/domain context remains useful even where an advert has only terse requirements.
    # This fallback is not another requirement tier and never resolves an essential unknown.
    retrieved["__advert_context__"] = select_evidence(job, draft, rules, baseline, limits)
    order = {"essential": 0, "desirable": 1}
    requirements = sorted(document["requirements"], key=lambda r: (order[r["priority"]], r["source"]["start"]))
    selected_ids, entity_ids = {c["id"] for c in base["evidence"]}, set()
    per_entity = {}
    deferred = set()
    # Round-robin within each priority class: a single requirement cannot consume every evidence slot.
    groups = [[r for r in requirements if r["priority"] == priority] for priority in ("essential", "desirable")]
    groups.append([{"id": "__advert_context__"}])
    for group in groups:
        max_hits = max((len(retrieved[r["id"]]["context"]["evidence"]) for r in group), default=0)
        for rank in range(max_hits):
            for requirement in group:
                result = retrieved[requirement["id"]]
                evidence = result["context"]["evidence"]
                if rank >= len(evidence):
                    continue
                claim = evidence[rank]
                entity_id = claim["entity_id"]
                if claim["id"] in selected_ids:
                    continue
                if (entity_id not in entity_ids and len(entity_ids) >= limits.max_entities
                        or per_entity.get(entity_id, 0) >= limits.max_claims_per_entity):
                    deferred.add(requirement["id"])
                    continue
                proposed = deepcopy(base)
                proposed["evidence"].append(claim)
                if entity_id not in entity_ids:
                    proposed["evidence_groups"].append(next(e for e in result["context"]["evidence_groups"] if e["id"] == entity_id))
                # Include only references actually carried by evidence/support bundles.
                def refs(value):
                    if isinstance(value, dict):
                        for key, item in value.items():
                            if key == "citations" and isinstance(item, list):
                                for ref in item:
                                    if ref in result["context"]["citations"]:
                                        proposed["citations"][ref] = result["context"]["citations"][ref]
                            else:
                                refs(item)
                    elif isinstance(value, list):
                        for item in value:
                            refs(item)
                refs(proposed["evidence"])
                refs(proposed["evidence_groups"])
                if len(serialized(proposed).encode()) > limits.max_context_bytes:
                    deferred.add(requirement["id"])
                    continue
                base = proposed
                selected_ids.add(claim["id"])
                entity_ids.add(entity_id)
                per_entity[entity_id] = per_entity.get(entity_id, 0) + 1
    matrix = []
    for requirement in document["requirements"]:
        result = retrieved[requirement["id"]]
        hits = [c["id"] for c in result["context"]["evidence"] if c["id"] in selected_ids]
        hits.extend(background_by_requirement[requirement["id"]])
        limited = requirement["id"] in deferred or any(
            item["reason"] == "bundle_exceeds_remaining_budget" for item in result["telemetry"]["skipped_entities"])
        matrix.append({**requirement, "evidence_ids": hits,
                       "retrieval_state": "evidence_retrieved" if hits else "budget_deferred" if limited else "no_evidence_retrieved",
                       "assessment": "not_assessed", "budget_limited": limited})
    # Requirements are advert data, separate from the candidate-context byte budget.
    return {"context": base, "requirement_document": document, "requirement_matrix": matrix,
            "telemetry": {"version": REQUIREMENT_VERSION, "context_bytes": len(serialized(base).encode()),
                          "context_sha256": fingerprint(base), "selected_entities": sorted(entity_ids),
                          "counts_by_priority": {p: sum(r["priority"] == p for r in matrix) for p in order},
                          "unresolved_essential_ids": [r["id"] for r in matrix if r["priority"] == "essential"],
                          "notice": "Evidence retrieval never satisfies a requirement automatically; essential unknowns remain review items."}}


def assessment_summary(matrix, judgments, allowed_evidence_ids, *, extraction_review=False, evidence_text_by_id=None):
    rows = {r["id"]: r for r in matrix}
    if len(rows) != len(matrix) or any(r["priority"] not in {"essential", "desirable"} for r in matrix):
        raise ValueError("Requirements must have unique IDs and essential/desirable priority")
    seen, assessed = set(), []
    allowed = set(allowed_evidence_ids)
    for judgment in judgments:
        key = judgment["requirement_id"]
        if key not in rows or key in seen:
            raise ValueError("Unknown or duplicate requirement judgment")
        seen.add(key)
        status = judgment["status"]
        if status not in {"supported", "transferable", "unknown", "confirmed_gap"}:
            raise ValueError("Unknown requirement assessment status")
        citations = judgment.get("evidence_ids", [])
        if not isinstance(citations, list) or any(key not in allowed for key in citations):
            raise ValueError("Requirement judgment cites unavailable evidence")
        if status != "unknown" and (not citations or not judgment.get("explanation")):
            raise ValueError("A supported/transferable/gap judgment requires cited evidence and explanation")
        if status == "confirmed_gap":
            quote = judgment.get("explicit_contradiction")
            if not isinstance(quote, str) or not quote.strip():
                raise ValueError("A confirmed gap requires explicit contradictory evidence, not retrieval absence")
            if evidence_text_by_id is not None and not any(quote in evidence_text_by_id.get(key, "") for key in citations):
                raise ValueError("Contradiction quote is not present in cited candidate evidence")
        assessed.append({**rows[key], "requirement_id": key, "status": status, "evidence_ids": citations,
                         "explanation": judgment.get("explanation", ""),
                         "explicit_contradiction": judgment.get("explicit_contradiction", "")})
    for key, row in rows.items():
        if key not in seen:
            assessed.append({**row, "requirement_id": key, "status": "unknown", "evidence_ids": [],
                             "explanation": "No assessment returned."})
    decision_rows = [r for r in assessed if recommendation_requirement(r)]
    failed = [r["id"] for r in decision_rows if r["priority"] == "essential" and r["status"] == "confirmed_gap"]
    unresolved = [r["id"] for r in decision_rows if r["priority"] == "essential" and r["status"] in {"unknown", "transferable"}]
    state = "confirmed_essential_gap" if failed else "needs_review" if (
        unresolved or extraction_review or not rows) else "essential_requirements_supported" if any(
            r["priority"] == "essential" for r in decision_rows) else "no_essential_requirements_identified"
    return {"state": state, "essential_gap_ids": failed, "unresolved_essential_ids": unresolved,
            "assessments": assessed,
            "policy": "Desirable matches never offset essential gaps or unknowns. Unknown is not rejection. Cited model judgments still require semantic accuracy evaluation; this is not a production fit gate."}
