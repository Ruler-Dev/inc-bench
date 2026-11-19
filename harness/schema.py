"""
Validatore JSON Schema minimale (sottoinsieme) - nessuna dipendenza.

Supporta: type, properties, required, additionalProperties:false, items,
enum, minimum, maximum, minItems.  Per progetti veri usa il pacchetto `jsonschema`.
"""
from __future__ import annotations

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "number": (int, float), "null": type(None),
}


def validate(value, schema: dict, path: str = "$") -> list[str]:
    errors: list[str] = []
    t = schema.get("type")
    if t:
        types = t if isinstance(t, list) else [t]
        ok = any(isinstance(value, _TYPES[x]) and not (x in ("integer", "number")
                 and isinstance(value, bool)) for x in types)
        if not ok:
            return [f"{path}: atteso {t}, trovato {type(value).__name__} ({value!r})"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} non in {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} < minimo {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} > massimo {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errors.append(f"{path}: manca il campo obbligatorio '{k}'")
        for k, v in value.items():
            if k in props:
                errors += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: campo non previsto '{k}' (allucinato?)")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: servono almeno {schema['minItems']} elementi")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors
