#!/usr/bin/env python3
"""Compile explicit candidate cost items into exact arithmetic and optional requests."""

import argparse, json, os, re, sys, tempfile
from fractions import Fraction

NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
NUMBER_RE = re.compile(r"^(" + NUMBER + r")$")
MAGNITUDE_RE = re.compile(r"^\s*(" + NUMBER + r")(?:\s+(.*?))?\s*$")
PRICE_RE = re.compile(r"^\s*(" + NUMBER + r")(?:\s+([A-Za-z]{3})(?:\s*/\s*([^/]+))?)?\s*$")
CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
MISSING = object()
class JsonNumber:
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text

class AuditError(Exception): pass

def reject_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuditError("duplicate JSON object key: " + key)
        result[key] = value
    return result

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.loads(handle.read(), parse_float=JsonNumber, parse_int=JsonNumber,
                              parse_constant=lambda value: (_ for _ in ()).throw(
                                  AuditError("non-finite number: " + value)),
                              object_pairs_hook=reject_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuditError("cannot load " + path + ": " + str(exc)) from exc

def encode_json(value, level=0):
    if isinstance(value, JsonNumber):
        return value.text
    if value is None or isinstance(value, bool):
        return "true" if value is True else "false" if value is False else "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise AuditError("binary float reached JSON output")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if not value:
            return "[]"
        pad = " " * (level + 2)
        return "[\n" + ",\n".join(pad + encode_json(v, level + 1) for v in value) + "\n" + " " * level + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        pad = " " * (level + 2)
        return "{\n" + ",\n".join(pad + json.dumps(str(k), ensure_ascii=False) + ": " +
                                    encode_json(value[k], level + 1) for k in sorted(value)) + "\n" + " " * level + "}"
    raise AuditError("unsupported JSON output type: " + type(value).__name__)

def write_json(path, payload):
    destination = os.path.abspath(path)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + os.path.basename(destination) + ".", dir=destination and os.path.dirname(destination))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encode_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise

def text(value):
    if isinstance(value, JsonNumber):
        return value.text
    return value if isinstance(value, str) else None

def exact(value, magnitude=False):
    token = text(value)
    if token is None:
        return None
    match = MAGNITUDE_RE.fullmatch(token) if magnitude else NUMBER_RE.fullmatch(token.strip())
    if not match:
        return None
    remainder = match.group(2) if magnitude else None
    if remainder and (remainder[0] in "+*/^" or re.match(r"^[x×]\s*" + NUMBER, remainder)):
        return None
    try:
        return Fraction(match.group(1))
    except (ValueError, ZeroDivisionError):
        return None

def fmt(value):
    if value.denominator == 1:
        return str(value.numerator)
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        return str(value.numerator) + "/" + str(value.denominator)
    digits = max(twos, fives, 1)
    scaled = abs(value.numerator) * 10 ** digits // value.denominator
    sign = "-" if value.numerator < 0 else ""
    digits_text = sign + str(scaled).rjust(digits + 1, "0")
    return (digits_text[:-digits] + "." + digits_text[-digits:]).rstrip("0").rstrip(".")

