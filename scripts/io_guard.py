#!/usr/bin/env python3
"""Strict JSON I/O, partial-result checkpoints, and provenance ledgers."""

import argparse
import copy
import datetime as _datetime
import hashlib
import importlib
import importlib.util
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

VERSION = "4.0.0"
MANUAL_REVIEW_NEXT = (
    "NEXT: after reviewing the candidate output and contract mappings, populate a real "
    "manual_review with reviewer, reviewed_at_utc, original contract_sha256 and "
    "evidence-bearing notes. Leave status=required until actually reviewed. Never "
    "auto-create reviewer/completed approval."
)
RUN_RECORD_NAME = "run_record.json"
RESULT_NAME = "result.json"
VALIDATION_NAME = "validation.json"
JOURNAL_NAME = "change_records.jsonl"
LEDGER_KEYS = (
    "sources",
    "uncertainty",
    "uncertainty_note",
    "limitations",
    "limitations_note",
    "change_records",
    "evidence",
    "claims",
)
SCHEMA_MARKERS = {
    "$schema",
    "$ref",
    "$dynamicRef",
    "$recursiveRef",
    "type",
    "properties",
    "items",
    "prefixItems",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
    "enum",
    "const",
    "additionalProperties",
}
PATH_KEY_RE = re.compile(
    r"(?:path|paths|file|files|source|sources|material|materials|"
    r"document|documents|attachment|attachments|input|inputs|artifact|pdf)",
    re.I,
)
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
UNKNOWN_EXACT = {
    "n/a",
    "na",
    "n.a.",
    "n.a",
    "not available",
    "not applicable",
    "unknown",
    "unk",
    "tbd",
    "to be determined",
    "pending",
    "未知",
    "不确定",
    "待补",
    "待确定",
    "暂无",
}
UNKNOWN_PREFIX = re.compile(
    r"^\s*(?:n\s*/\s*a|n\.a\.?|not available|not applicable|unknown|tbd|"
    r"to be determined|未知|待补|待确定)\s*(?:[:;,，；\-—]|$)",
    re.I,
)
FINISHED_RECHECK = {"pass", "passed", "verified", "confirmed", "matched", "complete"}
PENDING_WORDS = {
    "pending",
    "unknown",
    "unevaluated",
    "not_evaluated",
    "not-evaluated",
    "unreviewed",
    "unresolved",
    "incomplete",
    "待评估",
    "未知",
    "未决",
}


class GuardError(Exception):
    def __init__(self, code, message, exit_code=3, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details if details is not None else {}


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise GuardError(
            "cli_usage",
            message,
            2,
            {"command": self.prog, "usage": self.format_usage().strip()},
        )


class JSONHelp(argparse.Action):
    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        emit(
            {
                "ok": True,
                "status": "help",
                "command": parser.prog,
                "usage": parser.format_usage().strip(),
                "help": parser.format_help(),
            }
        )
        parser.exit(0)


def utc_now():
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def emit(value):
    text = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )
    sys.stdout.write(text + "\n")


def diagnostic(message):
    sys.stderr.write("io_guard: " + message + "\n")


def json_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")


def canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def hash_value(value):
    return sha256_bytes(canonical_bytes(value))


def result_hash_fields(result, result_path, result_raw_sha256=None):
    basis = "supplied_raw_result_bytes"
    if result_raw_sha256 is None:
        basis = "actual_result_path_bytes_verified_against_result"
        try:
            data = read_bytes(result_path, "result JSON raw bytes for direct audit")
            parsed = parse_json_bytes(data, result_path)
            if same_json(parsed, result):
                result_raw_sha256 = sha256_bytes(data)
            else:
                basis = "unavailable_result_path_does_not_match_supplied_object"
        except GuardError:
            basis = "unavailable_result_bytes_not_supplied_or_readable"
    elif not isinstance(result_raw_sha256, str) or not SHA256_RE.fullmatch(result_raw_sha256):
        raise GuardError("invalid_result_raw_sha256", "result_raw_sha256 must be the SHA256 of the actual result file bytes", 3)
    else:
        result_raw_sha256 = result_raw_sha256.lower()
    return {
        "result_sha256": result_raw_sha256,
        "result_canonical_sha256": hash_value(result),
        "result_sha256_basis": basis,
    }


def read_bytes(path, role="input"):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except PermissionError as exc:
        raise GuardError(
            "permission_denied",
            "permission denied reading " + role,
            4,
            {"path": str(path)},
        ) from exc
    except FileNotFoundError as exc:
        raise GuardError(
            "input_not_found", role + " does not exist", 3, {"path": str(path)}
        ) from exc
    except OSError as exc:
        raise GuardError(
            "read_failed",
            "cannot read " + role + ": " + str(exc),
            3,
            {"path": str(path)},
        ) from exc


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: " + repr(key))
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("non-finite JSON constant: " + value)


def parse_json_bytes(data, path):
    try:
        value = json.loads(
            data, object_pairs_hook=no_duplicate_keys, parse_constant=reject_constant
        )
    except (ValueError, UnicodeError) as exc:
        raise GuardError(
            "invalid_json",
            "strict JSON parsing failed: " + str(exc),
            3,
            {"path": str(path)},
        ) from exc
    nonfinite = find_nonfinite(value)
    if nonfinite:
        raise GuardError(
            "nonfinite_json_number",
            "JSON number is not finite at " + nonfinite[0]["path"],
            3,
            {"path": str(path), "occurrences": nonfinite[:20]},
        )
    return value


def load_json(path, role="JSON input"):
    data = read_bytes(path, role)
    value = parse_json_bytes(data, path)
    return value, data


