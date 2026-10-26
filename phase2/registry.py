"""Load and pin the finite, repository-controlled workflow and output contracts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from phase2.doc_policy import instructions, validate_rows, VERSION

ROOT = Path(__file__).resolve().parent
OPERATIONS = {"create_artifact", "revise_artifact", "investigate", "refresh_artifact", "review_artifact", "reconcile_package"}


def package_manifest(manifest):
    """Pin selective revision contracts alongside the usual publication schemas."""
    for key in manifest['steps']:
        if key=='review':
            manifest['prompts'][key]+='\nReview the resulting complete package against the reconcile instruction and standing decisions. Report remaining gaps; completion does not grant publication approval.'
            continue
        manifest['schemas'][key]={'anyOf':[manifest['schemas'][key],{
            'type':'object','properties':{'action':{'const':'keep'},'reason':{'type':'string','minLength':1,'maxLength':2000}},
            'required':['action','reason'],'additionalProperties':False}]}
        manifest['prompts'][key]+='''\nThis is a package reconciliation. Assess the current structured output AND actual Google Doc snapshots against the instruction, active decisions and updated dependencies. If the existing document remains accurate, complete and aligned, return only {"action":"keep","reason":"specific assessment"}. Keep requires an existing structured version, unchanged managed text and no content-policy findings. Otherwise return the complete revised artifact using the normal schema, or create it if missing. Substantial rewrites are authorized where necessary. Preserve supported evidence, qualifications and human annotations; never invent missing facts. Do not rewrite solely to change wording. Explain material changes in the artifact's editorial rows. All later documents will be assessed against the resulting upstream versions.'''
    manifest.pop('hash',None)
    manifest['hash']=digest(manifest)
    return manifest


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def snapshot():
    manifest = json.loads((ROOT / "workflows/initial.v1.json").read_text())
    registry = json.loads((ROOT / "prompts/registry.json").read_text())
    manifest["prompts"] = {
        key: (ROOT / "prompts" / entry["prompt"]).read_text()
        for key, entry in registry.items()
    }
    manifest["schemas"] = {
        key: json.loads((ROOT / "schemas" / entry["schema"]).read_text())
        for key, entry in registry.items()
    }
    manifest["policy"] = (ROOT / "prompts/evidence-policy.md").read_text()
    manifest["code_hash"] = digest({str(p.relative_to(ROOT)): p.read_text() for p in sorted(ROOT.rglob('*.py'))})
    manifest["hash"] = digest(manifest)
    return apply_content_policy(manifest)


def apply_content_policy(original):
    """Apply the current publication contract even to runs queued by older API code.

    Preserve the original manifest/hash for provenance; engine, steps and budgets
    remain pinned. The effective contract is hashed and saved with step inputs.
    """
    if original.get('content_policy_version')==VERSION:
        return original
    import copy
    manifest=copy.deepcopy(original)
    manifest['base_workflow_hash']=original.get('hash','')
    manifest["content_policy_version"] = VERSION
    for key, schema in manifest["schemas"].items():
        schema["properties"]["editorial_rows"] = {"type":"array","minItems":1,"maxItems":100,"items":{"type":"object","properties":{field:{"type":"string","minLength":1,"maxLength":12000} for field in ["topic","short","detail"]},"required":["topic","short","detail"],"additionalProperties":False}}
        schema["properties"]["policy_exception"] = {"type":"string","maxLength":2000}
        schema["required"] += ["editorial_rows","policy_exception"]
        manifest["prompts"][key] += "\n" + instructions(key)
    manifest["schemas"]["review"]["properties"]["policy_checks"] = {"type":"object"}
    manifest["schemas"]["review"]["required"].append("policy_checks")
    manifest["code_hash"] = digest({str(p.relative_to(ROOT)): p.read_text() for p in sorted(ROOT.rglob('*.py'))})
    manifest.pop('hash',None)
    manifest['hash']=digest(manifest)
    return manifest


def validate_output(manifest, key, output):
    schema = manifest["schemas"][key]
    Draft202012Validator(schema).validate(output)
    if output.get('action')=='keep':
        return output
    sources = {s["id"] for s in output["sources"]}
    if len(sources) != len(output["sources"]):
        raise ValueError("Source identifiers must be unique")
    claims = {c["id"] for c in output["claims"]}
    if len(claims) != len(output["claims"]):
        raise ValueError("Claim identifiers must be unique")
    for source in output["sources"]:
        from urllib.parse import urlsplit
        parsed = urlsplit(source["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Sources require public HTTP(S) URLs")
    for claim in output["claims"]:
        if not set(claim["source_ids"]) <= sources:
            raise ValueError("Claim refers to an unknown source")
        if claim["label"] in {"Verified", "Reported", "Alleged"} and not claim["source_ids"]:
            raise ValueError("Factual claims require claim-level source support")
    for scene in output.get("scenes", []):
        if not set(scene["claim_ids"]) <= claims:
            raise ValueError("Treatment scene refers to an unknown claim")
    for visual in output.get("visuals", []):
        if visual["source_id"] not in sources:
            raise ValueError("Visual candidates require a registered source")
    if key=='review' and output['readiness']=='ready_for_human_review' and any(f['severity']=='critical' for f in output['findings']):
        raise ValueError('Critical findings prevent readiness')
    if manifest.get("content_policy_version"):
        validate_rows(key, output)
    return output


def dependents(manifest, key):
    result = set()
    while True:
        found = {k for k, v in manifest["artifacts"].items()
                 if set(v["depends_on"]) & ({key} | result)}
        if found <= result:
            return result
        result |= found
