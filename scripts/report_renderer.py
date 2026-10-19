"""Deterministic local operational HTML and redacted audit Markdown rendering."""
from __future__ import annotations

import html
import json
import os,subprocess
from pathlib import Path

from identity_vault import IdentityVault
from privacy_gate import assert_no_phone, sanitize_text


def secure_current_user_acl(path:Path)->None:
    if os.name != "nt": return
    user=os.environ.get("USERNAME")
    if not user: raise RuntimeError("current Windows user is unavailable")
    completed=subprocess.run(["icacls",str(path),"/inheritance:r","/grant:r",f"{user}:F"],capture_output=True,text=True,timeout=15)
    if completed.returncode: path.unlink(missing_ok=True);raise RuntimeError("could not secure operational report ACL")

def render_call_brief(lead:dict)->str:
    questions="".join(f"<li>{html.escape(item)}</li>" for item in lead.get("discovery_questions",["","",""]))
    objections="".join(f"<li><strong>{html.escape(item['objection'])}</strong>: {html.escape(item['response'])}</li>" for item in lead.get("objections",[{"objection":"","response":""},{"objection":"","response":""}]))
    return (f"<h4>Customer position</h4><p>{html.escape(lead.get('latest_explicit_customer_position',''))}</p>"
      f"<h4>Main obstacle</h4><p>{html.escape(lead.get('main_obstacle',''))}</p>"
      f"<h4>Recommended next action</h4><p>{html.escape(lead.get('recommended_next_action',''))}</p>"
      f"<h4>Call opening</h4><p>{html.escape(lead.get('call_opening',''))}</p><h4>Discovery questions</h4><ol>{questions}</ol>"
      f"<h4>Objections</h4><ol>{objections}</ol><h4>Voicemail</h4><p>{html.escape(lead.get('voicemail',''))}</p>"
      f"<h4>Follow-up text</h4><p>{html.escape(lead['follow_up_text'])}</p><h4>Appointment close</h4><p>{html.escape(lead.get('appointment_close',''))}</p>"
      f"<h4>Human-handoff flags</h4><p>{html.escape(', '.join(lead.get('human_handoff_flags',[])) or 'None')}</p>"
      f"<h4>Messaging-risk flags</h4><p>{html.escape(', '.join(lead.get('messaging_risk_flags',[])) or 'None')}</p>")
def render_operational_html(analysis: dict, vault: IdentityVault, output: Path, acl_applier=secure_current_user_acl) -> None:
    rows=[]
    for lead in analysis["ranked_leads"]:
        identity=vault.get(lead["lead_key"])
        if not identity: raise KeyError(f"Missing operational identity for lead_key {lead['lead_key']}")
        fields=[identity.full_name,identity.phone_e164,identity.cdk_opportunity_id,identity.vehicle,
                str(lead["priority_score"]),render_call_brief(lead)]
        buttons=" ".join(f"<button type='button' disabled aria-disabled='true' title='Preview only: action service not installed' data-preview-action='{html.escape(action)}'>{html.escape(label)}</button>" for action,label in (
            ("call","Call"),("text","Text"),("open-cdk","Open CDK"),("approve","Approve"),("skip","Skip"),
            ("pause","Pause automation"),("dnc","Mark do-not-contact")))
        rows.append("<tr>"+"".join((f"<td>{value}</td>" if index==5 else f"<td>{html.escape(str(value))}</td>") for index,value in enumerate(fields))+f"<td>{buttons}</td></tr>")
    document="<!doctype html><meta charset='utf-8'><title>CDK Operational Inbox</title><h1>Local Operational Inbox</h1><p><strong>Sensitive temporary operational artifact: current Windows user only.</strong></p><table><thead><tr><th>Name</th><th>Phone</th><th>Opportunity</th><th>Vehicle</th><th>Priority</th><th>Complete live call brief</th><th>Inactive preview controls</th></tr></thead><tbody>"+"".join(rows)+"</tbody></table>"
    output.parent.mkdir(parents=True,exist_ok=True)
    temporary=output.with_name(output.name+".tmp")
    temporary.write_text(document,encoding="utf-8")
    replaced=False
    try:
        # Secure the source before it becomes the public filename, then re-apply
        # after replacement so a platform-specific rename cannot weaken the ACL.
        acl_applier(temporary)
        output.write_text(document,encoding="utf-8")
        temporary.unlink()
        replaced=True
        acl_applier(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        if replaced:
            output.unlink(missing_ok=True)
        raise


def render_audit_markdown(analysis: dict, vault: IdentityVault, output: Path, identities: dict[str,str]) -> None:
    lines=["# CDK Lead Copilot Audit Report","",f"Run: `{analysis['run_id']}`",""]
    for rank,lead in enumerate(analysis["ranked_leads"],1):
        identity=vault.get(lead["lead_key"])
        if not identity:raise KeyError(f"Missing audit identity for lead_key {lead['lead_key']}")
        lines += [f"## {rank}. {identity.full_name} | Opportunity {identity.cdk_opportunity_id} | Score {lead['priority_score']}",
                  "",f"- Position: {sanitize_text(lead['latest_explicit_customer_position'],identities)}",
                  f"- Obstacle: {sanitize_text(lead['main_obstacle'],identities)}",
                  f"- Next action: {sanitize_text(lead['recommended_next_action'],identities)}",""]
    report="\n".join(lines); assert_no_phone(report)
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(report,encoding="utf-8")
