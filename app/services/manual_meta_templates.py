"""Validate official WhatsApp templates and build a provider-native payload.

Only supported, approved templates are exposed to the manual inbox. Never
use examples as real customer values or convert a Meta template into free text.
"""
import json
import re

VAR = re.compile(r"\{\{([1-9][0-9]*)\}\}")


def eligible_templates(rows):
    result = []
    for row in rows:
        if row.get("status") != "APPROVED" or not row.get("id") or not row.get("name") or not row.get("language"):
            continue
        if row.get("category") not in {"MARKETING", "UTILITY"}:
            continue
        components = row.get("components") or []
        if not isinstance(components, list) or not all(isinstance(c, dict) for c in components):
            continue
        bodies = [c for c in components if c.get("type") == "BODY"]
        if len(bodies) != 1:
            continue
        body = bodies[0].get("text") or ""
        if not isinstance(body, str) or not body:
            continue
        variables = sorted(set(int(v) for v in VAR.findall(body)))
        rest = VAR.sub("", body)
        if "{" in rest or "}" in rest or variables != list(range(1, len(variables) + 1)):
            continue
        supported = True
        for c in components:
            kind = c.get("type")
            if kind == "BODY":
                continue
            if kind in {"HEADER", "FOOTER"}:
                value = c.get("text") or ""
                if not isinstance(value, str) or "{" in value or "}" in value or (kind == "HEADER" and c.get("format") != "TEXT"):
                    supported = False
            elif kind == "BUTTONS":
                buttons = c.get("buttons") or []
                if not isinstance(buttons, list) or any(not isinstance(btn, dict) or btn.get("type") != "QUICK_REPLY" or not btn.get("text") for btn in buttons):
                    supported = False
            else:
                supported = False
        if supported:
            result.append({"id": str(row["id"]), "name": row["name"], "language": row["language"],
                           "category": row["category"], "body": body, "variables": variables,
                           "components": components})
    return result


def template_payload(row, raw_parameters):
    try:
        values = json.loads(raw_parameters or "[]")
    except (TypeError, ValueError) as exc:
        raise ValueError("Parâmetros do template inválidos.") from exc
    if not isinstance(values, list) or len(values) != len(row["variables"]):
        raise ValueError("Preencha todas as variáveis do template.")
    if any(not isinstance(v, str) or not v.strip() or len(v) > 1024 or "\n" in v or "\r" in v or "\t" in v for v in values):
        raise ValueError("Cada variável deve conter texto válido em uma única linha.")
    values = [v.strip() for v in values]
    template = {"name": row["name"], "language": {"code": row["language"]}}
    components = []
    if values:
        components.append({"type": "body", "parameters": [{"type": "text", "text": v} for v in values]})
    for component in row["components"]:
        if component.get("type") == "BUTTONS":
            for index, button in enumerate(component["buttons"]):
                components.append({"type": "button", "sub_type": "quick_reply", "index": str(index),
                                   "parameters": [{"type": "payload", "payload": button["text"]}]})
    if components:
        template["components"] = components
    body = VAR.sub(lambda m: values[int(m.group(1)) - 1], row["body"])
    return template, body