def find_nonfinite(value, path=""):
    found = []
    if isinstance(value, float) and not math.isfinite(value):
        found.append({"path": path or "/", "value": repr(value)})
    elif isinstance(value, dict):
        for key, child in value.items():
            found.extend(find_nonfinite(child, pointer_join(path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_nonfinite(child, path + "/" + str(index)))
    return found


def ensure_parent(path):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise GuardError(
            "permission_denied",
            "permission denied creating output directory",
            4,
            {"path": str(Path(path).parent)},
        ) from exc
    except OSError as exc:
        raise GuardError(
            "mkdir_failed",
            "cannot create directory: " + str(exc),
            3,
            {"path": str(Path(path).parent)},
        ) from exc


def atomic_write_bytes(path, data):
    path = os.path.abspath(str(path))
    ensure_parent(path)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix="." + os.path.basename(path) + ".", dir=os.path.dirname(path)
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        try:
            directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except PermissionError as exc:
        raise GuardError(
            "permission_denied",
            "permission denied writing " + path,
            4,
            {"path": path},
        ) from exc
    except OSError as exc:
        raise GuardError(
            "write_failed", "atomic write failed: " + str(exc), 3, {"path": path}
        ) from exc
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def atomic_write_json(path, value):
    atomic_write_bytes(path, json_bytes(value))


def pointer_escape(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def pointer_unescape(value):
    return value.replace("~1", "/").replace("~0", "~")


def pointer_join(path, key):
    return path + "/" + pointer_escape(key)


def all_value_paths(value, path=""):
    paths = [path or "/"]
    if isinstance(value, dict):
        for key, child in value.items():
            paths.extend(all_value_paths(child, pointer_join(path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(all_value_paths(child, path + "/" + str(index)))
    return paths


def same_json(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        return all(same_json(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            same_json(a, b) for a, b in zip(left, right)
        )
    return left == right


def require_case(case):
    if not isinstance(case, dict):
        raise GuardError("invalid_case", "case root must be a JSON object", 3)
    if "case_id" not in case:
        raise GuardError("invalid_case", "case_id is missing", 3)
    if "output_contract" not in case:
        raise GuardError("invalid_case", "output_contract is missing", 3)


def valid_schema_type(value):
    allowed = {"null", "boolean", "object", "array", "number", "string", "integer"}
    if isinstance(value, str):
        return value in allowed
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and item in allowed for item in value)
        and len(set(value)) == len(value)
    )


def looks_like_json_schema(contract):
    if isinstance(contract, bool):
        return True
    if not isinstance(contract, dict) or contract == {}:
        return False
    if "$schema" in contract:
        return True
    if "fields" in contract and not any(
        key in contract
        for key in ("$ref", "properties", "items", "prefixItems", "allOf", "anyOf", "oneOf", "if")
    ):
        return False
    for key, value in contract.items():
        if key == "type" and valid_schema_type(value):
            return True
        if key in {"$ref", "$dynamicRef", "$recursiveRef"} and isinstance(value, str):
            return True
        if key in {"properties", "additionalProperties"} and isinstance(value, (dict, bool)):
            if key == "additionalProperties" or all(isinstance(child, (dict, bool)) for child in value.values()):
                return True
        if key in {"items", "not", "if", "then", "else"} and isinstance(value, (dict, bool, list)):
            return True
        if key in {"prefixItems", "allOf", "anyOf", "oneOf", "enum"} and isinstance(value, list):
            return True
        if key == "const":
            return True
    return False


def local_anchor_map(root):
    anchors = {}

    def add(identifier, value):
        if isinstance(identifier, str) and identifier.startswith("#") and not identifier.startswith("#/"):
            anchors.setdefault(identifier, []).append(value)

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("$anchor"), str):
                add("#" + value["$anchor"], value)
            if isinstance(value.get("$id"), str) and value["$id"].startswith("#") and not value["$id"].startswith("#/"):
                add(value["$id"], value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(root)
    return anchors


def resolve_local_ref(root, ref, anchors=None):
    if not isinstance(ref, str) or not ref.startswith("#"):
        return None
    if ref == "#":
        return root
    if ref.startswith("#/"):
        value = root
        for token in ref[2:].split("/"):
            token = pointer_unescape(token)
            if isinstance(value, dict) and token in value:
                value = value[token]
            elif isinstance(value, list) and token.isdigit() and int(token) < len(value):
                value = value[int(token)]
            else:
                return None
        return value
    targets = (anchors if anchors is not None else local_anchor_map(root)).get(ref, [])
    return targets[0] if len(targets) == 1 else None


def local_reference_issues(root):
    issues = []
    anchors = local_anchor_map(root)
    reported_ambiguous = set()

    def walk(value, path=""):
        if isinstance(value, dict):
            anchor = value.get("$anchor")
            if isinstance(anchor, str) and "#" + anchor not in reported_ambiguous:
                if len(anchors.get("#" + anchor, [])) > 1:
                    reported_ambiguous.add("#" + anchor)
                    issues.append({"code": "ambiguous_local_anchor", "path": path, "ref": "#" + anchor})
            for key, child in value.items():
                child_path = pointer_join(path, key)
                if key in {"$dynamicRef", "$recursiveRef"} and isinstance(child, str):
                    issues.append({"code": "unsupported_dynamic_local_ref", "path": child_path, "ref": child})
                elif key == "$ref" and isinstance(child, str) and child.startswith("#"):
                    if resolve_local_ref(root, child, anchors) is None:
                        issues.append({"code": "unresolved_local_ref", "path": child_path, "ref": child})
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index))

    walk(root)
    return issues


def has_external_refs(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and isinstance(child, str):
                if not child.startswith("#"):
                    return True
            if has_external_refs(child):
                return True
    elif isinstance(value, list):
        return any(has_external_refs(child) for child in value)
    return False


def classify_contract(contract):
    if looks_like_json_schema(contract):
        return "json_schema"
    if isinstance(contract, str):
        return "prose"
    if isinstance(contract, (dict, list)):
        return "declarative_fields"
    return "unsupported"


class ActiveCoverageError(Exception):
    def __init__(self, code, message, path="/output_contract"):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path


def collect_schema_fields(root, active=None):
    declarations = []
    seen = {}

    def add(path, required, schema_path):
        if path not in seen:
            item = {
                "path": path,
                "required_by_parent_schema": bool(required),
                "schema_path": schema_path,
                "alternative_occurrences": 1,
            }
            seen[path] = item
            declarations.append(item)
        else:
            seen[path]["alternative_occurrences"] += 1
            seen[path]["required_by_parent_schema"] = bool(
                seen[path]["required_by_parent_schema"] or required
            )

    def walk(schema, result_path, schema_path, required=False, ref_stack=None):
        if ref_stack is None:
            ref_stack = set()
        if not isinstance(schema, dict):
            return
        if active is not None:
            active["inspect_not_required"](schema, schema_path, result_path, ref_stack)
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#") and ref not in ref_stack:
            target = resolve_local_ref(root, ref)
            if target is not None:
                walk(
                    target,
                    result_path,
                    schema_path + "/$ref->" + ref,
                    required,
                    ref_stack | {ref},
                )
        properties = schema.get("properties")
        if isinstance(properties, dict):
            required_names = schema.get("required", [])
            required_names = required_names if isinstance(required_names, list) else []
            for name, child in properties.items():
                child_path = pointer_join(result_path, name)
                child_schema_path = pointer_join(pointer_join(schema_path, "properties"), name)
                add(child_path, name in required_names, child_schema_path)
                walk(child, child_path, child_schema_path, name in required_names, ref_stack)
        items = schema.get("items")
        item_path = result_path + "/*"
        if isinstance(items, list):
            for index, child in enumerate(items):
                walk(child, item_path, pointer_join(schema_path, "items") + "/" + str(index), False, ref_stack)
        elif items is not None:
            walk(items, item_path, pointer_join(schema_path, "items"), False, ref_stack)
        prefix = schema.get("prefixItems")
        if isinstance(prefix, list):
            for index, child in enumerate(prefix):
                walk(child, item_path, pointer_join(schema_path, "prefixItems") + "/" + str(index), False, ref_stack)
        all_of = schema.get("allOf")
        if isinstance(all_of, list):
            for index, child in enumerate(all_of):
                walk(child, result_path, pointer_join(schema_path, "allOf") + "/" + str(index), required, ref_stack)
        for keyword in ("anyOf", "oneOf"):
            variants = schema.get(keyword)
            if isinstance(variants, list):
                if active is None:
                    selected = range(len(variants))
                else:
                    selected = active["select"](schema, keyword, schema_path, result_path, variants)
                for index in selected:
                    walk(variants[index], result_path, pointer_join(schema_path, keyword) + "/" + str(index), required, ref_stack)
        if "if" in schema:
            if active is None:
                for keyword in ("if", "then", "else", "not"):
                    if keyword in schema:
                        walk(schema[keyword], result_path, pointer_join(schema_path, keyword), False, ref_stack)
            else:
                active["select_conditional"](schema, schema_path, result_path)
                selected_keyword = active["conditional_choice"]
                if selected_keyword:
                    walk(schema[selected_keyword], result_path, pointer_join(schema_path, selected_keyword), required, ref_stack)

    walk(root, "", "/output_contract")
    return declarations


def load_jsonschema_library(contract):
    try:
        spec = importlib.util.find_spec("jsonschema")
    except (ImportError, AttributeError, ValueError) as exc:
        return None, "jsonschema discovery failed: " + str(exc)
    if spec is None:
        return None, "importlib.util.find_spec did not find optional jsonschema; no dependency was installed"
    try:
        module = importlib.import_module("jsonschema")
        validator_class = module.validators.validator_for(contract)
        validator_class.check_schema(contract)
        return (module, validator_class), None
    except Exception as exc:
        return None, type(exc).__name__ + ": " + str(exc)


def jsonschema_branch_valid(contract, schema, instance, runtime):
    module, validator_class = runtime
    validator = validator_class(contract, format_checker=module.FormatChecker())
    if hasattr(validator, "evolve"):
        return validator.evolve(schema=schema).is_valid(instance)
    if isinstance(schema, dict):
        wrapped = {
            key: copy.deepcopy(contract[key])
            for key in ("$schema", "$id", "$anchor", "$defs", "definitions")
            if key in contract
        }
        wrapped.update(copy.deepcopy(schema))
        schema = wrapped
    branch_class = module.validators.validator_for(schema)
    return branch_class(schema, format_checker=module.FormatChecker()).is_valid(instance)


def active_schema_declarations(root, result):
    choices = []
    runtime = None

    def instances(result_path):
        occurrences, unused = resolve_pointer_occurrences(result, result_path)
        return [value for actual_path, present, value in occurrences if present]

    def get_runtime():
        nonlocal runtime
        if runtime is None:
            if has_external_refs(root):
                raise ActiveCoverageError("active_branch_external_refs", "active branch selection would require external schema access; network is forbidden")
            runtime, reason = load_jsonschema_library(root)
            if runtime is None:
                raise ActiveCoverageError("active_branch_validation_not_executed", reason)
        return runtime

    def selected_branches(schema, keyword, schema_path, result_path, variants):
        current_instances = instances(result_path)
        if not current_instances:
            choices.append({"schema_path": schema_path, "keyword": keyword, "active_indices": [], "reason": "no present instance at this result path"})
            return []
        branch_choices = []
        try:
            for instance in current_instances:
                valid = [
                    index
                    for index, variant in enumerate(variants)
                    if jsonschema_branch_valid(root, variant, instance, get_runtime())
                ]
                if keyword == "oneOf" and len(valid) != 1:
                    raise ActiveCoverageError(
                        "oneOf_requires_exactly_one_active_branch",
                        "current result matches " + str(len(valid)) + " oneOf branches at " + schema_path,
                        schema_path,
                    )
                if keyword == "anyOf" and not valid:
                    raise ActiveCoverageError("anyOf_has_no_active_branch", "current result matches no anyOf branch at " + schema_path, schema_path)
                branch_choices.append(tuple(valid))
        except ActiveCoverageError:
            raise
        except Exception as exc:
            raise ActiveCoverageError("active_branch_validation_error", type(exc).__name__ + ": " + str(exc), schema_path) from exc
        if len(set(branch_choices)) != 1:
            raise ActiveCoverageError(
                "conditional_array_branches_require_manual_review",
                "different array instances select different branches; wildcard coverage cannot flatten those alternatives safely",
                schema_path,
            )
        selected = list(branch_choices[0])
        choices.append({"schema_path": schema_path, "keyword": keyword, "active_indices": selected})
        return selected

    def select_conditional(schema, schema_path, result_path):
        current_instances = instances(result_path)
        if not current_instances:
            active["conditional_choice"] = None
            choices.append({"schema_path": schema_path, "keyword": "if", "active_keyword": None, "reason": "no present instance at this result path"})
            return
        selections = []
        try:
            for instance in current_instances:
                selection = "then" if jsonschema_branch_valid(root, schema["if"], instance, get_runtime()) else "else"
                selections.append(selection if selection in schema else None)
        except ActiveCoverageError:
            raise
        except Exception as exc:
            raise ActiveCoverageError("conditional_branch_validation_error", type(exc).__name__ + ": " + str(exc), schema_path) from exc
        if len(set(selections)) != 1:
            raise ActiveCoverageError(
                "conditional_array_branches_require_manual_review",
                "different array instances select different if/then/else branches; wildcard coverage requires manual review",
                schema_path,
            )
        selected = selections[0]
        active["conditional_choice"] = selected
        choices.append({"schema_path": schema_path, "keyword": "if", "active_keyword": selected})

    negation_exclusions = {}

    def inspect_not_required(schema, schema_path, result_path, ref_stack=None):
        ref_stack = set() if ref_stack is None else set(ref_stack)
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#") and ref not in ref_stack:
            target = resolve_local_ref(root, ref)
            if target is None:
                raise ActiveCoverageError("unresolved_negated_reference", "selected schema contains an unresolved local reference used for coverage", schema_path)
            inspect_not_required(target, schema_path + "/$ref->" + ref, result_path, ref_stack | {ref})
        if "not" not in schema:
            return
        negated = schema["not"]
        not_path = pointer_join(schema_path, "not")
        if not isinstance(negated, dict) or set(negated) != {"required"}:
            raise ActiveCoverageError(
                "unsupported_active_negation",
                "only selected not:{required:[nonempty strings]} is converted to property exclusions; complex negations require manual active coverage and no declarations are silently dropped",
                not_path,
            )
        required = negated["required"]
        if not isinstance(required, list) or not required or not all(isinstance(name, str) and name for name in required):
            raise ActiveCoverageError(
                "unsupported_active_negation",
                "not.required must be a nonempty list of nonempty property names; other negations cannot prove which declarations to exclude",
                not_path,
            )
        for name in required:
            excluded_path = pointer_join(result_path, name)
            negation_exclusions.setdefault(excluded_path, []).append(
                {"schema_path": not_path, "required_property": name, "reason": "active not:required forbids this property"}
            )

    active = {
        "select": selected_branches,
        "select_conditional": select_conditional,
        "conditional_choice": None,
        "inspect_not_required": inspect_not_required,
    }
    declarations = collect_schema_fields(root, active)
    active_declarations = [
        declaration for declaration in declarations if declaration["path"] not in negation_exclusions
    ]
    return active_declarations, {
        "status": "active",
        "choices": choices,
        "negation_exclusions": negation_exclusions,
        "excluded_result_paths": sorted(negation_exclusions),
        "static_declarations_preserved": True,
        "negation_support": "selected not:{required:[nonempty strings]} including local refs and allOf; complex negations require manual review",
    }


def normalize_declared_pointer(spec, base=""):
    if spec.startswith("/"):
        tokens = [pointer_unescape(token) for token in spec[1:].split("/")]
        return "/" + "/".join(pointer_escape(token) for token in tokens)
    path = base
    for token in spec.split("/"):
        path = pointer_join(path, token)
    return path


DECLARATIVE_CONTAINER_KEYS = (
    "field_requirements", "fields", "top_level_fields", "declared_fields",
    "field_paths", "all_declared_fields", "properties",
)
DECLARATIVE_CONTROL_KEYS = {
    "result_file", "format", "required_path", "output_path", "schema_style",
    "required_fields", "additional_top_level_fields", "additional_top_level_fields_allowed",
    "additional_top_level_fields_permitted", "additional_top_level_keys",
    "additional_top_level_keys_allowed", "additional_fields", "additional_fields_allowed",
    "allow_additional_top_level_fields", "allow_additional_fields",
    "allow_extra_fields", "allow_extra_top_level_fields",
    "description", "title", "version", "kind", "notes", "type",
    *DECLARATIVE_CONTAINER_KEYS,
}
DOMAIN_FINAL_STATUSES = (
    "undetermined", "conditional", "conditional_choice",
    "no_feasible_candidate", "no_feasible_plan", "infeasible",
)


def describe_declarative_field(value):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {"rule": value, "opaque_literal": isinstance(value, str)}


def declarative_field_map(contract):
    for key in DECLARATIVE_CONTAINER_KEYS:
        value = contract.get(key) if isinstance(contract, dict) else None
        if isinstance(value, (dict, list)):
            return value, key
        if value is not None:
            return value, key
    direct_fields = {
        key: value for key, value in contract.items() if key not in DECLARATIVE_CONTROL_KEYS
    }
    return (direct_fields if direct_fields else None), None


def declarative_field_nullable(spec):
    if not isinstance(spec, dict):
        return False
    if spec.get("nullable") is True:
        return True
    kind = spec.get("type")
    if kind == "null" or (isinstance(kind, list) and "null" in kind):
        return True
    for key in ("rule", "required_content", "constraint", "note"):
        text = spec.get(key)
        if isinstance(text, str) and re.search(
            r"(?:^|\s)nullable(?:\s|$)|may be null|or null|可空|允许\s*null|为\s*null|或\s*null",
            text,
            re.I,
        ):
            return True
    return False


def declarative_field_allowed_values(spec):
    if not isinstance(spec, dict):
        return None
    for key in ("allowed", "allowed_values", "enum", "final_states", "allowed_final_states"):
        values = spec.get(key)
        if isinstance(values, list) and all(isinstance(value, str) for value in values):
            return list(values)
    return None


def analyze_declarative_contract(contract):
    container, container_key = declarative_field_map(contract)
    fields = {}
    required_fields = contract.get("required_fields") if isinstance(contract, dict) else None
    literal_warnings = []
    if isinstance(container, dict):
        for name, raw in container.items():
            if name in DECLARATIVE_CONTROL_KEYS:
                continue
            fields[name] = describe_declarative_field(raw)
    elif isinstance(container, list):
        for item in container:
            if isinstance(item, str) and container_key in {"field_paths", "all_declared_fields"}:
                name = pointer_unescape(item[1:].split("/")[-1]) if item.startswith("/") else item
                fields.setdefault(name, {"rule": "declared field pointer"})
            elif isinstance(item, str):
                fields.setdefault(item, {"rule": "declared field"})
            elif isinstance(item, dict):
                name = item.get("name", item.get("key", item.get("field")))
                if isinstance(name, str):
                    path = item.get("path")
                    fields[name] = describe_declarative_field(item)
                    if isinstance(path, str):
                        fields[name]["path"] = path
                elif "path" in item or len(item) == 1:
                    fields[str(item.get("path", next(iter(item))))] = describe_declarative_field(item)
                else:
                    literal_warnings.append("a field list entry is not a simple name or recognizable field descriptor")
    elif container is not None:
        literal_warnings.append("a fields container is a literal opaque assignment, not a reliable field map")
    if isinstance(required_fields, list):
        for name in required_fields:
            if isinstance(name, str):
                fields.setdefault(name, {"rule": "listed in required_fields"})
            else:
                literal_warnings.append("required_fields contains a non-string entry")
    elif required_fields is not None:
        literal_warnings.append("required_fields is not an array")
    field_descriptors = []
    for name, spec in fields.items():
        field_descriptors.append({
            "name": name,
            "nullable": declarative_field_nullable(spec),
            "allowed": declarative_field_allowed_values(spec),
            "spec": spec,
        })
    additional_allowed = None
    closed_key = None
    if isinstance(contract, dict):
        for key in (
            "additional_top_level_fields", "additional_top_level_fields_allowed",
            "additional_top_level_fields_permitted", "additional_top_level_keys",
            "additional_top_level_keys_allowed", "additional_fields",
            "additional_fields_allowed", "allow_additional_top_level_fields",
            "allow_additional_fields", "allow_extra_fields", "allow_extra_top_level_fields",
        ):
            if key in contract and isinstance(contract[key], bool):
                additional_allowed = contract[key]
                closed_key = key
                break
    status_descriptor = fields.get("status", {})
    status_allowed = declarative_field_allowed_values(status_descriptor)
    if status_allowed is None and isinstance(status_descriptor, dict):
        rule_parts = []
        for key, value in status_descriptor.items():
            if isinstance(value, str):
                rule_parts.append(value)
        text = " ".join(rule_parts)
        found = []
        for candidate in ("complete", "partial", *DOMAIN_FINAL_STATUSES, "failed"):
            if re.search(r"(?<![A-Za-z0-9_])" + re.escape(candidate) + r"(?![A-Za-z0-9_])", text, re.I):
                found.append(candidate)
        status_allowed = found or None
    return {
        "container_key": container_key,
        "field_descriptors": field_descriptors,
        "top_level_names": [entry["name"] for entry in field_descriptors],
        "additional_top_level_fields_allowed": additional_allowed,
        "additional_fields_key": closed_key,
        "nullable_paths": ["/" + entry["name"] for entry in field_descriptors if entry["nullable"]],
        "allowed_statuses": status_allowed,
        "literal_warnings": literal_warnings,
    }


def nullable_paths_for_result(contract, kind, analysis):
    if kind != "json_schema":
        return set(analysis.get("nullable_paths", [])) if analysis else set()

    def accepts_null(schema, ref_stack=None):
        ref_stack = set() if ref_stack is None else ref_stack
        if isinstance(schema, bool):
            return schema
        if not isinstance(schema, dict):
            return False
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref not in ref_stack:
            target = resolve_local_ref(contract, ref)
            return accepts_null(target, ref_stack | {ref}) if target is not None else False
        kind_value = schema.get("type")
        if kind_value == "null" or (isinstance(kind_value, list) and "null" in kind_value):
            return True
        if "const" in schema and schema["const"] is None:
            return True
        if isinstance(schema.get("enum"), list) and any(value is None for value in schema["enum"]):
            return True
        if isinstance(schema.get("allOf"), list):
            return all(accepts_null(child, ref_stack) for child in schema["allOf"])
        for keyword in ("anyOf", "oneOf"):
            if isinstance(schema.get(keyword), list):
                return any(accepts_null(child, ref_stack) for child in schema[keyword])
        return "type" not in schema and "const" not in schema and "enum" not in schema

    paths = set()

    def walk(schema, path="", ref_stack=None):
        ref_stack = set() if ref_stack is None else ref_stack
        if not isinstance(schema, dict):
            return
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref not in ref_stack:
            target = resolve_local_ref(contract, ref)
            if target is not None:
                walk(target, path, ref_stack | {ref})
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                child_path = pointer_join(path, name)
                if accepts_null(child):
                    paths.add(child_path)
                walk(child, child_path, ref_stack)
        if isinstance(schema.get("items"), dict):
            walk(schema["items"], path + "/*", ref_stack)
        for keyword in ("allOf", "anyOf", "oneOf", "then", "else"):
            children = schema.get(keyword)
            if isinstance(children, list):
                for child in children:
                    walk(child, path, ref_stack)

    walk(contract)
    return paths


def collect_declarative_fields(contract):
    declarations = []
    seen = set()

    def add(path, required, contract_path):
        if path and path not in seen:
            seen.add(path)
            declarations.append(
                {
                    "path": path,
                    "required_by_parent_schema": bool(required),
                    "schema_path": contract_path,
                    "alternative_occurrences": 1,
                }
            )

    def entries(container, base, contract_path):
        if isinstance(container, dict):
            for name, spec in container.items():
                if not isinstance(name, str):
                    continue
                path = pointer_join(base, name)
                required = isinstance(spec, dict) and spec.get("required") is True
                add(path, required, pointer_join(contract_path, name))
                if isinstance(spec, dict):
                    nested = spec.get("fields", spec.get("declared_fields"))
                    if nested is not None:
                        entries(nested, path, pointer_join(contract_path, name))
        elif isinstance(container, list):
            for index, spec in enumerate(container):
                item_path = contract_path + "/" + str(index)
                if isinstance(spec, str):
                    if spec:
                        add(normalize_declared_pointer(spec, base), False, item_path)
                elif isinstance(spec, dict):
                    name = spec.get("path", spec.get("name", spec.get("key")))
                    if isinstance(name, str) and name:
                        path = normalize_declared_pointer(name, base)
                        add(path, spec.get("required") is True, item_path)
                        nested = spec.get("fields", spec.get("declared_fields"))
                        if nested is not None:
                            entries(nested, path, item_path)

    if isinstance(contract, list):
        entries(contract, "", "/output_contract")
    elif isinstance(contract, dict):
        container = None
        container_key = None
        for key in DECLARATIVE_CONTAINER_KEYS:
            if key in contract:
                container = contract[key]
                container_key = key
                break
        if container is not None:
            entries(container, "", pointer_join("/output_contract", container_key))
        elif contract and all(isinstance(key, str) for key in contract):
            field_map = {key: value for key, value in contract.items() if key not in DECLARATIVE_CONTROL_KEYS}
            entries(field_map, "", "/output_contract")
    return declarations


def contract_declarations(contract, kind=None):
    kind = kind or classify_contract(contract)
    if kind == "json_schema":
        return collect_schema_fields(contract)
    if kind == "declarative_fields":
        return collect_declarative_fields(contract)
    return []


def root_field_allowed(schema, name, root=None, ref_stack=None):
    root = schema if root is None else root
    ref_stack = set() if ref_stack is None else ref_stack
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        return False
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in ref_stack:
        target = resolve_local_ref(root, ref)
        if target is not None:
            return root_field_allowed(target, name, root, ref_stack | {ref})
    for keyword in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            for child in variants:
                if root_field_allowed(child, name, root, ref_stack):
                    return True
    properties = schema.get("properties")
    if isinstance(properties, dict) and name in properties:
        return True
    if "type" in schema and schema.get("type") != "object":
        types = schema.get("type")
        if not (isinstance(types, list) and "object" in types):
            return False
    if properties is not None or schema.get("type") == "object":
        return schema.get("additionalProperties", True) is not False
    return True


def json_value_matches_type(value, expected):
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "integer":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
        ) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    return False


def literal_schema_allows(root, schema, value, ref_stack=None):
    ref_stack = set() if ref_stack is None else ref_stack
    if isinstance(schema, bool):
        return schema
    if not isinstance(schema, dict):
        return False
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in ref_stack:
        target = resolve_local_ref(root, ref)
        if target is None:
            return False
        return literal_schema_allows(root, target, value, ref_stack | {ref})
    if "const" in schema and not same_json(schema["const"], value):
        return False
    if isinstance(schema.get("enum"), list) and not any(same_json(item, value) for item in schema["enum"]):
        return False
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(json_value_matches_type(value, item) for item in types):
            return False
    for keyword in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list):
            matches = sum(literal_schema_allows(root, child, value, ref_stack) for child in variants)
            if keyword == "allOf" and matches != len(variants):
                return False
            if keyword == "anyOf" and matches < 1:
                return False
            if keyword == "oneOf" and matches != 1:
                return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False
        if "pattern" in schema:
            try:
                if re.search(schema["pattern"], value) is None:
                    return False
            except (re.error, TypeError):
                return False
    return True


def injection_field_schema(root, schema, name, ref_stack=None):
    ref_stack = set() if ref_stack is None else ref_stack
    if not isinstance(schema, dict):
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref not in ref_stack:
        target = resolve_local_ref(root, ref)
        return injection_field_schema(root, target, name, ref_stack | {ref}) if target is not None else None
    if any(keyword in schema for keyword in ("oneOf", "anyOf", "if", "then", "else")):
        variants = schema.get("allOf")
        if not isinstance(variants, list):
            return None
    if isinstance(schema.get("allOf"), list):
        found = {}
        for child in schema["allOf"]:
            child_schema = injection_field_schema(root, child, name, ref_stack)
            if child_schema is None:
                return None
            found.update(child_schema)
        own = {key: value for key, value in schema.items() if key != "allOf"}
        own_schema = injection_field_schema(root, own, name, ref_stack)
        if own_schema is None:
            return None
        found.update(own_schema)
        return found
    properties = schema.get("properties")
    if isinstance(properties, dict) and name in properties:
        return properties[name]
    additional = schema.get("additionalProperties", True)
    if additional is False:
        return None
    if additional is True:
        return {}
    return additional if isinstance(additional, dict) else None


OMIT_SKELETON_VALUE = object()


def skeleton_value(root, schema, ref_stack=None, depth=0):
    if ref_stack is None:
        ref_stack = set()
    if depth > 100:
        return OMIT_SKELETON_VALUE
    if isinstance(schema, bool):
        return None if schema else {}
    if not isinstance(schema, dict):
        return OMIT_SKELETON_VALUE
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#") and ref not in ref_stack:
        target = resolve_local_ref(root, ref)
        if target is not None:
            return skeleton_value(root, target, ref_stack | {ref}, depth + 1)
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    for keyword in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(keyword)
        if isinstance(variants, list) and variants:
            if keyword == "allOf":
                merged = {}
                for child in variants:
                    child_value = skeleton_value(root, child, ref_stack, depth + 1)
                    if isinstance(child_value, dict):
                        merged.update(child_value)
                own = skeleton_value(
                    root,
                    {key: value for key, value in schema.items() if key not in {"allOf", "$ref"}},
                    ref_stack,
                    depth + 1,
                )
                if isinstance(own, dict):
                    merged.update(own)
                return merged if merged or own is not OMIT_SKELETON_VALUE else OMIT_SKELETON_VALUE
            for child in variants:
                child_value = skeleton_value(root, child, ref_stack, depth + 1)
                if child_value is not OMIT_SKELETON_VALUE:
                    return child_value
            return OMIT_SKELETON_VALUE
    schema_type = schema.get("type")
    types = schema_type if isinstance(schema_type, list) else [schema_type]
    if isinstance(schema.get("properties"), dict) or "object" in types:
        result = {}
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for name, child in properties.items():
                child_value = skeleton_value(root, child, ref_stack, depth + 1)
                if child_value is not OMIT_SKELETON_VALUE:
                    result[name] = child_value
        return result
    if "array" in types or "items" in schema or "prefixItems" in schema:
        return []
    if "string" in types:
        return "" if literal_schema_allows(root, schema, "") else OMIT_SKELETON_VALUE
    if "null" in types:
        return None
    if isinstance(schema.get("enum"), list) and any(item is None for item in schema["enum"]):
        return None
    return OMIT_SKELETON_VALUE


def set_if_allowed(result, root, name, value):
    if not isinstance(result, dict):
        return
    field_schema = injection_field_schema(root, root, name)
    if field_schema is not None and literal_schema_allows(root, field_schema, value):
        result[name] = copy.deepcopy(value)


def make_snapshot(case, case_path):
    contract = case["output_contract"]
    kind = classify_contract(contract)
    declarations = contract_declarations(contract, kind)
    declarative = analyze_declarative_contract(contract) if kind != "json_schema" else None
    return {
        "location": "/output_contract",
        "case_file": os.path.abspath(str(case_path)),
        "kind": kind,
        "detection": {
            "is_json_schema": kind == "json_schema",
            "method": "explicit $schema or root JSON-Schema structural keywords; otherwise manual/declarative",
            "external_refs_present": has_external_refs(contract),
            "local_reference_issues": local_reference_issues(contract) if kind == "json_schema" else [],
            "declarative_analysis": declarative,
        },
        "value": copy.deepcopy(contract),
        "sha256": hash_value(contract),
        "value_paths": all_value_paths(contract, "/output_contract"),
        "declared_fields": declarations,
    }


def file_record(path, role, origin):
    path = os.path.abspath(str(path))
    record = {
        "path": path,
        "role": role,
        "origin": origin,
        "found": False,
        "readable": False,
        "media_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
        "size_bytes": None,
        "sha256": None,
        "content_reviewed": False,
        "review_required": True,
        "review_note": "registration and SHA256 hashing do not establish semantic content review",
        "error": None,
    }
    try:
        if not os.path.isfile(path):
            record["error"] = "not_a_regular_file" if os.path.exists(path) else "missing"
            return record
        record["found"] = True
        data = read_bytes(path, role)
        record.update(
            {
                "readable": True,
                "size_bytes": len(data),
                "sha256": sha256_bytes(data),
                "content_reviewed": role == "case",
                "review_note": (
                    "case JSON was strictly parsed; referenced source files still require individual review"
                    if role == "case"
                    else "registration and SHA256 hashing do not establish semantic content review"
                ),
            }
        )
    except GuardError as exc:
        record["error"] = exc.code
    return record


def discover_task(requested):
    candidates = []
    if requested:
        candidates.append(os.path.abspath(requested))
    for path in ("/input/task.md", "/app/task.md", "/app/work/task.md", "/task.md"):
        if path not in candidates:
            candidates.append(path)
    probed = []
    selected = None
    for path in candidates:
        exists = os.path.isfile(path)
        probed.append({"path": path, "found": exists})
        if exists and selected is None:
            selected = path
    return selected, {
        "requested": os.path.abspath(requested) if requested else None,
        "status": "found" if selected else "missing",
        "selected": selected,
        "probed": probed,
    }


def resolve_case_input_candidates(candidate, case_directory):
    case_directory = os.path.realpath(os.path.abspath(case_directory))
    options = []

    def add(path, resolved_by, authorized_root):
        authorized_root = os.path.realpath(authorized_root)
        real = os.path.realpath(os.path.abspath(path))
        if real == authorized_root or real.startswith(authorized_root + os.sep):
            options.append({"path": real, "resolved_by": resolved_by})

    conventional = candidate[2:] if candidate.startswith("./") else candidate
    if os.path.basename(case_directory) == "input" and conventional.startswith("input/"):
        stripped = conventional[len("input/"):]
        add(os.path.join(case_directory, stripped), "input_root_literal_prefix", case_directory)
    add(os.path.join(case_directory, candidate), "case_relative", case_directory)
    add(os.path.join("/input", candidate), "system_input_root", "/input")
    unique = []
    seen = set()
    for item in options:
        if item["path"] not in seen:
            seen.add(item["path"])
            unique.append(item)
    return unique


def inspect_case_paths(case, work, output, case_path):
    candidates = []
    ambiguous = []
    excluded = [os.path.realpath(str(work)), os.path.realpath(str(output))]
    case_directory = os.path.dirname(os.path.abspath(str(case_path)))
    exact_path_keys = {
        "path", "file_path", "filepath", "local_path", "input_path",
        "material_path", "document_path", "attachment_path", "pdf_path",
        "file", "filename", "file_name", "source_file", "material_file",
        "document_file", "attachment_file", "files",
    }
    file_containers = ("materials", "files", "documents", "attachments", "input_files", "source_files", "pdfs")

    def excluded_path(path):
        real = os.path.realpath(path)
        return any(real == root or real.startswith(root + os.sep) for root in excluded)

    def concrete_filename(value):
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            return False
        if any(ord(character) < 32 for character in value) or "%" in value:
            return False
        suffix = Path(value).suffix[1:]
        return bool(suffix and any(character.isalpha() for character in suffix))

    def walk(value, key="", pointer=""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key == "output_contract":
                    continue
                walk(child, child_key, pointer_join(pointer, child_key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, key, pointer + "/" + str(index))
        elif isinstance(value, str):
            consider(value, key, pointer)

    def consider(value, key, pointer):
        candidate = value.strip()
        normalized_key = key.lower().replace("-", "_")
        if not candidate or "\n" in candidate or "\x00" in candidate or SCHEME_RE.match(candidate):
            return
        direct_syntax = normalized_key in exact_path_keys or normalized_key.endswith(("_path", "_paths", "_file", "_files"))
        parent = pointer[: -(len(pointer_escape(key)) + 1)]
        nested_name = normalized_key == "name" and any(
            parent.endswith("/" + container + "/" + pointer_unescape(parent.rsplit("/", 1)[-1]))
            and parent.rsplit("/", 1)[-1].isdigit()
            for container in file_containers
        )
        if nested_name and not concrete_filename(candidate):
            return
        if not direct_syntax and not nested_name:
            return
        if not candidate.startswith("/") and "%" in candidate:
            ambiguous.append({"pointer": pointer, "field": key, "reason": "percent-bearing label was not guessed to be a local file"})
            return
        if not direct_syntax and not nested_name and not (direct_syntax and candidate.startswith(("/", "./", "../"))):
            return
        if not direct_syntax and not nested_name:
            return
        if candidate.startswith(("~", "data:")):
            return
        if os.path.isabs(candidate):
            local = os.path.realpath(candidate)
            resolved_by = "absolute_case_input_realpath"
        else:
            options = resolve_case_input_candidates(candidate, case_directory)
            present = [item for item in options if os.path.exists(item["path"])]
            if len(present) > 1:
                ambiguous.append({
                    "pointer": pointer,
                    "field": key,
                    "reason": "multiple realinput candidates resolve this declared path; no silent first match is selected",
                    "candidates": [{"path": item["path"], "resolved_by": item["resolved_by"], "exists": True} for item in present],
                })
                return
            local = present[0]["path"] if present else (options[0]["path"] if options else None)
            resolved_by = present[0]["resolved_by"] if present else (options[0]["resolved_by"] if options else "no_authorized_candidate")
            if local is None:
                ambiguous.append({"pointer": pointer, "field": key, "reason": "relative path escapes authorized case/input realpaths", "candidate": candidate})
                return
        if excluded_path(local):
            return
        candidates.append({"path": local, "pointer": pointer, "field": key, "nested_name": nested_name, "resolved_by": resolved_by})

    walk(case)
    unique = []
    seen = set()
    for item in candidates:
        key = (item["path"], item["pointer"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return {
        "references": unique,
        "ambiguous_references": ambiguous,
        "static_analysis_complete": not ambiguous,
        "relative_base": case_directory,
        "fallback_base": "/input",
        "policy": "explicit path/file syntax and concrete names directly inside known file containers only; URLs, percent labels, and free-form material_source are not local paths",
    }


def discover_case_paths(case, work, output, case_path="/input/case.json"):
    return [
        (item["path"], item["pointer"])
        for item in inspect_case_paths(case, work, output, case_path)["references"]
    ]


def build_input_manifest(case, case_path, task, work, output):
    selected_task, task_discovery = discover_task(task)
    discovery = inspect_case_paths(case, work, output, case_path)
    records = [file_record(case_path, "case", "case_file")]

    def add_case_reference(path, pointer, resolved_by):
        record = file_record(path, "case_reference", pointer)
        record["resolved_by"] = resolved_by
        records.append(record)

    for item in discovery["references"]:
        path = item["path"]
        pointer = item["pointer"]
        if os.path.isdir(path):
            try:
                for root, dirs, files in os.walk(path, followlinks=False):
                    dirs.sort()
                    files.sort()
                    for name in files:
                        add_case_reference(os.path.join(root, name), pointer, item["resolved_by"])
            except OSError:
                add_case_reference(path, pointer, item["resolved_by"])
        else:
            add_case_reference(path, pointer, item["resolved_by"])
    if selected_task:
        records.append(file_record(selected_task, "task", "task_discovery"))
    else:
        for probe in task_discovery["probed"]:
            if not probe["found"]:
                records.append(file_record(probe["path"], "task_candidate", "task_discovery"))
    unique = []
    seen = set()
    for record in records:
        key = record["path"]
        if key in seen:
            continue
        seen.add(key)
        record["input_id"] = "input-%04d" % (len(unique) + 1)
        unique.append(record)
    return unique, task_discovery, discovery


def initial_ledgers(case_path):
    timestamp = utc_now()
    return {
        "sources": [
            {
                "source_id": "case-contract",
                "locator": os.path.abspath(str(case_path)),
                "role": "case_and_output_contract_only",
                "access": "strictly_parsed",
            }
        ],
        "uncertainty": [
            {
                "code": "inputs_not_semantically_reviewed",
                "detail": "Referenced files are registered and hashed, not yet reviewed for scientific meaning.",
                "blocking_complete": True,
            }
        ],
        "uncertainty_note": "Known bootstrap uncertainty is listed; this is not a none-known declaration.",
        "limitations": [
            "Partial bootstrap only: declared values may be null or empty and require task-specific completion.",
            "Structural checks do not establish arithmetic, source support, or scientific validity.",
        ],
        "limitations_note": "Concrete residual limitations remain until replaced by reviewed task-specific text.",
        "change_records": [
            {
                "operation": "bootstrap",
                "path": "result.json",
                "reason": "Created an early honest partial deliverable without inventing contract values.",
                "timestamp_utc": timestamp,
                "run_record": "run_record.json",
            }
        ],
    }


def make_initial_result(case, snapshot, case_path):
    contract = case["output_contract"]
    kind = snapshot["kind"]
    ledgers = initial_ledgers(case_path)
    if kind == "json_schema":
        result = skeleton_value(contract, contract)
        if result is OMIT_SKELETON_VALUE:
            result = None
        if not isinstance(result, dict):
            return result, ledgers
        set_if_allowed(result, contract, "case_id", case["case_id"])
        if "case_id" in result and not same_json(result["case_id"], case["case_id"]):
            result.pop("case_id", None)
        if result.get("status") != "partial":
            result.pop("status", None)
            set_if_allowed(result, contract, "status", "partial")
        set_if_allowed(result, contract, "contract_sha256", snapshot["sha256"])
        set_if_allowed(result, contract, "run_record_ref", RUN_RECORD_NAME)
        if "example_label" in case:
            set_if_allowed(result, contract, "example_label", case["example_label"])
        for key in LEDGER_KEYS:
            value = result.get(key)
            if key in result and (value is None or value == "" or value == [] or value == {}):
                result.pop(key, None)
        return result, ledgers
    analysis = snapshot["detection"].get("declarative_analysis") or {}
    if analysis.get("additional_top_level_fields_allowed") is not False:
        result = {"case_id": copy.deepcopy(case["case_id"]), "status": "partial"}
        result.update(copy.deepcopy(ledgers))
        result["contract_sha256"] = snapshot["sha256"]
        result["run_record_ref"] = RUN_RECORD_NAME
        result["contract_mapping"] = {
            "kind": kind,
            "manual_review_required": True,
            "declared_fields": [item["path"] for item in snapshot["declared_fields"]],
        }
        return result, ledgers
    result = {}
    descriptors = {entry["name"]: entry["spec"] for entry in analysis.get("field_descriptors", [])}
    if "case_id" in descriptors:
        result["case_id"] = copy.deepcopy(case["case_id"])
    allowed = analysis.get("allowed_statuses") or ["partial"]
    for candidate in ("partial", "undetermined", "conditional", "conditional_choice"):
        if candidate in allowed:
            result["status"] = candidate
            break
    for name, spec in descriptors.items():
        if name in result or name in {"case_id", "status"}:
            continue
        if "/" + name in analysis.get("nullable_paths", []):
            result[name] = None
            continue
        kind_value = spec.get("type")
        types = kind_value if isinstance(kind_value, list) else [kind_value]
        if "array" in types:
            result[name] = []
        elif "object" in types:
            result[name] = {}
        else:
            rule_text = " ".join(value for value in spec.values() if isinstance(value, str))
            if "array" in rule_text or "数组" in rule_text:
                result[name] = []
            elif "object" in rule_text or "对象" in rule_text:
                result[name] = {}
    return result, ledgers


def make_run_record(case, case_path, snapshot, manifest, task_discovery, ledgers, work, output, input_discovery=None):
    return {
        "record_version": "io-guard-run-record/v1",
        "tool": {"name": "io_guard.py", "version": VERSION},
        "created_at_utc": utc_now(),
        "updated_at_utc": utc_now(),
        "status": "partial",
        "processing_status": "partial",
        "raw_domain_status": None,
        "finalization": {
            "state": "open",
            "finalized_at_utc": None,
            "pending_validations": ["task-specific completion and scientific review"],
        },
        "case_id": copy.deepcopy(case["case_id"]),
        "case_id_type": type(case["case_id"]).__name__,
        "case_file": os.path.abspath(str(case_path)),
        "case_raw_sha256": sha256_bytes(read_bytes(case_path, "case file")),
        "output_contract_snapshot": snapshot,
        "all_declared_fields": [item["path"] for item in snapshot["declared_fields"]],
        "input_manifest": manifest,
        "input_discovery": input_discovery or {
            "references": [],
            "ambiguous_references": [],
            "static_analysis_complete": False,
            "reason": "run record predates static input discovery metadata",
        },
        "task_discovery": task_discovery,
        "work": os.path.abspath(str(work)),
        "output": os.path.abspath(str(output)),
        "result_path": os.path.abspath(os.path.join(str(output), RESULT_NAME)),
        "sources": copy.deepcopy(ledgers["sources"]),
        "uncertainty": copy.deepcopy(ledgers["uncertainty"]),
        "uncertainty_note": ledgers["uncertainty_note"],
        "limitations": copy.deepcopy(ledgers["limitations"]),
        "limitations_note": ledgers["limitations_note"],
        "change_records": copy.deepcopy(ledgers["change_records"]),
        "evidence": [],
        "claims": [],
        "manual_review": {
            "status": "required" if snapshot["kind"] in {"prose", "declarative_fields", "unsupported"} else "not_applicable",
            "contract_sha256": snapshot["sha256"],
            "reviewer": None,
            "reviewed_at_utc": None,
            "notes": "Narrative or non-Schema contracts require human/manual mapping review." if snapshot["kind"] != "json_schema" else "JSON Schema recognition is structural only.",
            "next": MANUAL_REVIEW_NEXT if snapshot["kind"] != "json_schema" else None,
            "next_required_actions": [MANUAL_REVIEW_NEXT] if snapshot["kind"] != "json_schema" else [],
        },
        "scientific_review": {
            "status": "not_verified",
            "unconditionally_verified": False,
            "notes": "This tool never proves mathematical optimality, source entailment, or scientific correctness.",
        },
        "change_journal": {
            "path": os.path.abspath(os.path.join(str(work), JOURNAL_NAME)),
            "format": "append-only-jsonl",
            "write_mode": "record-change subcommand only",
        },
    }


def resolve_pointer_occurrences(value, pointer):
    tokens = [pointer_unescape(token) for token in pointer.split("/")[1:]] if pointer.startswith("/") else []
    empty_arrays = []

    def walk(current, index, actual):
        if index == len(tokens):
            return [(actual or "/", True, current)]
        token = tokens[index]
        if token == "*":
            if isinstance(current, list):
                if not current:
                    empty_arrays.append(actual or "/")
                    return []
                found = []
                for item_index, child in enumerate(current):
                    found.extend(walk(child, index + 1, actual + "/" + str(item_index)))
                return found
            return [(actual + "/*", False, None)]
        if isinstance(current, dict) and token in current:
            return walk(current[token], index + 1, pointer_join(actual, token))
        return [(pointer_join(actual, token), False, None)]

    return walk(value, 0, ""), empty_arrays


def coverage_report(result, declarations):
    entries = []
    for declaration in declarations:
        occurrences, empty_arrays = resolve_pointer_occurrences(result, declaration["path"])
        missing = [path for path, present, unused in occurrences if not present]
        entries.append(
            {
                "path": declaration["path"],
                "required_by_parent_schema": declaration["required_by_parent_schema"],
                "covered": not missing,
                "missing_paths": missing,
                "checked_occurrences": len(occurrences),
                "not_applicable_empty_arrays": empty_arrays,
            }
        )
    return entries


def value_at_pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return False, None
    current = document
    for token in pointer.split("/")[1:]:
        token = pointer_unescape(token)
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            return False, None
    return True, current


def future_observation_context(container):
    if not isinstance(container, dict):
        return {}
    result = {}
    for key in (
        "cause", "consequence", "needed_confirmation", "needed_evidence",
        "required_confirmation", "required_evidence", "future_observation_semantics",
        "details", "detail", "reason", "conditions", "limitations", "item", "role",
    ):
        value = container.get(key)
        if isinstance(value, str) and meaningful_text(value):
            result[key] = value[:800]
    return result


def planning_advisory_scope(result, status_policy, explanation_paths):
    scope = {
        "eligible": False,
        "scope": "planning_comparison_only",
        "selection_type": "conditional_planning_preference_not_exact_winner",
        "execution_not_authorized": True,
        "evidence": {},
    }
    if (
        not isinstance(result, dict)
        or not status_policy.get("valid")
        or status_policy.get("processing_status") != "complete"
        or status_policy.get("raw_domain_status") not in {"undetermined", "conditional", "conditional_choice"}
        or not explanation_paths
        or has_blocking_complete_flag(result)
    ):
        return scope
    comparison_path = next(
        ("/" + key for key in ("comparison_matrix", "planning_comparison", "candidate_comparison")
         if isinstance(result.get(key), list) and result[key] and all(isinstance(row, dict) for row in result[key])),
        None,
    )
    selection_path = next(
        ("/" + key for key in ("selection", "conditional_selection", "planning_selection")
         if isinstance(result.get(key), dict)),
        None,
    )
    if comparison_path is None or selection_path is None:
        return scope
    selection = value_at_pointer(result, selection_path)[1]
    identifier = next(
        (selection[key] for key in ("plan_id", "candidate_id", "plan_key") if present_text(selection.get(key))),
        None,
    )
    version = next(
        (selection[key] for key in ("version", "plan_version") if present_text(selection.get(key))),
        None,
    )
    if identifier is None or version is None:
        return scope
    comparison = value_at_pointer(result, comparison_path)[1]
    identity_found = any(
        any(key in {"plan_id", "candidate_id", "plan_key"} and same_json(value, identifier) for key, value in row.items())
        and any(key in {"version", "plan_version"} and same_json(value, version) for key, value in row.items())
        for row in comparison
    )
    if not identity_found:
        return scope
    markers = []
    if selection.get("not_an_execution_authorization") is True:
        markers.append(selection_path + "/not_an_execution_authorization")
    for key, value in selection.items():
        if normalized_state_key(key) not in {
            "selection_type", "conditional_status", "selection_status", "execution_approval",
            "execution_authorization", "eligibility", "scope", "decision_boundary",
        } or not isinstance(value, str):
            continue
        text = value.strip().lower().replace("-", "_").replace(" ", "_")
        explicitly_denied = re.search(r"(?:^|_)not(?:_yet)?_(?:granted|approved|authorized)(?:_|$)", text)
        planning_only = "conditional" in text and any(
            marker in text for marker in ("planning", "selection_only", "for_limited_scientific_objective", "eligible")
        )
        if explicitly_denied or planning_only:
            markers.append(pointer_join(selection_path, key))
    if not markers:
        return scope
    scope.update({
        "eligible": True,
        "explanation_paths": list(explanation_paths),
        "evidence": {
            "comparison_path": comparison_path,
            "selection_path": selection_path,
            "selected_id_matches_comparison": True,
            "conditional_or_not_authorized_paths": markers,
        },
    })
    return scope


def is_future_observation_item(item, result, advisory_scope):
    if item.get("kind") != "blocking_unknown_pending" or not advisory_scope.get("eligible"):
        return False
    path = item.get("path", "")
    path_components = {pointer_unescape(token).lower() for token in path.split("/")}
    decision_fields = {
        "optimizer", "optimization", "optimization_search", "optimality", "best_candidate",
        "best_candidate_id", "winner", "max_candidates", "feasibility", "feasibility_claim",
        "constraint", "constraints", "constraint_satisfied", "constraints_satisfied",
    }
    if path_components & decision_fields or any(component.startswith("hard_safety") for component in path_components):
        return False
    found, record = value_at_pointer(result, path.rsplit("/", 1)[0])
    if not found or not isinstance(record, dict):
        return False
    context = item.get("context", {})
    cause = context.get("cause", context.get("reason"))
    needed = next((context[key] for key in (
        "needed_confirmation", "needed_evidence", "required_confirmation", "required_evidence",
    ) if meaningful_text(context.get(key))), None)
    consequence = next((context[key] for key in ("consequence", "detail", "details") if meaningful_text(context.get(key))), None)
    state = str(item.get("state", "")).lower().replace("-", "_").replace(" ", "_")
    explicit_future_record = (
        meaningful_text(context.get("future_observation_semantics"))
        and meaningful_text(cause)
        and meaningful_text(needed)
        and bool(consequence)
    )
    future_markers = (
        "future", "prospective", "unmeasured", "not measured", "not observed",
        "not supplied", "not confirmed", "not yet", "awaiting observation",
        "未测", "未观测", "未来", "待观测", "尚未", "前瞻",
    )
    has_pending_explanation = (
        meaningful_text(cause)
        and meaningful_text(needed)
        and bool(consequence)
        and any(marker in json.dumps(context, ensure_ascii=False).lower() for marker in future_markers)
        and (path.startswith("/uncertainties/") or path.startswith("/uncertainty/") or "/unknown_values/" in path)
    )
    leaf = path.rsplit("/", 1)[-1]
    future_null = (
        leaf.startswith("future_") or record.get("status") in {"unmeasured", "not_measured", "not_observed"}
    ) and state in {"unknown", "pending", "unmeasured", "not_measured", "not_observed"}
    if explicit_future_record or has_pending_explanation or future_null:
        item["advisory_kind"] = "future_observation_or_supplied_fact"
        item["actual_needed_measurement"] = needed or (
            "An actual later observation or supplied factual record is required; this null remains missing, not zero, measured, or validated."
        )
        return True
    gate_path = (
        re.match(r"^/selection/execution_prerequisites/[0-9]+/status$", path)
        or re.match(r"^/confirmations/[0-9]+/(?:status|current_status|confirmation_status)$", path)
    )
    concrete_gate = any(meaningful_text(record.get(key)) for key in ("item", "role", "required_confirmation", "confirmation_id"))
    uncertainty_explanations = any(
        path.startswith("/uncertainties/") or path.startswith("/uncertainty/")
        for path in advisory_scope.get("explanation_paths", [])
    )
    if gate_path and concrete_gate and uncertainty_explanations and state in {"unknown", "pending", "not_confirmed", "unverified"}:
        item["advisory_kind"] = "external_execution_gate_fact"
        item["actual_needed_measurement"] = needed or (
            "Obtain the actual item/role-specific confirmation and retain its evidence. This pending gate is neither approval nor permission to execute."
        )
        return True
    return False


def normalized_state_key(key):
    return key.lower().replace("-", "_") if isinstance(key, str) else ""


def is_structured_state_key(key):
    normalized = normalized_state_key(key)
    return normalized in {"status", "state", "feasibility", "evaluation_status", "decision_status", "review_status", "validation_status"} or normalized.endswith("_status")


def structured_state(container):
    states = {
        child.strip().lower().replace("-", "_").replace(" ", "_")
        for key, child in container.items()
        if is_structured_state_key(key) and isinstance(child, str)
    }
    return states.pop() if len(states) == 1 else None


def has_explicit_empty_semantics(container, key):
    if normalized_state_key(key) in {
        "adverse_events", "adverse_event_records", "conflicts", "conflict_records",
        "safety_events", "incidents", "accidents", "deviations", "protocol_deviations",
        "errors", "warnings",
    }:
        return True
    for suffix in ("_note", "_reason", "_empty_reason"):
        if meaningful_text(container.get(key + suffix)):
            return True
    reasons = container.get("empty_reasons")
    return isinstance(reasons, dict) and meaningful_text(reasons.get(key))


def placeholder_report(result, nullable_paths=None, explanation_paths=None):
    placeholders = []
    nullable_paths = set(nullable_paths or [])
    explanation_paths = set(explanation_paths or [])
    blocking_states = {"unknown", "pending", "unresolved", "unevaluated", "not_evaluated", "incomplete", "not_started"}
    observation_states = {"unmeasured", "not_measured", "not_observed"}
    null_states = blocking_states | observation_states | {"failed", "error"}
    surface_empty_fields = {"uncertainty", "limitations", "evidence", "claims", "evidence_refs", "evidence_ids"}

    def observation_state(container, key):
        if not isinstance(container, dict):
            return None
        states = {
            normalized_state_key(child_key): child
            for child_key, child in container.items()
            if is_structured_state_key(child_key) and isinstance(child, str)
        }
        state = states.get("status")
        if isinstance(key, str) and key.startswith("future_"):
            state = states.get("future_response_status", states.get("future_observation_status", state))
        normalized = state.strip().lower().replace("-", "_").replace(" ", "_") if isinstance(state, str) else None
        return normalized if normalized in null_states else structured_state(container)

    def nullable_observation_key(key):
        return isinstance(key, str) and (
            key == "value" or key.endswith("_value")
            or (key.startswith("future_") and not is_structured_state_key(key) and key != "future_observation_semantics")
        )

    def walk(value, path="", key=None, container=None):
        state = observation_state(container, key)
        if value is None:
            if nullable_observation_key(key) and state in null_states:
                if state in blocking_states | observation_states:
                    placeholders.append({"path": path or "/", "kind": "blocking_unknown_pending", "state": state, "context": future_observation_context(container)})
                return
            if path in nullable_paths:
                if not explanation_paths:
                    placeholders.append({"path": path or "/", "kind": "nullable_explanation_required"})
                return
            placeholders.append({"path": path or "/", "kind": "null"})
        elif isinstance(value, str):
            if not value.strip():
                placeholders.append({"path": path or "/", "kind": "empty_string"})
            elif is_structured_state_key(key):
                normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
                has_null_companion = isinstance(container, dict) and any(
                    nullable_observation_key(child_key) and container[child_key] is None
                    for child_key in container
                )
                if normalized in blocking_states and not has_null_companion:
                    placeholders.append({"path": path or "/", "kind": "blocking_unknown_pending", "state": normalized, "context": future_observation_context(container)})
        elif isinstance(value, list):
            if not value and normalized_state_key(key) not in surface_empty_fields:
                if not isinstance(container, dict) or not has_explicit_empty_semantics(container, key):
                    placeholders.append({"path": path or "/", "kind": "empty_array"})
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index), None, value)
        elif isinstance(value, dict):
            if not value:
                placeholders.append({"path": path or "/", "kind": "empty_object"})
            for child_key, child in value.items():
                walk(child, pointer_join(path, child_key), child_key, value)

    walk(result)
    return placeholders


def unknown_placeholder_report(result):
    found = []
    fake_na_prefix = re.compile(
        r"^\s*(?:n\s*/\s*a|n\.a\.?|not available|not applicable|tbd|to be determined)\s*(?:[:;,，；\-—]|$)",
        re.I,
    )
    fake_na_exact = {"n/a", "na", "n.a.", "n.a", "not available", "not applicable", "tbd", "to be determined"}
    state_sentinels = {"unknown", "pending", "未知", "不确定", "待补", "待确定", "暂无"}

    def walk(value, path="", key=None):
        if isinstance(value, str):
            literal = value.strip()
            normalized = re.sub(r"\s+", " ", literal.lower())
            sodium = literal == "Na"
            fake_na = not sodium and (normalized in fake_na_exact or fake_na_prefix.match(value))
            unknown_state = normalized in state_sentinels and not is_structured_state_key(key)
            if fake_na or unknown_state:
                found.append({"path": path or "/", "value": value[:160]})
        elif isinstance(value, dict):
            for child_key, child in value.items():
                walk(child, pointer_join(path, child_key), child_key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index), key)

    walk(result)
    return found


def present_text(value):
    return isinstance(value, str) and bool(value.strip()) and (value.strip() == "Na" or value.strip().lower() not in UNKNOWN_EXACT)


def meaningful_text(value):
    return present_text(value) and len(value.strip()) >= 3


def list_field(value):
    return isinstance(value, list)


def effective_surfaces(result, run_record):
    surfaces = {key: run_record.get(key) for key in LEDGER_KEYS}
    fallbacks = []
    if isinstance(result, dict):
        for key in LEDGER_KEYS:
            if key not in result:
                continue
            if key in {"evidence", "claims"} and result[key] == [] and isinstance(run_record.get(key), list) and run_record[key]:
                fallbacks.append(key)
                continue
            surfaces[key] = result[key]
    if fallbacks:
        surfaces["_nonempty_sidecar_fallbacks"] = fallbacks
    status = None
    if isinstance(result, dict) and "status" in result:
        status = result["status"]
    elif "raw_domain_status" in run_record and run_record["raw_domain_status"] is not None:
        status = run_record["raw_domain_status"]
    elif "status" in run_record:
        status = run_record["status"]
    return status, surfaces


def allowed_status_vocabulary(contract, kind, analysis):
    if kind != "json_schema":
        return analysis.get("allowed_statuses") if analysis else None
    values = set()

    def walk(schema, ref_stack=None):
        ref_stack = set() if ref_stack is None else ref_stack
        if not isinstance(schema, dict):
            return
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref not in ref_stack:
            target = resolve_local_ref(contract, ref)
            if target is not None:
                walk(target, ref_stack | {ref})
        if isinstance(schema.get("enum"), list) and all(isinstance(value, str) for value in schema["enum"]):
            values.update(schema["enum"])
        if isinstance(schema.get("const"), str):
            values.add(schema["const"])
        properties = schema.get("properties")
        if isinstance(properties, dict):
            if "status" in properties:
                walk(properties["status"], ref_stack)
            for child in properties.values():
                walk(child, ref_stack)
        for keyword in ("allOf", "anyOf", "oneOf", "if", "then", "else"):
            children = schema.get(keyword)
            if isinstance(children, list):
                for child in children:
                    walk(child, ref_stack)
            elif isinstance(children, dict):
                walk(children, ref_stack)

    walk(contract)
    return sorted(values) if values else None


def scientific_status_semantics(result, run_record, allowed_statuses):
    if isinstance(result, dict) and "status" in result:
        raw = result["status"]
    elif "raw_domain_status" in run_record and run_record["raw_domain_status"] is not None:
        raw = run_record["raw_domain_status"]
    else:
        raw = run_record.get("status", "partial")
    if not isinstance(raw, str):
        return {
            "raw_domain_status": raw,
            "processing_status": "partial",
            "processing_kind": "invalid",
            "valid": False,
            "allowed_statuses": allowed_statuses,
        }
    if raw in DOMAIN_FINAL_STATUSES:
        accepted = allowed_statuses is not None and raw in allowed_statuses
        return {
            "raw_domain_status": raw,
            "processing_status": "complete" if accepted else "partial",
            "processing_kind": "scientific_domain_final" if accepted else "invalid",
            "valid": accepted,
            "allowed_statuses": allowed_statuses,
        }
    accepted = raw in {"complete", "partial", "failed"} and (
        allowed_statuses is None or raw in allowed_statuses
    )
    return {
        "raw_domain_status": raw,
        "processing_status": raw if accepted else "partial",
        "processing_kind": "legacy_processing" if accepted else "invalid",
        "valid": accepted,
        "allowed_statuses": allowed_statuses,
    }


def scientific_explanation_paths(value, path=""):
    paths = []
    if isinstance(value, dict):
        cause = value.get("cause", value.get("reason", value.get("conditional_reason")))
        sequel = value.get("consequence", value.get("needed_confirmation", value.get("needed_evidence")))
        if meaningful_text(cause) and meaningful_text(sequel):
            paths.append(path + "/cause")
        state = value.get("status", value.get("value_or_status"))
        recheck = value.get("recheck")
        if isinstance(state, str) and state.strip().lower() in PENDING_WORDS:
            if isinstance(recheck, dict) and str(recheck.get("status", "")).lower() in FINISHED_RECHECK and meaningful_text(recheck.get("method")):
                paths.append(path + "/recheck")
            elif any(
                key != "status" and meaningful_text(child)
                for key, child in value.items()
                if key in {"cause", "details", "detail", "reason", "needed_confirmation", "needed_evidence", "conditions", "limitations"}
            ):
                paths.append(path + "/conditional_explanation")
        for key, child in value.items():
            child_path = pointer_join(path, key)
            paths.extend(scientific_explanation_paths(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(scientific_explanation_paths(child, path + "/" + str(index)))
    return paths


def has_blocking_complete_flag(value):
    if isinstance(value, dict):
        if value.get("blocking_complete") is True:
            return True
        return any(has_blocking_complete_flag(child) for child in value.values())
    if isinstance(value, list):
        return any(has_blocking_complete_flag(child) for child in value)
    return False


def no_feasible_evidence_report(case, result, run_record, optimization, surfaces):
    report = {
        "required": True,
        "supported": False,
        "basis": None,
        "case_id_matches": None,
        "message": "no matching exhaustive infeasibility proof or competent case-conflict evidence was supplied",
    }
    if isinstance(optimization, dict):
        candidates = optimization.get("candidates")
        if optimization.get("case_id") is not None and not same_json(optimization.get("case_id"), case["case_id"]):
            report["case_id_matches"] = False
            report["message"] = "optimization report case_id does not exactly match the scientific case_id and cannot prove this case infeasible"
            return report
        unresolved = optimization.get("unranked_candidate_ids", []) or []
        summary = optimization.get("decision_summary", {})
        if isinstance(summary, dict):
            unresolved.extend(summary.get("unresolved_candidate_ids", []) or [])
        search = optimization.get("search", {})
        if (
            optimization.get("status") == "infeasible"
            and isinstance(search, dict) and search.get("exhaustive") is True
            and isinstance(candidates, list) and candidates
            and all(candidate_feasibility(candidate).startswith("infeasible") for candidate in candidates)
            and not unresolved
        ):
            report.update({
                "supported": True,
                "basis": "matching_exhaustive_optimizer",
                "case_id_matches": True,
                "message": "matching case report is exhaustive and all real candidates are explicitly infeasible without unresolved candidates",
            })
            return report
        report["case_id_matches"] = True
        report["message"] = "the optimizer report is not an exhaustive, fully resolved, all-infeasible proof for this case"
        return report
    entries = result.get("infeasibility_evidence") if isinstance(result, dict) else None
    if entries is None:
        entries = run_record.get("infeasibility_evidence")
    source_ids = {
        source.get("source_id", source.get("id"))
        for source in surfaces.get("sources", [])
        if isinstance(source, dict)
    }
    if isinstance(entries, list) and entries:
        valid = True
        for evidence in entries:
            if not isinstance(evidence, dict):
                valid = False
                break
            recheck = evidence.get("recheck", {})
            if not meaningful_text(evidence.get("source_id")) or evidence["source_id"] not in source_ids:
                valid = False
            if not meaningful_text(evidence.get("locator")):
                valid = False
            if not isinstance(evidence.get("constraint_ids"), list) or not evidence["constraint_ids"] or not all(present_text(item) for item in evidence["constraint_ids"]):
                valid = False
            if not meaningful_text(evidence.get("details", evidence.get("reason"))):
                valid = False
            if not isinstance(recheck, dict) or not meaningful_text(recheck.get("method")) or str(recheck.get("status", "")).lower() not in FINISHED_RECHECK:
                valid = False
        if valid:
            report.update({
                "supported": True,
                "basis": "explicit_case_conflicts",
                "case_id_matches": True,
                "message": "explicit case constraint conflicts have source IDs, locators, constraint IDs, detailed causes and completed rechecks",
            })
            return report
    return report


def validate_ledgers(status, surfaces):
    issues = []

    def add(code, severity, message, path=None):
        item = {"code": code, "severity": severity, "message": message}
        if path:
            item["path"] = path
        issues.append(item)

    for key in ("sources", "uncertainty", "limitations", "change_records"):
        if key not in surfaces or not list_field(surfaces[key]):
            add("ledger_missing_or_not_list", "error", key + " must be an accessible JSON list", "/" + key)
    sources = surfaces.get("sources")
    if isinstance(sources, list):
        if status == "complete" and not sources:
            add("sources_empty", "error", "complete requires at least one source")
        ids = set()
        for index, source in enumerate(sources):
            path = "/sources/" + str(index)
            if not isinstance(source, dict):
                add("source_not_object", "error", "source entry must be an object", path)
                continue
            source_id = source.get("source_id", source.get("id"))
            if not present_text(source_id):
                add("source_id_missing", "error", "source requires source_id or id", path)
            elif source_id in ids:
                add("source_id_duplicate", "error", "source IDs must be unique", path)
            else:
                ids.add(source_id)
            locator = source.get("locator", source.get("source_locator"))
            if isinstance(source.get("source"), str) and not locator:
                locator = source["source"]
            if not present_text(locator):
                add("source_locator_missing", "error", "source requires a concrete locator", path)
    uncertainty = surfaces.get("uncertainty")
    if isinstance(uncertainty, list):
        for index, item in enumerate(uncertainty):
            if not isinstance(item, dict):
                add("uncertainty_not_object", "error", "uncertainty entry must be an object", "/uncertainty/" + str(index))
                continue
            if not meaningful_text(item.get("detail", item.get("description"))):
                add("uncertainty_detail_missing", "error", "uncertainty requires concrete detail", "/uncertainty/" + str(index))
            if status == "complete" and item.get("blocking_complete") is True:
                add("blocking_uncertainty", "error", "a declared uncertainty blocks complete", "/uncertainty/" + str(index))
        if status == "complete" and not uncertainty:
            note = surfaces.get("uncertainty_note")
            if not meaningful_text(note) or "none-known" not in note.lower():
                add("uncertainty_none_known_note_missing", "error", "empty uncertainty requires uncertainty_note containing none-known and a concrete explanation")
    limitations = surfaces.get("limitations")
    if isinstance(limitations, list):
        for index, item in enumerate(limitations):
            if isinstance(item, dict):
                text = item.get("detail", item.get("description"))
            else:
                text = item
            if not meaningful_text(text):
                add("limitation_not_concrete", "error", "each limitation requires concrete text", "/limitations/" + str(index))
        if status in {"partial", "failed"}:
            has_uncertainty = isinstance(uncertainty, list) and any(
                isinstance(item, dict) and item.get("status") != "none-known" for item in uncertainty
            )
            if not limitations and not has_uncertainty:
                add("partial_limitations_missing", "error", "partial/failed requires concrete limitations or uncertainty")
        if status == "complete" and not limitations:
            note = surfaces.get("limitations_note")
            if not meaningful_text(note) or "none-known" not in note.lower():
                add("limitations_none_known_note_missing", "error", "empty limitations requires limitations_note containing none-known and a concrete explanation")
    records = surfaces.get("change_records")
    if isinstance(records, list):
        if status == "complete" and not records:
            add("change_records_empty", "error", "complete requires change records")
        for index, record in enumerate(records):
            path = "/change_records/" + str(index)
            if not isinstance(record, dict):
                add("change_record_not_object", "error", "change record must be an object", path)
                continue
            for key in ("operation", "path", "reason"):
                if not present_text(record.get(key)):
                    add("change_record_field_missing", "error", "change record requires " + key, path)
            if status == "complete" and not present_text(record.get("timestamp_utc")):
                add("change_record_timestamp_missing", "error", "complete change records require timestamp_utc", path)
    return issues


def completed_review_record(record, sha256_value=None, hash_field="contract_sha256"):
    return (
        isinstance(record, dict)
        and record.get("status") == "completed"
        and present_text(record.get("reviewer"))
        and present_text(record.get("reviewed_at_utc"))
        and meaningful_text(record.get("notes"))
        and (sha256_value is None or record.get(hash_field) == sha256_value)
    )


def validate_input_manifest(run_record, status, case=None, case_path=None, work=None, output=None):
    issues = []
    manifest = run_record.get("input_manifest")

    def add(code, severity, message, path=None):
        issue = {"code": code, "severity": severity, "message": message}
        if path:
            issue["path"] = path
        issues.append(issue)

    if not isinstance(manifest, list):
        return [{"code": "input_manifest_invalid", "severity": "fatal", "message": "input_manifest must be a list"}]
    conditional = "error" if status == "complete" else "incomplete"
    if case is not None and case_path is not None:
        current_case_hash = sha256_bytes(read_bytes(case_path, "current case JSON"))
        if os.path.realpath(str(run_record.get("case_file", ""))) != os.path.realpath(str(case_path)):
            add("case_path_mismatch", "fatal", "run record case_file does not resolve to the audited current case realpath", str(case_path))
        if run_record.get("case_raw_sha256") != current_case_hash:
            add("case_raw_hash_mismatch", conditional, "current case bytes differ from the bootstrap raw case SHA256", str(case_path))
        discovery = inspect_case_paths(case, work or run_record.get("work", ""), output or run_record.get("output", ""), case_path)
        recorded_discovery = run_record.get("input_discovery")
        if not isinstance(recorded_discovery, dict) or recorded_discovery.get("references") != discovery["references"]:
            add("input_discovery_snapshot_mismatch", conditional, "current case path discovery differs from the original discovery snapshot; manifest editing cannot hide changed references")
        expected_realpaths = {os.path.realpath(item["path"]) for item in discovery["references"]}
        recorded_realpaths = {
            os.path.realpath(record["path"])
            for record in manifest
            if isinstance(record, dict) and record.get("role") == "case_reference" and present_text(record.get("path"))
        }
        for item in discovery["references"]:
            if os.path.realpath(item["path"]) not in recorded_realpaths and not os.path.isdir(item["path"]):
                add("manifest_missing_current_case_reference", conditional, "a current case file reference has no manifest entry", item["pointer"])
        for realpath in recorded_realpaths - expected_realpaths:
            if not any(os.path.isdir(item["path"]) and realpath.startswith(os.path.realpath(item["path"]) + os.sep) for item in discovery["references"]):
                add("manifest_reference_not_in_current_case", conditional, "manifest contains a case-reference path not justified by the current case or its declared directories", realpath)
        for item in discovery["references"]:
            if os.path.isdir(item["path"]):
                for root, dirs, files in os.walk(item["path"], followlinks=False):
                    dirs.sort()
                    files.sort()
                    for name in files:
                        child = os.path.realpath(os.path.join(root, name))
                        if child not in recorded_realpaths:
                            add("manifest_missing_directory_file", conditional, "a file inside a declared input directory is absent from the manifest", child)
        if not discovery["static_analysis_complete"] and not completed_review_record(
            run_record.get("input_manifest_review"), current_case_hash, "case_raw_sha256"
        ):
            add("input_manifest_manual_review_required", conditional, "some potential path labels cannot be determined statically; complete needs input_manifest_review with reviewer, UTC time, notes, and current case hash")
    for index, record in enumerate(manifest):
        if not isinstance(record, dict) or not present_text(record.get("path")):
            add("input_record_invalid", "fatal", "malformed input manifest entry", "/input_manifest/" + str(index))
            continue
        path = record["path"]
        found = os.path.isfile(path)
        severity = "error" if status == "complete" else "warning"
        if not found or not record.get("found") or not record.get("readable"):
            optional_task_probe = record.get("role") == "task_candidate"
            add(
                "optional_task_not_found" if optional_task_probe else "input_missing_or_unreadable",
                "warning" if optional_task_probe else severity,
                "optional task probe did not find a task; task_discovery retains the explicit missing state" if optional_task_probe else "registered input is missing or unreadable",
                path,
            )
            continue
        try:
            data = read_bytes(path, "registered input")
            if record.get("sha256") != sha256_bytes(data) or record.get("size_bytes") != len(data):
                add("input_changed_since_bootstrap", severity, "registered input bytes differ from the bootstrap snapshot", path)
        except GuardError:
            add("input_missing_or_unreadable", severity, "registered input cannot be read now", path)
        if record.get("review_required", True) is not False and record.get("content_reviewed") is not True:
            add("input_content_review_not_recorded", severity, "hashing/existence is not semantic content review; set content_reviewed only after actual inspection or record review_required=false with a reason", path)
    return issues


def evidence_report(surfaces, status):
    issues = []
    evidence_ids = set()
    evidence = surfaces.get("evidence")
    claims = surfaces.get("claims")
    source_ids = {
        source.get("source_id", source.get("id"))
        for source in surfaces.get("sources", [])
        if isinstance(source, dict) and present_text(source.get("source_id", source.get("id")))
    }
    pending_rechecks = 0

    def severity():
        return "error" if status == "complete" else "incomplete"

    if evidence is not None:
        if not isinstance(evidence, list):
            issues.append({"code": "evidence_not_list", "severity": "error", "message": "evidence must be a list when present"})
            evidence = []
        for index, item in enumerate(evidence):
            path = "/evidence/" + str(index)
            if not isinstance(item, dict):
                issues.append({"code": "evidence_not_object", "severity": "error", "message": "evidence entry must be an object", "path": path})
                continue
            evidence_id = item.get("evidence_id", item.get("id"))
            if not present_text(evidence_id):
                issues.append({"code": "evidence_id_missing", "severity": "error", "message": "evidence requires evidence_id or id", "path": path})
            elif evidence_id in evidence_ids:
                issues.append({"code": "evidence_id_duplicate", "severity": "error", "message": "evidence IDs must be unique", "path": path})
            else:
                evidence_ids.add(evidence_id)
            source = item.get("source")
            source_id = item.get("source_id")
            if isinstance(source, dict) and source_id is None:
                source_id = source.get("source_id")
            if source_id is None:
                issues.append({"code": "evidence_source_id_missing", "severity": severity(), "message": "evidence requires source_id resolved to the actual effective sources index", "path": path})
            elif not present_text(source_id) or source_id not in source_ids:
                issues.append({"code": "evidence_source_id_unresolved", "severity": "error", "message": "evidence.source_id must resolve to an actual source in the effective result/run_record sources index", "path": path, "source_id": source_id})
            locator = item.get("locator", item.get("source_locator"))
            if not locator and isinstance(source, dict):
                locator = source.get("locator", source.get("source_locator"))
            if not locator and isinstance(source, str):
                locator = source
            if not present_text(locator):
                issues.append({"code": "evidence_source_locator_missing", "severity": "error", "message": "evidence requires a source locator", "path": path})
            recheck = item.get("recheck")
            if not isinstance(recheck, dict) or not present_text(recheck.get("method")) or not present_text(recheck.get("status")):
                issues.append({"code": "evidence_recheck_insufficient", "severity": severity(), "message": "evidence recheck requires method and status; citations alone are insufficient", "path": path})
                pending_rechecks += 1
            elif str(recheck["status"]).lower() not in FINISHED_RECHECK:
                issues.append({"code": "evidence_recheck_pending", "severity": severity(), "message": "evidence recheck is not finished", "path": path})
                pending_rechecks += 1
    if claims is not None:
        if not isinstance(claims, list):
            issues.append({"code": "claims_not_list", "severity": "error", "message": "claims must be a list when present"})
            claims = []
        claim_ids = set()
        for index, item in enumerate(claims):
            path = "/claims/" + str(index)
            if not isinstance(item, dict):
                issues.append({"code": "claim_not_object", "severity": "error", "message": "claim entry must be an object", "path": path})
                continue
            claim_id = item.get("claim_id", item.get("id"))
            if not present_text(claim_id):
                issues.append({"code": "claim_id_missing", "severity": "error", "message": "claim requires claim_id or id", "path": path})
            elif claim_id in claim_ids:
                issues.append({"code": "claim_id_duplicate", "severity": "error", "message": "claim IDs must be unique", "path": path})
            else:
                claim_ids.add(claim_id)
            if not meaningful_text(item.get("statement", item.get("text"))):
                issues.append({"code": "claim_statement_missing", "severity": "error", "message": "claim requires statement or text", "path": path})
            refs = item.get("evidence_refs", item.get("evidence_ids"))
            if not isinstance(refs, list) or not refs or not all(present_text(ref) for ref in refs):
                issues.append({"code": "claim_evidence_refs_missing", "severity": severity(), "message": "claim requires nonempty evidence_refs/evidence_ids", "path": path})
            else:
                missing = [ref for ref in refs if ref not in evidence_ids]
                if missing:
                    issues.append({"code": "claim_evidence_ref_unresolved", "severity": "error", "message": "claim references missing evidence IDs", "path": path, "missing_ids": missing})
            recheck = item.get("recheck")
            if not isinstance(recheck, dict) or not present_text(recheck.get("method")) or not present_text(recheck.get("status")):
                issues.append({"code": "claim_recheck_insufficient", "severity": severity(), "message": "claim recheck requires method and status; scientific entailment remains manual", "path": path})
                pending_rechecks += 1
            elif str(recheck["status"]).lower() not in FINISHED_RECHECK:
                issues.append({"code": "claim_recheck_pending", "severity": severity(), "message": "claim recheck is not finished", "path": path})
                pending_rechecks += 1
    return {
        "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
        "claim_count": len(claims) if isinstance(claims, list) else 0,
        "pending_rechecks": pending_rechecks,
        "issues": issues,
        "scientific_correctness_inferred": False,
    }


def schema_validation(contract, result, status):
    report = {
        "applicable": True,
        "library": "jsonschema",
        "library_found": False,
        "executed": False,
        "valid": None,
        "errors": [],
        "issues": [],
        "reason_not_executed": None,
        "format_checker": True,
    }
    reference_issues = local_reference_issues(contract)
    if reference_issues:
        report["reason_not_executed"] = "unresolved, ambiguous, or unsupported local references require resolution before safe schema validation"
        for issue in reference_issues:
            report["issues"].append({"code": issue["code"], "severity": "error" if status == "complete" else "incomplete", "message": report["reason_not_executed"], "path": issue["path"], "ref": issue["ref"]})
        return report
    if has_external_refs(contract):
        report["reason_not_executed"] = "external $ref/$dynamicRef/$recursiveRef is forbidden because validation must not access network or external schemas"
        report["issues"].append({"code": "schema_external_ref_not_validated", "severity": "error" if status == "complete" else "incomplete", "message": report["reason_not_executed"]})
        return report
    runtime, reason = load_jsonschema_library(contract)
    if runtime is None:
        report["reason_not_executed"] = reason
        report["issues"].append({"code": "schema_validation_not_executed", "severity": "error" if status == "complete" else "warning", "message": reason})
        return report
    report["library_found"] = True
    try:
        module, validator_class = runtime
        validator = validator_class(contract, format_checker=module.FormatChecker())
        errors = sorted(validator.iter_errors(result), key=lambda error: list(error.absolute_path))
    except Exception as exc:
        report["reason_not_executed"] = type(exc).__name__ + ": " + str(exc)
        report["issues"].append({"code": "schema_validation_error", "severity": "error", "message": "jsonschema was found but schema/validation execution failed", "detail": report["reason_not_executed"][:500]})
        return report
    report["executed"] = True
    report["valid"] = not errors
    for error in errors[:100]:
        path = ""
        for token in error.absolute_path:
            path = pointer_join(path, token)
        actual, unused = resolve_pointer_occurrences(result, path) if path else ([("/", True, result)], [])
        is_placeholder = any(present and (value is None or value == "" or value == [] or value == {}) for unused_path, present, value in actual)
        transitional_status = path == "/status" and actual and actual[0][2] in {"partial", "failed"}
        severity = "incomplete" if (is_placeholder or transitional_status or error.validator == "required") and status != "complete" else "error"
        report["errors"].append({"path": path or "/", "validator": str(error.validator), "message": error.message[:1000], "severity": severity})
    if len(errors) > 100:
        report["errors_truncated"] = len(errors) - 100
    if errors:
        report["issues"].append({"code": "schema_validation_failed", "severity": "error" if status == "complete" else "incomplete", "message": "real jsonschema validation failed; see individual errors for their severity"})
    return report


def manual_review_report(run_record, snapshot, status, result=None):
    if snapshot["kind"] == "json_schema":
        return {"required": False, "status": "not_applicable", "issues": []}
    record = run_record.get("manual_review")
    reviewed_surface = "run_record"
    if isinstance(result, dict) and isinstance(result.get("manual_review"), dict) and result["manual_review"]:
        record = result["manual_review"]
        reviewed_surface = "result"
    completed = completed_review_record(record, snapshot["sha256"])
    issue = None
    if not completed:
        issue = {
            "code": "manual_review_required",
            "severity": "error" if status == "complete" else "warning",
            "message": "prose/declarative/unsupported contracts require manual_review.completed with reviewer, reviewed_at_utc, notes, and the original contract hash",
        }
    return {
        "required": True,
        "reviewed_surface": reviewed_surface,
        "status": "completed" if completed else "manual_review_required",
        "record_does_not_prove_scientific_review": True,
        "issues": [issue] if issue else [],
    }


def candidate_feasibility(candidate):
    if not isinstance(candidate, dict):
        return "unconfirmed"
    feasible_values = []
    confirmed = False
    string_state = None
    for key, value in candidate.items():
        normalized = key.lower().replace("-", "_")
        if normalized in {"feasible", "is_feasible", "constraint_satisfied", "constraints_satisfied"}:
            feasible_values.append(value)
        if normalized in {"confirmed_feasible", "feasibility_confirmed"} and value is True:
            confirmed = True
    feasibility = candidate.get("feasibility")
    if isinstance(feasibility, str):
        normalized = feasibility.strip().lower().replace("-", "_").replace(" ", "_")
        string_state = normalized if normalized in {"feasible", "infeasible", "unknown"} else "unconfirmed"
    elif isinstance(feasibility, dict):
        if feasibility.get("feasible") is not None:
            feasible_values.append(feasibility.get("feasible"))
        if feasibility.get("confirmed") is True or feasibility.get("confirmed_feasible") is True:
            confirmed = True
    if any(value is False for value in feasible_values):
        return "infeasible_confirmed" if confirmed else "infeasible"
    if string_state == "infeasible":
        return "infeasible"
    if string_state in {"unknown", "unconfirmed"}:
        return "unconfirmed"
    if string_state == "feasible":
        if confirmed and feasible_values and not all(value is True for value in feasible_values):
            return "unconfirmed"
        return "confirmed_feasible"
    if confirmed and feasible_values and not all(value is True for value in feasible_values):
        return "unconfirmed"
    if confirmed and (not feasible_values or all(value is True for value in feasible_values)):
        return "confirmed_feasible"
    return "unconfirmed"


def candidate_pending(candidate):
    if not isinstance(candidate, dict):
        return True
    for key in ("may_win", "could_be_optimal", "potential_winner", "winner_possible"):
        if candidate.get(key) is True:
            return True
    statuses = [
        candidate.get("status"),
        candidate.get("evaluation_status"),
        candidate.get("decision_status"),
    ]
    for status in statuses:
        if isinstance(status, str):
            normalized = status.strip().lower().replace("-", "_").replace(" ", "_")
            if normalized in PENDING_WORDS or normalized == "unranked":
                return True
    return False


def exact_claim_value(value):
    return value is True or (
        isinstance(value, str) and "exact" in value.lower() and "not" not in value.lower()
    )


def optimization_diagnostics(report, status, result=None):
    diagnostics = {
        "provided": True,
        "feasibility_checks": [],
        "exact_optimality_checks": [],
        "unresolved_candidates": [],
        "issues": [],
        "algorithm_executed": False,
        "scientific_optimality_verified": False,
        "nonconclusive_report_does_not_set_outer_result_status": True,
    }
    candidates_by_id = {}
    candidate_sources = []
    collisions = set()
    ranked_refs = []
    selected_refs = []
    unresolved_refs = []
    best_refs = []
    exact_flags = []
    exhaustive_flags = []
    truncated_flags = []
    conditional_severity = "error" if status == "complete" else "incomplete"

    def add_issue(code, severity, message, path=None, **extra):
        issue = {"code": code, "severity": severity, "message": message}
        if path:
            issue["path"] = path
        issue.update(extra)
        diagnostics["issues"].append(issue)

    def add_reference(target, path, value, role):
        target.append({"path": path, "value": value, "role": role})

    def add_id_list(target, child, path, role):
        if not isinstance(child, list):
            add_issue(
                "candidate_id_list_not_array",
                conditional_severity,
                role + " must be an array of candidate IDs",
                path,
            )
            return
        for index, candidate_id in enumerate(child):
            reference_path = path + "/" + str(index)
            add_reference(target, reference_path, candidate_id, role)
            if role == "best":
                add_reference(best_refs, reference_path, candidate_id, role)

    def register_candidate(candidate, path, identifier_hint=None, ranked=False):
        if not isinstance(candidate, dict):
            return
        candidate_id = candidate.get("candidate_id", candidate.get("id", identifier_hint))
        record = {
            "path": path,
            "candidate": candidate,
            "candidate_id": candidate_id if present_text(candidate_id) else None,
        }
        candidate_sources.append(record)
        if ranked:
            add_reference(ranked_refs, path, candidate, "ranked")
        if record["candidate_id"]:
            candidates_by_id.setdefault(record["candidate_id"], []).append(record)
            if any(
                not same_json(previous["candidate"], candidate)
                for previous in candidates_by_id[record["candidate_id"]][:-1]
            ) and record["candidate_id"] not in collisions:
                collisions.add(record["candidate_id"])
                add_issue(
                    "candidate_id_collision",
                    "error",
                    "the same candidate_id resolves to different candidate objects",
                    path,
                    candidate_id=record["candidate_id"],
                )

    selected_keys = {
        "selected_candidate",
        "selected",
        "selected_candidate_id",
        "selected_candidate_ids",
        "recommended_candidate",
        "recommended_candidate_id",
        "recommended_candidate_ids",
    }
    id_list_roles = {
        "ranked_candidate_ids": (ranked_refs, "ranked"),
        "pareto_candidate_ids": (selected_refs, "pareto"),
        "best_candidate_ids": (selected_refs, "best"),
        "unranked_candidate_ids": (unresolved_refs, "unranked"),
        "unresolved_candidate_ids": (unresolved_refs, "unresolved"),
    }

    def walk(value, path=""):
        if isinstance(value, dict):
            diagnostic_unresolved = value.get("code") == "unresolved_candidates"
            for key, child in value.items():
                child_path = pointer_join(path, key)
                normalized = key.lower().replace("-", "_")
                if normalized in {"candidates", "ranked", "ranked_candidates", "ranking"}:
                    role_ranked = normalized != "candidates"
                    if isinstance(child, list):
                        for index, candidate in enumerate(child):
                            entry_path = child_path + "/" + str(index)
                            if isinstance(candidate, dict):
                                register_candidate(candidate, entry_path, ranked=role_ranked)
                            elif role_ranked:
                                add_reference(ranked_refs, entry_path, candidate, "ranked")
                    elif isinstance(child, dict):
                        for identifier, candidate in child.items():
                            if isinstance(candidate, dict):
                                register_candidate(
                                    candidate,
                                    pointer_join(child_path, identifier),
                                    identifier_hint=identifier,
                                    ranked=role_ranked,
                                )
                    continue
                if normalized in id_list_roles:
                    target, role = id_list_roles[normalized]
                    add_id_list(target, child, child_path, role)
                    continue
                if normalized in selected_keys:
                    if isinstance(child, list):
                        for index, selected in enumerate(child):
                            add_reference(selected_refs, child_path + "/" + str(index), selected, normalized)
                    else:
                        add_reference(selected_refs, child_path, child, normalized)
                        if isinstance(child, dict):
                            register_candidate(child, child_path)
                    continue
                if normalized == "exhaustive":
                    exhaustive_flags.append((child_path, child))
                elif normalized == "truncated":
                    truncated_flags.append((child_path, child))
                if normalized in {
                    "exact_optimal",
                    "exact_optimality",
                    "is_exact_optimal",
                    "proven_optimal",
                    "globally_optimal",
                }:
                    exact_flags.append((child_path, child))
                elif normalized in {"optimality", "optimality_claim"} and isinstance(child, str):
                    exact_flags.append((child_path, child))
                if diagnostic_unresolved and normalized == "candidate_ids":
                    add_id_list(unresolved_refs, child, child_path, "diagnostic_unresolved")
                    continue
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index))

    walk(report)
    report_best_ids = {
        reference["value"] for reference in best_refs if present_text(reference["value"])
    }

    def selector_values(value):
        if isinstance(value, list):
            return value
        if value in (None, False, "") or value == []:
            return []
        return [value]

    result_claims = {
        "completed_optimization": False,
        "exact_optimal": False,
        "exact_candidate_selection": False,
        "evidence": [],
    }
    claim_evidence_seen = set()

    def mark_result_claim(kind, path, detail):
        key = (kind, path)
        if key not in claim_evidence_seen:
            claim_evidence_seen.add(key)
            result_claims["evidence"].append({"kind": kind, "path": path, "detail": detail})

    def mark_completed(path, exact=False, selection=False):
        result_claims["completed_optimization"] = True
        if exact:
            result_claims["exact_optimal"] = True
        if selection:
            result_claims["exact_candidate_selection"] = True
        mark_result_claim("exact_candidate_selection" if selection else "completed_optimization", path, "explicit result decision claim")

    def scan_result(value, path="", scoped=False):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = pointer_join(path, key)
                normalized = key.lower().replace("-", "_")
                child_scoped = scoped or normalized in {
                    "optimization",
                    "optimization_result",
                    "optimization_report",
                    "optimization_decision",
                }
                if normalized in {
                    "exact_optimal",
                    "exact_optimality",
                    "is_exact_optimal",
                    "proven_optimal",
                    "globally_optimal",
                    "optimality",
                    "optimality_claim",
                } and exact_claim_value(child):
                    mark_completed(child_path, exact=True)
                if normalized in {
                    "optimization_completed",
                    "completed_optimization",
                    "optimization_complete",
                    "deterministic_optimization_complete",
                } and child is True:
                    mark_completed(child_path)
                if normalized in {"optimization_status", "optimization_result_status"} or (
                    scoped and normalized in {"status", "completion_status"}
                ):
                    if isinstance(child, str):
                        decision_status = child.strip().lower().replace("-", "_")
                        if decision_status in {"optimal", "exact", "exact_optimal", "proven_optimal", "globally_optimal"}:
                            mark_completed(child_path, exact=True)
                        elif decision_status in {"complete", "completed", "deterministic_complete"}:
                            mark_completed(child_path)
                if scoped and normalized in {"complete", "completed", "deterministic_complete"} and child is True:
                    mark_completed(child_path)
                exact_selector = normalized in {
                    "best_candidate_id",
                    "best_candidate_ids",
                    "exact_candidate",
                    "exact_candidate_id",
                    "exact_candidate_ids",
                    "selected_exact_candidate",
                    "selected_exact_candidate_id",
                    "selected_best_candidate",
                    "selected_best_candidate_id",
                }
                generic_selector = normalized in selected_keys
                if exact_selector or generic_selector:
                    selected_values = selector_values(child)
                    if exact_selector and selected_values:
                        mark_completed(child_path, exact=True, selection=True)
                    for selected in selected_values:
                        selected_id = (
                            selected.get("candidate_id", selected.get("id"))
                            if isinstance(selected, dict)
                            else selected
                        )
                        if generic_selector and selected_id in report_best_ids:
                            mark_completed(child_path, exact=True, selection=True)
                        add_reference(selected_refs, child_path, selected, "result_" + normalized)
                scan_result(child, child_path, child_scoped)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                scan_result(child, path + "/" + str(index), scoped)

    if isinstance(result, dict):
        scan_result(result)

    def resolve_references(references, purpose):
        resolved = []
        for reference in references:
            path = reference["path"]
            value = reference["value"]
            if isinstance(value, dict):
                candidate_id = value.get("candidate_id", value.get("id"))
                key = candidate_id if present_text(candidate_id) else "path:" + path
                bucket = candidates_by_id.get(candidate_id, []) if present_text(candidate_id) else []
                if candidate_id in collisions:
                    continue
                canonical = bucket[0] if bucket else {
                    "path": path,
                    "candidate": value,
                    "candidate_id": candidate_id if present_text(candidate_id) else None,
                }
                resolved.append({**reference, "key": key, "canonical": canonical, "purpose": purpose})
                continue
            candidate_id = value
            if not present_text(candidate_id):
                if value not in (None, False):
                    add_issue(
                        purpose + "_candidate_unreadable",
                        conditional_severity,
                        "candidate reference must be a nonempty ID string or candidate object",
                        path,
                    )
                continue
            bucket = candidates_by_id.get(candidate_id, [])
            if candidate_id in collisions:
                add_issue(
                    "candidate_reference_ambiguous",
                    "error",
                    "candidate ID cannot be safely selected because the index contains colliding objects",
                    path,
                    candidate_id=candidate_id,
                )
                resolved.append(
                    {
                        **reference,
                        "key": candidate_id,
                        "canonical": None,
                        "purpose": purpose,
                    }
                )
            elif not bucket:
                add_issue(
                    purpose + "_candidate_unresolved",
                    conditional_severity,
                    "candidate ID does not resolve to a real candidate in the candidates index",
                    path,
                    candidate_id=candidate_id,
                )
                resolved.append({**reference, "key": candidate_id, "canonical": None, "purpose": purpose})
            else:
                resolved.append({**reference, "key": candidate_id, "canonical": bucket[0], "purpose": purpose})
        return resolved

    check_groups = {}

    def collect_checks(references, purpose, group_kind):
        for entry in resolve_references(references, purpose):
            canonical = entry["canonical"]
            if canonical is None:
                continue
            group = check_groups.setdefault(
                entry["key"],
                {
                    "candidate_id": canonical["candidate_id"],
                    "candidate_path": canonical["path"],
                    "candidate": canonical["candidate"],
                    "roles": set(),
                    "reference_paths": set(),
                    "ranked": False,
                    "selected": False,
                },
            )
            group["roles"].add(entry["role"])
            group["reference_paths"].add(entry["path"])
            group[group_kind] = True

    collect_checks(ranked_refs, "ranked", "ranked")
    collect_checks(selected_refs, "selected", "selected")
    for key, group in check_groups.items():
        state = candidate_feasibility(group["candidate"])
        diagnostics["feasibility_checks"].append(
            {
                "candidate_id": group["candidate_id"],
                "candidate_path": group["candidate_path"],
                "reference_paths": sorted(group["reference_paths"]),
                "roles": sorted(group["roles"]),
                "state": state,
            }
        )
        if group["ranked"] and state != "confirmed_feasible":
            add_issue(
                "ranked_candidate_feasibility_unconfirmed",
                "error" if state.startswith("infeasible") or status == "complete" else "incomplete",
                "every explicitly ranked candidate must be confirmed feasible; full unselected candidate tables are not selected recommendations",
                group["candidate_path"],
                candidate_id=group["candidate_id"],
            )
        if group["selected"] and state != "confirmed_feasible":
            add_issue(
                "selected_candidate_feasibility_unconfirmed",
                "error" if state.startswith("infeasible") or status == "complete" else "incomplete",
                "best, Pareto, selected, and recommended candidates must resolve to confirmed feasible candidates",
                group["candidate_path"],
                candidate_id=group["candidate_id"],
                roles=sorted(group["roles"]),
            )

    potential = {}

    def add_potential(key, candidate_id, candidate, path, reason):
        if candidate is not None and candidate_feasibility(candidate).startswith("infeasible"):
            return
        entry = potential.setdefault(
            key,
            {
                "candidate_id": candidate_id if present_text(candidate_id) else None,
                "paths": set(),
                "reasons": set(),
                "may_change_selection": True,
            },
        )
        if path:
            entry["paths"].add(path)
        entry["reasons"].add(reason)

    for source in candidate_sources:
        candidate = source["candidate"]
        state = candidate_feasibility(candidate)
        if state == "unconfirmed" or candidate_pending(candidate):
            add_potential(
                source["candidate_id"] or "path:" + source["path"],
                source["candidate_id"],
                candidate,
                source["path"],
                "unknown_feasibility" if state == "unconfirmed" else "pending_or_unranked",
            )
    for entry in resolve_references(unresolved_refs, "unresolved"):
        canonical = entry["canonical"]
        add_potential(
            entry["key"],
            entry["value"],
            canonical["candidate"] if canonical else None,
            canonical["path"] if canonical else entry["path"],
            "explicit_unresolved_id",
        )

    exhaustive_true = bool(exhaustive_flags) and all(value is True for unused_path, value in exhaustive_flags)
    exhaustive_incomplete = any(value is not True for unused_path, value in exhaustive_flags) or any(
        value is True for unused_path, value in truncated_flags
    )
    report_status = report.get("status") if isinstance(report, dict) else None
    report_partial = not isinstance(report, dict) or report.get("partial") is True or report_status == "partial"
    if exhaustive_incomplete:
        add_potential(
            "search:incomplete",
            None,
            None,
            exhaustive_flags[0][0] if exhaustive_flags else "/search",
            "non_exhaustive_or_truncated_search",
        )
    elif report_partial:
        add_potential("report:partial", None, None, "/status", "partial_report")

    true_exact_flags = [(path, value) for path, value in exact_flags if exact_claim_value(value)]
    false_exact_flags = [(path, value) for path, value in exact_flags if value is False]
    exact_true = bool(true_exact_flags)
    for key, entry in potential.items():
        public_entry = {
            "candidate_id": entry["candidate_id"],
            "paths": sorted(entry["paths"]),
            "reasons": sorted(entry["reasons"]),
            "may_change_selection": True,
        }
        diagnostics["unresolved_candidates"].append(public_entry)
        if exact_true:
            add_issue(
                "exact_optimal_with_unresolved_candidates",
                "error",
                "exact_optimal requires every unresolved or unevaluated candidate that could change selection to be excluded",
                public_entry["paths"][0] if public_entry["paths"] else "/candidates",
                candidate_id=entry["candidate_id"],
                reasons=public_entry["reasons"],
            )
        else:
            add_issue(
                "unknown_or_pending_candidate_may_win",
                "warning",
                "an unresolved candidate or unevaluated search space may change selection; this blocks exact claims but not an ordinary planning-complete result",
                public_entry["paths"][0] if public_entry["paths"] else "/candidates",
                candidate_id=entry["candidate_id"],
            )

    if true_exact_flags and false_exact_flags:
        add_issue("conflicting_exact_optimal_flags", "error", "exact_optimal is both claimed and denied in the same report")
    exact_check = {
        "claim_present": bool(exact_flags),
        "exact_optimal_claimed": exact_true,
        "paths": [path for path, unused in true_exact_flags],
        "search_exhaustive_true": exhaustive_true,
        "report_status": report_status,
        "best_candidate_ids_nonempty": bool(best_refs),
        "report_partial": report_partial,
        "unresolved_that_may_change_selection": len(potential),
    }
    diagnostics["exact_optimality_checks"].append(exact_check)
    if exact_true:
        if not exhaustive_true:
            add_issue(
                "exact_optimal_without_exhaustive_search",
                "error",
                "exact_optimal=true requires a consistent explicit search.exhaustive=true; no optimizer was executed by this audit",
                true_exact_flags[0][0],
            )
        if report_status != "optimal":
            add_issue(
                "exact_optimal_requires_optimal_status",
                "error",
                "exact_optimal=true requires report.status=optimal",
                true_exact_flags[0][0],
                report_status=report_status,
            )
        if not best_refs:
            add_issue(
                "exact_optimal_without_best_candidates",
                "error",
                "exact_optimal=true requires nonempty best_candidate_ids resolvable to real candidates",
                true_exact_flags[0][0],
            )
        if report_partial:
            add_issue(
                "exact_optimal_from_partial_report",
                "error",
                "a partial report cannot support an exact optimality claim",
                true_exact_flags[0][0],
            )
    elif best_refs:
        add_issue(
            "best_candidates_without_exact_optimal",
            conditional_severity,
            "best_candidate_ids denotes exact winners and is inconsistent with exact_optimal=false or a missing exact claim",
            best_refs[0]["path"],
        )
    if report_status == "optimal" and exact_flags and not exact_true:
        add_issue(
            "optimal_status_requires_exact_optimal",
            conditional_severity,
            "status=optimal conflicts with an explicit non-true exact_optimal flag",
            exact_flags[0][0],
        )

    nonconclusive = report_partial or report_status in {
        "unresolved",
        "infeasible",
        "partial",
        "invalid_request",
    }
    diagnostics["report_status"] = report_status
    diagnostics["report_partial"] = report_partial
    diagnostics["result_decision_claims"] = result_claims
    if nonconclusive:
        add_issue(
            "optimization_report_nonconclusive",
            "warning",
            "a nonconclusive optimization report does not by itself determine the outer result status; result-specific claims are checked separately",
            "/status",
            report_status=report_status,
        )
        if result_claims["completed_optimization"]:
            add_issue(
                "nonconclusive_report_with_completed_optimization_claim",
                conditional_severity,
                "result claims deterministic completed optimization but the report is unresolved, infeasible, partial, invalid_request, or explicitly partial",
                "/status",
                report_status=report_status,
            )
        if result_claims["exact_candidate_selection"]:
            add_issue(
                "nonconclusive_report_with_exact_candidate_selection",
                conditional_severity,
                "result selects an exact candidate despite a nonconclusive optimization report",
                "/best_candidate_ids",
                report_status=report_status,
            )
    if result_claims["exact_optimal"] and not exact_true:
        add_issue(
            "result_exact_claim_not_supported_by_report",
            conditional_severity,
            "result claims exact optimality without a valid-looking exact_optimal=true claim in the linked report; all exact requirements must also pass",
            "/decision_summary/exact_optimal",
        )
    return diagnostics


def issue_counts(issues):
    counts = {"fatal": 0, "error": 0, "incomplete": 0, "warning": 0, "manual": 0}
    for issue in issues:
        severity = issue.get("severity", "error")
        counts[severity] = counts.get(severity, 0) + 1
    return counts


def run_record_reference_report(result, result_path, output, reference_base=None):
    protected = os.path.abspath(os.path.join(str(output), RUN_RECORD_NAME))
    base = os.path.abspath(reference_base or os.path.dirname(os.path.abspath(str(result_path))))
    raw = result.get("run_record_ref") if isinstance(result, dict) else None
    supplied = isinstance(result, dict) and "run_record_ref" in result
    deferred = not supplied or raw is None or (isinstance(raw, str) and not raw.strip())
    invalid_code = None
    resolved = protected
    if supplied and not isinstance(raw, str) and raw is not None:
        invalid_code = "run_record_ref_invalid"
    elif isinstance(raw, str) and raw.strip():
        resolved = raw if os.path.isabs(raw) else os.path.join(base, raw)
        resolved = os.path.abspath(resolved)
    protected_real = os.path.realpath(protected)
    resolved_real = os.path.realpath(resolved)
    if invalid_code is None and resolved_real != protected_real:
        invalid_code = "run_record_ref_escape"
    return {
        "supplied": supplied,
        "deferred_to_protected": deferred,
        "raw": raw,
        "resolved_path": os.path.abspath(resolved),
        "resolved_realpath": resolved_real,
        "protected_path": protected,
        "protected_realpath": protected_real,
        "valid": invalid_code is None,
        "error_code": invalid_code,
        "reference_base": base,
    }


def version_change_contract_declaration(contract):
    specs = []
    if isinstance(contract, str):
        specs.append(contract)
    elif isinstance(contract, dict):
        if "version_changes" in contract:
            specs.append(contract["version_changes"])
        for container_key in ("top_level_fields", "fields", "field_requirements", "properties"):
            container = contract.get(container_key)
            if isinstance(container, dict) and "version_changes" in container:
                specs.append(container["version_changes"])
            elif isinstance(container, list):
                specs.extend(
                    item for item in container
                    if isinstance(item, dict) and item.get("path") == "/version_changes"
                )
    text = " ".join(
        json.dumps(spec, ensure_ascii=False, sort_keys=True) if not isinstance(spec, str) else spec
        for spec in specs
    )
    chinese_empty_record = "无改动" in text and "数组" in text and "记录" in text
    english_empty_record = bool(re.search(
        r"\b(?:empty|unchanged|no\s+(?:candidate\s+|source\s+|version\s+|proposed\s+)?changes?)\b.{0,120}"
        r"\b(?:empty|unchanged|no\s+(?:candidate\s+|source\s+|version\s+|proposed\s+)?changes?)\b",
        text,
        re.I,
    ))
    return {"allows_explicit_empty_unchanged": chinese_empty_record or english_empty_record, "declaration": text[:1200]}


def unchanged_version_proof(result, run_record, case, case_path, result_path):
    proof = {
        "applicable": False,
        "accepted": False,
        "unchanged_versions_documented": False,
        "reason": None,
        "does_not_prove_optimality_or_execution_approval": True,
    }
    if not isinstance(result, dict) or result.get("version_changes") != []:
        return proof
    declaration = version_change_contract_declaration(case.get("output_contract"))
    if not declaration["allows_explicit_empty_unchanged"]:
        return proof
    proof.update({"applicable": True, "contract_declaration": declaration})
    case_data = read_bytes(case_path, "current case for unchanged-version proof")
    case_hash = sha256_bytes(case_data)
    if run_record.get("case_raw_sha256") != case_hash:
        proof["reason"] = "current case bytes do not match run_record.case_raw_sha256"
        return proof
    input_manifest = run_record.get("input_manifest")
    case_records = [
        record for record in input_manifest if isinstance(record, dict) and record.get("role") == "case"
    ] if isinstance(input_manifest, list) else []
    if not any(
        os.path.realpath(str(record.get("path", ""))) == os.path.realpath(str(case_path))
        and record.get("sha256") == case_hash
        and record.get("size_bytes") == len(case_data)
        and record.get("found") is True
        and record.get("readable") is True
        for record in case_records
    ):
        proof["reason"] = "the current case bytes are not preserved by a matching case entry in input_manifest"
        return proof
    checkpoint = run_record.get("last_checkpoint")
    accepted_hash = checkpoint.get("candidate_sha256") if isinstance(checkpoint, dict) else None
    if not isinstance(accepted_hash, str) or not SHA256_RE.match(accepted_hash):
        proof["reason"] = "no prior accepted candidate byte hash is available"
        return proof
    current_hash = sha256_bytes(read_bytes(result_path, "current result for unchanged-version proof"))

    def file_hash(path):
        if not present_text(path):
            return None
        try:
            return sha256_bytes(read_bytes(path, "unchanged-version source artifact"))
        except GuardError:
            return None

    checkpoint_path = checkpoint.get("candidate_source_path")
    if checkpoint_path is not None and file_hash(checkpoint_path) != accepted_hash:
        proof["reason"] = "the prior accepted source candidate bytes are missing or differ from last_checkpoint.candidate_sha256"
        return proof
    if checkpoint_path is None and current_hash == accepted_hash:
        proof.update({
            "accepted": True,
            "unchanged_versions_documented": True,
            "basis": "current_result_matches_prior_accepted_candidate",
            "raw_accepted_candidate_sha256": accepted_hash,
            "case_raw_sha256": case_hash,
        })
        return proof
    if current_hash == accepted_hash:
        proof.update({
            "accepted": True,
            "unchanged_versions_documented": True,
            "basis": "accepted_candidate_and_source_bytes_match",
            "raw_accepted_candidate_sha256": accepted_hash,
            "case_raw_sha256": case_hash,
        })
        return proof
    migration = run_record.get("v4_semantic_migration")
    if not isinstance(migration, dict) or migration.get("kind") != "user_authorized_v3_to_v4_status_semantics_migration":
        proof["reason"] = "current result bytes differ from the prior accepted candidate without an explicit, evidence-bearing status-semantic migration"
        return proof
    source_result_path = migration.get("source_result_path")
    source_result_data = None
    if file_hash(source_result_path) == accepted_hash and file_hash(migration.get("source_candidate_path")) == accepted_hash:
        try:
            source_result_data = read_bytes(source_result_path, "pre-migration result")
        except GuardError:
            source_result_data = None
    if source_result_data is None or migration.get("source_result_sha256") != accepted_hash or migration.get("source_candidate_sha256") != accepted_hash:
        proof["reason"] = "the preserved pre-migration result and source candidate do not both match the prior accepted raw bytes"
        return proof
    if migration.get("source_case_sha256") != case_hash:
        proof["reason"] = "the pre-migration case hash is not the current, manifest-verified case hash"
        return proof
    if (
        migration.get("authorization_scope") != "isolated_r3_to_v4_semantic_migration_regression"
        or not meaningful_text(migration.get("authorized_by"))
        or migration.get("original_candidate_inputs_changed") is not False
        or migration.get("scientific_fields_changed") is not False
        or migration.get("authorized_changed_paths") != ["/status"]
    ):
        proof["reason"] = "the migration is not explicitly limited to /status while preserving original scientific fields and candidate inputs"
        return proof
    try:
        source_result = parse_json_bytes(source_result_data, source_result_path)
    except GuardError:
        proof["reason"] = "the pre-migration result is not strict JSON"
        return proof
    if not isinstance(source_result, dict) or not isinstance(result, dict):
        proof["reason"] = "a status-only semantic migration requires object results"
        return proof
    expected_result = copy.deepcopy(source_result)
    expected_result["status"] = result.get("status")
    if (
        source_result.get("status") != migration.get("status_from")
        or result.get("status") != migration.get("status_to")
        or not same_json(result, expected_result)
    ):
        proof["reason"] = "current result edits exceed the authorized /status semantic migration"
        return proof
    proof.update({
        "accepted": True,
        "unchanged_versions_documented": True,
        "basis": "verified_user_authorized_status_only_semantic_migration",
        "raw_accepted_candidate_sha256": accepted_hash,
        "case_raw_sha256": case_hash,
        "preserved_pre_migration_result_path": os.path.abspath(str(source_result_path)),
        "preserved_source_candidate_path": os.path.abspath(str(migration.get("source_candidate_path"))),
        "authorized_changed_paths": ["/status"],
        "status_from": source_result.get("status"),
        "status_to": result.get("status"),
        "scientific_fields_unchanged": True,
        "migration_is_execution_approval": False,
    })
    return proof


def unverified_execution_claim_paths(result):
    found = []
    boolean_keys = {
        "execution_approved", "approved_for_execution", "execution_authorized",
        "execution_permission_granted", "execution_approval_granted", "ready_for_execution",
        "safety_approved", "experiment_executed", "execution_completed",
    }
    string_keys = {"execution_approval", "execution_authorization", "execution_status", "experiment_execution"}
    completed_strings = {"approved", "granted", "authorized", "executed", "completed"}

    def walk(value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = normalized_state_key(key)
                child_path = pointer_join(path, key)
                if (normalized in boolean_keys and child is True) or (
                    normalized in string_keys and isinstance(child, str)
                    and child.strip().lower().replace("-", "_") in completed_strings
                ):
                    found.append(child_path)
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + "/" + str(index))

    walk(result)
    return found


def audit_documents(case, result, run_record, case_path, result_path, work, output, optimization=None, allow_status_transition=False, run_record_reference=None, result_raw_sha256=None):
    snapshot = run_record.get("output_contract_snapshot") if isinstance(run_record, dict) else None
    if not isinstance(snapshot, dict):
        raise GuardError("run_record_invalid", "run_record lacks output_contract_snapshot", 6)
    expected_snapshot = make_snapshot(case, case_path)
    contract = case["output_contract"]
    kind = expected_snapshot["kind"]
    declarations = expected_snapshot["declared_fields"]
    issues = []

    def add(code, severity, message, path=None, **extra):
        issue = {"code": code, "severity": severity, "message": message}
        if path:
            issue["path"] = path
        issue.update(extra)
        issues.append(issue)

    if not same_json(run_record.get("case_id"), case["case_id"]):
        add("case_id_mismatch_run_record", "fatal", "run_record case_id differs in value or original JSON type", "/case_id")
    if isinstance(result, dict) and "case_id" in result and not same_json(result["case_id"], case["case_id"]):
        add("case_id_mismatch_result", "fatal", "result case_id differs in value or original JSON type", "/case_id")
    if not isinstance(result, dict) and kind != "json_schema":
        add("nonobject_result_requires_manual_review", "error", "non-Schema result root is not a ledger-capable object")
    if snapshot.get("sha256") != expected_snapshot["sha256"] or not same_json(snapshot.get("value"), contract):
        add("output_contract_snapshot_mismatch", "fatal", "current case output_contract differs from the original raw snapshot or hash", "/output_contract")
    if snapshot.get("declared_fields") != declarations or run_record.get("all_declared_fields") != [item["path"] for item in declarations]:
        add("declared_field_snapshot_mismatch", "fatal", "stored all-declared-field coverage metadata was changed or is inconsistent with the original contract", "/all_declared_fields")
    reference = run_record_reference or run_record_reference_report(result, result_path, output)
    if not reference["valid"]:
        add(
            reference["error_code"],
            "fatal",
            "when run_record_ref is present and nonempty, it must resolve to the unique protected output/run_record.json realpath; missing, null, or empty refs use the protected fallback",
            "/run_record_ref",
            resolved_realpath=reference["resolved_realpath"],
            protected_realpath=reference["protected_realpath"],
        )
    declarative_analysis = expected_snapshot["detection"].get("declarative_analysis") or {}
    allowed_statuses = allowed_status_vocabulary(contract, kind, declarative_analysis)
    unused_raw_status, surfaces = effective_surfaces(result, run_record)
    status_policy = scientific_status_semantics(result, run_record, allowed_statuses)
    raw_domain_status = status_policy["raw_domain_status"]
    status = status_policy["processing_status"]
    if not status_policy["valid"]:
        add(
            "status_semantics_invalid",
            "fatal",
            "raw domain status must be an explicitly allowed legacy processing status or declared scientific final vocabulary; processing and scientific states are never silently rewritten",
            "/status",
            raw_domain_status=status_policy["raw_domain_status"],
            allowed_statuses=allowed_statuses,
        )
    rr_domain_status = run_record.get("raw_domain_status", run_record.get("status", "partial"))
    rr_policy = scientific_status_semantics(
        {"status": run_record["status"] if rr_domain_status is None else rr_domain_status},
        run_record,
        allowed_statuses,
    )
    if isinstance(result, dict) and "status" in result and rr_policy["valid"] and status != rr_policy["processing_status"] and not allow_status_transition:
        add(
            "audit_status_mismatch",
            "fatal",
            "result and run_record processing statuses differ; checkpoint is the controlled transition path while raw domain status may differ",
            "/status",
            result_processing_status=status,
            run_record_processing_status=rr_policy["processing_status"],
        )
    if isinstance(result, dict) and declarative_analysis.get("additional_top_level_fields_allowed") is False:
        allowed_names = set(declarative_analysis.get("top_level_names", []))
        for field_name in result:
            if field_name not in allowed_names:
                add("additional_top_level_field_forbidden", "error", "declarative contract is closed to its actual scientific top-level fields, not to sidecar-only bookkeeping", "/" + str(field_name))
    for warning in declarative_analysis.get("literal_warnings", []):
        add(
            "unsupported_declarative_declaration",
            "error" if status == "complete" else "incomplete",
            warning + "; no reliable field map is assumed from a literal assignment",
            "/output_contract",
        )
    if isinstance(result, dict) and "contract_sha256" in result and result["contract_sha256"] != expected_snapshot["sha256"]:
        add("result_contract_hash_mismatch", "fatal", "result contract_sha256 is not the original contract snapshot hash", "/contract_sha256")
    reference_issues = local_reference_issues(contract) if kind == "json_schema" else []
    coverage_metadata = {"status": "active", "choices": []}
    active_declarations = None
    if kind != "json_schema":
        active_declarations = declarations
    elif reference_issues:
        coverage_metadata = {"status": "manual_review_required", "reason": "unresolved or ambiguous local references", "references": reference_issues}
    else:
        try:
            active_declarations, coverage_metadata = active_schema_declarations(contract, result)
        except ActiveCoverageError as exc:
            active_declarations = None
            coverage_metadata = {"status": "manual_review_required", "reason": exc.message, "code": exc.code, "schema_path": exc.path}
    for reference_issue in reference_issues:
        add(
            reference_issue["code"],
            "error" if status == "complete" else "incomplete",
            "local schema reference cannot be resolved safely for declared-field coverage",
            reference_issue["path"],
            ref=reference_issue["ref"],
        )
    if active_declarations is None:
        add(
            coverage_metadata.get("code", "active_coverage_manual_review_required"),
            "error" if status == "complete" else "incomplete",
            coverage_metadata.get("reason", "active branch coverage requires manual review; mutually exclusive fields are not demanded as a flat union"),
            coverage_metadata.get("schema_path", "/output_contract"),
        )
        coverage = [
            {
                "path": declaration["path"],
                "covered": None,
                "coverage_status": "manual_review_required",
                "required_by_parent_schema": declaration["required_by_parent_schema"],
            }
            for declaration in declarations
        ]
    else:
        coverage = coverage_report(result, active_declarations)
        for entry in coverage:
            if not entry["covered"]:
                add("declared_field_missing", "error" if status == "complete" else "incomplete", "an active declared contract field path is absent; all declared fields remain preserved in the original snapshot", entry["path"], missing_paths=entry["missing_paths"])
    if isinstance(result, dict):
        active_case_id = active_declarations is not None and any(item["path"] == "/case_id" for item in active_declarations)
        root_has_alternatives = isinstance(contract, dict) and any(keyword in contract for keyword in ("anyOf", "oneOf", "if", "then", "else"))
        unrestricted_case_id = (
            kind == "json_schema"
            and active_declarations is not None
            and not root_has_alternatives
            and root_field_allowed(contract, "case_id")
        )
        non_schema_case_id_allowed = (
            declarative_analysis.get("additional_top_level_fields_allowed") is not False
            or "case_id" in declarative_analysis.get("top_level_names", [])
        )
        if ((kind != "json_schema" and non_schema_case_id_allowed) or active_case_id or unrestricted_case_id) and "case_id" not in result:
            add("case_id_missing_result", "error" if status == "complete" else "incomplete", "result must preserve case_id whenever its active result structure can accept it; an early partial remains pending", "/case_id")
    nullable_paths = nullable_paths_for_result(contract, kind, declarative_analysis)
    nullable_paths.discard("/run_record_ref")
    explanation_paths = scientific_explanation_paths(result)
    raw_domain_status = status_policy["raw_domain_status"]
    if raw_domain_status in {"undetermined", "conditional", "conditional_choice"} and not explanation_paths:
        add(
            "scientific_conditional_explanation_required",
            "error" if status == "complete" else "incomplete",
            "conditional/undetermined scientific final states require actual uncertainty, cause, confirmation, conditional explanation, or completed recheck paths",
            "/status",
        )
    if has_blocking_complete_flag(result):
        add(
            "blocking_complete_declared",
            "error" if status == "complete" else "incomplete",
            "an explicit blocking_complete=true declaration prevents a complete delivery regardless of scientific status wording",
            "/status",
        )
    advisory_scope = planning_advisory_scope(result, status_policy, explanation_paths)
    unchanged_versions = unchanged_version_proof(result, run_record, case, case_path, result_path)
    for claim_path in unverified_execution_claim_paths(result):
        add(
            "unverified_execution_or_approval_claim",
            "error" if status == "complete" else "incomplete",
            "a self-declared completed execution or execution/safety approval is not established by this planning audit; retain the actual independently authorized record or an explicit pending/not-granted state",
            claim_path,
        )
    placeholders = placeholder_report(result, nullable_paths, explanation_paths)
    unknowns = unknown_placeholder_report(result)
    linked_surfaces = {}
    sidecar_fallbacks = surfaces.get("_nonempty_sidecar_fallbacks", [])
    for key in LEDGER_KEYS:
        if key in run_record and (not isinstance(result, dict) or key not in result or key in sidecar_fallbacks):
            linked_surfaces[key] = run_record[key]
    if kind != "json_schema" and "manual_review" in run_record:
        linked_surfaces["manual_review"] = run_record["manual_review"]
    for key, value in linked_surfaces.items():
        for item in placeholder_report({key: value}):
            item["path"] = "run_record:" + item["path"]
            placeholders.append(item)
        for item in unknown_placeholder_report({key: value}):
            item["path"] = "run_record:" + item["path"]
            unknowns.append(item)
    domain_pending_groups = {}
    for item in placeholders:
        if item["kind"] == "blocking_unknown_pending":
            if is_future_observation_item(item, result, advisory_scope):
                domain_pending_groups.setdefault((item.get("state", "unknown"), item["advisory_kind"]), []).append(item)
            else:
                add("blocking_unknown_pending", "error" if status == "complete" else "incomplete", "a structured unknown/pending state and its nullable value are explicit, but without a contract-allowed, evidence-backed planning scope and a clearly future/gated fact they block formal complete rather than being mislabeled as known or N/A", item["path"], state=item.get("state"))
        elif item["kind"] == "nullable_explanation_required":
            add("nullable_explanation_required", "error" if status == "complete" else "incomplete", "an allowed nullable selection still requires an actual cause/consequence/needed-confirmation path; a bare preference-null or feasibility claim is not approved", item["path"])
        elif item["path"] == "/version_changes" and unchanged_versions.get("accepted"):
            add(
                "unchanged_versions_documented",
                "warning",
                "an explicitly permitted empty version_changes array is supported by current case, manifest and prior accepted-candidate byte hashes; any status-only migration is separately verified and no scientific fields or execution approval were changed",
                item["path"],
                total_count=1,
                affected_paths=[item["path"]],
                result_facts_retained=True,
                raw_accepted_candidate_sha256=unchanged_versions["raw_accepted_candidate_sha256"],
                case_raw_sha256=unchanged_versions["case_raw_sha256"],
                proof_basis=unchanged_versions["basis"],
            )
        else:
            add("empty_or_null_placeholder", "error" if status == "complete" else "incomplete", "null/empty bootstrap-style value must be replaced or explicitly justified outside complete; an empty version_changes array additionally requires a contract permission and exact unchanged-source hash evidence", item["path"], placeholder_kind=item["kind"])
    for (state, advisory_kind), entries in domain_pending_groups.items():
        affected_paths = [entry["path"] for entry in entries]
        needed_actions = sorted({entry["actual_needed_measurement"] for entry in entries})
        add(
            "domain_uncertainty_grouped",
            "warning",
            "%d structured future-observation, supplied-fact, or external-gate facts remain documented at conditional planning delivery. They are not measured outcomes, satisfied prerequisites, exact winners, or execution approvals." % len(entries),
            affected_paths[0] if affected_paths else "/status",
            state=state,
            advisory_kind=advisory_kind,
            total_count=len(entries),
            affected_paths=affected_paths,
            result_facts_retained=True,
            actual_needed_measurement=needed_actions,
            execution_not_authorized=True,
            planning_scope=advisory_scope["scope"],
        )
    for item in unknowns:
        add("na_unknown_placeholder", "fatal", "a bare N/A/TBD/unknown sentinel cannot impersonate an obtained value; use an explicit structured state instead", item["path"])
    for key in sidecar_fallbacks:
        add("nonempty_sidecar_fallback", "warning", "an empty result " + key + " list did not hide the nonempty run_record list; the sidecar list was audited", "/" + key)
    issues.extend(validate_ledgers(status, surfaces))
    issues.extend(validate_input_manifest(run_record, status, case, case_path, work, output))
    if kind == "json_schema":
        schema = schema_validation(contract, result, status)
    else:
        schema = {
            "applicable": False,
            "library": "jsonschema",
            "library_found": False,
            "executed": False,
            "valid": None,
            "errors": [],
            "issues": [],
            "reason_not_executed": "the original output_contract was not recognized as JSON Schema; no schema-valid claim is made",
        }
    issues.extend(schema["issues"])
    if isinstance(schema, dict):
        for error in schema.get("errors", []):
            if error.get("severity") in {"fatal", "error"}:
                issues.append({"code": "schema_type_or_value_error", "severity": error["severity"], "message": error["message"], "path": error["path"], "validator": error.get("validator")})
    manual = manual_review_report(run_record, expected_snapshot, status, result)
    issues.extend(manual["issues"])
    evidence = evidence_report(surfaces, status)
    issues.extend(evidence["issues"])
    if optimization is None:
        optimization_report = {"provided": False, "status": "not_run", "algorithm_executed": False, "issues": []}
    else:
        optimization_report = optimization_diagnostics(optimization, status, result)
        issues.extend(optimization_report["issues"])
    no_feasible_validation = {"required": False, "supported": None}
    if raw_domain_status in {"no_feasible_candidate", "no_feasible_plan", "infeasible"}:
        no_feasible_validation = no_feasible_evidence_report(case, result, run_record, optimization, surfaces)
        if not no_feasible_validation["supported"]:
            add(
                "no_feasible_evidence_required",
                "error" if status == "complete" else "incomplete",
                no_feasible_validation["message"] + "; selection=null alone or a hard-approval claim never proves no feasible candidate",
                "/status",
            )
    counts = issue_counts(issues)
    hard_failure = counts["fatal"] + counts["error"]
    incomplete = counts["incomplete"] + counts["manual"]
    audit_passed = hard_failure == 0 and incomplete == 0
    if status == "complete":
        checkpoint_eligible = audit_passed
    else:
        checkpoint_eligible = counts["fatal"] == 0 and counts["error"] == 0
    levels = {
        "structural": {
            "status": "pass" if audit_passed else "fail",
            "covers": "strict JSON, identity, original contract coverage, status, finite numbers, placeholder policy, ledger mechanics, and optional real jsonschema execution",
        },
        "arithmetic": {
            "status": "not_run",
            "unconditionally_verified": False,
            "detail": "Finite-number parsing is not arithmetic recomputation; no domain equation, objective, optimum, or search proof was executed.",
        },
        "source_anchor": {
            "status": "locators_and_rechecks_checked" if not evidence["issues"] else "limited",
            "unconditionally_verified": False,
            "detail": "Source IDs, locators, references, and recorded rechecks are mechanically inspected; no citation entailment or scientific support is inferred.",
        },
        "scientific_review": {
            "status": "not_unconditionally_verified",
            "unconditionally_verified": False,
            "detail": "Passing this audit is not mathematical or scientific validation; qualified review remains necessary.",
        },
    }
    pending = [
        {"code": issue["code"], "severity": issue["severity"], "path": issue.get("path"), "message": issue["message"]}
        for issue in issues
        if issue["severity"] != "warning"
    ]
    pending.extend(
        [
            {"code": "arithmetic_not_recomputed", "severity": "scientific_review", "message": levels["arithmetic"]["detail"]},
            {"code": "scientific_review_not_unconditional", "severity": "scientific_review", "message": levels["scientific_review"]["detail"]},
        ]
    )
    report = {
        "report_version": "io-guard-validation/v1",
        "tool": {"name": "io_guard.py", "version": VERSION},
        "audited_at_utc": utc_now(),
        "case_id": copy.deepcopy(case["case_id"]),
        "case_raw_sha256": sha256_bytes(read_bytes(case_path, "case file")),
        "case_path": os.path.abspath(str(case_path)),
        "result_path": os.path.abspath(str(result_path)),
        **result_hash_fields(result, result_path, result_raw_sha256),
        "run_record_path": None,
        "contract": {
            "kind": kind,
            "sha256": expected_snapshot["sha256"],
            "all_declared_fields": [item["path"] for item in declarations],
            "required_fields_are_not_sufficient": True,
        },
        "effective_status": raw_domain_status,
        "processing_status": status,
        "raw_domain_status": raw_domain_status,
        "domain_status_semantics": status_policy,
        "scientific_explanation_paths": explanation_paths,
        "planning_advisory_scope": advisory_scope,
        "unchanged_versions": unchanged_versions,
        "no_feasible_evidence": no_feasible_validation,
        "status_transition_allowed_for_this_audit": allow_status_transition,
        "audit_passed": audit_passed,
        "checkpoint_eligible": checkpoint_eligible,
        "scientific_or_mathematical_validity_claimed": False,
        "issue_counts": counts,
        "issues": issues,
        "coverage": coverage,
        "active_coverage": coverage_metadata,
        "run_record_reference": reference,
        "placeholders": placeholders,
        "unknown_placeholders": unknowns,
        "ledger_surfaces": {
            "location": "result values override run_record values; missing keys and empty evidence/claims with nonempty sidecars use run_record fallback",
            "keys_present_in_result": [key for key in LEDGER_KEYS if isinstance(result, dict) and key in result],
            "keys_present_in_run_record": [key for key in LEDGER_KEYS if key in run_record],
            "nonempty_sidecar_fallbacks": surfaces.get("_nonempty_sidecar_fallbacks", []),
        },
        "schema_validation": schema,
        "manual_review": manual,
        "source_evidence_ledger": evidence,
        "input_manifest": run_record.get("input_manifest"),
        "optimization_diagnostics": optimization_report,
        "review_levels": levels,
        "pending_validations": pending,
    }
    return report


def run_audit(case_path, result_path, work, output, optimization_path=None, run_record_override=None, allow_status_transition=False, reference_base=None):
    case, unused_case_bytes = load_json(case_path, "case JSON")
    require_case(case)
    result, result_data = load_json(result_path, "result JSON")
    reference = run_record_reference_report(result, result_path, output, reference_base)
    preliminary_path = reference["protected_path"]
    if run_record_override is not None:
        override_path = os.path.abspath(str(run_record_override))
        if os.path.realpath(override_path) != reference["protected_realpath"]:
            reference["valid"] = False
            reference["error_code"] = "run_record_override_escape"
        else:
            preliminary_path = override_path
    run_record, unused_rr_bytes = load_json(preliminary_path, "run record JSON")
    if not isinstance(run_record, dict):
        raise GuardError("run_record_invalid", "run record root must be an object", 6)
    optimization = None
    if optimization_path:
        optimization, unused_opt_bytes = load_json(optimization_path, "optimization report JSON")
    report = audit_documents(
        case,
        result,
        run_record,
        case_path,
        result_path,
        work,
        output,
        optimization,
        allow_status_transition,
        reference,
        result_raw_sha256=sha256_bytes(result_data),
    )
    report["run_record_path"] = preliminary_path
    return report, run_record


def existing_bootstrap_reusable(output, case, snapshot):
    paths = [os.path.join(output, name) for name in (RUN_RECORD_NAME, RESULT_NAME, VALIDATION_NAME)]
    existing = [os.path.lexists(path) for path in paths]
    if not any(existing):
        return False
    if not all(existing):
        raise GuardError(
            "partial_bootstrap_state",
            "some protected bootstrap files exist but others are missing; refusing to overwrite a possible last-known-good state",
            6,
            {"paths": paths},
        )
    try:
        run_record, unused = load_json(paths[0], "existing run record")
        load_json(paths[1], "existing result")
        load_json(paths[2], "existing validation")
    except GuardError as exc:
        raise GuardError(
            "existing_state_unreadable",
            "existing bootstrap state cannot be safely replaced: " + exc.message,
            6,
            {"paths": paths},
        ) from exc
    if not isinstance(run_record, dict) or not same_json(run_record.get("case_id"), case["case_id"]):
        raise GuardError("existing_case_mismatch", "refusing to overwrite existing state for a different or untyped case_id", 6)
    old_snapshot = run_record.get("output_contract_snapshot", {})
    if old_snapshot.get("sha256") != snapshot["sha256"] or not same_json(old_snapshot.get("value"), case["output_contract"]):
        raise GuardError("existing_contract_mismatch", "refusing to overwrite last-known-good state with a changed output_contract", 6)
    return True


def command_bootstrap(args):
    case_path = os.path.abspath(args.case)
    work = os.path.abspath(args.work)
    output = os.path.abspath(args.output)
    case, case_data = load_json(case_path, "case JSON")
    require_case(case)
    snapshot = make_snapshot(case, case_path)
    try:
        Path(work).mkdir(parents=True, exist_ok=True)
        Path(output).mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise GuardError("permission_denied", "permission denied creating work/output", 4) from exc
    if existing_bootstrap_reusable(output, case, snapshot):
        emit(
            {
                "ok": True,
                "command": "bootstrap",
                "status": "reused",
                "case_id": case["case_id"],
                "reused": True,
                "reason": "existing parseable same-case/same-contract files were preserved",
                "manual_review_next": MANUAL_REVIEW_NEXT if snapshot["kind"] != "json_schema" else None,
                "raw_domain_status_vocabulary": snapshot["detection"].get("declarative_analysis", {}).get("allowed_statuses"),
                "result": os.path.join(output, RESULT_NAME),
                "run_record": os.path.join(output, RUN_RECORD_NAME),
                "validation": os.path.join(output, VALIDATION_NAME),
            }
        )
        return 0
    manifest, task_discovery, input_discovery = build_input_manifest(case, case_path, args.task, work, output)
    result, ledgers = make_initial_result(case, snapshot, case_path)
    run_record = make_run_record(case, case_path, snapshot, manifest, task_discovery, ledgers, work, output, input_discovery)
    run_record["raw_domain_status"] = copy.deepcopy(result.get("status")) if isinstance(result, dict) else None
    run_record["case_raw_sha256"] = sha256_bytes(case_data)
    run_path = os.path.join(output, RUN_RECORD_NAME)
    result_path = os.path.join(output, RESULT_NAME)
    validation_path = os.path.join(output, VALIDATION_NAME)
    atomic_write_json(run_path, run_record)
    atomic_write_json(result_path, result)
    report, unused = run_audit(case_path, result_path, work, output, None, run_path)
    atomic_write_json(validation_path, report)
    emit(
        {
            "ok": True,
            "command": "bootstrap",
            "status": "partial",
            "case_id": case["case_id"],
            "reused": False,
            "contract": {
                "kind": snapshot["kind"],
                "sha256": snapshot["sha256"],
                "declared_field_count": len(snapshot["declared_fields"]),
            },
            "inputs_registered": len(manifest),
            "manual_review_next": MANUAL_REVIEW_NEXT if snapshot["kind"] != "json_schema" else None,
            "raw_domain_status_vocabulary": allowed_status_vocabulary(
                case["output_contract"], snapshot["kind"], snapshot["detection"].get("declarative_analysis")
            ),
            "existence_or_hashing_means_content_reviewed": False,
            "task_discovery": task_discovery["status"],
            "result": result_path,
            "run_record": run_path,
            "validation": validation_path,
            "audit_passed": report["audit_passed"],
            "checkpoint_eligible": report["checkpoint_eligible"],
        }
    )
    return 0


def command_audit(args):
    report, unused = run_audit(
        os.path.abspath(args.case),
        os.path.abspath(args.result),
        os.path.abspath(args.work),
        os.path.abspath(args.output),
        os.path.abspath(args.optimization_report) if args.optimization_report else None,
    )
    validation_path = os.path.join(os.path.abspath(args.output), VALIDATION_NAME)
    atomic_write_json(validation_path, report)
    emit(
        {
            "ok": report["audit_passed"],
            "command": "audit",
            "status": report["effective_status"],
            "processing_status": report["processing_status"],
            "audit_passed": report["audit_passed"],
            "checkpoint_eligible": report["checkpoint_eligible"],
            "issue_counts": report["issue_counts"],
            "schema_validation": {
                "executed": report["schema_validation"].get("executed"),
                "valid": report["schema_validation"].get("valid"),
                "reason_not_executed": report["schema_validation"].get("reason_not_executed"),
            },
            "validation": validation_path,
            "scientific_or_mathematical_validity_claimed": False,
        }
    )
    if not report["audit_passed"]:
        diagnostic("audit did not pass; inspect validation.json")
        return 5
    return 0


def previous_complete_result(output):
    result_path = os.path.join(output, RESULT_NAME)
    validation_path = os.path.join(output, VALIDATION_NAME)
    run_record_path = os.path.join(output, RUN_RECORD_NAME)
    if not all(os.path.isfile(path) for path in (result_path, validation_path, run_record_path)):
        return False
    try:
        result, result_data = load_json(result_path, "existing result")
        validation, unused = load_json(validation_path, "existing validation")
        run_record, unused = load_json(run_record_path, "existing run record")
        case, unused_case_data = load_json(run_record["case_file"], "last-good case for status semantics")
    except (GuardError, KeyError):
        return False
    kind = classify_contract(case["output_contract"])
    analysis = analyze_declarative_contract(case["output_contract"]) if kind != "json_schema" else None
    policy = scientific_status_semantics(
        result,
        run_record,
        allowed_status_vocabulary(case["output_contract"], kind, analysis),
    )
    checkpoint = run_record.get("last_checkpoint", {})
    accepted_processing = checkpoint.get("accepted_processing_status") if isinstance(checkpoint, dict) else None
    if accepted_processing is None and isinstance(checkpoint, dict) and checkpoint.get("accepted_status") == "complete":
        accepted_processing = "complete"
    checkpoint_guarantee = (
        accepted_processing == "complete"
        and checkpoint.get("candidate_sha256") == sha256_bytes(result_data)
    )
    return policy["processing_status"] == "complete" and (
        validation.get("audit_passed") is True or checkpoint_guarantee
    )


def commit_staged_files(entries):
    prepared = []
    backups = []
    replaced = []
    rollback_errors = []
    try:
        for source, target in entries:
            ensure_parent(target)
            descriptor, staged_copy = tempfile.mkstemp(
                prefix="." + os.path.basename(target) + ".commit-", dir=os.path.dirname(target)
            )
            try:
                with open(source, "rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
                    while True:
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        target_handle.write(chunk)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    os.unlink(staged_copy)
                except OSError:
                    pass
                raise
            prepared.append((staged_copy, target))
        for unused_source, target in entries:
            if os.path.exists(target):
                descriptor, backup = tempfile.mkstemp(
                    prefix="." + os.path.basename(target) + ".backup-", dir=os.path.dirname(target)
                )
                os.close(descriptor)
                shutil.copyfile(target, backup)
                backups.append((target, backup))
            else:
                backups.append((target, None))
        for staged_copy, target in prepared:
            os.replace(staged_copy, target)
            replaced.append(target)
    except (PermissionError, OSError) as exc:
        backup_map = dict(backups)
        for target in reversed(replaced):
            backup = backup_map.get(target)
            try:
                if backup is None:
                    os.unlink(target)
                else:
                    restoration = target + ".rollback-" + str(os.getpid())
                    shutil.copyfile(backup, restoration)
                    os.replace(restoration, target)
            except OSError as rollback_exc:
                rollback_errors.append({"path": target, "error": str(rollback_exc)})
        code = "permission_denied" if isinstance(exc, PermissionError) else "checkpoint_commit_failed"
        exit_code = 4 if isinstance(exc, PermissionError) else 3
        if rollback_errors:
            code = "checkpoint_rollback_failed"
            exit_code = 70
        raise GuardError(
            code,
            "checkpoint commit failed; rollback " + ("also failed" if rollback_errors else "restored replaced files") + ": " + str(exc),
            exit_code,
            {"rollback_errors": rollback_errors},
        ) from exc
    finally:
        for staged_copy, unused_target in prepared:
            if os.path.exists(staged_copy):
                try:
                    os.unlink(staged_copy)
                except OSError:
                    pass
        for unused_target, backup in backups:
            if backup and os.path.exists(backup):
                try:
                    os.unlink(backup)
                except OSError:
                    pass


def command_checkpoint(args):
    case_path = os.path.abspath(args.case)
    work = os.path.abspath(args.work)
    output = os.path.abspath(args.output)
    candidate_path = os.path.abspath(args.candidate)
    candidate_data = read_bytes(candidate_path, "checkpoint candidate")
    candidate = parse_json_bytes(candidate_data, candidate_path)
    run_record_path = os.path.join(output, RUN_RECORD_NAME)
    try:
        Path(work).mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix=".io-guard-checkpoint-", dir=work)
    except PermissionError as exc:
        raise GuardError("permission_denied", "permission denied creating checkpoint staging area", 4) from exc
    with temporary:
        staged_result = os.path.join(temporary.name, RESULT_NAME)
        atomic_write_bytes(staged_result, candidate_data)
        provisional, run_record = run_audit(
            case_path,
            staged_result,
            work,
            output,
            os.path.abspath(args.optimization_report) if args.optimization_report else None,
            run_record_path,
            True,
            output,
        )
        if previous_complete_result(output) and provisional["processing_status"] != "complete":
            emit(
                {
                    "ok": False,
                    "command": "checkpoint",
                    "status": "rejected",
                    "reason": "last_good_downgrade_refused",
                    "message": "an audit-passed complete result cannot be replaced by partial/failed",
                    "result_preserved": os.path.join(output, RESULT_NAME),
                    "validation_preserved": os.path.join(output, VALIDATION_NAME),
                }
            )
            diagnostic("checkpoint downgrade rejected; last-known-good complete was preserved")
            return 6
        if not provisional["checkpoint_eligible"]:
            emit(
                {
                    "ok": False,
                    "command": "checkpoint",
                    "status": "rejected",
                    "candidate": candidate_path,
                    "candidate_sha256": sha256_bytes(candidate_data),
                    "result_preserved": os.path.join(output, RESULT_NAME),
                    "validation_preserved": os.path.join(output, VALIDATION_NAME),
                    "issue_counts": provisional["issue_counts"],
                    "issues": provisional["issues"],
                }
            )
            diagnostic("checkpoint rejected; current result.json and validation.json were preserved")
            return 5
        new_run_record = copy.deepcopy(run_record)
        new_run_record["status"] = provisional["processing_status"]
        new_run_record["processing_status"] = provisional["processing_status"]
        new_run_record["raw_domain_status"] = copy.deepcopy(provisional["raw_domain_status"])
        new_run_record["updated_at_utc"] = utc_now()
        new_run_record["finalization"] = {
            "state": "open",
            "finalized_at_utc": None,
            "pending_validations": provisional["pending_validations"],
        }
        new_run_record["last_checkpoint"] = {
            "timestamp_utc": utc_now(),
            "candidate_sha256": sha256_bytes(candidate_data),
            "candidate_source_path": candidate_path,
            "accepted_status": provisional["effective_status"],
            "accepted_processing_status": provisional["processing_status"],
        }
        final_report = audit_documents(
            load_json(case_path, "case JSON")[0],
            candidate,
            new_run_record,
            case_path,
            os.path.join(output, RESULT_NAME),
            work,
            output,
            load_json(os.path.abspath(args.optimization_report), "optimization report JSON")[0]
            if args.optimization_report
            else None,
            False,
            result_raw_sha256=sha256_bytes(candidate_data),
        )
        final_report["run_record_path"] = run_record_path
        if not final_report["checkpoint_eligible"]:
            emit(
                {
                    "ok": False,
                    "command": "checkpoint",
                    "status": "rejected",
                    "reason": "post-transition audit failed; no protected output was replaced",
                    "issues": final_report["issues"],
                    "result_preserved": os.path.join(output, RESULT_NAME),
                    "validation_preserved": os.path.join(output, VALIDATION_NAME),
                }
            )
            return 5
        staged_run_record = os.path.join(temporary.name, RUN_RECORD_NAME)
        staged_validation = os.path.join(temporary.name, VALIDATION_NAME)
        atomic_write_json(staged_run_record, new_run_record)
        atomic_write_json(staged_validation, final_report)
        commit_staged_files(
            (
                (staged_run_record, run_record_path),
                (staged_result, os.path.join(output, RESULT_NAME)),
                (staged_validation, os.path.join(output, VALIDATION_NAME)),
            )
        )
    emit(
        {
            "ok": True,
            "command": "checkpoint",
            "status": final_report["effective_status"],
            "processing_status": final_report["processing_status"],
            "candidate_sha256": sha256_bytes(candidate_data),
            "audit_passed": final_report["audit_passed"],
            "partial_accepted_with_limitations": final_report["effective_status"] != "complete",
            "result": os.path.join(output, RESULT_NAME),
            "result_sha256": final_report["result_sha256"],
            "validation": os.path.join(output, VALIDATION_NAME),
            "run_record": run_record_path,
            "scientific_or_mathematical_validity_claimed": False,
        }
    )
    return 0


def command_finalize(args):
    case_path = os.path.abspath(args.case)
    work = os.path.abspath(args.work)
    output = os.path.abspath(args.output)
    result_path = os.path.join(output, RESULT_NAME)
    run_record_path = os.path.join(output, RUN_RECORD_NAME)
    validation_path = os.path.join(output, VALIDATION_NAME)
    report, run_record = run_audit(
        case_path,
        result_path,
        work,
        output,
        os.path.abspath(args.optimization_report) if args.optimization_report else None,
    )
    accepted = report["checkpoint_eligible"]
    if report["effective_status"] == "complete" and not report["audit_passed"]:
        accepted = False
    final_time = utc_now()
    run_record["updated_at_utc"] = final_time
    run_record["finalization"] = {
        "state": "finalized" if accepted else "rejected",
        "finalized_at_utc": final_time,
        "attempted_status": report["effective_status"],
        "processing_status": report["processing_status"],
        "raw_domain_status": report["raw_domain_status"],
        "audit_passed": report["audit_passed"],
        "pending_validations": report["pending_validations"],
        "result_sha256": report["result_sha256"],
        "result_canonical_sha256": report["result_canonical_sha256"],
        "result_sha256_basis": report["result_sha256_basis"],
        "note": "Rejected complete was not silently downgraded; inspect pending_validations. No mathematical/scientific guarantee is attached to finalized status.",
    }
    report["report_kind"] = "final"
    report["finalization_state"] = "finalized" if accepted else "rejected"
    report["finalized_at_utc"] = final_time
    report["run_record_finalization"] = copy.deepcopy(run_record["finalization"])
    atomic_write_json(run_record_path, run_record)
    atomic_write_json(validation_path, report)
    emit(
        {
            "ok": accepted,
            "command": "finalize",
            "status": report["effective_status"],
            "processing_status": report["processing_status"],
            "finalization_state": report["finalization_state"],
            "audit_passed": report["audit_passed"],
            "pending_validations": len(report["pending_validations"]),
            "review_levels": report["review_levels"],
            "result": result_path,
            "run_record": run_record_path,
            "validation": validation_path,
            "scientific_or_mathematical_validity_claimed": False,
        }
    )
    if not accepted:
        diagnostic("finalization rejected; invalid complete was not relabeled as valid")
        return 5
    return 0


def normalize_sha(value, option):
    if value is None:
        return None
    if not SHA256_RE.match(value):
        raise GuardError("invalid_sha256", option + " must contain exactly 64 hexadecimal characters", 2)
    return value.lower()


def last_journal_entry(path):
    if not os.path.exists(path):
        return None, 0
    data = read_bytes(path, "change journal")
    if not data:
        return None, 0
    lines = data.splitlines()
    previous_hash = None
    sequence = 0
    for index, line in enumerate(lines):
        if not line.strip():
            raise GuardError("change_journal_corrupt", "blank lines are not permitted in the append-only JSONL journal", 6, {"line": index + 1})
        value = parse_json_bytes(line, path + ":" + str(index + 1))
        if not isinstance(value, dict):
            raise GuardError("change_journal_corrupt", "journal line is not an object", 6, {"line": index + 1})
        body = {key: child for key, child in value.items() if key != "entry_sha256"}
        expected = hash_value(body)
        if value.get("entry_sha256") != expected:
            raise GuardError("change_journal_corrupt", "journal entry hash mismatch", 6, {"line": index + 1})
        if value.get("previous_entry_sha256") != previous_hash:
            raise GuardError("change_journal_corrupt", "journal hash chain mismatch", 6, {"line": index + 1})
        if value.get("sequence") != index + 1:
            raise GuardError("change_journal_corrupt", "journal sequence mismatch", 6, {"line": index + 1})
        previous_hash = value["entry_sha256"]
        sequence = index + 1
    if not data.endswith(b"\n"):
        raise GuardError("change_journal_corrupt", "journal must end with a newline before append", 6)
    return previous_hash, sequence


def command_record_change(args):
    if not present_text(args.operation) or not present_text(args.path) or not present_text(args.reason):
        raise GuardError("invalid_change_record", "operation, path, and reason must be nonempty concrete strings", 2)
    work = os.path.abspath(args.work)
    journal = os.path.join(work, JOURNAL_NAME)
    try:
        Path(work).mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise GuardError("permission_denied", "permission denied creating work directory", 4) from exc
    previous_hash, sequence = last_journal_entry(journal)
    entry = {
        "timestamp_utc": utc_now(),
        "sequence": sequence + 1,
        "operation": args.operation,
        "path": args.path,
        "reason": args.reason,
        "before_sha256": normalize_sha(args.before_sha, "--before-sha"),
        "after_sha256": normalize_sha(args.after_sha, "--after-sha"),
        "evidence_refs": args.evidence_ref,
        "previous_entry_sha256": previous_hash,
    }
    entry["entry_sha256"] = hash_value(entry)
    line = canonical_bytes(entry) + b"\n"
    try:
        descriptor = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        with os.fdopen(descriptor, "ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except PermissionError as exc:
        raise GuardError("permission_denied", "permission denied appending change journal", 4, {"path": journal}) from exc
    except OSError as exc:
        raise GuardError("journal_append_failed", "cannot append journal: " + str(exc), 3, {"path": journal}) from exc
    emit(
        {
            "ok": True,
            "command": "record-change",
            "status": "recorded",
            "journal": journal,
            "sequence": entry["sequence"],
            "timestamp_utc": entry["timestamp_utc"],
            "entry_sha256": entry["entry_sha256"],
            "other_files_modified": False,
        }
    )
    return 0


def add_common_audit_arguments(parser, candidate=False):
    parser.add_argument("--case", required=True, help="strict case.json path")
    if candidate:
        parser.add_argument("--candidate", required=True, help="candidate result JSON; audited in temporary work staging")
    else:
        parser.add_argument("--result", required=True, help="result JSON path")
    parser.add_argument("--work", required=True, help="working directory")
    parser.add_argument("--output", required=True, help="output directory containing run_record.json")
    parser.add_argument("--optimization-report", help="optional strict JSON diagnostics report; never executes an optimizer")


def build_parser():
    parser = JSONArgumentParser(prog="io_guard.py", add_help=False, allow_abbrev=False, description="Runtime JSON contract and integrity guard; standard library only, with optional already-installed jsonschema validation.")
    parser.add_argument("--help", action=JSONHelp, help="print this help as one stdout JSON object")
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JSONArgumentParser)
    bootstrap = subparsers.add_parser("bootstrap", add_help=False, allow_abbrev=False, help="create protected early partial outputs")
    bootstrap.add_argument("--help", action=JSONHelp, help="print bootstrap help as stdout JSON")
    bootstrap.add_argument("--case", required=True)
    bootstrap.add_argument("--work", required=True)
    bootstrap.add_argument("--output", required=True)
    bootstrap.add_argument("--task", help="optional task.md; fallback locations are probed without forcing an error")
    bootstrap.set_defaults(handler=command_bootstrap)
    audit = subparsers.add_parser("audit", add_help=False, allow_abbrev=False, help="mechanically audit a result and write output/validation.json")
    audit.add_argument("--help", action=JSONHelp, help="print audit help as stdout JSON")
    add_common_audit_arguments(audit)
    audit.set_defaults(handler=command_audit)
    checkpoint = subparsers.add_parser("checkpoint", add_help=False, allow_abbrev=False, help="audit a staging copy before atomically replacing protected outputs")
    checkpoint.add_argument("--help", action=JSONHelp, help="print checkpoint help as stdout JSON")
    add_common_audit_arguments(checkpoint, candidate=True)
    checkpoint.set_defaults(handler=command_checkpoint)
    finalize = subparsers.add_parser("finalize", add_help=False, allow_abbrev=False, help="audit and record final/rejected state with explicit review levels")
    finalize.add_argument("--help", action=JSONHelp, help="print finalize help as stdout JSON")
    finalize.add_argument("--case", required=True)
    finalize.add_argument("--work", required=True)
    finalize.add_argument("--output", required=True)
    finalize.add_argument("--optimization-report")
    finalize.set_defaults(handler=command_finalize)
    record = subparsers.add_parser("record-change", add_help=False, allow_abbrev=False, help="append one hash-chained UTC JSONL change record")
    record.add_argument("--help", action=JSONHelp, help="print record-change help as stdout JSON")
    record.add_argument("--work", required=True)
    record.add_argument("--operation", required=True)
    record.add_argument("--path", required=True)
    record.add_argument("--reason", required=True)
    record.add_argument("--before-sha")
    record.add_argument("--after-sha")
    record.add_argument("--evidence-ref", action="append", default=[])
    record.set_defaults(handler=command_record_change)
    return parser


def main(argv=None):
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        return args.handler(args)
    except GuardError as exc:
        emit({"ok": False, "status": "error", "error": {"code": exc.code, "message": exc.message, "details": exc.details}})
        diagnostic(exc.code + ": " + exc.message)
        return exc.exit_code
    except KeyboardInterrupt:
        emit({"ok": False, "status": "error", "error": {"code": "interrupted", "message": "operation interrupted", "details": {}}})
        diagnostic("interrupted")
        return 130
    except PermissionError as exc:
        emit({"ok": False, "status": "error", "error": {"code": "permission_denied", "message": str(exc), "details": {}}})
        diagnostic("permission_denied: " + str(exc))
        return 4
    except Exception as exc:
        emit({"ok": False, "status": "error", "error": {"code": "internal_error", "message": type(exc).__name__ + ": " + str(exc), "details": {}}})
        diagnostic("internal_error: " + type(exc).__name__ + ": " + str(exc))
        return 70


if __name__ == "__main__":
    sys.exit(main())