def pointer(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")

def resolve_pointer(document, locator):
    part = locator.split("#", 1)[1] if "#" in locator else locator
    if not part.startswith("/"):
        if isinstance(document, dict):
            for i, item in enumerate(document.get("hard_constraints", [])):
                if isinstance(item, dict) and item.get("id") == locator:
                    return "/hard_constraints/" + str(i)
        return None
    current = document
    try:
        for token in part[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            current = current[int(token)] if isinstance(current, list) else current[token]
        return part
    except (KeyError, IndexError, ValueError, TypeError):
        return None

def unit_atom(value):
    if not value:
        return ""
    atom = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip()).strip("_")
    return ("u_" + atom if atom[:1].isdigit() else atom) if atom else ""

def slug(value):
    return unit_atom(str(value))[:80] or "unnamed"

class Audit:
    def __init__(self, case, caps):
        self.case, self.caps = case, caps
        self.diagnostics, self.plans, self.policy = [], [], []
        self.errors = 0
        self.evidence, self.evidence_keys = {}, {}
        self.book, self.book_pointer = self.find_book()
        self.book_currency = self.valid_currency(self.book.get("currency")) if self.book else None

    def diag(self, code, path, message, blocking=True, severity="error"):
        self.diagnostics.append({"code": code, "severity": severity, "case_json_pointer": path, "message": message})
        if blocking:
            self.errors += 1

    def find_book(self):
        if not isinstance(self.case, dict):
            return {}, None
        book = self.case.get("price_book")
        if isinstance(book, dict):
            return book, "/price_book"
        contract = self.case.get("planning_contract")
        if isinstance(contract, dict) and isinstance(contract.get("price_book"), dict):
            return contract["price_book"], "/planning_contract/price_book"
        return {}, None

    @staticmethod
    def valid_currency(value):
        return value if isinstance(value, str) and CURRENCY_RE.fullmatch(value) else None

    def price(self, item, path):
        raw, price_path, book_entry = item.get("unit_price", MISSING), pointer(path, "unit_price"), None
        if (not isinstance(raw, (str, JsonNumber)) or not PRICE_RE.fullmatch(str(raw))) and item.get("price_id") is not None:
            price_id = item.get("price_id")
            prices = self.book.get("prices") if self.book else None
            if isinstance(price_id, str) and price_id and isinstance(prices, dict) and price_id in prices:
                book_entry = prices[price_id]
                price_path = pointer(pointer(self.book_pointer, "prices"), price_id)
                raw = book_entry.get("unit_price", MISSING) if isinstance(book_entry, dict) else book_entry
        if not isinstance(raw, (str, JsonNumber)):
            return None
        match = PRICE_RE.fullmatch(str(raw))
        if not match:
            return None
        currency = match.group(2)
        currency_path = price_path if currency else None
        if not currency and isinstance(item.get("currency"), str):
            currency, currency_path = self.valid_currency(item["currency"]), pointer(path, "currency")
        if not currency and isinstance(book_entry, dict):
            currency = self.valid_currency(book_entry.get("currency"))
            currency_path = pointer(price_path, "currency") if currency else None
        if not currency and self.book_currency:
            currency, currency_path = self.book_currency, pointer(self.book_pointer, "currency")
        if not currency:
            return {"value": Fraction(match.group(1)), "currency": None}
        denominator = match.group(3)
        if not denominator and isinstance(book_entry, dict) and isinstance(book_entry.get("charging_unit"), str):
            denominator = book_entry["charging_unit"]
        return {"value": Fraction(match.group(1)), "currency": currency, "currency_path": currency_path,
                "denominator": denominator.strip() if denominator else "", "path": price_path, "raw": str(raw)}

    def cost_item(self, item, plan_index, item_index, plan_path):
        path = pointer(pointer(plan_path, "cost_items"), item_index)
        if not isinstance(item, dict):
            self.diag("unsupported_cost_item", path, "cost item must be an object")
            return {"case_json_pointer": path, "status": "unresolved", "reason": "item is not an object"}
        item_id = item.get("item_id") if item.get("item_id") not in (None, "") else item.get("item")
        if not isinstance(item_id, str) or not item_id:
            self.diag("missing_item_id", path, "item_id/item must be a non-empty string")
            item_id = None
        photo = "quantity_per_event" in item or "event_count" in item
        climate = "quantity" in item or "charge_events" in item
        if photo and climate:
            self.diag("ambiguous_formula_schema", path, "photo and climate operand fields cannot be mixed")
        q_name, e_name = ("quantity_per_event", "event_count") if photo or not climate else ("quantity", "charge_events")
        formula = q_name + " * " + e_name + " * unit_price"
        q_raw, e_raw = item.get(q_name), item.get(e_name)
        q, e = exact(q_raw, magnitude=True), exact(e_raw, magnitude=True)
        price = self.price(item, path)
        charging = item.get("charging_unit") or (price or {}).get("denominator") or None
        charged_unit = unit_atom(charging)
        currency = (price or {}).get("currency")
        problems = []
        if q is None:
            problems.append("missing_or_unsupported_" + q_name)
        if e is None:
            problems.append("missing_or_unsupported_" + e_name)
        if price is None:
            problems.append("missing_or_unsupported_unit_price")
        elif currency is None:
            problems.append("currency_unresolved")
        cost = q * e * price["value"] if q is not None and e is not None and price is not None else None
        source_amount = exact(item.get("amount_USD")) if "amount_USD" in item else None
        amount_check = {"status": "not_provided"} if "amount_USD" not in item else {"status": "not_numeric", "raw": text(item.get("amount_USD")), "case_json_pointer": pointer(path, "amount_USD")}
        if source_amount is not None:
            amount_check = {"source_amount_USD": fmt(source_amount), "case_json_pointer": pointer(path, "amount_USD")}
            if currency != "USD":
                amount_check["status"] = "not_compared_without_explicit_USD_unit_price"
            elif cost is None:
                amount_check["status"] = "not_comparable"
            elif cost != source_amount:
                amount_check.update(status="mismatch", derived_cost=fmt(cost))
                problems.append("source_amount_mismatch")
            else:
                amount_check["status"] = "match"
        for problem in problems:
            self.diag(problem, path, problem.replace("_", " "))
        flags = {key: item[key] for key in ("paid", "excluded", "contingency", "payment_status", "cost_class") if key in item}
        row = {
            "line_id": "p%03d_l%03d_%s" % (plan_index, item_index, slug(item_id or item_index)),
            "item_id": item_id, "item": item.get("item") if isinstance(item.get("item"), str) else None,
            "quantity_per_event": fmt(q) if q is not None else None,
            "event_count": fmt(e) if e is not None else None,
            "unit_price": fmt(price["value"]) if price is not None else None,
            "computed_cost": fmt(cost) if cost is not None else None,
            "currency": currency, "case_json_pointer": path, "operator": "mul", "formula": formula,
            "source_formula": item.get("formula"),
            "charged_semantic": charging, "charging_basis": item.get("fixed_cost", item.get("fixed_or_recurrent")),
            "unit_values": {"quantity_per_event": charged_unit, "event_count": "",
                            "unit_price": currency + ("*" + charged_unit + "^-1" if charged_unit else "") if currency else None,
                            "computed_cost": currency},
            "source_operands": {
                q_name: {"raw": text(q_raw), "case_json_pointer": pointer(path, q_name)},
                e_name: {"raw": text(e_raw), "case_json_pointer": pointer(path, e_name)},
                "unit_price": {"raw": (price or {}).get("raw"), "case_json_pointer": (price or {}).get("path")},
                "currency": {"raw": currency, "case_json_pointer": (price or {}).get("currency_path")}},
            "source_amount_check": amount_check, "accounting_flags": flags,
            "status": "pass" if not problems and item_id and q is not None and e is not None and cost is not None else "unresolved",
            "reason": "; ".join(problems + ([] if item_id else ["missing_item_id"])) or None}
        return {"output": row, "q": q, "e": e, "p": price["value"] if price else None,
                "price": price, "cost": cost, "currency": currency, "charging_unit": charged_unit,
                "path": path, "formula": formula, "raw_item": item}

    def staff(self, candidate, plan_path):
        tasks = candidate.get("tasks")
        note = "Only identical task fields and units are summed; roles, machines and duration are not interchangeable; sums are not elapsed time or a schedule."
        if not isinstance(tasks, list):
            return {"status": "not_provided", "groups": [], "unresolved": [], "note": note}
        groups, unresolved = {}, []
        for i, task in enumerate(tasks):
            path = pointer(pointer(plan_path, "tasks"), i)
            if not isinstance(task, dict):
                unresolved.append({"case_json_pointer": path, "reason": "task is not an object"})
                continue
            for field, raw in task.items():
                match = re.fullmatch(r"(.+)_(h|hours|min)", field)
                if not match:
                    continue
                value, unit = exact(raw), match.group(2)
                group = groups.setdefault((field, unit), {"role": match.group(1), "value": Fraction(0), "known": 0, "unknown": 0, "task_ids": set(), "refs": []})
                if value is None:
                    group["unknown"] += 1
                    unresolved.append({"task_id": task.get("task_id"), "field": field, "case_json_pointer": pointer(path, field),
                                       "reason": "missing or non-numeric; not zero-filled"})
                else:
                    group["value"], group["known"] = group["value"] + value, group["known"] + 1
                    if isinstance(task.get("task_id"), str) and task["task_id"]:
                        group["task_ids"].add(task["task_id"])
                    group["refs"].append(pointer(path, field))
        output = []
        for (field, unit), group in sorted(groups.items()):
            partial = bool(group["unknown"])
            output.append({"group_id": "tasks:" + field + ":" + unit, "role": group["role"], "field": field, "unit": unit,
                           "status": "partial" if partial else "pass", "computed_sum": fmt(group["value"]) if group["known"] else None,
                           "known_value_count": group["known"], "unresolved_value_count": group["unknown"],
                           "task_ids": sorted(group["task_ids"]), "case_json_pointers": group["refs"]})
        if unresolved:
            self.diag("unresolved_task_quantities", pointer(plan_path, "tasks"),
                      "Some role/task quantities remain unresolved; no zero substitution", blocking=False, severity="warning")
        return {"status": "partial" if unresolved else "pass", "groups": output, "unresolved": unresolved, "note": note}

    def run(self):
        candidates = self.case.get("candidates") if isinstance(self.case, dict) else None
        if not isinstance(candidates, list):
            self.diag("missing_candidates", "/candidates", "candidates must be an array")
            return
        identities = set()
        for i, candidate in enumerate(candidates):
            path = "/candidates/" + str(i)
            if not isinstance(candidate, dict):
                self.diag("unsupported_candidate", path, "candidate must be an object")
                continue
            plan_id, version = candidate.get("plan_id"), candidate.get("version")
            if not isinstance(plan_id, str) or not plan_id or not isinstance(version, str) or not version:
                self.diag("invalid_plan_identity", path, "plan_id and version must be non-empty strings")
            identity = (plan_id, version)
            if identity in identities:
                self.diag("duplicate_plan_identity", path, "duplicate (plan_id, version)")
            identities.add(identity)
            items = candidate.get("cost_items")
            if not isinstance(items, list):
                self.diag("missing_cost_items", pointer(path, "cost_items"), "cost_items must be an array")
                items = []
            rows = [self.cost_item(item, i, j, path) for j, item in enumerate(items)]
            by_currency = {}
            for row in rows:
                if row["output"]["status"] == "pass" and row["cost"] is not None:
                    by_currency[row["currency"]] = by_currency.get(row["currency"], Fraction(0)) + row["cost"]
            complete = bool(rows) and len(by_currency) == 1 and all(row["output"]["status"] == "pass" for row in rows)
            if len(by_currency) > 1:
                self.diag("mixed_currency", path, "No cross-currency total or conversion was attempted")
                for row in rows: row["output"].update(status="unresolved", reason="; ".join(filter(None, (row["output"]["reason"], "mixed_currency"))))
            total = next(iter(by_currency.values())) if complete else None
            currency = next(iter(by_currency)) if complete else None
            totals = [{"currency": cur, "unit": cur, "computed_cost": fmt(value),
                       "complete_plan_listed_direct_total": complete,
                       "scope": "partial pre-plan-reconciliation subtotal, not a complete plan total" if not complete else "all listed direct lines, including any explicitly paid/excluded/contingency lines"}
                      for cur, value in sorted(by_currency.items())]
            output = {"plan_id": plan_id, "version": version, "case_json_pointer": path,
                      "status": "pass" if complete else "unresolved", "lines": [row["output"] for row in rows],
                      "listed_direct_cost_totals": totals, "staff_and_tasks": self.staff(candidate, path)}
            self.plans.append({"output": output, "rows": rows, "index": i, "path": path,
                               "total": total, "currency": currency, "total_id": "listeddirect_p%03d" % i})

    def parse_policy(self):
        if self.caps is None:
            return None
        if not isinstance(self.caps, dict):
            self.diag("invalid_policy", "$caps", "caps must be an object")
            return {"status": "invalid", "constraints": []}
        constraints = self.caps.get("constraints")
        if not isinstance(constraints, list):
            self.diag("invalid_policy_constraints", "$caps.constraints", "constraints must be an array")
            constraints = []
        seen = set()
        for i, cap in enumerate(constraints):
            path = "$caps.constraints[%d]" % i
            if not isinstance(cap, dict):
                self.diag("invalid_cap", path, "constraint must be an object")
                continue
            identifier = cap.get("id")
            if not isinstance(identifier, str) or not identifier or slug(identifier) in seen:
                self.diag("invalid_cap_id", pointer(path, "id"), "constraint id must be non-empty and unique after normalization")
            seen.add(slug(identifier))
            value, unit = exact(cap.get("value")), cap.get("unit")
            currency, sense = self.valid_currency(cap.get("currency")), cap.get("sense")
            if value is None or unit != currency or currency is None or sense not in ("le", "ge"):
                self.diag("invalid_cap_value", path, "requires exact value, unit == explicit currency, and sense le/ge")
            source = cap.get("source")
            quote = source.get("quote") if isinstance(source, dict) else None
            locator = source.get("locator") if isinstance(source, dict) else None
            resolved = resolve_pointer(self.case, locator) if isinstance(locator, str) and locator else None
            if not resolved or not isinstance(quote, str) or not quote:
                self.diag("invalid_cap_source", pointer(path, "source"), "source locator must resolve in case and quote must be non-empty")
            case_ref = cap.get("case_ref")
            if "case_ref" in cap and (not isinstance(case_ref, str) or not case_ref or not resolve_pointer(self.case, case_ref)):
                self.diag("empty_case_ref", pointer(path, "case_ref"), "case_ref must be a non-empty resolvable case id/reference")
            scope = cap.get("scope")
            selected = list(range(len(self.plans)))
            if scope is not None:
                refs = scope.get("plans") if isinstance(scope, dict) else None
                if isinstance(scope, dict) and "plan_id" in scope:
                    refs = [scope]
                if not isinstance(refs, list) or not refs:
                    self.diag("invalid_cap_scope", pointer(path, "scope"), "scope requires non-empty plans")
                    refs = []
                selected = []
                for ref in refs:
                    matches = [j for j, plan in enumerate(self.plans)
                               if (ref == plan["output"]["plan_id"] if isinstance(ref, str) else
                                   isinstance(ref, dict) and ref.get("plan_id") == plan["output"]["plan_id"] and
                                   ("version" not in ref or ref.get("version") == plan["output"]["version"]))]
                    if not matches:
                        self.diag("unknown_scope_plan", pointer(path, "scope"), "scope plan reference does not resolve")
                    selected.extend(matches)
            if value is not None and currency is not None and sense in ("le", "ge"):
                self.policy.append({"id": identifier, "value": value, "currency": currency, "sense": sense,
                                    "plans": sorted(set(selected)), "locator": resolved, "quote": quote,
                                    "output": {"id": identifier, "value": fmt(value), "unit": unit,
                                               "currency": currency, "sense": sense,
                                               "source": {"locator": locator, "quote": quote}, "results": []}})
        return {"status": "provided", "constraints": [cap["output"] for cap in self.policy]}

    def evaluate_policy(self):
        for cap in self.policy:
            for index in cap["plans"]:
                plan = self.plans[index]
                result = {"plan_id": plan["output"]["plan_id"], "version": plan["output"]["version"], "status": "unresolved"}
                if plan["currency"] != cap["currency"] or plan["total"] is None:
                    result["reason"] = "cost total unavailable or currency/unit mismatch; not treated as feasible"
                    if plan["total"] is not None:
                        self.diag("cap_currency_mismatch", cap["locator"] or "$caps", "A numerical cost cap cannot compare different currency units")
                else:
                    left = plan["total"]
                    passed = left <= cap["value"] if cap["sense"] == "le" else left >= cap["value"]
                    result.update(status="pass" if passed else "fail", computed_cost=fmt(left),
                                  comparison=fmt(left) + (" <= " if cap["sense"] == "le" else " >= ") + fmt(cap["value"]) + " " + cap["currency"])
                cap["output"]["results"].append(result)

    def ev(self, locator, quote, kind):
        locator = "case.json#" + locator if locator.startswith("/") else locator
        key = (locator, quote, kind)
        if key not in self.evidence_keys:
            identifier = "ev%04d" % (len(self.evidence) + 1)
            self.evidence_keys[key] = identifier
            self.evidence[identifier] = {"locator": locator, "quote": quote, "type": kind}
        return self.evidence_keys[key]

    def expression_constraint(self, index, comparison):
        return {"op": "implies", "args": [{"op": "eq", "args": [{"ref": "charge_factor"}, {"const": JsonNumber(str(index))}]}, comparison]}

    def request(self, selection):
        parameters = [{"id": "charge_factor", "type": "integer", "unit": "",
                       "domain": {"allowed_values": [JsonNumber(str(i)) for i in range(len(self.plans))]},
                       "evidence_refs": [self.ev(plan["path"], encode_json({"plan_id": plan["output"]["plan_id"], "version": plan["output"]["version"]}).replace("\n", " "), "case_candidate") for plan in self.plans]}]
        plans, inputs, derived = [], {}, []
        total_refs = {}
        for plan in self.plans:
            i = plan["index"]
            plan_ev = parameters[0]["evidence_refs"][len(plans)]
            plans.append({"plan_id": plan["output"]["plan_id"], "version": plan["output"]["version"],
                          "assignments": {"charge_factor": JsonNumber(str(i))}, "evidence_refs": [plan_ev]})
            refs = []
            for row in plan["rows"]:
                out = row["output"]
                stem = "p%03d_l%03d" % (i, len(refs))
                source = out["source_operands"]
                q_name, e_name = out["formula"].split(" * ")[:2]
                values = []
                for prefix, field, value, unit in (("q", q_name, row["q"], row["charging_unit"]),
                                                   ("n", e_name, row["e"], ""),
                                                   ("price", "unit_price", row["p"], out["unit_values"]["unit_price"])):
                    identifier = prefix + "_" + stem
                    ref = self.ev(source[field]["case_json_pointer"], source[field]["raw"] or "null", "case_cost_operand")
                    ev_refs = [ref]
                    if field == "unit_price" and source["currency"]["case_json_pointer"] != source[field]["case_json_pointer"]:
                        ev_refs.append(self.ev(source["currency"]["case_json_pointer"], row["currency"], "case_currency_contract"))
                    inputs[identifier] = {"status": "known", "type": "number", "nature": "case_cost_operand",
                                          "unit": unit, "value": JsonNumber(fmt(value)), "evidence_refs": ev_refs}
                    values.append({"ref": identifier})
                item_ev = self.ev(row["path"], encode_json(row["raw_item"]).replace("\n", " "), "case_cost_item")
                refs.append(item_ev)
                derived.append({"id": "linecost_" + stem, "unit": row["currency"],
                                "expression": {"op": "mul", "args": values}, "evidence_refs": [item_ev]})
            if len(plan["rows"]) == 1:
                expression = {"ref": "linecost_p%03d_l000" % i}
            else:
                expression = {"op": "add", "args": [{"ref": "linecost_p%03d_l%03d" % (i, j)} for j in range(len(plan["rows"]))]}
            derived.append({"id": plan["total_id"], "unit": plan["currency"], "expression": expression,
                            "evidence_refs": sorted(set(refs))})
            total_refs[i] = {"ref": plan["total_id"]}
        constraints, objectives = [], []
        decision = {"type": "feasibility"}
        for cap in self.policy:
            cap_ev = self.ev(cap["locator"], cap["quote"], "case_policy_constraint")
            for index in cap["plans"]:
                plan = self.plans[index]
                expression = self.expression_constraint(index, {"op": cap["sense"], "args": [total_refs[index],
                    {"const": JsonNumber(fmt(cap["value"])), "unit": cap["currency"]}]})
                constraints.append({"id": slug(cap["id"]) + "__p%03d" % index, "expression": expression,
                                    "evidence_refs": sorted(set([cap_ev, parameters[0]["evidence_refs"][index]]))})
        if selection:
            currency = self.plans[0]["currency"]
            values = sorted({plan["total"] for plan in self.plans})
            target_evs = parameters[0]["evidence_refs"]
            parameters.append({"id": "planningcost_target", "type": "number", "unit": currency,
                               "domain": {"allowed_values": [JsonNumber(fmt(value)) for value in values]},
                               "evidence_refs": target_evs})
            for index, plan in enumerate(self.plans):
                plans[index]["assignments"]["planningcost_target"] = JsonNumber(fmt(plan["total"]))
                constraints.append({"id": "audit_target_reconciles_p%03d" % index,
                    "expression": self.expression_constraint(index, {"op": "eq", "args": [{"ref": "planningcost_target"}, total_refs[index]]}),
                    "evidence_refs": [target_evs[index]]})
            objectives = [{"id": "planning_cost", "unit": currency, "expression": {"ref": "planningcost_target"},
                           "evidence_refs": target_evs}]
            decision = {"type": "single", "objective_id": "planning_cost", "direction": "minimize"}
        return {"schema_version": "1.0.0", "case_id": self.case.get("case_id"), "evidence": self.evidence,
                "parameters": parameters, "plans": plans, "input_values": inputs, "derived_values": derived,
                "objectives": objectives, "constraints": constraints, "decision": decision}

def base_report(case):
    return {"schema_version": "resource-audit-1.0.0", "case_id": case.get("case_id") if isinstance(case, dict) else None,
            "status": "partial", "scope": {"resource_arithmetic": "listed candidates/cost_items only",
            "computed_meaning": "exact prospective arithmetic; not quotes, bookings, approvals, observed responses, or scientific feasibility",
            "total_meaning": "currency-separated sums of supported listed direct lines; never an all-resource grand total",
            "scientific_decision": "not evaluated; cost-only results cannot identify a science winner"},
            "coverage": {"cost_lines": 0, "pass": 0, "unresolved": 0}, "plans": [], "policy": {}, "canonical_request": {}, "diagnostics": []}

HELP = {"name": "audit_resources.py", "usage": "audit_resources.py --case CASE.json --output AUDIT.json [--caps CAPS.json] [--emit-request REQUEST.json]",
        "options": {"--case": "input case JSON (required)", "--output": "audit JSON (required)",
                    "--caps": "optional explicit structured cost policy; no budget inference without it",
                    "--emit-request": "write optimize.py canonical v1.0.0 only when every cost line and policy input is complete"},
        "policy_example": {"constraints": [{"id": "HC1", "value": "3200.00", "unit": "EUR", "currency": "EUR", "sense": "le",
                          "source": {"locator": "/hard_constraints/0", "quote": "planned committed total <= 3200 EUR"}}]},
        "numbers": "source JSON number tokens and decimal strings become Fraction; audit numbers and Fraction results are strings",
        "fallback": "missing, symbolic, mixed-currency, unsupported, or source amount mismatch remains unresolved; no zero/one/default fabrication"}

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        print(encode_json(HELP))
        return 0
    parser = argparse.ArgumentParser(description=HELP["usage"])
    parser.add_argument("--case", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--caps")
    parser.add_argument("--emit-request")
    args = parser.parse_args(argv)
    case, caps, report = None, None, base_report({})
    try:
        case = load_json(args.case)
        report = base_report(case)
        caps = load_json(args.caps) if args.caps else None
        audit = Audit(case, caps)
        audit.run()
        policy = audit.parse_policy()
        audit.evaluate_policy()
        report["plans"] = [plan["output"] for plan in audit.plans]
        report["policy"] = policy or {"status": "not_provided", "constraints": [],
                                      "scope": "resource arithmetic only; no budget or selection policy was inferred"}
        rows = [row["output"] for plan in audit.plans for row in plan["rows"]]
        report["coverage"] = {"cost_lines": len(rows), "pass": sum(row["status"] == "pass" for row in rows),
                              "unresolved": sum(row["status"] != "pass" for row in rows)}
        report["diagnostics"] = audit.diagnostics
        selection = caps.get("selection") if isinstance(caps, dict) else None
        single = selection == {"type": "single", "direction": "minimize"}
        if selection not in (None, {"type": "feasibility"}) and not single:
            audit.diag("unsupported_selection", "$caps.selection", "only default feasibility or explicit {type:single,direction:minimize} is supported")
        if single and (not audit.plans or len({plan["currency"] for plan in audit.plans}) != 1 or any(plan["total"] is None for plan in audit.plans)):
            audit.diag("unsupported_cost_objective", "$caps.selection", "single cost selection requires one common resolved currency in every plan")
        emitted = False
        if args.emit_request:
            if audit.errors or not audit.plans or any(plan["total"] is None for plan in audit.plans) or not isinstance(case.get("case_id"), str) or not case.get("case_id"):
                report["canonical_request"] = {"requested": True, "status": "withheld", "path": args.emit_request,
                                               "reason": "partial/invalid resource arithmetic or policy; no complete canonical request was written"}
            else:
                request = audit.request(single)
                write_json(args.emit_request, request)
                emitted = True
                report["canonical_request"] = {"requested": True, "status": "written", "path": args.emit_request,
                                               "decision": request["decision"], "scope": "cost arithmetic and explicit numerical cost constraints only"}
        else:
            report["canonical_request"] = {"requested": False, "status": "not_requested"}
        report["status"] = "pass" if report["coverage"]["unresolved"] == 0 and not audit.errors else "partial"
        if audit.errors and not emitted:
            report["canonical_request"] = dict(report["canonical_request"], complete_request_available=False)
    except (AuditError, OSError, RecursionError, TypeError, ValueError) as exc:
        report["diagnostics"].append({"code": "invalid_input", "severity": "error", "case_json_pointer": "$", "message": str(exc)})
        report["canonical_request"] = {"requested": bool(args.emit_request), "status": "withheld" if args.emit_request else "not_requested"}
        if args.emit_request:
            report["canonical_request"]["path"] = args.emit_request
        write_json(args.output, report)
        print(encode_json({"case_id": report["case_id"], "status": report["status"], "output": args.output, "error": str(exc)}))
        return 2
    write_json(args.output, report)
    print(encode_json({"case_id": report["case_id"], "status": report["status"], "coverage": report["coverage"],
                       "output": args.output, "canonical_request": report["canonical_request"]["status"]}))
    return 0

if __name__ == "__main__":
    sys.exit(main())
