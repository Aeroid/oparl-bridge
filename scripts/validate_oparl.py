#!/usr/bin/env python3
"""Validate the running oparl-bridge API against OParl 1.1 JSON schemas."""

import json
import sys
from collections import defaultdict

import httpx
import jsonschema

API_BASE = "http://localhost:8000/oparl/v1.1"
SCHEMA_BASE = "https://raw.githubusercontent.com/OParl/spec/master/schema"
SCHEMA_TYPES = [
    "System", "Body", "Organization", "Meeting",
    "AgendaItem", "Paper", "File", "Person", "Membership", "Consultation",
]

# Fields that use OParl-specific "url" format — not standard URI, skip format check
FORMAT_SKIP = {"url"}


def fetch_schemas() -> dict[str, dict]:
    schemas = {}
    print("Fetching OParl 1.1 schemas...")
    with httpx.Client(follow_redirects=True, timeout=15) as client:
        for name in SCHEMA_TYPES:
            r = client.get(f"{SCHEMA_BASE}/{name}.json")
            if r.status_code == 200:
                schemas[name] = r.json()
            else:
                print(f"  WARNING: could not fetch {name}.json ({r.status_code})")
    print(f"  {len(schemas)} schemas loaded.\n")
    return schemas


def make_validator(schema: dict) -> jsonschema.Draft4Validator:
    def strip_custom(s):
        if not isinstance(s, dict):
            return s
        return {
            k: strip_custom(v)
            for k, v in s.items()
            if k not in {"references", "backreference", "cardinality", "schema"}
        }

    clean = strip_custom(schema)
    clean_str = json.dumps(clean).replace('"format": "url"', '"format": "uri"')
    clean = json.loads(clean_str)
    return jsonschema.Draft4Validator(clean, format_checker=None)


def is_external_ref_error(error: jsonschema.ValidationError) -> bool:
    """Return True if this error is just an OParl external reference (URL string where
    the schema expects an object or array of objects) — these are valid per OParl spec."""
    val = error.instance
    # Single external ref: string URL where object expected
    if error.validator == "type" and error.validator_value == "object" and isinstance(val, str):
        return True
    # Array item external ref
    if (error.validator == "type" and error.validator_value == "object"
            and isinstance(val, str) and val.startswith("http")):
        return True
    return False


def paginate(client: httpx.Client, url: str):
    """Yield all objects from a paginated OParl list endpoint."""
    while url:
        r = client.get(url)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} for {url}")
            return
        data = r.json()
        yield from data.get("data", [])
        links = data.get("links", {})
        url = links.get("next")


def type_name(obj: dict) -> str | None:
    t = obj.get("type", "")
    if t.startswith("https://schema.oparl.org/1.1/"):
        return t.split("/")[-1]
    return None


def main():
    schemas = fetch_schemas()
    validators = {name: make_validator(s) for name, s in schemas.items()}

    errors: dict[str, list[str]] = defaultdict(list)
    counts: dict[str, int] = defaultdict(int)

    with httpx.Client(base_url=API_BASE, follow_redirects=True, timeout=30) as client:
        # System
        r = client.get("/")
        system = r.json()
        counts["System"] += 1
        for e in validators["System"].iter_errors(system):
            if not is_external_ref_error(e):
                errors["System"].append(f"  /: {e.json_path} — {e.message}")

        body_url = system.get("body", "")

        # Body list
        for body in paginate(client, body_url):
            counts["Body"] += 1
            for e in validators["Body"].iter_errors(body):
                if not is_external_ref_error(e):
                    errors["Body"].append(f"  body/{body.get('id','?')}: {e.json_path} — {e.message}")

            for list_url, schema_name in [
                (body.get("organization"), "Organization"),
                (body.get("meeting"),      "Meeting"),
                (body.get("paper"),        "Paper"),
                (body.get("person"),       "Person"),
            ]:
                if not list_url:
                    continue
                for obj in paginate(client, list_url):
                    name = type_name(obj) or schema_name
                    if name not in validators:
                        continue
                    counts[name] += 1
                    for e in validators[name].iter_errors(obj):
                        if is_external_ref_error(e):
                            continue
                        oid = obj.get("id", "?")
                        errors[name].append(f"  {oid}: {e.json_path} — {e.message}")

    # Report
    total_errors = sum(len(v) for v in errors.values())
    print("=" * 60)
    print("OParl 1.1 Validation Report")
    print("=" * 60)
    for name in SCHEMA_TYPES:
        c = counts.get(name, 0)
        e = errors.get(name, [])
        status = "OK" if not e else f"{len(e)} error(s)"
        print(f"  {name:<18} {c:>5} objects   {status}")
        for msg in e[:5]:
            print(msg)
        if len(e) > 5:
            print(f"    ... and {len(e)-5} more")
    print("=" * 60)
    print(f"Total: {sum(counts.values())} objects, {total_errors} error(s)")
    sys.exit(0 if total_errors == 0 else 1)


if __name__ == "__main__":
    main()
