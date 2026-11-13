"""Server-owned tools. A verifier can abstain; the core remains subject-neutral."""
import re
from .algebra import verify, expression, equation_solution


def chunks(material):
    # Preserve paragraphs when possible; offsets and digest make sources auditable.
    import hashlib
    digest = hashlib.sha256(material.encode()).hexdigest()[:12]
    result = []
    start = 0
    while start < len(material):
        end = min(start + 600, len(material))
        boundary = material.rfind('\n', start + 300, end)
        if boundary > start: end = boundary + 1
        result.append({'id': f'material:{len(result)+1}', 'text': material[start:end],
                       'start': start, 'end': end, 'document_hash': digest})
        start = end
    return result


def terms(text):
    text = text.lower()
    tokens = set(re.findall(r'[a-z0-9_]+', text))
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        tokens.update(run[i:i+2] for i in range(len(run)-1))
    return tokens - {'什么', '如何', '解释', '说明', '根据', '材料', '问题', '理解', '学习'}


def retrieve(sources, query, limit=2):
    query_terms = terms(query)
    ranked = [(len(query_terms & terms(s['text'])), i, s) for i, s in enumerate(sources)]
    return [s for score, _, s in sorted(ranked, key=lambda x: (-x[0], x[1])) if score > 0][:limit]


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
