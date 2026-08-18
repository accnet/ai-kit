"""Deterministic semantic compatibility primitives for normalized contracts."""

from __future__ import annotations


def indexed_contract_items(payload: dict, key: str) -> dict[str, dict]:
    return {
        str(item.get("name")): item
        for item in payload.get(key) or []
        if isinstance(item, dict) and item.get("name")
    }


def enum_narrowed(previous: object, current: object) -> bool:
    old_values = previous if isinstance(previous, list) else None
    new_values = current if isinstance(current, list) else None
    if new_values is None:
        return False
    if old_values is None:
        return True
    return bool(old_values) and not set(old_values).issubset(set(new_values))


def schema_shape_breaks(previous: object, current: object) -> bool:
    old = previous if isinstance(previous, dict) else {}
    new = current if isinstance(current, dict) else {}
    if tuple(old.get(key) for key in ("ref", "type", "format")) != tuple(new.get(key) for key in ("ref", "type", "format")):
        return True
    if enum_narrowed(old.get("enum"), new.get("enum")):
        return True
    old_required, new_required = set(old.get("required") or []), set(new.get("required") or [])
    if not new_required.issubset(old_required):
        return True
    old_properties = old.get("properties") or {}
    new_properties = new.get("properties") or {}
    if isinstance(old_properties, dict) and isinstance(new_properties, dict):
        for name, old_property in old_properties.items():
            if name not in new_properties or schema_shape_breaks(old_property, new_properties[name]):
                return True
    if "items" in old and ("items" not in new or schema_shape_breaks(old.get("items"), new.get("items"))):
        return True
    if old.get("additional_properties") is True and new.get("additional_properties") is False:
        return True
    for key in ("one_of", "any_of", "all_of"):
        if (key in old and old.get(key) != new.get(key)) or (key in new and key not in old):
            return True
    return False


def _media_content_index(value: object) -> dict[str, dict]:
    return {
        str(item.get("media_type")): item.get("schema") or {}
        for item in (value if isinstance(value, list) else [])
        if isinstance(item, dict) and item.get("media_type")
    }


def _security_alternative(item: object) -> dict[str, set[str]]:
    if not isinstance(item, dict):
        return {}
    return {
        str(scheme.get("name")): set(str(scope) for scope in (scheme.get("scopes") or []))
        for scheme in item.get("schemes") or []
        if isinstance(scheme, dict) and scheme.get("name")
    }


def _auth_requirement_tightened(previous: object, current: object) -> bool:
    old = [_security_alternative(item) for item in (previous if isinstance(previous, list) else [])]
    new = [_security_alternative(item) for item in (current if isinstance(current, list) else [])]
    if not old:
        return bool(new) and not any(not alternative for alternative in new)
    if not new:
        return False
    for old_alternative in old:
        if not any(
            set(new_alternative).issubset(set(old_alternative))
            and all(new_alternative[name].issubset(old_alternative[name]) for name in new_alternative)
            for new_alternative in new
        ):
            return True
    return False


def security_scheme_breaks(previous: dict, current: dict) -> bool:
    keys = ("type", "scheme", "bearer_format", "location", "parameter", "open_id_connect_url")
    if tuple(previous.get(item) for item in keys) != tuple(current.get(item) for item in keys):
        return True
    old_flows, new_flows = previous.get("flows") or {}, current.get("flows") or {}
    if not isinstance(old_flows, dict) or not isinstance(new_flows, dict):
        return old_flows != new_flows
    for name, old_flow in old_flows.items():
        new_flow = new_flows.get(name)
        if not isinstance(old_flow, dict) or not isinstance(new_flow, dict):
            return True
        if tuple(old_flow.get(item) for item in ("authorization_url", "token_url", "refresh_url")) != tuple(new_flow.get(item) for item in ("authorization_url", "token_url", "refresh_url")):
            return True
        if not set(old_flow.get("scopes") or []).issubset(set(new_flow.get("scopes") or [])):
            return True
    return False


def contract_field_breaks(entity: str, previous: dict, current: dict) -> list[dict]:
    findings: list[dict] = []
    old_fields = indexed_contract_items({"fields": previous.get("fields") or []}, "fields")
    new_fields = indexed_contract_items({"fields": current.get("fields") or []}, "fields")
    for name, old_field in sorted(old_fields.items()):
        candidate = new_fields.get(name)
        location = f"{entity}.{name}"
        if not candidate:
            findings.append({"kind": "field-removed", "severity": "breaking", "entity": location, "detail": "field was removed"})
            continue
        if tuple(old_field.get(key) for key in ("type", "format", "ref")) != tuple(candidate.get(key) for key in ("type", "format", "ref")):
            findings.append({"kind": "field-shape-changed", "severity": "breaking", "entity": location, "detail": "type, format, or reference changed"})
        if not old_field.get("required") and candidate.get("required"):
            findings.append({"kind": "field-now-required", "severity": "breaking", "entity": location, "detail": "optional field became required"})
        if enum_narrowed(old_field.get("enum"), candidate.get("enum")):
            detail = "field gained an enum, rejecting previously accepted values" if not isinstance(old_field.get("enum"), list) else "new enum excludes one or more previous values"
            findings.append({"kind": "enum-narrowed", "severity": "breaking", "entity": location, "detail": detail})
    for name, candidate in sorted(new_fields.items()):
        if name not in old_fields and candidate.get("required"):
            findings.append({"kind": "required-field-added", "severity": "breaking", "entity": f"{entity}.{name}", "detail": "new required field was added"})
    return findings


