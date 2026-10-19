"""Cross-file semantic validation for model analysis output."""
import json
from pathlib import Path

def validate_semantics(result_path:Path,extraction:dict|Path)->dict:
    result=json.loads(result_path.read_text(encoding="utf-8"))
    source=json.loads(extraction.read_text(encoding="utf-8")) if isinstance(extraction,Path) else extraction
    expected={x["lead_key"]:x for x in source["leads"]};ranked=result["ranked_leads"];keys=[x["lead_key"] for x in ranked]
    if result["run_id"]!=source["run_id"]:raise ValueError("analysis run_id mismatch")
    if len(ranked)!=len(expected) or (expected and not ranked):raise ValueError("analysis lead count mismatch")
    if len(keys)!=len(set(keys)):raise ValueError("duplicate analysis lead_key")
    if set(keys)!=set(expected):raise ValueError("analysis lead keys do not match extraction")
    if any(x.get("first_name")!=expected[x["lead_key"]].get("first_name") for x in ranked):raise ValueError("analysis first_name mismatch")
    result["ranked_leads"]=sorted(ranked,key=lambda x:(-x["priority_score"],x["lead_key"]))
    result_path.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    return result
