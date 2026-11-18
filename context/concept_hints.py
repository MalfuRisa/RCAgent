"""Consume generated concepts using their original JavaScript regex semantics."""
import json
import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = '''let input=""; process.stdin.setEncoding("utf8");
process.stdin.on("data",s=>input+=s); process.stdin.on("end",()=>{
 const {question,concepts}=JSON.parse(input); const matches=[],skipped=[];
 for(const c of concepts){try{if(new RegExp(c.patternSource,c.patternFlags||"").test(question))
 matches.push({key:c.key,label:c.label,meaning:c.meaning,panels:c.panels});}
 catch(e){skipped.push({key:c.key,error:String(e)});}}
 process.stdout.write(JSON.stringify({matches,skipped}));});'''


def concept_hint(question, budget=None):
    path = Path(os.getenv('RCAGENT_CONCEPTS_PATH', str(Path(__file__).resolve().parents[1] / 'skills/kylin-insights-observability/references/concepts.json')))
    node = os.getenv('RCAGENT_CONCEPT_NODE') or shutil.which('node')
    cached = Path('/root/.cache/rcagent-node/package/bin/node')
    node = node or (str(cached) if cached.is_file() else None)
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not node:
            raise ValueError('Existing Node runtime unavailable; original JS expressions not reinterpreted')
        concepts = data['concepts']
        if not isinstance(concepts, list) or any(not isinstance(c, dict) or
                not isinstance(c.get('patternSource'), str) or
                not isinstance(c.get('patternFlags', ''), str) for c in concepts):
            raise TypeError('Generated concepts must be an array of objects with string regex fields')
        result = subprocess.run([node, '-e', SCRIPT], input=json.dumps({'question':question,'concepts':concepts}),
            text=True, capture_output=True, timeout=max(.01,min(2,budget.remaining('investigation'))) if budget else 2, check=True)
        matched = json.loads(result.stdout)
        return {'status':'provided','schemaVersion':data.get('schemaVersion'),'source':data.get('source'),
            'generated_file':str(path),'question_verbatim':question,**matched,
            'limitations':['Planning knowledge only; names/IDs/availability and attribution are unverified. No hand-written dictionary or model call.']}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        return {'status':'not_provided','generated_file':str(path),'reason':str(exc),
                'limitations':['Continue with discovered tools; no guessed concepts or fallback dictionary']}
