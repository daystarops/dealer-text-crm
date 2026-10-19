"""Local auditable lead-eligibility administration; never sends messages."""
import argparse
from pathlib import Path
from identity_vault import default_vault
ROOT=Path(__file__).resolve().parents[1]
def main()->int:
    p=argparse.ArgumentParser();p.add_argument("lead_key");p.add_argument("status");p.add_argument("basis");p.add_argument("source");p.add_argument("timestamp");a=p.parse_args()
    vault=default_vault(ROOT)
    try:vault.set_eligibility(a.lead_key,a.status,a.basis,a.source,a.timestamp)
    finally:vault.close()
    return 0
if __name__=="__main__":raise SystemExit(main())
