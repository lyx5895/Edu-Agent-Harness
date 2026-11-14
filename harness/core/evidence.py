"""Evidence grounding, public projections and server-owned feedback templates."""
import json
import re
import unicodedata
from harness.core.tasks import sentences


KNOWLEDGE_ID=re.compile(r'^(math|physics|chemistry|biology)_\w+$')

KNOWLEDGE_LICENSE='K12-KGraph，CC BY-NC-SA 4.0，仅限研究与非商业教学'

def dump(x): return json.dumps(x,ensure_ascii=False,sort_keys=True)

FALLBACKS={
    'zh':{'default':'请指出你最不确定的部分，并说明你目前的思路？',
          'probe':'你得出这个回答时，依据的规则或材料是什么？',
          'reading':'请指出你用到的原文位置，并说说它如何支持你的判断？',
          'writing':'你最想表达的观点是什么，哪一句还没有表达清楚？',
          'experiment':'你记录了什么现象或数据，它能支持哪一步判断？',
          'programming':'请给一个你不确定的输入，并描述程序现在与预期的输出？',
          'concept':'你能给出一个例子，说明你现在怎样理解这个概念吗？',
          'problem_solving':'你最不确定的是哪一步，这一步使用了什么依据？',
          'self_correct':'请根据你刚才说明的思路自行修正作答，并写出依据。',
          'self_correct_retry':'请按照你刚才说明的思路重新作答，再提交检查。',
          'explain_failed':'这次生成的讲解未通过检查，请重试或请教师核对。',
          'teacher_review':'当前自动反馈需要教师复核。请保留你的作答和疑问，等待老师指导。',
          'locate':'请再检查这一处：「{quote}」。这一步依据的是什么，结果成立吗？',
          'passed_tool':'本次作答通过工具验证。','passed_model':'本次作答通过模型评分规则检查（仍可请教师复核）。',
          'offer_check':'可以开始一题新的独立检查。','check_failed':'本次独立检查尚未通过。可继续作答，或回到原任务请求帮助。'},
    'en':{'default':'Which part are you least sure about, and what is your current reasoning?',
          'probe':'What rule or information did you rely on to get this answer?',
          'reading':'Which part of the text did you use, and how does it support your conclusion?',
          'writing':'What is the main point you want to make, and which sentence does not express it clearly yet?',
          'experiment':'What observation or data did you record, and which step of your reasoning does it support?',
          'programming':'Give one input you are unsure about and describe the actual versus expected output?',
          'concept':'Can you give an example that shows how you currently understand this concept?',
          'problem_solving':'Which step are you least sure about, and what did you base that step on?',
          'self_correct':'Please revise your answer yourself following the reasoning you just described, and show why.',
          'self_correct_retry':'Please redo the problem following the reasoning you just described, then submit it again.',
          'explain_failed':'The generated explanation did not pass checks; please retry or ask your teacher.',
          'teacher_review':'This feedback needs a teacher to review it. Please keep your work and questions; your teacher will follow up.',
          'locate':'Take another look at this part: "{quote}". What is this step based on, and does it hold?',
          'passed_tool':'Your answer passed the tool check. ','passed_model':'Your answer passed the rubric check by the model (a teacher may still review it). ',
          'offer_check':'You can start a new independent check.','check_failed':'This independent check is not passed yet. Keep working, or go back to the original task for help.'}}

def verbatim_quote(quote, text):
    """Return an exact substring of text to store as evidence, or None.
    A quote whose tail was garbled near the length cap keeps its verbatim prefix."""
    if not quote: return None
    if quote in text: return quote
    loose=loose_match(quote,text)
    if loose: return loose
    trimmed=quote.rstrip().rstrip('.…').rstrip()
    low,high=0,len(trimmed)
    while low<high:
        mid=(low+high+1)//2
        if trimmed[:mid] in text: low=mid
        else: high=mid-1
    return trimmed[:low] if low>=max(12,int(.8*len(trimmed))) else None

def _loose(text):
    # NFKC folds full-width forms; whitespace and quote-mark variants are ignored. Keeps an index map back to text.
    chars,index=[],[]
    for i,ch in enumerate(text):
        for c in unicodedata.normalize('NFKC',ch).lower():
            if c.isspace() or c in '"\'“”‘’「」『』`': continue
            chars.append(c);index.append(i)
    return ''.join(chars),index

def loose_match(quote,text):
    q,_=_loose(quote);t,index=_loose(text)
    at=t.find(q) if len(q)>=6 else -1
    return text[index[at]:index[at+len(q)-1]+1] if at>=0 else None

def trim_after_question(text):
    # Locate feedback must end on its question; drop trailing instructions after it.
    end = max(text.rfind('?'), text.rfind('？'))
    return text[:end+1] if end > 0 else text

def ground(assessment, text):
    """Bind evidence to the student's own words: the verbatim quote, else the numbered sentence the model chose."""
    quote=verbatim_quote(assessment.get('evidence_quote',''),text)
    if quote is None:
        spans=sentences(text);n=assessment.get('evidence_sentence',0)
        if 1<=n<=len(spans): quote=spans[n-1]
    return quote

def review_risks(p):
    risks=[]
    if p.get('status') in ('incorrect','incomplete') and not p.get('cross_review',{}).get('agreed'): risks.append('negative_judgment')
    if p.get('assessment',{}).get('confidence')!='high': risks.append('low_confidence')
    if p.get('evidence')=='independent_model_assessed': risks.append('independent_by_model_only')
    if str(p.get('task',{}).get('reference_origin','')).startswith('model_generated'): risks.append('model_reference')
    if any(x.get('source')=='model' and x.get('status')=='unknown' for x in p.get('criteria_results',[])): risks.append('criterion_undetermined')
    return risks

def public_dimensions(rows):
    # Assessment observations can contain the private reference; keep them teacher-only.
    return [{k:r[k] for k in ('id','description','concept_id','status','source') if k in r} for r in rows]
