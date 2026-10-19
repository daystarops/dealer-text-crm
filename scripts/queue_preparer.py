"""Prepare eligible analyzed messages in the local queue; never send them."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from identity_vault import default_vault
from messaging import MessageQueue, QueueRequest
from sender_config import load_sender_config

ROOT=Path(__file__).resolve().parents[1]


def prepare(analysis_path: Path) -> dict:
    analysis=json.loads(analysis_path.read_text(encoding="utf-8"))
    status_path=ROOT/"private"/"queue-status.json";status_path.parent.mkdir(parents=True,exist_ok=True)
    try:config=load_sender_config(ROOT/"private"/"personal-sender.json")
    except (FileNotFoundError,ValueError):
        status={"newly_queued":0,"already_existing":0,"skipped_ineligible":0,"blocked":0,"sender_not_configured":True,"sent":0};status_path.write_text(json.dumps(status,indent=2)+"\n",encoding="utf-8");return status
    status={"newly_queued":0,"already_existing":0,"skipped_ineligible":0,"blocked":0,"sender_not_configured":False,"sent":0};vault=default_vault(ROOT);queue=MessageQueue(ROOT/"private"/"message-queue.db")
    try:
        for lead in analysis["ranked_leads"]:
            identity=vault.get(lead["lead_key"])
            if not identity or identity.sms_eligibility_status not in {"ELIGIBLE","DOCUMENTED"} or identity.do_not_contact:
                status["skipped_ineligible"]+=1;continue
            request=QueueRequest(lead["lead_key"],identity.phone_e164,config.sender_identity_key,lead["follow_up_text"],"SMS",identity.eligibility_basis,config.default_automation_mode,identity.conversation_thread_id,bool(lead["human_handoff_flags"] or lead["messaging_risk_flags"]))
            try:outcome=queue.enqueue(request,vault)
            except ValueError:
                status["blocked"]+=1;continue
            if outcome.created_new:status["newly_queued"]+=1
            elif outcome.deduplicated:status["already_existing"]+=1
            elif outcome.blocked_reason:status["blocked"]+=1
    finally:queue.close();vault.close()
    status_path.write_text(json.dumps(status,indent=2)+"\n",encoding="utf-8");return status
def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("analysis",type=Path);args=parser.parse_args()
    print(json.dumps(prepare(args.analysis)));return 0


if __name__=="__main__":raise SystemExit(main())
