"""Grade attempts and dimensions, ground evidence and cross-review judgments.

Internal OpenEngine mixin; uses its transaction, model and session primitives.
"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import json
import re
from harness.grading.assessment import adjudicate
from harness.grading.criteria import aggregate, tool_grade
from harness.errors import Conflict
from harness.core.evidence import FALLBACKS, ground
from harness.core.tasks import language


class GradingMixin:
    def _attempt(self,t):
        c,s,task,text,trace,payload,o=t.c,t.s,t.task,t.text,t.trace,t.payload,t.o
        if s['completed']: raise Conflict('本任务已完成，请创建独立检查或继续学习')
        if not text: raise ValueError('请填写作答')
        verification,tool_results=self.tools.verify(task,text)
        assessment_ctx=dict(t.ctx)
        if t.ctx.get('image_evidence_id'):
            img=c.execute('SELECT payload FROM learning_events WHERE id=?',(t.ctx['image_evidence_id'],)).fetchone()
            assessment_ctx['image_url']=self.image_url(json.loads(img[0]))
        assessment,quote,dimensions=self._grade(task,text,assessment_ctx,verification,trace)
        if quote is None:
            # An ungrounded judgment is not evidence; keep the attempt and route it to a teacher.
            trace['rejected_assessment']=assessment
            assessment={**assessment,'status':'unknown','confidence':'low','evidence_quote':'',
                        'observation':'模型判定未能引用学生原文，已转教师复核。'}
            payload['quote_unverified']=True
        else: assessment['evidence_quote']=quote
        if self.reviewer and not task.get('criteria') and (quote is None or assessment['status']=='unknown' or assessment['confidence']!='high'):
            assessment,quote=self._resolve_unknown(task,text,assessment_ctx,verification,assessment,quote,payload)
        t.assessment=assessment
        o['grounded']=quote is not None and assessment['status'] in ('incorrect','incomplete')
        status,source=adjudicate(task,text,verification,assessment)
        if task.get('criteria'):
            payload['criteria_results']=dimensions
            dimension_status=aggregate(dimensions)
            payload['whole_task_status']=status
            payload['dimension_status']=dimension_status
            payload['rubric_conflict']=dimension_status=='correct' and status!='correct'
            # Dimension success cannot erase an uncovered whole-task requirement.
            status=aggregate([{'status':status},{'status':dimension_status}])
            source='model_rubric'
        if source=='model_rubric' and status in ('incorrect','incomplete') and self.reviewer and 'cross_review' not in payload:
            status=self._cross_review(task,text,assessment_ctx,verification,status,payload)
        payload['grading_conflict']=verification['status'] in ('correct','incorrect') and assessment['status'] in ('correct','incorrect') and verification['status']!=assessment['status']
        if re.fullmatch(r'(我)?(懂了|会了|知道了|明白了|学会了)[。！! ]*',text): status='unknown'
        o['status']=status
        payload.update(status=status,assessment=assessment,verification=verification,source=source,
            task=deepcopy(task),assisted=s['assisted'])
        trace['tools']+=tool_results
        lines=FALLBACKS[language(task['question']+text)]
        if status=='correct':
            s['completed']=True;s['pending_question']=None
            payload['evidence']=('independent_verified' if source in ('sympy','rubric_tools') else 'independent_model_assessed') if task['phase']=='check' else ('assisted_complete' if s['assisted'] else 'practice_complete')
            payload['feedback']=lines['passed_tool' if source in ('sympy','rubric_tools') else 'passed_model']
            if task['phase']=='teach': payload['feedback']+=lines['offer_check']
        elif task['phase']=='check':
            payload['feedback']=lines['check_failed']
        else:
            if status in ('incorrect','incomplete'): s['failures']+=1
            o['failures']=s['failures']
            s['hypotheses']=(s['hypotheses']+[{'hypothesis':assessment['hypothesis'],
                'quote':quote or '','certainty':'tentative'}])[-3:]
        s['pending_image']=None

    def _resolve_unknown(self,task,text,ctx,verification,assessment,quote,payload):
        second=self.reviewer.assess(task,text,ctx,verification)
        second_quote=ground(second,text)
        # Only a confident, grounded negative judgment is adopted; a reviewer "correct" never replaces an unsure primary.
        resolved=second['status'] in ('incorrect','incomplete') and second['confidence']=='high' and second_quote is not None
        payload['cross_review']={'reviewer':self.reviewer.model_name,'status':second['status'],'confidence':second['confidence'],
            'observation':second['observation'],'agreed':False,'overturned':False,'resolved_unknown':resolved}
        if not resolved: return assessment,quote
        payload.pop('quote_unverified',None)
        return {**second,'evidence_quote':second_quote},second_quote

    def _cross_review(self,task,text,ctx,verification,status,payload):
        second=self.reviewer.assess(task,text,ctx,verification)
        overturned=second['status']=='correct' and second['confidence']=='high'
        payload['cross_review']={'reviewer':self.reviewer.model_name,'status':second['status'],'confidence':second['confidence'],
            'observation':second['observation'],'agreed':second['status'] in ('incorrect','incomplete'),'overturned':overturned}
        # Two models disagreeing on a negative judgment is not evidence either way; a teacher decides.
        return 'unknown' if overturned else status

    def _grade(self,task,text,ctx,verification,trace):
        with ThreadPoolExecutor(max_workers=1) as pool:
            graded=pool.submit(copy_context().run,self.grade_dimensions,task,text,ctx) if task.get('criteria') else None
            assessment=self.model.assess(task,text,ctx,verification)
            quote=ground(assessment,text)
            if quote is None:
                trace['rejected_assessment']=assessment
                assessment=self.model.assess(task,text,{**ctx,'quote_feedback':
                    '上次evidence_quote不是学生原文且evidence_sentence无效，请填写student_sentences中的编号并逐字复制该句。'},verification)
                quote=ground(assessment,text)
            return assessment,quote,(graded.result() if graded else None)

    def grade_dimensions(self,task,text,context):
        from harness.grading.criteria import GRADERS
        results=[];pending=[]
        for criterion in task['criteria']:
            result=tool_grade(criterion,{**task,'material':'\n'.join(x['text'] for x in context.get('materials',[]))},text)
            if criterion['kind'] not in GRADERS and criterion['kind'] != 'model':
                result['reason']='未安装该评分插件：'+criterion['kind']
            elif result['status']=='unknown' and criterion['kind'] not in ('source_quote','word_count') and hasattr(self.model,'assess_criterion'):
                pending.append((criterion,result))
            results.append(result)
        if pending:
            with ThreadPoolExecutor(max_workers=len(pending)) as pool:
                futures=[(result,pool.submit(copy_context().run,self.model.assess_criterion,task,criterion,text,context))
                         for criterion,result in pending]
                for result,future in futures:
                    assessment=future.result()
                    quote=ground(assessment,text)
                    valid=quote is not None and assessment['confidence']=='high'
                    result.update(status=assessment['status'] if valid else 'unknown',source='model',
                                  reason=assessment['observation'],evidence_quote=quote or '')
        return results
