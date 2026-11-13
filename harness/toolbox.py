"""Server-owned tools. A verifier can abstain; the core remains subject-neutral."""
import math
import re
from .algebra import verify, expression, equation_solution


PAGE_BREAK = '\f'


def chunks(material):
    # Preserve paragraphs when possible; offsets and digest make sources auditable.
    # Form feeds separate PDF pages (see materials.read_material); chunks never span pages.
    import hashlib
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    paged = PAGE_BREAK in material
    result = []
    page_start = 0
    for page, body in enumerate(material.split(PAGE_BREAK), 1):
        start, page_end = page_start, page_start + len(body)
        while start < page_end:
            end = min(start + 600, page_end)
            boundary = material.rfind('\n', start + 300, end)
            if boundary > start: end = boundary + 1
            if material[start:end].strip():
                chunk = {'id': f'material:{len(result)+1}', 'text': material[start:end],
                         'start': start, 'end': end, 'document_hash': digest}
                if paged: chunk['page'] = page
                result.append(chunk)
            start = end
        page_start = page_end + 1
    return result


STOPWORDS = {'什么', '如何', '解释', '说明', '根据', '材料', '问题', '理解', '学习',
             'the', 'a', 'an', 'of', 'to', 'and', 'is', 'in', 'what', 'how', 'why', 'explain'}


def terms(text):
    text = text.lower()
    tokens = re.findall(r'[a-z0-9_]+', text)
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        tokens.extend([run[i:i+2] for i in range(len(run)-1)] or [run])
    return [t for t in tokens if t not in STOPWORDS]


def bm25(query, docs, k1=1.5, b=0.75):
    # Lucene-style idf stays positive, so one- or two-chunk materials still rank matches.
    docs = [terms(d) for d in docs]
    if not docs: return []
    avg = sum(map(len, docs)) / len(docs) or 1
    df = {}
    for d in docs:
        for t in set(d): df[t] = df.get(t, 0) + 1
    scores = []
    for d in docs:
        score = 0.0
        for t in set(terms(query)):
            tf = d.count(t)
            if tf:
                idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg))
        scores.append(score)
    return scores


def retrieve(sources, query, limit=2):
    scores = bm25(query, [s['text'] for s in sources])
    ranked = sorted(zip(scores, range(len(sources)), sources), key=lambda x: (-x[0], x[1]))
    return [s for score, _, s in ranked if score > 0][:limit]


class AlgebraVerifier:
    name='algebra.sympy'
    def run(self, task, text):
        initial=task.get('algebra_initial','')
        kind=task.get('algebra_kind','none')
        # Extraction must be grounded verbatim in the actual question.
        if not initial or kind not in ('expression','equation') or re.sub(r'\s','',initial) not in re.sub(r'\s','',task['question']):
            return None
        try:
            (expression if kind=='expression' else equation_solution)(initial)
        except (ValueError, SyntaxError, TypeError): return None
        result=verify({'initial':initial,'kind':kind},text)
        return {**result,'tool':self.name,'scope':'algebra_equivalence',
                'unverified_requirements':['解释与依据','目标形式','题目其他要求']}


class Toolbox:
    def __init__(self, verifiers=None):
        self.verifiers=[AlgebraVerifier()] if verifiers is None else verifiers
    def verify(self,task,text):
        results=[]
        for verifier in self.verifiers:
            result=verifier.run(task,text)
            if result:
                results.append(result)
                if result['status']!='unknown': return result,results
        return {'status':'unknown','source':'model_rubric','reason':'没有适用的确定性验证结果。'},results
