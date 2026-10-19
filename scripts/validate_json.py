"""Validate a JSON document, using jsonschema when it is installed."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from cdk_review_driver import validate_shape


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: validate_json.py DOCUMENT SCHEMA", file=sys.stderr); return 2
    document=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    schema=json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    try:
        import jsonschema  # type: ignore
    except ImportError:
        errors=validate_shape(document,schema)
        if errors: print("\n".join(errors),file=sys.stderr); return 1
    else:
        jsonschema.Draft202012Validator(schema).validate(document)
    if "leads" in document and (document.get("reviewed_count") != len(document["leads"]) or document.get("batch_size") != len(document["leads"])):
        print("batch_size, reviewed_count, and lead count must match", file=sys.stderr); return 1
    print(f"Valid: {sys.argv[1]}"); return 0


if __name__ == "__main__": raise SystemExit(main())
