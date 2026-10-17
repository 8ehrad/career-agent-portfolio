"""Application-specific rich evidence selection and cited pack generation.

Deliberately separate from fit scoring. Feature-gated; legacy packs remain readable.
"""
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
import logging
import re

from .accuracy_v2 import declared_skill_evidence
from .evidence_selection import fingerprint, serialized
from .models import ApplicationPack
from .requirements import recommendation_requirement
from .application_writing import (PROMPT as WRITING_PROMPT, STYLE_VERSION, bullet_limits,
    writing_schema, unpack_response, validate_summary, check_support, writer_evidence,
    revision_errors)

VERSION = 'rich-application-v4'
SELECTION_VERSION = 'application-dossiers-v3'
DELIVERY = {'contribution', 'method', 'architecture', 'implementation', 'data_preparation', 'outcome', 'functionality'}
WORK = {'professional', 'professional_placement', 'research_employment'}
PROJECT = {'portfolio', 'portfolio_related_to_professional', 'university_project'}
LOG = logging.getLogger(__name__)


class ApplicationGenerationIncomplete(RuntimeError):
    """No publishable application was produced; keep the previous job/pack intact."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__('CV generation did not complete. No new documents were saved. Please retry Prepare.')


def advert_identity(job):
    return fingerprint({k:job.to_dict().get(k) for k in
        ('id','title','company','location','description','advert_document')})


def pack_identity(job, draft, baseline, model_id):
    return fingerprint({'version':VERSION,'style':STYLE_VERSION,'selector':SELECTION_VERSION,'profile':fingerprint(draft),
        'baseline':fingerprint(baseline.data),'advert':advert_identity(job),'model':model_id})


def select_application_evidence(job, draft, index, *, max_bytes=64000):
    claims={c['id']:c for c in draft['claims']}
    entities={e['id']:e for e in draft['entities']}
    rich=job.company_context.get('rich_matching',{})
    rows=rich.get('requirement_document',{}).get('requirements',[])
    # Only grounded technical criteria seed application selection; no behavioural scoring proxy.
    rows=[r for r in rows if recommendation_requirement(r)
          and r.get('quote') and r['quote'].casefold() in job.description.casefold()]
    scores=Counter();links={};used_matching=False
    if rich.get('profile_sha256')==fingerprint(draft) and rows:
        valid_rows={r['id'] for r in rows}
        for row in rich.get('assessment',{}).get('assessments',[]):
            if row.get('requirement_id',row.get('id')) not in valid_rows:continue
            for key in row.get('evidence_ids',[]):
                if key in claims:
                    scores[key]+=3;links.setdefault(key,set()).add(row.get('requirement_id',row.get('id')))
                    used_matching=True
    queries=[(r['id'],r['text']) for r in rows[:20]] or [('advert',job.title+' '+job.description[:18000])]
    for req,query in queries:
        for rank,hit in enumerate(index.lexical_search(query,top_k=8)):
            scores[hit['claim_id']]+=1/(rank+1)
            links.setdefault(hit['claim_id'],set()).add(req)
    # Preserve employment chronology with one concrete contribution per role, not invented job facts.
    for e in entities.values():
        if e['category'] in WORK:
            for key in e['evidence'].get('contribution',[])[:1]:scores[key]+=.01
    selected=[];counts=Counter();seen=set()
    for key in sorted(scores,key=lambda k:(-scores[k],k)):
        c=claims[key];e=entities[c['entity_id']]
        if c['kind'] not in DELIVERY or e['category'] not in WORK|PROJECT:continue
        if counts[e['id']]>=2:continue
        normal=' '.join(c['statement'].lower().split())
        if normal in seen:continue
        # History includes superseded metrics. Do not publish numeric outcomes contradicted by
        # a candidate-confirmed outcome for the same entity.
        confirmed=[claims[i] for i in e['evidence'].get('outcome',[])
                   if claims[i]['attestation']=='candidate_confirmed']
        if confirmed and c['kind']=='outcome' and c['attestation']!='candidate_confirmed':continue
        selected.append(c);counts[e['id']]+=1;seen.add(normal)
    # At most three relevant projects, preserving technical relevance over employment status.
    project_ids=[]
    for c in selected:
        e=entities[c['entity_id']]
        if e['category'] in PROJECT and e['id'] not in project_ids:project_ids.append(e['id'])
    selected=[c for c in selected if entities[c['entity_id']]['category'] in WORK or c['entity_id'] in project_ids[:3]]
    # A CV needs the achievement, not two disconnected implementation details. Retain one
    # contribution from each selected record; choose by relevance, never by employment status.
    for entity_id in dict.fromkeys(c['entity_id'] for c in selected):
        contributions=[claims[k] for k in entities[entity_id]['evidence'].get('contribution',[])]
        if not contributions or any(c['entity_id']==entity_id and c['kind']=='contribution' for c in selected):continue
        anchor=max(contributions,key=lambda c:scores[c['id']])
        positions=[n for n,c in enumerate(selected) if c['entity_id']==entity_id]
        if len(positions)>=2:selected[positions[-1]]=anchor
        else:selected.append(anchor)
    query={'text':job.title+' '+job.description[:18000], 'quote':'', 'category':'technical'}
    skills=declared_skill_evidence(query,draft)[:18]
    record_ids={c['entity_id'] for c in selected}
    # Retrieval chooses experiences, not the final sentences. Expand whole selected records
    # so the writer sees purpose, implementation and results together. Never follow a
    # relationship into another record: shared implementations are not shared achievements.
    dossier_kinds=DELIVERY|{'problem','context','data'}
    selected=[];excluded=[]
    for c in draft['claims']:
        if c['entity_id'] not in record_ids or c['kind'] not in dossier_kinds:continue
        e=entities[c['entity_id']]
        confirmed=[claims[i] for i in e['evidence'].get('outcome',[])
                   if claims[i]['attestation']=='candidate_confirmed']
        # Conservative publication policy: historical outcomes remain in the profile,
        # not the writing allowlist, when a corrected outcome exists for this record.
        if confirmed and c['kind']=='outcome' and c['attestation']!='candidate_confirmed':
            excluded.append(c['id']);continue
        selected.append(deepcopy(c))
    context={'version':SELECTION_VERSION,'profile_sha256':fingerprint(draft),
        'selection_basis':'matching-plus-bm25' if used_matching else 'bm25',
        'claims':selected,'skills':skills,
        'project_order':project_ids[:3],
        'records':[deepcopy(e) for e in entities.values() if e['id'] in record_ids],
        'constraints':[c for c in draft['claims'] if c['entity_id'] in record_ids and c['kind']=='limitation'],
        'relationships':[r for r in draft['relationships'] if r.get('from_entity') in record_ids or r.get('to_entity') in record_ids],
        'requirement_links':{k:sorted(v) for k,v in links.items() if k in {c['id'] for c in selected}}}
    context['excluded_historical_outcome_ids']=excluded
    context['technical_vocabulary'] = [c['statement'] for c in draft['claims']
        if c['kind']=='skill_mention' and c['entity_id']=='SKILLS' and ':' not in c['statement']]
    context['dossiers']=[{'entity_id':e['id'],
        'groups':{kind:[c['id'] for c in selected if c['entity_id']==e['id'] and c['kind']==kind]
                  for kind in sorted(dossier_kinds)},
        'instruction':'Distinct projects within this experience must not share tools or outcomes without explicit evidence.'}
        for e in context['records']]
    # Omit entity-wide lists of unrelated claim IDs, not the record's factual metadata.
    for e in context['records']:e.pop('evidence',None)
    if len(serialized(context).encode())>max_bytes:
        raise ValueError('Application evidence exceeds context bound; refusing to drop constraints')
    return context


def _claim_text(c):
    text=c['statement'].strip()
    # Confirmed correction is stored as an imperative, not candidate prose.
    if text.startswith('Use $54 average pricing error,'):
        text=text.replace('Use $54','Achieved $54',1)
    return text


def _label(entity):
    if entity['category'] in WORK:
        return entity['role_or_degree']['value']+' at '+entity['organization']['value']
    return entity['title']


def _unit(text, ids, entity_id):
    return {'text':text,'evidence_ids':ids,'entity_id':entity_id}


def extractive_content(context):
    entities={e['id']:e for e in context['records']}
    bullets=[_unit(_claim_text(c),[c['id']],c['entity_id']) for c in context['claims']]
    # A concise letter uses two direct evidence examples with explicit source context.
    examples=[];seen=set()
    for b in bullets:
        if b['entity_id'] in seen:continue
        e=entities[b['entity_id']]
        examples.append(_unit(b['text'],b['evidence_ids'],b['entity_id']))
        seen.add(b['entity_id'])
        if len(examples)==2:break
    return {'bullets':bullets,'letter_examples':examples}


# Kept as an import-compatible name; v3 has one canonical writing contract.
PROMPT = WRITING_PROMPT


def validate_content(content, context):
    # Constraints are factual negative boundaries supplied to the writer. They may
    # support a scoped sentence, but a delivery claim is still required.
    allowed={c['id']:c for c in [*context['claims'],*context['constraints']]}
    records={e['id']:e for e in context['records']}
    if set(content)!={'bullets','letter_examples'}:raise ValueError('Invalid application response')
    seen=set();counts=Counter()
    for field,limit,chars in [('bullets',18,600),('letter_examples',2,900)]:
        values=content[field]
        if not isinstance(values,list) or not 1<=len(values)<=limit or (field=='letter_examples' and len(values)!=2):
            raise ValueError('Invalid unit count')
        for unit in values:
            if set(unit)!={'text','evidence_ids','entity_id'}:raise ValueError('Invalid cited unit')
            ids=unit['evidence_ids'];text=unit['text'];entity=unit['entity_id']
            if not isinstance(text,str) or not text.strip() or len(text)>chars:raise ValueError('Invalid text')
            if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)):raise ValueError('Invalid citations')
            if entity not in records or any(not isinstance(i,str) or i not in allowed or allowed[i]['entity_id']!=entity for i in ids):
                raise ValueError('Unknown/cross-entity claim reference')
            if len(text.split())<8 or text.rstrip().endswith(('...', ':', ';')):
                raise ValueError('Fragment rather than a complete achievement')
            if not any(allowed[i]['kind'] in DELIVERY for i in ids):
                raise ValueError('Context alone is not candidate delivery')
            check_support(text,[allowed[i] for i in ids],context.get('technical_vocabulary',()))
            if field=='bullets':
                if text.casefold() in seen:raise ValueError('Duplicate bullet')
                seen.add(text.casefold());counts[entity]+=1
    limits=bullet_limits(context)
    if any(v>limits[k] for k,v in counts.items()):raise ValueError('Too many bullets for record')
    required={e['id'] for e in context['records'] if e['category'] in WORK}
    if not required<=set(counts):raise ValueError('Employment chronology omitted')
    projects={e['id'] for e in context['records'] if e['category'] in PROJECT}
    if projects and not projects.intersection(counts):raise ValueError('Selected projects omitted')
    return content


class RichApplicationGenerator:
    def __init__(self,draft,index,*,provider=None,model_id='extractive',max_attempts=1):
        if max_attempts not in (1,2):raise ValueError('At most one bounded rewrite is permitted')
        self.draft,self.index,self.provider,self.model_id=draft,index,provider,model_id
        self.max_attempts=max_attempts

    def identity(self,job,profile):return pack_identity(job,self.draft,profile,self.model_id)

    def generate(self,job,profile):
        try:
            context=select_application_evidence(job,self.draft,self.index)
        except ValueError as exc:
            raise ApplicationGenerationIncomplete('evidence_context_invalid') from exc
        context_bytes=len(serialized(context).encode())
        writer_payload_bytes=[]
        mode='rich_model';warnings=[]
        if self.provider is not None:
            schema=writing_schema(context)
            payload={'job':{'title':job.title,'company':job.company,'description':job.description[:18000]},
                     'evidence':writer_evidence(context)}
            attempts=[]
            try:
                for attempt in range(self.max_attempts):
                    writer_payload_bytes.append(len(serialized(payload).encode()))
                    response=self.provider.structured(WRITING_PROMPT,payload,schema,version=VERSION,max_tokens=4500)
                    try:
                        content=validate_content(unpack_response(response,context),context)
                        summary=validate_summary(response['summary'],context)
                        if response['section_order'] not in {'projects_first','work_first'}:
                            raise ValueError('Invalid section order')
                        if sum(len(u['text'].split()) for u in content['bullets'])+len(summary['text'].split())>560:
                            raise ValueError('CV prose exceeds document word budget; rewrite without truncation')
                        break
                    except ValueError as exc:
                        details=revision_errors(response,context,exc)
                        attempts.append(details)
                        if attempt+1>=self.max_attempts:raise
                        payload={**payload,'revision':{'previous_response':response,'validation_errors':details,
                            'instruction':'Correct every listed issue. Rewrite unsupported content or cite genuinely supporting supplied evidence. Return the complete corrected response.'}}
                mode='rich_model'
            except Exception as exc:
                LOG.warning('Rich application generation incomplete: %s',type(exc).__name__)
                raise ApplicationGenerationIncomplete(type(exc).__name__) from exc
        else:raise ApplicationGenerationIncomplete('writer_unavailable')
        entities={e['id']:e for e in context['records']}
        sections=[]
        for e in context['records']:
            bullets=[u for u in content['bullets'] if u['entity_id']==e['id']]
            if bullets:sections.append({'entity_id':e['id'],'title':_label(e),'category':e['category'],
                'organization':e.get('organization'),'role_or_degree':e.get('role_or_degree'),
                'dates':e.get('dates'),'location':e.get('location'),'bullets':bullets})
        sections.sort(key=lambda s:(0 if s['category'] in WORK else 1,
            -int(((s.get('dates') or {}).get('start') or '0000-00').replace('-','')) if s['category'] in WORK
            else context['project_order'].index(s['entity_id'])))
        # Labels outside generated prose make personal/academic scope explicit in the letter too.
        examples=[]
        for u in content['letter_examples']:
            e=entities[u['entity_id']]
            scope=('University project' if e['category']=='university_project' else
                   'Personal project' if e['category'] in PROJECT else 'Employment')
            examples.append(f"{scope} - {_label(e)}\n{u['text']}")
        letter=f"Dear Hiring Team,\n\nI am applying for the {job.title} position at {job.company}.\n\n"+'\n\n'.join(examples)
        letter+='\n\nI would welcome the opportunity to discuss how this experience could contribute to your team.'
        letter+='\n\nKind regards,\n'+profile.name
        claims={c['id']:c for c in [*context['claims'],*context['constraints']]}
        used=sorted({i for field in content.values() for u in field for i in u['evidence_ids']}|set(summary['evidence_ids']))
        # Include explicit CV skills used in the written achievements too, not only literal
        # advert keywords. This does not infer proficiency or borrow employer requirements.
        demonstrated_text=' '.join(u['text'] for u in content['bullets'])
        extra_skills=declared_skill_evidence({'text':demonstrated_text,'quote':'','category':'technical'},self.draft)
        context['skills']=list({c['id']:c for c in [*context['skills'],*extra_skills]}.values())[:18]
        awards=[{'text':c['statement'],'evidence_ids':[c['id']]} for c in self.draft['claims']
                if c['entity_id']=='AWARD-OLYMPIAD' and c['kind']!='limitation' and
                c['statement'].startswith('Awarded a gold medal')]
        facts=[{k:e.get(k) for k in ('id','title','category','role_or_degree','organization','dates')}
               for e in self.draft['entities'] if e['category'] in {'education','certification'}]
        metadata={'version':VERSION,'identity':self.identity(job,profile),'profile_sha256':fingerprint(self.draft),
            'style_version':STYLE_VERSION,'summary':summary,'validation_rewrites':attempts,
            'advert_sha256':advert_identity(job),'selector_version':SELECTION_VERSION,'model_id':self.model_id,
            'generation_mode':mode,'warnings':warnings,'selection_basis':context['selection_basis'],
            'requires_human_review':True,'constraints':context['constraints'],'relationships':context['relationships'],
            'claim_audit':{i:deepcopy(claims[i]) for i in used},'letter_examples':content['letter_examples'],
            'skill_audit':{c['id']:deepcopy(c) for c in context['skills']},
            'award_audit':{c['id']:deepcopy(c) for c in self.draft['claims'] if c['id'] in
                           {i for a in awards for i in a['evidence_ids']}},
            'dossiers':context['dossiers'],
            'excluded_historical_outcome_ids':context['excluded_historical_outcome_ids'],
            'requirement_links':context['requirement_links'],'context_bytes':context_bytes,
            'writer_payload_bytes':writer_payload_bytes}
        return ApplicationPack(job_id=job.id,created_at=datetime.now(UTC).isoformat(),profile_version=self.draft['version'],
            cv_headline='Data Scientist and AI Engineer',cv_summary=summary['text'],selected_evidence_ids=used,
            tailored_bullets=[u['text'] for u in content['bullets']],cover_letter=letter,
            recruiter_message=f'Hello, I would like to discuss the {job.title} position at {job.company}. My CV is available on request.',
            interview_topics=job.requirements[:6],gaps_to_address=job.gaps[:10],
            generation_metadata=metadata,rich_content={'sections':sections,'qualifications':facts,
                'section_order':response['section_order'],'awards':awards,
                'skills':[{'text':c['claim'],'evidence_ids':[c['id']]} for c in context['skills']]})
