#!/usr/bin/env python3
"""Deterministic, exact-arithmetic optimizer for bounded canonical requests."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1.0.0"
NUMERIC_KINDS = {"number", "integer"}
VALUE_KINDS = NUMERIC_KINDS | {"string", "boolean"}
DIRECTIONS = {"minimize", "maximize"}
INCLUSIVITIES = {"closed", "open", "left_closed", "right_closed"}


class RequestError(Exception):
    def __init__(self, message: str, path: str = "$") -> None:
        super().__init__(message)
        self.message = message
        self.path = path

    def diagnostic(self) -> Dict[str, Any]:
        return {
            "code": "invalid_request",
            "severity": "error",
            "path": self.path,
            "message": self.message,
        }


class JsonNumber:
    """Keeps the source token so Fraction never receives a binary float."""

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __str__(self) -> str:
        return self.text


class UnitParser:
    def __init__(self, text: str, path: str) -> None:
        self.text = text
        self.path = path
        self.pos = 0

    def parse(self) -> Tuple[Tuple[str, int], ...]:
        if self.text == "" or self.text == "1":
            return ()
        result = self._expression()
        self._space()
        if self.pos != len(self.text):
            raise RequestError("invalid unit expression", self.path)
        return _unit_tuple(result)

    def _expression(self) -> Dict[str, int]:
        result = self._factor()
        while True:
            self._space()
            if self.pos >= len(self.text) or self.text[self.pos] not in "*/":
                break
            operator = self.text[self.pos]
            self.pos += 1
            other = self._factor()
            sign = 1 if operator == "*" else -1
            for name, power in other.items():
                result[name] = result.get(name, 0) + sign * power
        return {name: power for name, power in result.items() if power}

    def _factor(self) -> Dict[str, int]:
        self._space()
        if self.pos >= len(self.text):
            raise RequestError("invalid unit expression", self.path)
        if self.text[self.pos] == "(":
            self.pos += 1
            result = self._expression()
            self._space()
            if self.pos >= len(self.text) or self.text[self.pos] != ")":
                raise RequestError("unclosed parenthesis in unit", self.path)
            self.pos += 1
        else:
            start = self.pos
            while self.pos < len(self.text) and self.text[self.pos] not in " \t\r\n*/()^":
                self.pos += 1
            name = self.text[start:self.pos]
            if not name or name == "1":
                raise RequestError("invalid unit atom", self.path)
            result = {name: 1}
        self._space()
        power = 1
        if self.pos < len(self.text) and self.text[self.pos] == "^":
            self.pos += 1
            self._space()
            start = self.pos
            if self.pos < len(self.text) and self.text[self.pos] in "+-":
                self.pos += 1
            digits = self.pos
            while self.pos < len(self.text) and self.text[self.pos].isdigit():
                self.pos += 1
            if self.pos == digits:
                raise RequestError("unit exponent must be an integer", self.path)
            power = int(self.text[start:self.pos])
        return {name: exponent * power for name, exponent in result.items()}

    def _space(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1


def _unit_tuple(units: Dict[str, int]) -> Tuple[Tuple[str, int], ...]:
    return tuple(sorted((name, power) for name, power in units.items() if power))


def parse_unit(value: Any, path: str) -> Tuple[Tuple[str, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, str):
        raise RequestError("unit must be a string", path)
    return UnitParser(value, path).parse()


def unit_text(unit: Tuple[Tuple[str, int], ...]) -> str:
    return "*".join(name if power == 1 else f"{name}^{power}" for name, power in unit)


def unit_combine(
    left: Tuple[Tuple[str, int], ...],
    right: Tuple[Tuple[str, int], ...],
    right_sign: int = 1,
) -> Tuple[Tuple[str, int], ...]:
    result = dict(left)
    for name, power in right:
        result[name] = result.get(name, 0) + right_sign * power
    return _unit_tuple(result)


@dataclass(frozen=True)
class Static:
    kind: str
    unit: Tuple[Tuple[str, int], ...]


@dataclass
class EvalValue:
    known: bool
    value: Any
    static: Static
    issues: Tuple[str, ...] = ()


@dataclass
class Parameter:
    identifier: str
    kind: str
    unit: Tuple[Tuple[str, int], ...]
    domain: "Domain"
    evidence_refs: List[str]


@dataclass
class InputValue:
    identifier: str
    kind: str
    unit: Tuple[Tuple[str, int], ...]
    status: str
    value: Any
    evidence_refs: List[str]


@dataclass
class Definition:
    identifier: str
    expression: Dict[str, Any]
    unit: Tuple[Tuple[str, int], ...]
    evidence_refs: List[str]
    static: Optional[Static] = None
    path: str = "$"


@dataclass
class Constraint:
    identifier: str
    expression: Dict[str, Any]
    evidence_refs: List[str]


@dataclass
class Plan:
    identifier: str
    version: str
    assignments: Dict[str, Any]
    domains: Dict[str, "Domain"]
    evidence_refs: List[str]


class Domain:
    kind: str

    def __len__(self) -> int:
        raise NotImplementedError

    def value_at(self, index: int) -> Any:
        raise NotImplementedError

    def contains(self, value: Any) -> bool:
        raise NotImplementedError


@dataclass
class AllowedDomain(Domain):
    kind: str
    values: Tuple[Any, ...]

    def __len__(self) -> int:
        return len(self.values)

    def value_at(self, index: int) -> Any:
        return self.values[index]

    def contains(self, value: Any) -> bool:
        return value in self.values


@dataclass
class GridDomain(Domain):
    kind: str
    lower: Fraction
    upper: Fraction
    step: Fraction
    inclusivity: str
    first_index: int
    last_index: int

    def __len__(self) -> int:
        return max(0, self.last_index - self.first_index + 1)

    def value_at(self, index: int) -> Fraction:
        if index < 0 or index >= len(self):
            raise IndexError(index)
        return self.lower + (self.first_index + index) * self.step

    def contains(self, value: Any) -> bool:
        if self.lower > value or value > self.upper:
            return False
        offset = (value - self.lower) / self.step
        if offset.denominator != 1:
            return False
        index = offset.numerator
        if self.inclusivity in {"open", "right_closed"} and index == 0:
            return False
        if self.inclusivity in {"open", "left_closed"} and value == self.upper:
            return False
        return True


def expect_object(value: Any, path: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestError("expected an object", path)
    return value


def expect_array(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise RequestError("expected an array", path)
    return value


def keys(
    obj: Dict[str, Any], path: str, required: Iterable[str], optional: Iterable[str] = ()
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(obj))
    if missing:
        raise RequestError(f"missing required key: {missing[0]}", path)
    extra = sorted(set(obj) - allowed)
    if extra:
        raise RequestError(f"unexpected key: {extra[0]}", path)


def nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequestError("expected a non-empty string", path)
    return value


def enum_value(value: Any, allowed: Iterable[str], path: str, message: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise RequestError(message, path)
    return value


def as_fraction(value: Any, path: str, kind: Optional[str] = None) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (JsonNumber, int, float)):
        raise RequestError("expected a JSON number", path)
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise RequestError("NaN and Infinity are not allowed", path)
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise RequestError("invalid finite number", path) from exc
    if kind == "integer" and result.denominator != 1:
        raise RequestError("expected an integer value", path)
    return result


def scalar(
    value: Any, path: str, expected_kind: Optional[str] = None
) -> Tuple[str, Any]:
    if isinstance(value, bool):
        kind, parsed = "boolean", value
    elif isinstance(value, str):
        kind, parsed = "string", value
    elif isinstance(value, (JsonNumber, int, float)):
        number = as_fraction(value, path)
        kind, parsed = ("integer" if number.denominator == 1 else "number"), number
    else:
        raise RequestError("expected a number, string, or boolean", path)
    if expected_kind is not None:
        if expected_kind in NUMERIC_KINDS:
            if kind not in NUMERIC_KINDS:
                raise RequestError(f"expected {expected_kind}", path)
            if expected_kind == "integer" and parsed.denominator != 1:
                raise RequestError("expected an integer value", path)
        elif kind != expected_kind:
            raise RequestError(f"expected {expected_kind}", path)
    return kind, parsed


def domain_sort_key(value: Any) -> Tuple[int, Any]:
    if isinstance(value, Fraction):
        return 0, value
    if isinstance(value, bool):
        return 1, int(value)
    return 2, value


def make_domain(raw: Any, path: str, kind: str) -> Domain:
    obj = expect_object(raw, path)
    keys(obj, path, (), {"allowed_values", "lower", "upper", "step", "inclusivity"})
    if "allowed_values" in obj:
        if set(obj) != {"allowed_values"}:
            raise RequestError("allowed_values cannot be combined with grid fields", path)
        values = expect_array(obj["allowed_values"], path + ".allowed_values")
        if not values:
            raise RequestError("allowed_values must not be empty", path + ".allowed_values")
        parsed: List[Any] = []
        for index, value in enumerate(values):
            _, item = scalar(value, f"{path}.allowed_values[{index}]", kind)
            if item in parsed:
                raise RequestError("duplicate domain value", f"{path}.allowed_values[{index}]")
            parsed.append(item)
        parsed.sort(key=domain_sort_key)
        return AllowedDomain(kind, tuple(parsed))
    required = {"lower", "upper", "step", "inclusivity"}
    if set(obj) != required:
        raise RequestError("a grid requires exactly lower, upper, step, inclusivity", path)
    if kind not in NUMERIC_KINDS:
        raise RequestError("grids are only valid for number or integer parameters", path)
    lower = as_fraction(obj["lower"], path + ".lower", kind)
    upper = as_fraction(obj["upper"], path + ".upper", kind)
    step = as_fraction(obj["step"], path + ".step", kind)
    if lower > upper:
        raise RequestError("lower must not exceed upper", path)
    if step <= 0:
        raise RequestError("step must be greater than zero", path + ".step")
    inclusivity = enum_value(obj["inclusivity"], INCLUSIVITIES, path + ".inclusivity", "invalid inclusivity")
    quotient = (upper - lower) / step
    first = 1 if inclusivity in {"open", "right_closed"} else 0
    last = math.floor(quotient)
    if inclusivity in {"open", "left_closed"} and quotient.denominator == 1:
        last -= 1
    domain = GridDomain(kind, lower, upper, step, inclusivity, first, last)
    if len(domain) == 0:
        raise RequestError("grid contains no values after applying inclusivity", path)
    return domain


def encode_number(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)
    denominator = value.denominator
    twos = 0
    fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator == 1:
        digits = max(twos, fives, 1)
        scaled = abs(value.numerator) * (10 ** digits) // value.denominator
        sign = "-" if value.numerator < 0 else ""
        text = str(scaled).rjust(digits + 1, "0")
        result = sign + text[:-digits] + "." + text[-digits:]
        return result.rstrip("0").rstrip(".")
    return f"{value.numerator}/{value.denominator}"


def encode_value(value: Any) -> Any:
    return encode_number(value) if isinstance(value, Fraction) else value


def validate_evidence_refs(value: Any, path: str, evidence: Dict[str, Any]) -> List[str]:
    refs = expect_array(value, path)
    result: List[str] = []
    for index, ref in enumerate(refs):
        item = nonempty_string(ref, f"{path}[{index}]")
        if item not in evidence:
            raise RequestError(f"unknown evidence id: {item}", f"{path}[{index}]")
        if item in result:
            raise RequestError("duplicate evidence reference", f"{path}[{index}]")
        result.append(item)
    return result


class Compiler:
    def __init__(self, request: Dict[str, Any]) -> None:
        self.request = request
        self.evidence: Dict[str, Any] = {}
        self.parameters: Dict[str, Parameter] = {}
        self.inputs: Dict[str, InputValue] = {}
        self.derived: Dict[str, Definition] = {}
        self.objectives: Dict[str, Definition] = {}
        self.constraints: List[Constraint] = []
        self.symbols: Dict[str, Static] = {}
        self.node_static: Dict[int, Static] = {}
        self.node_domains: Dict[int, Domain] = {}
        self.order: List[str] = []
        self.visiting: List[str] = []
        self.states: Dict[str, int] = {}

    def compile(self) -> None:
        keys(
            self.request,
            "$",
            {
                "schema_version",
                "case_id",
                "evidence",
                "parameters",
                "plans",
                "input_values",
                "derived_values",
                "objectives",
                "constraints",
                "decision",
            },
        )
        if self.request["schema_version"] != SCHEMA_VERSION:
            raise RequestError(f"schema_version must be {SCHEMA_VERSION}", "$.schema_version")
        nonempty_string(self.request["case_id"], "$.case_id")
        self._compile_evidence(self.request["evidence"])
        self._compile_parameters(self.request["parameters"])
        self._compile_inputs(self.request["input_values"])
        self._compile_definitions(self.request["derived_values"], True)
        self._compile_definitions(self.request["objectives"], False)
        for identifier, parameter in self.parameters.items():
            self.symbols[identifier] = Static(parameter.kind, parameter.unit)
        for identifier, item in self.inputs.items():
            self.symbols[identifier] = Static(item.kind, item.unit)
        for identifier in self.derived:
            self._visit_derived(identifier, self.derived[identifier].path)
        for objective in self.objectives.values():
            objective.static = self._analyze(objective.expression, objective.path + ".expression")
            if objective.static.kind not in NUMERIC_KINDS:
                raise RequestError("objective expression must be numeric", objective.path + ".expression")
            if objective.static.unit != objective.unit:
                raise RequestError(
                    "declared objective unit does not match expression unit",
                    objective.path + ".unit",
                )
        self._compile_constraints(self.request["constraints"])
        self._compile_decision(self.request["decision"])

    def _compile_evidence(self, raw: Any) -> None:
        obj = expect_object(raw, "$.evidence")
        for identifier, item in obj.items():
            nonempty_string(identifier, "$.evidence")
            path = "$.evidence." + identifier
            entry = expect_object(item, path)
            keys(entry, path, {"locator", "quote", "type"})
            for field in ("locator", "quote", "type"):
                nonempty_string(entry[field], path + "." + field)
            self.evidence[identifier] = entry

    def _compile_parameters(self, raw: Any) -> None:
        items = expect_array(raw, "$.parameters")
        if not items:
            raise RequestError("at least one parameter is required", "$.parameters")
        for index, item in enumerate(items):
            path = f"$.parameters[{index}]"
            obj = expect_object(item, path)
            keys(obj, path, {"id", "type", "domain", "evidence_refs"}, {"unit"})
            identifier = nonempty_string(obj["id"], path + ".id")
            self._new_value_id(identifier, path + ".id")
            kind = enum_value(obj["type"], VALUE_KINDS, path + ".type", "invalid parameter type")
            unit = parse_unit(obj.get("unit"), path + ".unit")
            if unit and kind not in NUMERIC_KINDS:
                raise RequestError("only numeric values may have a non-empty unit", path + ".unit")
            domain = make_domain(obj["domain"], path + ".domain", kind)
            refs = validate_evidence_refs(obj["evidence_refs"], path + ".evidence_refs", self.evidence)
            self.parameters[identifier] = Parameter(identifier, kind, unit, domain, refs)

    def _compile_inputs(self, raw: Any) -> None:
        obj = expect_object(raw, "$.input_values")
        for identifier, item in obj.items():
            path = "$.input_values." + identifier
            self._new_value_id(identifier, path)
            entry = expect_object(item, path)
            keys(entry, path, {"status", "type", "nature", "unit", "evidence_refs"}, {"value"})
            kind = enum_value(entry["type"], VALUE_KINDS, path + ".type", "invalid input type")
            nonempty_string(entry["nature"], path + ".nature")
            unit = parse_unit(entry["unit"], path + ".unit")
            if unit and kind not in NUMERIC_KINDS:
                raise RequestError("only numeric values may have a non-empty unit", path + ".unit")
            refs = validate_evidence_refs(entry["evidence_refs"], path + ".evidence_refs", self.evidence)
            status = enum_value(entry["status"], {"known", "unknown"}, path + ".status", "status must be known or unknown")
            if status == "known":
                if "value" not in entry:
                    raise RequestError("known input requires value", path)
                _, value = scalar(entry["value"], path + ".value", kind)
            else:
                if "value" in entry:
                    raise RequestError("unknown input must not contain a value placeholder", path + ".value")
                value = None
            self.inputs[identifier] = InputValue(identifier, kind, unit, status, value, refs)

    def _compile_definitions(self, raw: Any, derived: bool) -> None:
        field = "derived_values" if derived else "objectives"
        items = expect_array(raw, "$." + field)
        target = self.derived if derived else self.objectives
        for index, item in enumerate(items):
            path = f"$.{field}[{index}]"
            obj = expect_object(item, path)
            keys(obj, path, {"id", "expression", "evidence_refs"}, {"unit"})
            identifier = nonempty_string(obj["id"], path + ".id")
            self._new_value_id(identifier, path + ".id")
            unit = parse_unit(obj.get("unit"), path + ".unit")
            refs = validate_evidence_refs(obj["evidence_refs"], path + ".evidence_refs", self.evidence)
            expression = expect_object(obj["expression"], path + ".expression")
            target[identifier] = Definition(identifier, expression, unit, refs, path=path)

    def _new_value_id(self, identifier: str, path: str) -> None:
        if identifier in self.parameters or identifier in self.inputs or identifier in self.derived or identifier in self.objectives:
            raise RequestError(f"duplicate value id: {identifier}", path)

    def _visit_derived(self, identifier: str, path: str) -> Static:
        state = self.states.get(identifier, 0)
        if state == 2:
            result = self.derived[identifier].static
            assert result is not None
            return result
        if state == 1:
            start = self.visiting.index(identifier)
            cycle = self.visiting[start:] + [identifier]
            raise RequestError("cyclic derived DAG: " + " -> ".join(cycle), path)
        self.states[identifier] = 1
        self.visiting.append(identifier)
        definition = self.derived[identifier]
        static = self._analyze(definition.expression, definition.path + ".expression", derived_context=True)
        if static.unit != definition.unit:
            raise RequestError("declared derived unit does not match expression unit", definition.path + ".unit")
        definition.static = static
        self.symbols[identifier] = static
        self.visiting.pop()
        self.states[identifier] = 2
        self.order.append(identifier)
        return static

    def _analyze(self, node: Any, path: str, derived_context: bool = False) -> Static:
        obj = expect_object(node, path)
        result: Static
        if "ref" in obj:
            keys(obj, path, {"ref"})
            identifier = nonempty_string(obj["ref"], path + ".ref")
            if derived_context and identifier in self.derived:
                result = self._visit_derived(identifier, path + ".ref")
            elif identifier in self.symbols and not (derived_context and identifier in self.objectives):
                result = self.symbols[identifier]
            else:
                raise RequestError(f"unknown or disallowed ref: {identifier}", path + ".ref")
        elif "const" in obj:
            keys(obj, path, {"const"}, {"unit"})
            kind, _ = scalar(obj["const"], path + ".const")
            unit = parse_unit(obj.get("unit"), path + ".unit")
            if unit and kind not in NUMERIC_KINDS:
                raise RequestError("only numeric constants may have a non-empty unit", path + ".unit")
            result = Static(kind, unit)
        else:
            if "op" not in obj:
                raise RequestError("expression must be ref, const, or op", path)
            op = obj.get("op")
            if op == "in_domain":
                keys(obj, path, {"op", "value", "domain"})
                child = self._analyze(obj["value"], path + ".value", derived_context)
                domain = make_domain(obj["domain"], path + ".domain", child.kind)
                self.node_domains[id(obj)] = domain
                result = Static("boolean", ())
            else:
                keys(obj, path, {"op", "args"})
                args = expect_array(obj.get("args"), path + ".args")
                children = [
                    self._analyze(arg, f"{path}.args[{index}]", derived_context)
                    for index, arg in enumerate(args)
                ]
                result = self._analyze_operator(op, children, path)
        self.node_static[id(obj)] = result
        return result

    def _analyze_operator(self, op: Any, args: List[Static], path: str) -> Static:
        counts = {
            "add": (2, None), "mul": (2, None), "min": (2, None), "max": (2, None),
            "sub": (2, 2), "div": (2, 2), "ceil_div": (2, 2),
            "floor": (1, 1), "ceil": (1, 1), "ceil_to_step": (2, 3),
            "eq": (2, 2), "ne": (2, 2), "lt": (2, 2), "le": (2, 2), "gt": (2, 2), "ge": (2, 2),
            "all": (1, None), "any": (1, None), "not": (1, 1), "implies": (2, 2),
        }
        op = enum_value(op, counts, path + ".op", f"unsupported operator: {op}")
        minimum, maximum = counts[op]
        if len(args) < minimum or (maximum is not None and len(args) > maximum):
            raise RequestError("wrong argument count", path + ".args")
        if op in {"all", "any", "not", "implies"}:
            if any(arg.kind != "boolean" for arg in args):
                raise RequestError("logical operands must be boolean", path + ".args")
            return Static("boolean", ())
        if any(arg.kind not in NUMERIC_KINDS for arg in args) and op not in {"eq", "ne"}:
            raise RequestError("operator requires numeric operands", path + ".args")
        if op in {"eq", "ne"}:
            first, second = args
            both_numeric = first.kind in NUMERIC_KINDS and second.kind in NUMERIC_KINDS
            if not both_numeric and first.kind != second.kind:
                raise RequestError("comparison operands have incompatible types", path + ".args")
        if op in {"add", "sub", "min", "max", "eq", "ne", "lt", "le", "gt", "ge"}:
            if args[0].unit != args[1].unit or any(arg.unit != args[0].unit for arg in args):
                raise RequestError("units must match for arithmetic or comparison", path + ".args")
        if op in {"eq", "ne", "lt", "le", "gt", "ge"}:
            return Static("boolean", ())
        kind = "number" if any(arg.kind == "number" for arg in args) else "integer"
        if op in {"add", "sub", "min", "max"}:
            return Static(kind, args[0].unit)
        if op == "mul":
            unit = args[0].unit
            for arg in args[1:]:
                unit = unit_combine(unit, arg.unit)
            return Static(kind, unit)
        if op == "div":
            return Static("number", unit_combine(args[0].unit, args[1].unit, -1))
        if op == "ceil_div":
            return Static("integer", unit_combine(args[0].unit, args[1].unit, -1))
        if op in {"floor", "ceil"}:
            return Static("integer", args[0].unit)
        if op == "ceil_to_step":
            if any(arg.unit != args[0].unit for arg in args):
                raise RequestError("ceil_to_step operands must have matching units", path + ".args")
            return Static(kind, args[0].unit)
        raise AssertionError(op)

    def _compile_constraints(self, raw: Any) -> None:
        items = expect_array(raw, "$.constraints")
        seen: set = set()
        for index, item in enumerate(items):
            path = f"$.constraints[{index}]"
            obj = expect_object(item, path)
            keys(obj, path, {"id", "expression", "evidence_refs"})
            identifier = nonempty_string(obj["id"], path + ".id")
            if identifier in seen:
                raise RequestError(f"duplicate constraint id: {identifier}", path + ".id")
            seen.add(identifier)
            refs = validate_evidence_refs(obj["evidence_refs"], path + ".evidence_refs", self.evidence)
            expression = expect_object(obj["expression"], path + ".expression")
            static = self._analyze(expression, path + ".expression")
            if static.kind != "boolean":
                raise RequestError("constraint expression must be boolean", path + ".expression")
            self.constraints.append(Constraint(identifier, expression, refs))

    def _compile_decision(self, raw: Any) -> None:
        obj = expect_object(raw, "$.decision")
        decision_type = enum_value(
            obj.get("type"),
            {"feasibility", "single", "lexicographic", "pareto"},
            "$.decision.type",
            "invalid decision type",
        )
        single = decision_type == "single"
        if decision_type == "feasibility":
            keys(obj, "$.decision", {"type"})
            self.decision = {"type": decision_type, "objectives": []}
            return
        if single:
            keys(obj, "$.decision", {"type", "objective_id", "direction"})
            entries = [{"id": obj["objective_id"], "direction": obj["direction"]}]
        elif decision_type in {"lexicographic", "pareto"}:
            keys(obj, "$.decision", {"type", "objectives"})
            entries = expect_array(obj["objectives"], "$.decision.objectives")
            if not entries:
                raise RequestError("at least one decision objective is required", "$.decision.objectives")
        normalized = []
        seen = set()
        for index, entry in enumerate(entries):
            path = "$.decision" if single else f"$.decision.objectives[{index}]"
            item = expect_object(entry, path)
            keys(item, path, {"id", "direction"})
            identifier_path = path + (".objective_id" if single else ".id")
            identifier = nonempty_string(item["id"], identifier_path)
            if identifier not in self.objectives:
                raise RequestError(f"unknown objective id: {identifier}", identifier_path)
            if identifier in seen:
                raise RequestError("duplicate decision objective", identifier_path)
            seen.add(identifier)
            direction = enum_value(
                item["direction"],
                DIRECTIONS,
                path + ".direction",
                "direction must be explicitly minimize or maximize",
            )
            normalized.append((identifier, direction))
        self.decision = {"type": decision_type, "objectives": normalized}

    def evaluate(self, node: Dict[str, Any], env: Dict[str, EvalValue]) -> EvalValue:
        static = self.node_static[id(node)]
        if "ref" in node:
            return env[node["ref"]]
        if "const" in node:
            _, value = scalar(node["const"], "$.const", static.kind)
            return EvalValue(True, value, static)
        if node["op"] == "in_domain":
            child = self.evaluate(node["value"], env)
            if not child.known:
                return EvalValue(False, None, static, child.issues)
            return EvalValue(True, self.node_domains[id(node)].contains(child.value), static)
        args = [self.evaluate(arg, env) for arg in node["args"]]
        op = node["op"]
        issues = tuple(sorted({issue for arg in args for issue in arg.issues}))
        if op in {"all", "any"}:
            values = [arg for arg in args if arg.known]
            if op == "all" and any(arg.value is False for arg in values):
                return EvalValue(True, False, static)
            if op == "any" and any(arg.value is True for arg in values):
                return EvalValue(True, True, static)
            if any(not arg.known for arg in args):
                return EvalValue(False, None, static, issues)
            return EvalValue(True, op == "all", static)
        if op == "not":
            if not args[0].known:
                return EvalValue(False, None, static, issues)
            return EvalValue(True, not args[0].value, static)
        if op == "implies":
            left, right = args
            if left.known and left.value is False:
                return EvalValue(True, True, static)
            if left.known and left.value is True:
                return right
            if right.known and right.value is True:
                return EvalValue(True, True, static)
            return EvalValue(False, None, static, issues)
        if any(not arg.known for arg in args):
            return EvalValue(False, None, static, issues)
        values = [arg.value for arg in args]
        if op == "add":
            value = sum(values, Fraction(0))
        elif op == "sub":
            value = values[0] - values[1]
        elif op == "mul":
            value = Fraction(1)
            for item in values:
                value *= item
        elif op == "div":
            if values[1] == 0:
                return EvalValue(False, None, static, issues + ("division by zero",))
            value = values[0] / values[1]
        elif op == "min":
            value = min(values)
        elif op == "max":
            value = max(values)
        elif op == "floor":
            value = Fraction(math.floor(values[0]))
        elif op == "ceil":
            value = Fraction(math.ceil(values[0]))
        elif op == "ceil_div":
            if values[1] == 0:
                return EvalValue(False, None, static, issues + ("ceil_div divisor is zero",))
            value = Fraction(math.ceil(values[0] / values[1]))
        elif op == "ceil_to_step":
            origin = values[2] if len(values) == 3 else Fraction(0)
            if values[1] <= 0:
                return EvalValue(False, None, static, issues + ("ceil_to_step step must be positive",))
            value = origin + math.ceil((values[0] - origin) / values[1]) * values[1]
        elif op in {"eq", "ne", "lt", "le", "gt", "ge"}:
            operations = {
                "eq": lambda a, b: a == b, "ne": lambda a, b: a != b,
                "lt": lambda a, b: a < b, "le": lambda a, b: a <= b,
                "gt": lambda a, b: a > b, "ge": lambda a, b: a >= b,
            }
            value = operations[op](values[0], values[1])
        else:
            raise AssertionError(op)
        return EvalValue(True, value, static)

    def initial_environment(self, assignments: Dict[str, Any]) -> Dict[str, EvalValue]:
        env: Dict[str, EvalValue] = {}
        for identifier, parameter in self.parameters.items():
            static = Static(parameter.kind, parameter.unit)
            env[identifier] = EvalValue(True, assignments[identifier], static)
        for identifier, item in self.inputs.items():
            static = Static(item.kind, item.unit)
            if item.status == "known":
                env[identifier] = EvalValue(True, item.value, static)
            else:
                env[identifier] = EvalValue(
                    False, None, static, (f"input_value '{identifier}' is unknown",)
                )
        return env


def metric_output(
    result: EvalValue, definition: Definition
) -> Dict[str, Any]:
    static = definition.static
    assert static is not None
    return {
        "status": "known" if result.known else "unknown",
        "type": static.kind,
        "value": encode_value(result.value) if result.known else None,
        "unit": unit_text(static.unit),
        "evidence_refs": list(definition.evidence_refs),
        "issues": list(result.issues),
    }


def candidate_identifier(plan: Plan, assignments: Dict[str, Any]) -> str:
    payload = [
        plan.identifier,
        plan.version,
        [[identifier, encode_value(assignments[identifier])] for identifier in sorted(assignments)],
    ]
    return "candidate:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def evaluate_candidate(compiler: Compiler, plan: Plan, assignments: Dict[str, Any]) -> Dict[str, Any]:
    env = compiler.initial_environment(assignments)
    derived_results: Dict[str, EvalValue] = {}
    for identifier in compiler.order:
        definition = compiler.derived[identifier]
        result = compiler.evaluate(definition.expression, env)
        env[identifier] = result
        derived_results[identifier] = result
    objective_results: Dict[str, EvalValue] = {}
    for identifier, definition in compiler.objectives.items():
        objective_results[identifier] = compiler.evaluate(definition.expression, env)
    constraint_outputs: Dict[str, Any] = {}
    failed = False
    unresolved = False
    elimination: List[str] = []
    all_issues: List[str] = []
    for constraint in compiler.constraints:
        result = compiler.evaluate(constraint.expression, env)
        if not result.known:
            status = "unknown"
            unresolved = True
            all_issues.extend(f"constraints.{constraint.identifier}: {issue}" for issue in result.issues)
        elif result.value:
            status = "pass"
        else:
            status = "fail"
            failed = True
            elimination.append(f"constraint:{constraint.identifier}:fail")
        constraint_outputs[constraint.identifier] = {
            "status": status,
            "value": result.value if result.known else None,
            "evidence_refs": list(constraint.evidence_refs),
            "issues": list(result.issues),
        }
    for identifier, result in derived_results.items():
        all_issues.extend(f"derived_values.{identifier}: {issue}" for issue in result.issues)
    for identifier, result in objective_results.items():
        all_issues.extend(f"objectives.{identifier}: {issue}" for issue in result.issues)
    evidence_refs = set(plan.evidence_refs)
    for parameter in compiler.parameters.values():
        evidence_refs.update(parameter.evidence_refs)
    for item in compiler.inputs.values():
        evidence_refs.update(item.evidence_refs)
    for definition in list(compiler.derived.values()) + list(compiler.objectives.values()):
        evidence_refs.update(definition.evidence_refs)
    for constraint in compiler.constraints:
        evidence_refs.update(constraint.evidence_refs)
    return {
        "candidate_id": candidate_identifier(plan, assignments),
        "plan_id": plan.identifier,
        "plan_version": plan.version,
        "parameters": {
            identifier: {
                "type": parameter.kind,
                "value": encode_value(assignments[identifier]),
                "unit": unit_text(parameter.unit),
                "fixed": identifier in plan.assignments,
                "evidence_refs": list(parameter.evidence_refs),
            }
            for identifier, parameter in sorted(compiler.parameters.items())
        },
        "derived_values": {
            identifier: metric_output(derived_results[identifier], compiler.derived[identifier])
            for identifier in compiler.order
        },
        "objectives": {
            identifier: metric_output(result, compiler.objectives[identifier])
            for identifier, result in sorted(objective_results.items())
        },
        "constraints": constraint_outputs,
        "feasibility": "infeasible" if failed else ("unknown" if unresolved else "feasible"),
        "evidence_refs": sorted(evidence_refs),
        "elimination_reasons": sorted(elimination),
        "evaluation_issues": sorted(set(all_issues)),
        "decision_status": "eliminated" if failed else "pending",
    }


def build_plans(compiler: Compiler, raw: Any) -> List[Plan]:
    items = expect_array(raw, "$.plans")
    if not items:
        raise RequestError("at least one plan is required", "$.plans")
    plans: List[Plan] = []
    seen = set()
    for index, item in enumerate(items):
        path = f"$.plans[{index}]"
        obj = expect_object(item, path)
        keys(obj, path, {"plan_id", "version", "evidence_refs"}, {"assignments", "domain_overrides"})
        identifier = nonempty_string(obj["plan_id"], path + ".plan_id")
        version = nonempty_string(obj["version"], path + ".version")
        if (identifier, version) in seen:
            raise RequestError("duplicate (plan_id, version); plan versions are never deduplicated", path)
        seen.add((identifier, version))
        refs = validate_evidence_refs(obj["evidence_refs"], path + ".evidence_refs", compiler.evidence)
        domains = {key: parameter.domain for key, parameter in compiler.parameters.items()}
        overrides = expect_object(obj.get("domain_overrides", {}), path + ".domain_overrides")
        for parameter_id, raw_domain in overrides.items():
            if parameter_id not in compiler.parameters:
                raise RequestError(f"unknown parameter id: {parameter_id}", path + ".domain_overrides")
            parameter = compiler.parameters[parameter_id]
            domains[parameter_id] = make_domain(
                raw_domain, path + ".domain_overrides." + parameter_id, parameter.kind
            )
        raw_assignments = expect_object(obj.get("assignments", {}), path + ".assignments")
        assignments: Dict[str, Any] = {}
        for parameter_id, raw_value in raw_assignments.items():
            if parameter_id not in compiler.parameters:
                raise RequestError(f"unknown parameter id: {parameter_id}", path + ".assignments")
            parameter = compiler.parameters[parameter_id]
            _, value = scalar(raw_value, path + ".assignments." + parameter_id, parameter.kind)
            if not domains[parameter_id].contains(value):
                raise RequestError("fixed assignment is outside the effective plan domain", path + ".assignments." + parameter_id)
            assignments[parameter_id] = value
        plans.append(Plan(identifier, version, assignments, domains, refs))
    plans.sort(key=lambda plan: (plan.identifier, plan.version))
    return plans


def candidates_in_order(plans: List[Plan], parameters: Dict[str, Parameter]) -> Iterable[Tuple[Plan, Dict[str, Any]]]:
    identifiers = sorted(parameters)
    for plan in plans:
        domains = []
        fixed_values: Dict[str, Any] = {}
        for identifier in identifiers:
            if identifier in plan.assignments:
                value = plan.assignments[identifier]
                domains.append(AllowedDomain(parameters[identifier].kind, (value,)))
                fixed_values[identifier] = value
            else:
                domains.append(plan.domains[identifier])
        indices = [0] * len(domains)
        while True:
            assignments = {
                identifier: domains[position].value_at(indices[position])
                for position, identifier in enumerate(identifiers)
            }
            yield plan, assignments
            position = len(domains) - 1
            while position >= 0:
                indices[position] += 1
                if indices[position] < len(domains[position]):
                    break
                indices[position] = 0
                position -= 1
            if position < 0:
                break


def objective_values(candidate: Dict[str, Any], entries: List[Tuple[str, str]]) -> Optional[Tuple[Fraction, ...]]:
    values = []
    for identifier, _ in entries:
        metric = candidate["objectives"][identifier]
        if metric["status"] != "known":
            return None
        values.append(Fraction(metric["value"]))
    return tuple(values)


def ranking_key(values: Tuple[Fraction, ...], entries: List[Tuple[str, str]], candidate_id: str) -> Tuple[Any, ...]:
    directed = [value if entries[index][1] == "minimize" else -value for index, value in enumerate(values)]
    return tuple(directed) + (candidate_id,)


def dominates(
    left: Tuple[Fraction, ...], right: Tuple[Fraction, ...], entries: List[Tuple[str, str]]
) -> bool:
    strictly_better = False
    for index, (_, direction) in enumerate(entries):
        if direction == "minimize":
            if left[index] > right[index]:
                return False
            if left[index] < right[index]:
                strictly_better = True
        else:
            if left[index] < right[index]:
                return False
            if left[index] > right[index]:
                strictly_better = True
    return strictly_better


def finalize_decision(compiler: Compiler, candidates: List[Dict[str, Any]], exhaustive: bool) -> Dict[str, Any]:
    decision_type = compiler.decision["type"]
    entries = compiler.decision["objectives"]
    eligible = [item for item in candidates if item["feasibility"] != "infeasible"]
    feasible = [item for item in candidates if item["feasibility"] == "feasible"]
    unresolved: List[Dict[str, Any]] = []
    known: List[Tuple[Dict[str, Any], Tuple[Fraction, ...]]] = []
    value_map: Dict[str, Tuple[Fraction, ...]] = {}
    best_ids: List[str] = []
    pareto_ids: List[str] = []
    for candidate in eligible:
        reasons = []
        for identifier, output in candidate["constraints"].items():
            if output["status"] == "unknown":
                reasons.append(f"constraint:{identifier}:unknown")
        if decision_type != "feasibility":
            for identifier, _ in entries:
                if candidate["objectives"][identifier]["status"] != "known":
                    reasons.append(f"objective:{identifier}:unknown")
        if reasons or candidate["feasibility"] == "unknown":
            unresolved.append(candidate)
            candidate["decision_status"] = "unranked"
            candidate["elimination_reasons"] = sorted(set(candidate["elimination_reasons"] + reasons))
        else:
            if entries:
                values = objective_values(candidate, entries)
                assert values is not None
                known.append((candidate, values))
                value_map[candidate["candidate_id"]] = values
            candidate["decision_status"] = "ranked"
    if decision_type in {"single", "lexicographic"}:
        known.sort(key=lambda pair: ranking_key(pair[1], entries, pair[0]["candidate_id"]))
        ranked_ids = [pair[0]["candidate_id"] for pair in known]
        if known:
            winning_values = known[0][1]
            best_ids = [pair[0]["candidate_id"] for pair in known if pair[1] == winning_values]
    elif decision_type == "pareto":
        dominators: Dict[str, List[str]] = {pair[0]["candidate_id"]: [] for pair in known}
        for candidate, values in known:
            identifier = candidate["candidate_id"]
            for other, other_values in known:
                if identifier != other["candidate_id"] and dominates(other_values, values, entries):
                    dominators[identifier].append(other["candidate_id"])
        for candidate, _ in known:
            identifier = candidate["candidate_id"]
            if dominators[identifier]:
                candidate["decision_status"] = "dominated"
                candidate["elimination_reasons"] = sorted(set(
                    candidate["elimination_reasons"]
                    + ["dominated_by:" + other for other in dominators[identifier]]
                ))
            else:
                candidate["decision_status"] = "pareto"
                pareto_ids.append(identifier)
        ranked_ids = sorted(value_map)
        pareto_ids.sort()
    else:
        ranked_ids = sorted(item["candidate_id"] for item in feasible)
    unresolved_ids = sorted(item["candidate_id"] for item in unresolved)
    if not exhaustive:
        status = "partial"
    elif not feasible and not any(item["feasibility"] == "unknown" for item in candidates):
        status = "infeasible"
    elif unresolved:
        status = "unresolved"
    elif decision_type in {"single", "lexicographic"}:
        status = "optimal" if best_ids else "infeasible"
    else:
        status = "complete"
    if status != "optimal":
        best_ids = []
    return {
        "status": status,
        "ranked_candidate_ids": ranked_ids,
        "pareto_candidate_ids": pareto_ids,
        "unranked_candidate_ids": unresolved_ids,
        "best_candidate_ids": best_ids,
        "decision_summary": {
            "type": decision_type,
            "exact_optimal": status == "optimal",
            "all_ties_retained": True,
            "pareto_has_unique_winner": False,
            "unresolved_candidate_ids": unresolved_ids,
        },
    }


def optimize(request: Any, max_candidates: Optional[int]) -> Dict[str, Any]:
    obj = expect_object(request, "$")
    compiler = Compiler(obj)
    compiler.compile()
    plans = build_plans(compiler, obj["plans"])
    total = 0
    for plan in plans:
        count = 1
        for identifier in compiler.parameters:
            if identifier not in plan.assignments:
                count *= len(plan.domains[identifier])
        total += count
    evaluated: List[Dict[str, Any]] = []
    stop_reason = "candidate_cap_exceeded"
    for plan, assignments in candidates_in_order(plans, compiler.parameters):
        if max_candidates is not None and len(evaluated) >= max_candidates:
            break
        evaluated.append(evaluate_candidate(compiler, plan, assignments))
    else:
        stop_reason = "enumeration_complete"
    exhaustive = len(evaluated) == total
    if exhaustive:
        stop_reason = "enumeration_complete"
    decision = finalize_decision(compiler, evaluated, exhaustive)
    coverage = Fraction(len(evaluated), total) if total else Fraction(0)
    diagnostics: List[Dict[str, Any]] = []
    if not exhaustive:
        diagnostics.append({
            "code": "candidate_cap_exceeded",
            "severity": "warning",
            "message": "candidate budget stopped enumeration; rankings and Pareto results are partial",
        })
    if decision["unranked_candidate_ids"]:
        diagnostics.append({
            "code": "unresolved_candidates",
            "severity": "warning",
            "message": "unknown inputs or evaluations were not zero-filled, ranked, or used for dominance pruning",
            "candidate_ids": decision["unranked_candidate_ids"],
        })
    return {
        "case_id": obj["case_id"],
        "status": decision["status"],
        "search": {
            "total": total,
            "evaluated": len(evaluated),
            "exhaustive": exhaustive,
            "truncated": not exhaustive,
            "stop_reason": stop_reason,
            "max_candidates": max_candidates,
            "coverage": {
                "evaluated": len(evaluated),
                "total": total,
                "fraction": encode_number(coverage),
            },
            "plan_count": len(plans),
            "candidate_order": "plan_id, plan_version, parameter id, ascending canonical domain value; ties use candidate_id",
        },
        "ranked_candidate_ids": decision["ranked_candidate_ids"],
        "pareto_candidate_ids": decision["pareto_candidate_ids"],
        "unranked_candidate_ids": decision["unranked_candidate_ids"],
        "best_candidate_ids": decision["best_candidate_ids"],
        "decision_summary": decision["decision_summary"],
        "candidates": evaluated,
        "diagnostics": diagnostics,
    }


def invalid_report(case_id: Optional[str], error: RequestError, max_candidates: Optional[int]) -> Dict[str, Any]:
    return {
        "case_id": case_id,
        "status": "invalid_request",
        "search": {
            "total": 0,
            "evaluated": 0,
            "exhaustive": False,
            "truncated": False,
            "stop_reason": "validation_failed",
            "max_candidates": max_candidates,
            "coverage": {"evaluated": 0, "total": 0, "fraction": "0"},
            "plan_count": 0,
            "candidate_order": "not_applicable",
        },
        "ranked_candidate_ids": [],
        "pareto_candidate_ids": [],
        "unranked_candidate_ids": [],
        "best_candidate_ids": [],
        "decision_summary": {
            "type": None,
            "exact_optimal": False,
            "all_ties_retained": True,
            "pareto_has_unique_winner": False,
            "unresolved_candidate_ids": [],
        },
        "candidates": [],
        "diagnostics": [error.diagnostic()],
    }


def reject_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def reject_constant(value: str) -> None:
    raise RequestError(f"non-finite JSON number is forbidden: {value}")


def load_request(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeError) as exc:
        raise RequestError(f"cannot read UTF-8 input: {exc}", "$input") from exc
    try:
        return json.loads(
            text,
            parse_float=JsonNumber,
            parse_int=JsonNumber,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except RequestError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RequestError(f"invalid JSON: {exc}", "$input") from exc


def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    destination = os.path.abspath(path)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + os.path.basename(destination) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def nonnegative_integer(text: str) -> int:
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if value < 0 or str(value) != text:
        raise argparse.ArgumentTypeError("must be a canonical non-negative integer")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Exact finite-domain optimizer; Python standard library only")
    parser.add_argument("--input", required=True, help="canonical request JSON")
    parser.add_argument("--output", required=True, help="atomically written report JSON")
    parser.add_argument("--max-candidates", type=nonnegative_integer, default=None, help="global evaluation cap; 0 is allowed")
    args = parser.parse_args(argv)
    case_id: Optional[str] = None
    try:
        request = load_request(args.input)
        if isinstance(request, dict) and isinstance(request.get("case_id"), str):
            case_id = request["case_id"]
        report = optimize(request, args.max_candidates)
        exit_code = 0
    except (RequestError, RecursionError) as exc:
        if isinstance(exc, RecursionError):
            exc = RequestError("expression or object nesting exceeds interpreter recursion limit")
        report = invalid_report(case_id, exc, args.max_candidates)
        exit_code = 2
    try:
        atomic_write_json(args.output, report)
    except OSError as exc:
        print(f"error: cannot atomically write output: {exc}", file=sys.stderr)
        return 2
    print(f"{report['case_id']}: {report['status']}; evaluated {report['search']['evaluated']}/{report['search']['total']}; output {args.output}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
