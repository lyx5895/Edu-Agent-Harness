"""Generate, audit and fall back to server-owned teaching feedback.

Internal OpenEngine mixin; uses its transaction, model and session primitives.
"""
import re
from harness.core.evidence import FALLBACKS, trim_after_question
from harness.llm.model import ModelUnavailable
from harness.core.tasks import language


class FeedbackMixin:
    def feedback(self,task,action,text,context,assessment):
        lines=FALLBACKS[language(task.get('question','')+text)]
        if action=='teacher_review':
            return lines['teacher_review'], {'candidate':None,'review':{},'reasons':[],'fallback':False,'delivered_action':'teacher_review'}
        if action=='self_correct':
            return lines['self_correct'], {'candidate':None,'review':{'acceptable':True,'reason':'服务器只邀请自行作答，不提供答案'},'reasons':[],'fallback':False,'source':'policy_template'}
        english=language(task.get('question','')+text)=='en'
        candidate,review,reasons,cross=self.draft(task,action,text,context,assessment,english)
        first=None
        left=self.model.budget_left() if hasattr(self.model,'budget_left') else None
        # One rewrite with the rejection reasons; needs respond, translate and two reviews at most.
        if reasons and reasons!=['length'] and (left is None or left>=4):
            first={'candidate':candidate,'reasons':reasons,'review':review.get('reason','')}
            try:
                candidate,review,reasons,cross=self.draft(task,action,text,context,assessment,english,first)
            except ModelUnavailable:
                candidate,review,reasons,cross=first['candidate'],{'acceptable':False,'reason':first['review']},first['reasons'],None
        lines=FALLBACKS[language(task.get('question','')+text)]
        fallback=lines['probe'] if action=='probe' else lines['default']
        if action!='explain':fallback=lines.get(task.get('task_type'),fallback)
        if action=='self_correct': fallback=lines['self_correct_retry']
        if action=='explain': fallback=lines['explain_failed']
        delivered='clarify' if reasons else action
        if action=='locate' and assessment.get('evidence_quote'):
            quote=assessment['evidence_quote'].strip().rstrip('.。')
            if len(quote)>120: quote=(quote[:120].rsplit(' ',1)[0] if ' ' in quote[:120] else quote[:120])+'…'
            fallback=lines['locate'].format(quote=quote)
            if reasons: delivered='locate_template'
        validation={'candidate':candidate,'review':review,'reasons':reasons,'fallback':bool(reasons),'delivered_action':delivered}
        if first: validation['first_draft']=first
        if cross is not None: validation['cross_review']={**cross,'reviewer':self.reviewer.model_name}
        return (fallback if reasons else candidate),validation

    def draft(self,task,action,text,context,assessment,english,revision=None):
        candidate=self.model.respond(task,action,text,context,assessment,revision) if revision else self.model.respond(task,action,text,context,assessment)
        if english and candidate and language(candidate)=='zh': candidate=self.model.translate(candidate)
        if action=='locate' and candidate: candidate=trim_after_question(candidate)
        review=self.model.review(task,candidate,action,text)
        reasons=[]
        if not candidate or len(candidate)>1200: reasons.append('length')
        if action=='probe':
            if candidate.count('？')+candidate.count('?') != 1 or not candidate.endswith(('？','?')) or any(x in candidate for x in ('。','!','！','\n')):
                reasons.append('probe_must_be_one_question')
        if action=='locate' and (not 1<=candidate.count('？')+candidate.count('?')<=2 or not candidate.rstrip().endswith(('？','?'))):
            reasons.append('locate_must_end_with_one_question')
        if re.search(r'你已经掌握|已通过独立检查|系统指令',candidate): reasons.append('authority_claim')
        if english and language(candidate)=='zh': reasons.append('language_mismatch')
        answer=re.sub(r'\s','',task['reference'])
        if action!='explain' and len(answer)>=3 and answer in re.sub(r'\s','',candidate): reasons.append('reference_disclosure')
        if not review['acceptable']: reasons.append('model_review')
        cross=None
        if not reasons and action!='explain' and self.reviewer:
            cross=self.reviewer.review(task,candidate,action,text)
            if not cross['acceptable']: reasons.append('cross_review')
        return candidate,review,reasons,cross
