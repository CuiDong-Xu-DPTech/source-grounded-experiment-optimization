#!/usr/bin/env python3
"""Verify exact quotation strings against declared physical PDF pages."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
TOOL_VERSION = "1.1.0"
SCHEMA = "source-validation/v1"
PAGE_RE = re.compile(r"(?:^|[#,&])\s*(?:physical_page|page)\s*=\s*(\d+)(?=$|[,&#])")
PATH_FIELDS = ("path", "path_or_url", "file", "file_path", "source_path")
MATERIAL_KEYS = {"source_id", "id", "provided", "physical_page", "content", "anchor_id", "exact_quote", "kind", "role", *PATH_FIELDS}
class InputError(Exception): pass
class SourceError(Exception): pass
def fold(text):
    return " ".join(text.split())
def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()
def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: " + key)
        result[key] = value
    return result
def reject_constant(value):
    raise ValueError("non-finite JSON constant: " + value)
def load_json(path):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise InputError("cannot read JSON input: " + str(exc)) from exc
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError) as exc:
        raise InputError("strict JSON parsing failed: " + str(exc)) from exc
    if not isinstance(value, dict):
        raise InputError("JSON root must be an object")
    return value, digest_bytes(data)
def records(container, direct_keys):
    if isinstance(container, list):
        return [(None, value) for value in container]
    if isinstance(container, dict):
        if any(key in container for key in direct_keys):
            return [(None, container)]
        return [(key, container[key]) for key in sorted(container)]
    raise InputError("record container must be a list or object")
def source_list(container):
    result, invalid, duplicates = {}, [], set()
    if container is None:
        return result, invalid, duplicates
    try:
        entries = records(container, ("source_id", *PATH_FIELDS))
    except InputError as exc:
        return result, [str(exc)], duplicates
    for hint, value in entries:
        if isinstance(value, str) and hint is not None:
            value = {"path": value}
        if not isinstance(value, dict):
            invalid.append("source record is not an object")
            continue
        source_id = value.get("source_id", value.get("id", hint))
        if not isinstance(source_id, str) or not source_id:
            invalid.append("source record needs a non-empty source_id")
            continue
        if source_id in result:
            duplicates.add(source_id)
        result[source_id] = value
    return result, invalid, duplicates
def scope_item(item_id, evidence_id, reason, field=None):
    item = {"item_id": item_id, "evidence_id": evidence_id, "code": "unverified_scope",
            "scope": "manual", "reason": reason}
    if field is not None:
        item["field"] = field
    return item
def expand_material(value, label):
    if isinstance(value, list):
        return [(label + "[" + str(index) + "]", None, item)
                for index, item in enumerate(value)]
    if isinstance(value, dict):
        if any(key in value for key in MATERIAL_KEYS):
            return [(label, None, value)]
        return [(label + "." + key, key, value[key]) for key in sorted(value)]
    return [(label, None, value)]
def case_materials(container):
    sources, duplicates, primary_ids, aliases = {}, set(), set(), {}
    visual, notes, anchors, scopes, unreadable = [], [], [], [], []
    def problem(label, reason, primary=False):
        if primary:
            unreadable.append({"item_id": label, "evidence_id": None,
                               "code": "primary_source_unreadable", "reason": reason})
        else:
            scopes.append(scope_item(label, None, reason))
    def add_visual(label, value):
        if not isinstance(value, dict):
            problem(label, "rendered visual auxiliary list items must be objects")
            return
        item = {"item_id": label, "kind": "visual_auxiliary",
                "path": value.get("path"), "physical_page": value.get("physical_page"),
                "opened_for_text": False, "exists": None, "real_path": None}
        for key in PATH_FIELDS:
            if key in value:
                item[key] = value[key]
        if value.get("provided") is False and declared_path(value) is None:
            item["path_status"] = "declared_unprovided"
            item["exists"] = False
        visual.append(item)
    def add_unprovided(label, hint, value):
        if declared_path(value) is not None:
            problem(label, "provided:false conflicts with a supplied path")
            return
        source_id = value.get("source_id", value.get("id", hint))
        if hint == "primary" or source_id == "primary":
            problem(label, "required primary PDF is declared unprovided", True)
            return
        notes.append({"item_id": label, "kind": "declared_unprovided_source",
                      "source_id": source_id, "provided": False, "path": None,
                      "note": value.get("source_note"),
                      "validation_scope": "not_a_supplied_pdf; referenced evidence cannot fall back to it"})
    def add_source(label, hint, value):
        if isinstance(value, str) and hint is not None:
            value = {"path": value}
        if not isinstance(value, dict):
            problem(label, "unrecognized source material structure", hint == "primary")
            return
        if value.get("provided") is False:
            add_unprovided(label, hint, value)
            return
        if "anchor_id" in value or "exact_quote" in value:
            anchors.append((label, hint, value))
            return
        source_id = value.get("source_id", value.get("id", hint))
        path = declared_path(value)
        if (source_id == "rendered_pages" or hint == "rendered_pages" or
                value.get("kind") in {"rendered_page", "visual_auxiliary"} or
                (isinstance(path, str) and path.lower().endswith(
                    (".png", ".jpg", ".jpeg", ".tif", ".tiff")))):
            add_visual(label, value)
            return
        if not isinstance(source_id, str) or not source_id:
            problem(label, "PDF catalog record needs a non-empty source_id/id")
            return
        if path is None:
            problem(label, "PDF catalog record has no supplied path", hint == "primary")
            return
        if source_id in sources:
            duplicates.add(source_id)
        sources[source_id] = value
        if hint is not None:
            aliases[hint] = source_id
        if hint == "primary" or source_id == "primary":
            primary_ids.add(source_id)
    def handle(label, hint, value):
        if hint in {"rendered_pages", "verified_anchors"}:
            children = expand_material(value, label)
            if hint == "rendered_pages":
                for child_label, _, child in children:
                    add_visual(child_label, child)
            else:
                anchors.extend(children)
            return
        is_record = isinstance(value, dict) and any(key in value for key in MATERIAL_KEYS)
        if isinstance(value, (list, dict)) and not is_record and not (
                isinstance(value, str) and hint is not None):
            children = expand_material(value, label)
            if not children:
                problem(label, "empty or unrecognized nested source material container")
            for child_label, child_hint, child in children:
                handle(child_label, child_hint, child)
            return
        add_source(label, hint, value)
    for entry_label, entry_hint, entry in expand_material(container, "source_materials"):
        handle(entry_label, entry_hint, entry)
    if len(primary_ids) > 1:
        scopes.append(scope_item("source_materials.primary", None,
                                "multiple primary source IDs make anchor fallback ambiguous"))
    return {"sources": sources, "duplicates": duplicates, "primary_ids": primary_ids,
            "aliases": aliases, "visual": visual, "notes": notes, "anchors": anchors,
            "scopes": scopes, "unreadable": unreadable}
def declared_path(record):
    return next((record[key] for key in PATH_FIELDS if record.get(key) not in (None, "")), None)
def path_values(record):
    declarations = []
    for key in PATH_FIELDS:
        value = record.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise SourceError(key + " must be a local path string")
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value):
            raise SourceError("URLs and other URI schemes are not local source paths")
        declarations.append((key, value))
    return declarations
def locator_source(record):
    locator = record.get("locator")
    if not isinstance(locator, str) or "#" not in locator:
        return None
    prefix = locator.split("#", 1)[0]
    if not prefix or "/" in prefix or "\\" in prefix:
        return None
    return prefix
def physical_page(record):
    values = []
    for key in ("physical_page", "page"):
        if key not in record or record[key] is None:
            continue
        value = record[key]
        if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
            value = int(value)
        if type(value) is not int or value < 1:
            return None, "physical_page/page must be a positive integer or decimal string (1-based physical page)"
        values.append((key, value))
    locator = record.get("locator")
    if locator is not None and not isinstance(locator, str):
        return None, "locator must be a string"
    if isinstance(locator, str):
        values.extend(("locator", int(value)) for value in PAGE_RE.findall(locator))
    if not values:
        return None, "a physical page is required; section, location, or printed-page labels are insufficient"
    if len({value for _, value in values}) != 1:
        return None, "conflicting physical page declarations: " + repr(values)
    return values[0][1], None
class SourceGuard:
    def __init__(self, case_path, case_sources, registry_sources, duplicates, fitz):
        self.root = os.path.realpath(os.path.dirname(os.path.abspath(case_path)))
        self.input_prefix = os.path.basename(self.root) == "input"
        self.case_sources = case_sources
        self.registry_sources = registry_sources
        self.duplicates = duplicates
        self.fitz = fitz
        self.documents = {}
        self.hashes = {}
        self.hash_errors = {}
        self.used = {}
    def close(self):
        for document in self.documents.values():
            document.close()
    def confined_path(self, record, pdf_required):
        declared = path_values(record)
        if not declared:
            raise SourceError("source path is missing")
        resolved = {}
        for key, raw in declared:
            if not os.path.isabs(raw) and self.input_prefix and raw.startswith("input/"):
                raw = raw[len("input/"):]
            candidate = raw if os.path.isabs(raw) else os.path.join(self.root, raw)
            real = os.path.realpath(candidate)
            if real != self.root and not real.startswith(self.root + os.sep):
                raise SourceError("source path escapes the case input directory: " + key)
            resolved[key] = real
        if len(set(resolved.values())) != 1:
            raise SourceError("conflicting local path declarations: " + repr(resolved))
        real = next(iter(resolved.values()))
        if pdf_required and not real.lower().endswith(".pdf"):
            raise SourceError("only declared local PDF files are supported for native text verification")
        return real
    def confined(self, record):
        real = self.confined_path(record, True)
        if not os.path.isfile(real):
            raise SourceError("source PDF is missing or is not a regular file")
        return real
    def inspect_visual(self, item):
        result = dict(item)
        try:
            real = self.confined_path(item, False)
            result["real_path"] = real
            result["exists"] = os.path.exists(real)
            result["is_file"] = os.path.isfile(real)
            result["path_status"] = "present" if result["exists"] else "missing"
        except SourceError as exc:
            result["path_status"] = "rejected: " + str(exc)
        return result
    def resolve(self, record):
        from_locator = locator_source(record)
        declared = record.get("source_id")
        if declared is not None and not isinstance(declared, str):
            raise SourceError("source_id must be a string")
        source_id = declared or from_locator
        if declared and from_locator and declared != from_locator:
            raise SourceError("source_id and locator source disagree")
        if source_id in self.duplicates:
            raise SourceError("duplicate source_id is ambiguous: " + str(source_id))
        source, origin = None, None
        if source_id:
            source = self.case_sources.get(source_id)
            origin = "case" if source is not None else None
            if source is None:
                source = self.registry_sources.get(source_id)
                origin = "registry" if source is not None else None
        local_path = None
        if declared_path(record) is not None:
            local_path = self.confined(record)
        source_path = self.confined(source) if source is not None else None
        if local_path and source_path and local_path != source_path:
            raise SourceError("evidence path disagrees with its declared source")
        selected = source_path or local_path
        if selected is None:
            raise SourceError("source cannot be resolved from case or registry")
        declared_sha = source.get("sha256") if source is not None else None
        key = (source_id or "", selected)
        self.used.setdefault(key, {
            "source_id": source_id, "path": selected, "source_catalog": origin,
            "sha256": None, "declared_sha256": declared_sha,
            "declared_sha256_matches": None, "page_count": None,
        })
        return source_id, selected
    def text(self, path, page_number):
        if self.fitz is None:
            raise SourceError("PyMuPDF (fitz) is unavailable")
        if path not in self.documents:
            try:
                document = self.fitz.open(path)
                if document.needs_pass:
                    document.close()
                    raise SourceError("encrypted PDF requires unavailable credentials")
                self.documents[path] = document
                self.hashes[path] = digest_file(path)
            except SourceError:
                raise
            except Exception as exc:
                raise SourceError("cannot open PDF: " + str(exc)) from exc
        document = self.documents[path]
        for item in self.used.values():
            if item["path"] == path:
                item["sha256"] = self.hashes[path]
                item["page_count"] = len(document)
                declared = item.get("declared_sha256")
                if declared:
                    matches = isinstance(declared, str) and declared.lower() == self.hashes[path]
                    item["declared_sha256_matches"] = matches
                    if not matches:
                        self.hash_errors[path] = "actual source SHA256 does not match its catalog declaration"
        if path in self.hash_errors:
            raise SourceError(self.hash_errors[path])
        if page_number > len(document):
            raise SourceError("physical page is outside the PDF page range")
        try:
            return document.load_page(page_number - 1).get_text("text")
        except Exception as exc:
            raise SourceError("physical page text extraction failed: " + str(exc)) from exc
def base_item(item_id, evidence_id, field, quote, kind, provenance):
    return {
        "item_id": item_id,
        "evidence_id": evidence_id,
        "field": field,
        "quote": quote,
        "quote_sha256": digest_bytes(quote.encode("utf-8")),
        "provenance_kind": kind,
        "provenance": provenance,
    }
def evidence_work(evidence_records):
    work, scope = [], []
    for index, (hint, evidence) in enumerate(evidence_records):
        fallback = hint if hint is not None else "evidence[" + str(index) + "]"
        if not isinstance(evidence, dict):
            scope.append(scope_item(fallback, fallback, "evidence record is not an object"))
            continue
        evidence_id = evidence.get("evidence_id", evidence.get("id", fallback))
        if not isinstance(evidence_id, str) or not evidence_id:
            evidence_id = fallback
        provenance = {"registry_evidence_ids": [evidence_id], "case_anchor_ids": []}
        candidates = []
        quote = evidence.get("quote")
        if isinstance(quote, str) and fold(quote):
            candidates.append((evidence, evidence_id + "#quote", "quote", quote))
        if "spans" in evidence:
            try:
                spans = records(evidence["spans"], ("verbatim", "source_id", *PATH_FIELDS, "locator", "physical_page", "page"))
            except InputError:
                scope.append(scope_item(evidence_id, evidence_id, "spans must be a list or object", "spans"))
                spans = []
            for span_index, (span_hint, span) in enumerate(spans):
                label = span_hint if span_hint is not None else str(span_index)
                field = "spans[" + label + "].verbatim"
                item_id = evidence_id + "#spans[" + label + "]"
                if not isinstance(span, dict) or not isinstance(span.get("verbatim"), str) or not fold(span.get("verbatim", "")):
                    scope.append(scope_item(item_id, evidence_id, "each span needs a non-empty verbatim string", field))
                    continue
                combined = dict(evidence)
                for key in ("source_id", *PATH_FIELDS, "locator", "physical_page", "page"):
                    if key in span:
                        combined[key] = span[key]
                candidates.append((combined, item_id, field, span["verbatim"]))
        if not candidates and not any(item["item_id"] == evidence_id for item in scope):
            scope.append(scope_item(evidence_id, evidence_id,
                                    "no executable quote; use quote or spans[].verbatim, not context_note/paraphrase"))
        for record, item_id, field, text in candidates:
            work.append((base_item(item_id, evidence_id, field, text,
                                  "registry_evidence", provenance), record))
    return work, scope
def anchor_work(entries, primary_ids, aliases):
    work, scope, seen = [], [], set()
    for index, (label, hint, anchor) in enumerate(entries):
        fallback = hint if hint is not None else "anchor[" + str(index) + "]"
        if not isinstance(anchor, dict):
            scope.append(scope_item(label, fallback, "verified anchor list items must be objects"))
            continue
        anchor_id = anchor.get("anchor_id", anchor.get("id", fallback))
        quote = anchor.get("exact_quote")
        if not isinstance(anchor_id, str) or not anchor_id:
            anchor_id = fallback
        if anchor_id in seen:
            scope.append(scope_item(label, anchor_id, "duplicate case anchor_id is ambiguous"))
            continue
        seen.add(anchor_id)
        if not isinstance(quote, str) or not fold(quote):
            scope.append(scope_item(anchor_id, anchor_id, "case anchor needs a non-empty exact_quote", "exact_quote"))
            continue
        source = anchor.get("source")
        if source is not None and not isinstance(source, str):
            scope.append(scope_item(anchor_id, anchor_id, "anchor source must be a source ID string", "source"))
            continue
        source = aliases.get(source, source)
        if source is None and len(primary_ids) == 1:
            source = next(iter(primary_ids))
        if source is None and len(primary_ids) > 1:
            scope.append(scope_item(anchor_id, anchor_id, "anchor has no source and primary fallback is ambiguous"))
            continue
        combined = dict(anchor)
        if source is not None:
            combined["source_id"] = source
        provenance = {"registry_evidence_ids": [], "case_anchor_ids": [anchor_id]}
        item = base_item("case_anchor:" + anchor_id + "#exact_quote", anchor_id,
                         "exact_quote", quote, "case_verified_anchor", provenance)
        item["anchor_id"] = anchor_id
        if isinstance(anchor.get("location"), str):
            item["anchor_location"] = anchor["location"]
        item["case_material_path"] = label
        work.append((item, combined))
    return work, scope
def work_key(item_record):
    item, record = item_record
    page, problem = physical_page(record)
    if problem:
        return None
    source = record.get("source_id") or locator_source(record)
    if not isinstance(source, str):
        return None
    return source, page, item["quote"]
def merge_anchor_work(registry, anchors):
    by_id, by_value, output = {}, {}, []
    for pair in registry:
        item, record = pair
        identifiers = {item["evidence_id"], record.get("anchor_id")}
        for identifier in identifiers:
            if isinstance(identifier, str):
                by_id.setdefault(identifier, []).append(pair)
        key = work_key(pair)
        if key is not None:
            by_value.setdefault(key, []).append(pair)
    merged_count = 0
    for anchor_pair in anchors:
        item, record = anchor_pair
        anchor_id = item["anchor_id"]
        key = work_key(anchor_pair)
        id_matches = by_id.get(anchor_id, [])
        value_matches = by_value.get(key, []) if key is not None else []
        selected = None
        if id_matches:
            same = [pair for pair in id_matches if work_key(pair) == key]
            if len(same) == 1:
                selected = same[0]
        elif len(value_matches) == 1:
            selected = value_matches[0]
        if selected is None and (id_matches or len(value_matches) > 1):
            item["code"] = "unverified_scope"
            item["scope"] = "manual"
            item["reason"] = "case anchor has conflicting or ambiguous registry identity/value matches"
            output.append(("unverified_scope", item))
            continue
        if selected is None:
            output.append(("work", anchor_pair))
            continue
        registry_item, _ = selected
        registry_item["provenance_kind"] = "registry_evidence+case_verified_anchor"
        registry_item["provenance"]["case_anchor_ids"].append(anchor_id)
        registry_item["anchor_id"] = anchor_id
        if "anchor_location" in item:
            registry_item["anchor_location"] = item["anchor_location"]
        registry_item["case_material_path"] = item["case_material_path"]
        merged_count += 1
    return list(registry), output, merged_count
def verify(item, record, guard):
    page_number, problem = physical_page(record)
    if problem:
        result = scope_item(item["item_id"], item["evidence_id"], problem, item["field"])
        for key in ("quote", "quote_sha256", "provenance_kind", "provenance", "anchor_id"):
            if key in item:
                result[key] = item[key]
        return "unverified_scope", result
    item["physical_page"] = page_number
    item["physical_page_numbering"] = "1-based"
    try:
        source_id, path = guard.resolve(record)
        item["source_id"] = source_id
        item["source_path"] = path
        if guard.fitz is None:
            raise SourceError("PyMuPDF (fitz) is unavailable; no installation was attempted")
        page_text = guard.text(path, page_number)
        item["source_sha256"] = guard.hashes[path]
    except SourceError as exc:
        item["code"] = "unreadable"
        item["reason"] = str(exc)
        return "unreadable", item
    if not fold(page_text):
        item["code"] = "empty_text_extraction"
        item["reason"] = "no native page text; OCR or visual transcription requires separate manual evidence"
        return "unreadable", item
    if fold(item["quote"]) not in fold(page_text):
        item["code"] = "mismatch"
        item["reason"] = "case-sensitive folded quote is not a contiguous substring of this physical page's extracted text"
        return "mismatches", item
    item["match"] = "exact_case_sensitive_folded_contiguous_substring"
    return "verified_items", item
def report_template(case_path, registry_path):
    return {
        "schema": SCHEMA,
        "tool_version": TOOL_VERSION,
        "case_id": None,
        "status": "partial",
        "method": "PyMuPDF native Page.get_text('text'); one physical page; case-sensitive contiguous substring after whitespace folding only",
        "normalization": {"case_sensitive": True, "unicode_normalization": False,
                          "whitespace": "strip ends and replace each Unicode whitespace run with one ASCII space",
                          "fuzzy_matching": False, "cross_page_matching": False, "ocr": False},
        "input_files": {"case": {"path": os.path.abspath(case_path), "sha256": None}, "registry": {"path": os.path.abspath(registry_path), "sha256": None}},
        "pymupdf": {"available": False, "version": None, "error": None},
        "total": 0, "exact_quote_verified": 0, "case_anchor_total": 0, "case_anchor_merged_total": 0, "case_anchor_independent_total": 0,
        "verified_items": [], "mismatches": [], "unreadable": [], "unverified_scope": [],
        "source_files": [], "visual_auxiliary_records": [], "material_notes": [],
        "exact_text_match_only": True, "scientific_support_verified": False,
        "scientific_validation": "not_assessed; actual reviewer agreement is separate and no benchmark score is assigned",
    }
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify exact quote/spans[].verbatim strings on declared 1-based physical PDF pages; not scientific-support scoring.",
        allow_abbrev=False,
    )
    parser.add_argument("--case", required=True, help="case.json with source_materials")
    parser.add_argument("--registry", required=True, help="evidence-registry.json with sources/evidence")
    parser.add_argument("--output", required=True, help="destination source-validation.json")
    args = parser.parse_args(argv)
    report = report_template(args.case, args.registry)
    case = registry = None
    for role, path in (("case", args.case), ("registry", args.registry)):
        try:
            value, sha256 = load_json(path)
            report["input_files"][role]["sha256"] = sha256
            if role == "case":
                case = value
                report["case_id"] = case.get("case_id")
            else:
                registry = value
        except InputError as exc:
            report["unreadable"].append({"item_id": "input:" + role, "evidence_id": None,
                                         "code": "input_error", "reason": str(exc)})
    fitz = None
    if case is not None and registry is not None:
        try:
            fitz = importlib.import_module("fitz")
            version = getattr(fitz, "VersionBind", None) or getattr(fitz, "__version__", None) or "unknown"
            report["pymupdf"] = {"available": True, "version": str(version), "error": None}
        except Exception as exc:
            report["pymupdf"]["error"] = type(exc).__name__ + ": " + str(exc) + "; install nothing and report partial"
        if "source_materials" in case:
            materials = case_materials(case["source_materials"])
        else:
            materials = case_materials([])
            report["unverified_scope"].append(scope_item("source_materials", None, "source_materials is missing"))
        case_sources = materials["sources"]
        if "sources" in registry:
            registry_sources, registry_invalid, registry_duplicates = source_list(registry["sources"])
        else:
            registry_sources, registry_invalid, registry_duplicates = {}, [], set()
        report["unverified_scope"].extend(materials["scopes"])
        report["unreadable"].extend(materials["unreadable"])
        for index, reason in enumerate(registry_invalid):
            report["unverified_scope"].append(scope_item("registry:sources[" + str(index) + "]", None, reason))
        try:
            evidence_records = records(registry.get("evidence"), direct_keys=("evidence_id", "quote", "spans"))
        except InputError as exc:
            evidence_records = []
            report["unverified_scope"].append(scope_item("registry:evidence", None, str(exc)))
        registry_work, evidence_scope = evidence_work(evidence_records)
        report["unverified_scope"].extend(evidence_scope)
        anchors, anchor_scope = anchor_work(materials["anchors"], materials["primary_ids"], materials["aliases"])
        report["unverified_scope"].extend(anchor_scope)
        remaining_work, merged, merged_count = merge_anchor_work(registry_work, anchors)
        for kind, item in merged:
            if kind == "unverified_scope":
                report[kind].append(item)
        work = remaining_work + [pair for kind, pair in merged if kind == "work"]
        report["case_anchor_total"] = len(materials["anchors"])
        report["case_anchor_merged_total"] = merged_count
        report["case_anchor_independent_total"] = sum(kind == "work" for kind, _ in merged)
        report["material_notes"] = sorted(materials["notes"], key=lambda item: item["item_id"])
        if not work:
            report["unverified_scope"].append(scope_item("registry:evidence", None,
                                                        "no real quote or executable case anchor evidence records"))
        guard = SourceGuard(args.case, case_sources, registry_sources,
                            materials["duplicates"] | registry_duplicates, fitz)
        try:
            for source_id in sorted(materials["primary_ids"]):
                try:
                    guard.confined(case_sources[source_id])
                except SourceError as exc:
                    report["unreadable"].append({"item_id": "source_materials.primary:" + source_id,
                                                 "evidence_id": None, "code": "primary_source_unreadable",
                                                 "reason": str(exc)})
            report["visual_auxiliary_records"] = [guard.inspect_visual(item) for item in materials["visual"]]
            for item, record in work:
                bucket, checked = verify(item, record, guard)
                report[bucket].append(checked)
        finally:
            guard.close()
        report["source_files"] = [guard.used[key] for key in sorted(guard.used)]
    for bucket in ("verified_items", "mismatches", "unreadable", "unverified_scope"):
        report[bucket].sort(key=lambda value: str(value.get("item_id", "")))
    report["exact_quote_verified"] = len(report["verified_items"])
    report["total"] = sum(len(report[bucket]) for bucket in
                          ("verified_items", "mismatches", "unreadable", "unverified_scope"))
    if report["total"] and report["exact_quote_verified"] == report["total"]:
        report["status"] = "verified"
    data = (json.dumps(report, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        output = Path(args.output)
        if os.path.realpath(output) in {os.path.realpath(args.case), os.path.realpath(args.registry)}:
            raise OSError("output must not overwrite case or registry")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name("." + output.name + ".tmp-" + str(os.getpid()))
        temporary.write_bytes(data)
        os.replace(temporary, output)
    except OSError as exc:
        sys.stderr.write("verify_sources: cannot write output: " + str(exc) + "\n")
        return 3
    return 0 if report["status"] == "verified" else 1
if __name__ == "__main__":
    sys.exit(main())