def operation_semantic_breaks(key: str, previous: dict, current: dict) -> list[dict]:
    findings: list[dict] = []
    old_request, new_request = previous.get("request"), current.get("request")
    if not isinstance(old_request, dict) and isinstance(new_request, dict) and new_request.get("required"):
        findings.append({"kind": "required-request-body-added", "severity": "breaking", "entity": key, "detail": "operation now requires a request body"})
    elif isinstance(old_request, dict) and isinstance(new_request, dict):
        if not old_request.get("required") and new_request.get("required"):
            findings.append({"kind": "request-body-now-required", "severity": "breaking", "entity": key, "detail": "optional request body became required"})
        old_content, new_content = _media_content_index(old_request.get("content")), _media_content_index(new_request.get("content"))
        for media_type in sorted(set(old_content) - set(new_content)):
            findings.append({"kind": "request-media-type-removed", "severity": "breaking", "entity": f"{key} request {media_type}", "detail": "accepted request media type was removed"})
        for media_type in sorted(set(old_content) & set(new_content)):
            if schema_shape_breaks(old_content[media_type], new_content[media_type]):
                findings.append({"kind": "request-schema-changed", "severity": "breaking", "entity": f"{key} request {media_type}", "detail": "request schema became incompatible"})
    old_responses = {str(item.get("status")): item for item in previous.get("responses") or [] if isinstance(item, dict) and item.get("status") is not None}
    new_responses = {str(item.get("status")): item for item in current.get("responses") or [] if isinstance(item, dict) and item.get("status") is not None}
    for status in sorted(set(old_responses) - set(new_responses)):
        findings.append({"kind": "response-status-removed", "severity": "breaking", "entity": f"{key} response {status}", "detail": "documented response status was removed"})
    for status in sorted(set(new_responses) - set(old_responses)):
        if new_responses[status].get("category") in {"client-error", "server-error", "default"}:
            findings.append({"kind": "error-status-added", "severity": "breaking", "entity": f"{key} response {status}", "detail": "operation added a documented error outcome"})
    for status in sorted(set(old_responses) & set(new_responses)):
        old_content, new_content = _media_content_index(old_responses[status].get("content")), _media_content_index(new_responses[status].get("content"))
        for media_type in sorted(set(old_content) - set(new_content)):
            findings.append({"kind": "response-media-type-removed", "severity": "breaking", "entity": f"{key} response {status} {media_type}", "detail": "response media type was removed"})
        for media_type in sorted(set(old_content) & set(new_content)):
            if schema_shape_breaks(old_content[media_type], new_content[media_type]):
                category = old_responses[status].get("category")
                kind = "error-schema-changed" if category in {"client-error", "server-error", "default"} else "response-schema-changed"
                findings.append({"kind": kind, "severity": "breaking", "entity": f"{key} response {status} {media_type}", "detail": "response schema became incompatible"})
    if _auth_requirement_tightened(previous.get("auth"), current.get("auth")):
        findings.append({"kind": "auth-requirement-tightened", "severity": "breaking", "entity": key, "detail": "operation requires additional authentication schemes or scopes"})
    return findings


def event_semantic_breaks(key: str, previous: dict, current: dict) -> list[dict]:
    findings: list[dict] = []
    old_messages = {str(item.get("name")): item for item in previous.get("messages") or [] if isinstance(item, dict) and item.get("name")}
    new_messages = {str(item.get("name")): item for item in current.get("messages") or [] if isinstance(item, dict) and item.get("name")}
    for name in sorted(set(old_messages) - set(new_messages)):
        findings.append({"kind": "event-message-removed", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event message was removed"})
    for name in sorted(set(new_messages) - set(old_messages)):
        findings.append({"kind": "event-message-added", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event stream added a message variant consumers may not handle"})
    for name in sorted(set(old_messages) & set(new_messages)):
        old_message, new_message = old_messages[name], new_messages[name]
        if old_message.get("content_type") != new_message.get("content_type"):
            findings.append({"kind": "event-content-type-changed", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event content type changed"})
        if schema_shape_breaks(old_message.get("payload"), new_message.get("payload")):
            findings.append({"kind": "event-payload-changed", "severity": "breaking", "entity": f"{key} message {name}", "detail": "event payload became incompatible"})
    return findings
