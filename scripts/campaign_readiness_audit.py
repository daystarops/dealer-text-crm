from pathlib import Path
from campaign_readiness import audit,write_outputs
ROOT=Path(__file__).resolve().parents[1]
if __name__=="__main__":
    candidates,summary=audit(ROOT);paths=write_outputs(ROOT,candidates,summary)
    print({"summary":summary,"outputs":[str(p) for p in paths]})
