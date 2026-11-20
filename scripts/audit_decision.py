#!/usr/bin/env python3
"""Mechanical candidate/version audit; does not assess scientific validity."""
import argparse
import hashlib
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

KINDS = {"conditional_planning", "original_preference", "revision_proposal",
         "rejection", "undetermined"}
UNKNOWN_KINDS = {"conditional_planning", "revision_proposal", "undetermined"}
COMPARATORS = {"le", "lt", "ge", "gt", "eq"}
HEX256 = re.compile(r"^[0-9a-f]{64}$")
CASE_CANDIDATE = re.compile(r"^/candidates/(0|[1-9][0-9]*)$")
PDF = re.compile(r"^([A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.pdf#physical_page=([0-9]+)(?:-([0-9]+))?$")


class InputFailure(Exception):
    pass


class PointerFailure(Exception):
    pass


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputFailure("duplicate JSON object key: " + key)
        result[key] = value
    return result


def finite_float(raw):
    value = float(raw)
    if not math.isfinite(value):
        raise InputFailure("non-finite JSON number: " + raw)
    return value


def nonfinite(raw):
    raise InputFailure("non-finite JSON number: " + raw)


def load_json(path):
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates,
                           parse_constant=nonfinite, parse_float=finite_float)
    except InputFailure:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputFailure("cannot load strict JSON " + str(path) + ": " + str(exc))
    if not isinstance(value, dict):
        raise InputFailure("top-level JSON value must be an object: " + str(path))
    return value, raw


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def pointer_tokens(pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise PointerFailure("JSON Pointer must be non-empty and start with '/'")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def resolve_pointer(document, pointer):
    value = document
    for token in pointer_tokens(pointer):
        if isinstance(value, dict):
            if token not in value:
                raise PointerFailure("unresolved object key: " + token)
            value = value[token]
        elif isinstance(value, list):
            if not re.match(r"^(0|[1-9][0-9]*)$", token):
                raise PointerFailure("invalid array index: " + token)
            if int(token) >= len(value):
                raise PointerFailure("array index out of range: " + token)
            value = value[int(token)]
        else:
            raise PointerFailure("pointer traverses a scalar")
    return value


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def diff_paths(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                result.append(child)
            else:
                result.extend(diff_paths(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list):
        result = []
        for index in range(max(len(before), len(after))):
            child = path + "/" + str(index)
            if index >= len(before) or index >= len(after):
                result.append(child)
            else:
                result.extend(diff_paths(before[index], after[index], child))
        return result
    return [] if canonical(before) == canonical(after) else [path or "/"]


def compare_values(actual, comparator, limit):
    if comparator == "le":
        return actual <= limit
    if comparator == "lt":
        return actual < limit
    if comparator == "ge":
        return actual >= limit
    if comparator == "gt":
        return actual > limit
    return actual == limit


def error_report(message, code=2):
    issue = {"code": "input_error", "path": "", "message": message}
    report = {
        "schema_version": "decision-audit/v1", "case_id": None,
        "source_status": {"status": "error", "scientific_accuracy": "not_assessed"},
        "counts": {"candidates": 0, "checks": 0, "validated_constraints": 0,
                   "blocking_issues": 1, "conditional_unknowns": 0, "passed_checks": 0,
                   "failed_checks": 0, "unknown_checks": 0},
        "blocking_issues": [issue], "conditional": {"unknowns": []},
        "conditional_unknowns": [], "validated_constraints": [], "original_identity_records": [],
        "decision": {"state": "invalid", "selection": None, "execution_authorized": False,
                     "execution_ready": False, "invalid_selection": False},
        "can_finalize": False,
        "final_validation": {"outcome": "invalid", "processing_status": "error",
                             "exit_code": code, "invalid_selection": False},
    }
    return report, code


class Audit:
    def __init__(self, case, ledger, case_raw):
        self.case, self.ledger, self.case_raw = case, ledger, case_raw
        self.errors, self.unknowns, self.validated = [], [], []
        self.identities, self.records, self.by_id = [], [], {}
        self.invalid_selection, self.hard_ids = False, []

    def error(self, code, path, message, candidate_id=None, selection=False):
        issue = {"code": code, "path": path, "message": message}
        if candidate_id is not None:
            issue["candidate_id"] = candidate_id
        self.errors.append(issue)
        if selection:
            self.invalid_selection = True

    def unknown(self, code, path, message, candidate_id=None, source_ref=None):
        item = {"code": code, "path": path, "message": message}
        if candidate_id is not None:
            item["candidate_id"] = candidate_id
        if source_ref is not None:
            item["source_ref"] = source_ref
        self.unknowns.append(item)

    def source_ref(self, ref, path):
        if not isinstance(ref, dict):
            self.error("invalid_source_ref", path, "source reference must be an object")
            return None
        locator, quote = ref.get("locator"), ref.get("quote")
        if not nonempty(locator) or not nonempty(quote):
            self.error("invalid_source_ref", path, "locator and non-empty quote are required")
            return None
        if locator.startswith("/"):
            try:
                resolve_pointer(self.case, locator)
            except PointerFailure as exc:
                self.error("unresolved_source_ref", path + "/locator", str(exc))
                return None
        else:
            match = PDF.fullmatch(locator)
            materials = self.case.get("source_materials", [])
            materials = materials if isinstance(materials, list) else []
            sources = {m.get("path"): m for m in materials if isinstance(m, dict)}
            if not match or ".." in locator.split("/"):
                self.error("unsafe_source_ref", path + "/locator",
                           "use a case JSON Pointer or declared PDF#physical_page=N locator")
                return None
            pdf_path = locator.split("#", 1)[0]
            if pdf_path not in sources:
                self.error("unresolved_source_ref", path + "/locator", "PDF is not declared in source_materials")
                return None
            start = int(match.group(2))
            end = int(match.group(3)) if match.group(3) else start
            pages = sources[pdf_path].get("physical_pages")
            if start < 1 or end < start or (isinstance(pages, str) and pages.isdigit() and end > int(pages)):
                self.error("invalid_source_ref", path + "/locator", "physical page range is outside the source")
                return None
        return {"locator": locator, "quote": quote,
                "quote_verification": "presence_only_manual_source_review_required"}

    def evidence_refs(self, value, path, required=False):
        if value is None and not required:
            return []
        if not isinstance(value, list) or (required and not value):
            self.error("invalid_evidence_refs", path, "evidence_refs must be a non-empty array")
            return []
        refs = []
        for index, ref in enumerate(value):
            checked = self.source_ref(ref, path + "/" + str(index))
            if checked is not None:
                refs.append(checked)
        return refs

    def validate_changes(self, candidate, path, parent, revised, kind):
        changes = candidate.get("changes")
        if not isinstance(changes, list):
            self.error("invalid_changes", path + "/changes", "changes must be an array")
            return []
        if kind == "original" and changes:
            self.error("original_has_changes", path + "/changes", "an original cannot contain changes")
        if kind == "revision" and not changes:
            self.error("revision_without_changes", path + "/changes", "a revision requires changes")
        paths, tokens = [], []
        for index, change in enumerate(changes):
            item_path = path + "/changes/" + str(index)
            if not isinstance(change, dict):
                self.error("invalid_change", item_path, "change must be an object")
                continue
            change_path = change.get("path")
            if not nonempty(change_path):
                self.error("invalid_change_path", item_path + "/path", "a JSON Pointer is required")
                continue
            paths.append(change_path)
            for field in ("before", "after"):
                if field not in change:
                    self.error("invalid_change", item_path + "/" + field, "before and after are required")
            if not nonempty(change.get("reason")):
                self.error("invalid_change", item_path + "/reason", "a change reason is required")
            self.evidence_refs(change.get("evidence_refs"), item_path + "/evidence_refs", True)
            if parent is None or revised is None:
                continue
            try:
                before, after = resolve_pointer(parent, change_path), resolve_pointer(revised, change_path)
                tokens.append(pointer_tokens(change_path))
                if "before" in change and canonical(change["before"]) != canonical(before):
                    self.error("change_before_mismatch", item_path + "/before", "before differs from the parent")
                if "after" in change and canonical(change["after"]) != canonical(after):
                    self.error("change_after_mismatch", item_path + "/after", "after differs from the revision")
                if canonical(before) == canonical(after):
                    self.error("change_without_difference", item_path, "parent and revision values are identical")
            except PointerFailure as exc:
                self.error("unresolved_change_path", item_path + "/path", str(exc) +
                           "; replace an existing containing object rather than hiding additions")
        if kind == "revision" and parent is not None and revised is not None:
            for difference in diff_paths(parent, revised):
                try:
                    diff_tokens = pointer_tokens(difference)
                except PointerFailure:
                    diff_tokens = []
                if not any(diff_tokens[:len(item)] == item for item in tokens):
                    self.error("unrecorded_parameter_change", path + "/changes",
                               "canonical difference is not covered: " + difference)
        return paths

    def operand(self, value, unit):
        operand_unit, number = unit, value
        if isinstance(value, dict):
            if set(value) != {"value", "unit"}:
                raise ValueError("measurement object must contain exactly value and unit")
            number, operand_unit = value["value"], value["unit"]
        if not isinstance(number, str) or not nonempty(operand_unit):
            raise ValueError("numeric value and unit must be strings")
        if operand_unit != unit:
            raise ValueError("unit strings must match exactly; conversion is not performed")
        try:
            return Fraction(number), operand_unit
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("invalid exact arithmetic string: " + str(exc))

    def validate_check(self, check, path, candidate_id, parameter_path, revised):
        if not isinstance(check, dict):
            self.error("invalid_check", path, "check must be an object")
            return None
        check_id, kind, status = check.get("check_id"), check.get("kind"), check.get("status")
        severity, constraint_id = check.get("severity"), check.get("constraint_id")
        if not nonempty(check_id):
            self.error("invalid_check_id", path + "/check_id", "non-empty check_id is required")
            check_id = None
        if not nonempty(kind) or status not in {"pass", "fail", "unknown"}:
            self.error("invalid_check", path, "kind and pass/fail/unknown status are required")
        if severity not in {"blocking", "advisory"} or (kind != "control" and severity != "blocking"):
            self.error("invalid_severity", path + "/severity", "only controls may be advisory")
        if constraint_id is not None and not nonempty(constraint_id):
            self.error("invalid_constraint_id", path + "/constraint_id", "constraint_id must be a string")
            constraint_id = None
        source_ref = self.source_ref(check.get("source_ref"), path + "/source_ref")
        evidence = self.evidence_refs(check.get("evidence_refs"), path + "/evidence_refs")
        expected = check.get("is_expected_negative_control", False)
        if not isinstance(expected, bool):
            self.error("invalid_control", path + "/is_expected_negative_control", "boolean required")
            expected = False
        expectation_ref = None
        if expected:
            if kind != "control":
                self.error("invalid_control_exemption", path, "only controls may use a negative-control exemption")
            expectation_ref = self.source_ref(check.get("expectation_source_ref"),
                                              path + "/expectation_source_ref")
        matches = []
        if "matches" in check:
            matches.append(check["matches"])
        if isinstance(check.get("actual"), dict) and "matches" in check["actual"]:
            matches.append(check["actual"]["matches"])
        if isinstance(check.get("qa"), dict) and "matches" in check["qa"]:
            matches.append(check["qa"]["matches"])
        if any(not isinstance(value, bool) for value in matches):
            self.error("invalid_matches", path, "matches must be boolean")
            matches = []
        effective = status if status in {"pass", "fail", "unknown"} else "unknown"
        if kind == "self_qa" and any(value is False for value in matches):
            effective = "fail"
            if status != "fail":
                self.error("self_qa_matches_false", path,
                           "qa matches=false is a self/selection failure, not a scientific negative result", candidate_id)
        qa_actual = kind == "self_qa" and isinstance(check.get("actual"), dict) and "matches" in check["actual"]
        fields = [] if qa_actual and "comparator" not in check and "limit" not in check else [
            name for name in ("actual", "comparator", "limit") if name in check]
        computed, computation_status, diagnostic = None, "not_provided", None
        formula, unit = check.get("derived_formula"), check.get("unit")
        actual_proof_path = check.get("actual_proof_path")
        if actual_proof_path is not None:
            try:
                proof_value = resolve_pointer(revised, actual_proof_path)
                if "actual" not in check or canonical(proof_value) != canonical(check["actual"]):
                    self.error("actual_proof_mismatch", path + "/actual_proof_path",
                               "actual must exactly equal the resolved canonical parameter value", candidate_id)
            except (PointerFailure, TypeError) as exc:
                self.error("unresolved_actual_proof", path + "/actual_proof_path", str(exc), candidate_id)
        if fields:
            if len(fields) != 3 or not nonempty(unit) or not nonempty(formula):
                self.error("incomplete_computation", path,
                           "actual/comparator/limit require exact unit and derived_formula", candidate_id)
                computation_status, diagnostic = "incomplete", "comparison could not be evaluated"
            elif check["comparator"] not in COMPARATORS:
                self.error("invalid_comparator", path + "/comparator", "use le, lt, ge, gt, or eq", candidate_id)
                computation_status, diagnostic = "invalid", "unknown comparator"
            else:
                try:
                    actual, actual_unit = self.operand(check["actual"], unit)
                    limit, limit_unit = self.operand(check["limit"], unit)
                    if actual_unit != limit_unit:
                        raise ValueError("unit mismatch")
                    computed = compare_values(actual, check["comparator"], limit)
                    computation_status = "computed"
                except ValueError as exc:
                    computation_status, diagnostic = "uncomputable", str(exc)
            if computation_status != "computed":
                if effective != "fail":
                    effective = "unknown"
                self.unknown("uncomputable_check", path, diagnostic, candidate_id, source_ref)
                if status == "pass":
                    self.error("uncomputable_pass", path, "a declared pass needs valid exact computation", candidate_id)
            elif computed is False:
                effective = "fail"
                if status != "fail":
                    self.error("computed_status_mismatch", path, "arithmetic proves the comparison false", candidate_id)
            elif status == "fail":
                self.error("computed_status_mismatch", path, "declared fail conflicts with true arithmetic", candidate_id)
        expected_failure = bool(kind == "control" and expected and effective == "fail" and expectation_ref)
        blocking_failure = bool(effective == "fail" and severity == "blocking" and not expected_failure)
        return {
            "candidate_id": candidate_id, "check_path": path, "check_id": check_id,
            "constraint_id": constraint_id, "kind": kind, "declared_status": status,
            "status": effective, "severity": severity, "source_ref": source_ref,
            "evidence_refs": evidence, "expectation_source_ref": expectation_ref,
            "expected_negative_control_failure": expected_failure,
            "selection_blocking_failure": blocking_failure,
            "proof": {"parameter_case_path": parameter_path, "actual_proof_path": actual_proof_path,
                      "actual": check.get("actual"), "comparator": check.get("comparator"),
                      "limit": check.get("limit"), "unit": unit, "derived_formula": formula,
                      "computed_result": computed, "computation_status": computation_status,
                      "diagnostic": diagnostic},
        }

    def candidate(self, candidate, index):
        path = "/candidates/" + str(index)
        if not isinstance(candidate, dict):
            self.error("invalid_candidate", path, "candidate must be an object")
            return None
        candidate_id = candidate.get("candidate_id")
        if not nonempty(candidate_id):
            self.error("invalid_candidate_id", path + "/candidate_id", "non-empty candidate_id required")
            candidate_id = None
        elif candidate_id in self.by_id:
            self.error("duplicate_candidate_id", path + "/candidate_id", "candidate_id must be globally unique", candidate_id)
        source_path, source = candidate.get("source_case_path"), None
        if not isinstance(source_path, str) or not CASE_CANDIDATE.fullmatch(source_path):
            self.error("invalid_source_case_path", path + "/source_case_path", "use stable /candidates/N")
        else:
            try:
                source = resolve_pointer(self.case, source_path)
                if not isinstance(source, dict):
                    raise PointerFailure("source candidate is not an object")
            except PointerFailure as exc:
                self.error("unresolved_source_case_path", path + "/source_case_path", str(exc))
        source_plan, version, kind = (candidate.get(name) for name in
                                      ("source_plan_id", "source_version", "revision_kind"))
        if not nonempty(source_plan) or not nonempty(version):
            self.error("invalid_identity", path, "source_plan_id and source_version are required")
        if kind not in {"original", "revision"}:
            self.error("invalid_revision_kind", path + "/revision_kind", "original or revision required")
        if source is not None:
            if source_plan != source.get("plan_id"):
                self.error("source_plan_mismatch", path + "/source_plan_id", "plan_id differs from source", candidate_id)
            if kind == "original" and version != source.get("version"):
                self.error("source_version_mismatch", path + "/source_version", "original version differs", candidate_id)
        parent_ref = candidate.get("parent_plan_ref")
        if kind == "original" and parent_ref is not None:
            self.error("original_has_parent", path + "/parent_plan_ref", "an original cannot have a parent")
        if kind == "revision":
            if not isinstance(parent_ref, str) or not CASE_CANDIDATE.fullmatch(parent_ref):
                self.error("invalid_parent_ref", path + "/parent_plan_ref", "parent must be /candidates/N")
            elif source_path != parent_ref:
                self.error("parent_path_mismatch", path + "/parent_plan_ref", "parent and source paths differ")
            if source is not None and version == source.get("version"):
                self.error("revision_version_not_distinct", path + "/source_version", "revision version is unchanged", candidate_id)
        parameter_path = candidate.get("parameter_case_path")
        declared_hash = candidate.get("source_parameters_sha256")
        has_parameters, revised = "canonical_parameters" in candidate, candidate.get("canonical_parameters")
        parent, parent_hash, hash_status = None, None, "not_provided"
        if declared_hash is not None and (not isinstance(declared_hash, str) or not HEX256.fullmatch(declared_hash)):
            self.error("invalid_parameter_hash", path + "/source_parameters_sha256", "lowercase SHA256 required")
            declared_hash = None
        if kind == "revision" and declared_hash is None:
            self.error("revision_hash_required", path + "/source_parameters_sha256", "new verified hash required")
        if kind == "revision" and not has_parameters:
            self.error("revision_parameters_required", path + "/canonical_parameters", "actual revised object required")
        if declared_hash is not None or has_parameters:
            if not isinstance(parameter_path, str) or not parameter_path.startswith("/"):
                self.error("invalid_parameter_path", path + "/parameter_case_path", "parent parameter pointer required")
            else:
                try:
                    parent = resolve_pointer(self.case, parameter_path)
                    parent_hash = sha256(parent)
                except PointerFailure as exc:
                    self.error("unresolved_parameter_path", path + "/parameter_case_path", str(exc))
            if has_parameters and not isinstance(revised, dict):
                self.error("invalid_canonical_parameters", path + "/canonical_parameters", "object required")
                revised = None
            if not has_parameters:
                self.error("canonical_parameters_required", path + "/canonical_parameters",
                           "hash must be recomputed from an object, not a description")
            elif revised is not None:
                computed_hash = sha256(revised)
                if declared_hash is not None and declared_hash != computed_hash:
                    self.error("parameter_hash_mismatch", path + "/source_parameters_sha256", "hash mismatch", candidate_id)
                else:
                    hash_status = "verified" if declared_hash is not None else "computed_not_declared"
                if parameter_path == source_path and parent is not None:
                    for field, stated in (("plan_id", source_plan), ("version", version)):
                        if field in parent and canonical(revised.get(field)) != canonical(stated):
                            self.error("canonical_identity_mismatch", path + "/canonical_parameters/" + field,
                                       "embedded identity must match the ledger identity", candidate_id)
                if kind == "original" and parent is not None and canonical(revised) != canonical(parent):
                    self.error("original_parameters_changed", path + "/canonical_parameters", "original was modified", candidate_id)
                if kind == "revision" and parent_hash == computed_hash:
                    self.error("parameters_not_changed", path + "/canonical_parameters", "revision object is unchanged", candidate_id)
        change_paths = self.validate_changes(candidate, path, parent, revised, kind)
        checks = candidate.get("checks")
        if not isinstance(checks, list):
            self.error("invalid_checks", path + "/checks", "checks must be an array")
            checks = []
        check_records, seen_check, seen_constraint = [], set(), set()
        for check_index, check in enumerate(checks):
            check_path = path + "/checks/" + str(check_index)
            record = self.validate_check(check, check_path, candidate_id, parameter_path, revised)
            if record is None:
                continue
            check_id, constraint_id = record["check_id"], record["constraint_id"]
            if check_id is not None:
                if check_id in seen_check:
                    self.error("duplicate_check_id", check_path + "/check_id", "duplicate in candidate", candidate_id)
                seen_check.add(check_id)
            if constraint_id is not None:
                if constraint_id in seen_constraint:
                    self.error("duplicate_constraint_id", check_path + "/constraint_id", "duplicate in candidate", candidate_id)
                seen_constraint.add(constraint_id)
            if record["status"] == "unknown" and record["proof"]["computation_status"] in {"not_provided", "computed"}:
                self.unknown("check_unknown", check_path, "explicit unknown check", candidate_id, record["source_ref"])
            check_records.append(record)
            self.validated.append(record)
        self.identities.append({
            "candidate_id": candidate_id, "candidate_path": path, "source_case_path": source_path,
            "source_plan_id": source_plan, "source_version": version,
            "parent_source_version": source.get("version") if source else None, "revision_kind": kind,
            "parent_plan_ref": parent_ref, "parameter_case_path": parameter_path,
            "declared_source_parameters_sha256": declared_hash, "computed_parent_parameters_sha256": parent_hash,
            "computed_candidate_parameters_sha256": sha256(revised) if isinstance(revised, dict) else None,
            "parameter_hash_status": hash_status, "change_paths": change_paths,
        })
        record = {"candidate_id": candidate_id, "path": path, "kind": kind,
                  "source_version": version, "checks": check_records}
        if candidate_id is not None and candidate_id not in self.by_id:
            self.by_id[candidate_id] = record
        self.records.append(record)
        return record

    def selection(self):
        selection = self.ledger.get("selection")
        if not isinstance(selection, dict):
            self.error("invalid_selection_schema", "/selection", "selection object required")
            return None, None, False
        candidate_id, kind = selection.get("candidate_id"), selection.get("kind")
        selected_auth = selection.get("execution_authorized")
        if candidate_id is not None and not isinstance(candidate_id, str):
            self.error("invalid_selection_schema", "/selection/candidate_id", "string or null required", selection=True)
            candidate_id = None
        if kind not in KINDS:
            self.error("invalid_selection_schema", "/selection/kind", "unsupported kind", selection=True)
        if not nonempty(selection.get("rationale")):
            self.error("invalid_selection_schema", "/selection/rationale", "rationale is required")
        if not isinstance(selected_auth, bool):
            self.error("invalid_selection_schema", "/selection/execution_authorized", "boolean required")
            selected_auth = False
        selected = self.by_id.get(candidate_id) if candidate_id is not None else None
        if candidate_id is not None and selected is None:
            self.error("invalid_selection", "/selection/candidate_id", "unresolved candidate", candidate_id, True)
        if candidate_id is None and kind in {"original_preference", "revision_proposal"}:
            self.error("invalid_selection", "/selection/candidate_id", "candidate required", selection=True)
        if kind == "undetermined" and candidate_id is not None:
            self.error("invalid_selection", "/selection/candidate_id", "undetermined requires null", candidate_id, True)
        if selected is not None:
            if kind == "original_preference" and selected["kind"] != "original":
                self.error("invalid_selection", "/selection", "revision selected as original", candidate_id, True)
            if kind == "revision_proposal" and selected["kind"] != "revision":
                self.error("invalid_selection", "/selection", "revision_proposal needs a revision", candidate_id, True)
        if selected_auth != self.ledger.get("execution_authorized"):
            self.error("authorization_mismatch", "/selection/execution_authorized", "authorization flags differ", candidate_id)
        checks = selected["checks"] if selected else []
        failures = [item for item in checks if item["selection_blocking_failure"]]
        unknown_checks = [item for item in checks if item["status"] == "unknown" and item["severity"] == "blocking"]
        if kind != "rejection":
            for item in failures:
                self.error("invalid_selection", item["check_path"],
                           "selected blocking " + item["kind"] + " check failed", candidate_id, True)
        missing = False
        if selected is not None and kind not in {"rejection", "undetermined"}:
            present = {item["constraint_id"] for item in checks if item["constraint_id"] is not None}
            for constraint_id in self.hard_ids:
                if constraint_id not in present:
                    self.unknown("missing_constraint_check", selected["path"] + "/checks",
                                 "missing source hard constraint " + constraint_id, candidate_id)
                    missing = True
            if not any(item["kind"] == "self_qa" for item in checks):
                self.unknown("missing_self_qa", selected["path"] + "/checks", "self_qa record required", candidate_id)
                missing = True
        if (unknown_checks or missing) and kind not in UNKNOWN_KINDS:
            self.error("invalid_selection", "/selection/kind",
                       "unknown blocking state cannot be original_preference or rejection", candidate_id, True)
        if (self.ledger.get("execution_authorized") or selected_auth) and (
                failures or unknown_checks or missing or kind != "original_preference"):
            self.error("execution_not_authorized_for_state", "/selection/execution_authorized",
                       "execution authorization conflicts with failed, unknown, or proposal state", candidate_id, True)
        if kind in UNKNOWN_KINDS and not (unknown_checks or missing):
            self.unknown("conditional_decision", "/selection", "explicit conditional selection", candidate_id)
        if kind == "revision_proposal":
            self.unknown("revision_pending_approval", "/selection", "revision approval remains required", candidate_id)
        return selected, selection, selected_auth

    def run(self):
        if not isinstance(self.case.get("candidates"), list):
            raise InputFailure("case.candidates must be an array")
        hard = self.case.get("hard_constraints", [])
        if isinstance(hard, list):
            self.hard_ids = [item.get("constraint_id") for item in hard
                             if isinstance(item, dict) and nonempty(item.get("constraint_id"))]
        case_id, computed_case_hash = self.case.get("case_id"), hashlib.sha256(self.case_raw).hexdigest()
        ledger_case_id, ledger_case_hash = self.ledger.get("case_id"), self.ledger.get("source_case_sha256")
        source_ok = True
        if ledger_case_id != case_id:
            self.error("case_id_mismatch", "/case_id", "ledger is bound to another case")
            source_ok = False
        if not isinstance(ledger_case_hash, str) or not HEX256.fullmatch(ledger_case_hash):
            self.error("invalid_case_hash", "/source_case_sha256", "lowercase SHA256 required")
            source_ok = False
        elif ledger_case_hash != computed_case_hash:
            self.error("case_hash_mismatch", "/source_case_sha256", "hash must cover exact case bytes")
            source_ok = False
        if not nonempty(self.ledger.get("planning_scope")):
            self.error("invalid_planning_scope", "/planning_scope", "non-empty planning scope required")
        authorized = self.ledger.get("execution_authorized")
        if not isinstance(authorized, bool):
            self.error("invalid_authorization", "/execution_authorized", "boolean required")
            authorized = False
        evidence = self.evidence_refs(self.ledger.get("execution_approval_evidence"),
                                      "/execution_approval_evidence")
        if authorized and not evidence:
            self.error("execution_evidence_required", "/execution_approval_evidence", "approval evidence required")
        candidates = self.ledger.get("candidates")
        if not isinstance(candidates, list):
            self.error("invalid_candidates", "/candidates", "candidates must be an array")
            candidates = []
        versions = set()
        for index, candidate in enumerate(candidates):
            record = self.candidate(candidate, index)
            if record is None:
                continue
            identity = self.identities[-1]
            key = (identity["source_case_path"], identity["source_version"])
            if all(key) and key in versions:
                self.error("duplicate_source_version", record["path"] + "/source_version",
                           "source path/version identity duplicated", record["candidate_id"])
            if all(key):
                versions.add(key)
        selected, selection, selected_auth = self.selection()
        if self.errors:
            outcome = "invalid"
        elif selection is not None and selection.get("kind") in UNKNOWN_KINDS:
            outcome = "conditional"
        else:
            outcome = "complete"
        execution_ready = bool(outcome == "complete" and selection and
                               selection.get("kind") == "original_preference" and authorized and selected_auth and evidence)
        counts = {name: sum(item["status"] == name for item in self.validated) for name in ("pass", "fail", "unknown")}
        return {
            "schema_version": "decision-audit/v1", "case_id": case_id,
            "source_status": {"status": "verified" if source_ok else "mismatch", "case_id": case_id,
                              "ledger_case_id": ledger_case_id, "computed_case_sha256": computed_case_hash,
                              "provided_case_sha256": ledger_case_hash,
                              "scope": "case_identity_and_strict_syntax_only",
                              "strict_json": "duplicate_keys_and_nonfinite_numbers_rejected",
                              "scientific_accuracy": "not_assessed", "quote_verification": "manual_source_review_required"},
            "counts": {"candidates": len(self.records), "checks": len(self.validated),
                       "validated_constraints": len(self.validated), "blocking_issues": len(self.errors),
                       "conditional_unknowns": len(self.unknowns), "passed_checks": counts["pass"],
                       "failed_checks": counts["fail"], "unknown_checks": counts["unknown"]},
            "blocking_issues": self.errors, "conditional": {"unknowns": self.unknowns},
            "conditional_unknowns": self.unknowns, "validated_constraints": self.validated,
            "original_identity_records": self.identities,
            "decision": {"state": outcome, "selection": selection,
                         "selected_candidate_id": selected["candidate_id"] if selected else None,
                         "execution_authorized": authorized, "selection_execution_authorized": selected_auth,
                         "execution_approval_evidence": evidence, "execution_ready": execution_ready,
                         "invalid_selection": self.invalid_selection},
            "can_finalize": outcome == "complete",
            "final_validation": {"outcome": outcome, "processing_status": outcome,
                                 "exit_code": 1 if outcome == "invalid" else 0,
                                 "invalid_selection": self.invalid_selection},
        }, 1 if self.errors else 0


def write_report(path, report):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate decision-ledger provenance, revisions, checks, and selection.")
    parser.add_argument("--case", required=True, help="strict source case JSON")
    parser.add_argument("--ledger", required=True, help="strict decision ledger JSON")
    parser.add_argument("--output", required=True, help="deterministic validation report JSON")
    args = parser.parse_args(argv)
    try:
        case, case_raw = load_json(args.case)
        ledger, _ = load_json(args.ledger)
        report, code = Audit(case, ledger, case_raw).run()
    except (InputFailure, OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        report, code = error_report(str(exc))
    except Exception as exc:
        report, code = error_report("unexpected validation failure: " + str(exc))
    try:
        write_report(args.output, report)
    except OSError as exc:
        print("decision audit could not write output: " + str(exc), file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
