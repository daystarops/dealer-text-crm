"""Run identifier and analysis workspace path validation."""
import re
from pathlib import Path
RUN_ID_RE=re.compile(r"^cdk-[0-9]{8}T[0-9]{6}-[a-f0-9]{8}$")
def validate_run_id(value:str)->str:
    if not isinstance(value,str) or len(value)>36 or not RUN_ID_RE.fullmatch(value):raise ValueError("invalid run_id")
    return value
def analysis_workspace(base:Path,run_id:str)->Path:
    validate_run_id(run_id);parent=base.resolve();candidate=(parent/run_id).resolve()
    if candidate.parent!=parent:raise ValueError("analysis workspace escaped its parent")
    return candidate
