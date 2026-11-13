"""Live cross-task workflow regression; no application student records are changed."""
import json
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from harness.open_engine import OpenEngine


def main():
    output=Path('reports/teaching-v06-real.json')
    report={'scope':'scripted workflow and grading checks, not learning outcomes','cases':[]}
    with tempfile.TemporaryDirectory(prefix='edu-v06-') as tmp:
        e=OpenEngine(Path(tmp)/'test.sqlite')
        def run(name,fn):
            start=time.time()
            try:
                details=fn()
                row={'name':name,'passed':True,'details':details}
            except Exception as error:row={'name':name,'passed':False,'error':str(error)}
            row['seconds']=round(time.time()-start,2);report['cases'].append(row)
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
            print(name,row['passed'],row.get('error',''),flush=True)

        def generation():
            data=dict(class_id='Live',title='跨学科备课',material='蒸发从周围环境吸收热量。英文段落应围绕一个中心观点展开。',goals=[
                dict(course_id='语文',concept_id='evidence',domain='语文',objective='根据蒸发材料引用原文并解释原因',task_type='reading',check_requirements=['引用材料并解释']),
                dict(course_id='英语',concept_id='paragraph',domain='英语',objective='写三个英语句子表达一个中心观点',task_type='writing')])
            p=e.generate_pack(data,'generate')
            assert p['status']=='draft' and len(p['body']['items'])==2
            return {'pack_id':p['pack_id'],'items':p['body']['items'],'model_calls':len(e.model.calls)}
        run('cross_subject_draft_generation',generation)

        def reading():
            a=e.publish_assignment(dict(class_id='Live',course_id='阅读',concept_id='quote',domain='语文',task_type='reading',
                objective='引用原文解释现象',question='根据材料说明为什么湿衣服晾干时会吸热，引用原文并解释。',
                material='水在蒸发时会从周围环境吸收热量。',rubric='必须引用材料原文，并说明湿衣服水分蒸发吸热。',
                reference='“水在蒸发时会从周围环境吸收热量。”湿衣服晾干是水分蒸发，所以会从周围吸热。',
                criteria=[dict(id='quote',kind='source_quote',description='引用材料原文',expected='水在蒸发时会从周围环境吸收热量。')]))
            s=e.create('reader',assignment_id=a['assignment_id'])
            r=e.operation(s['session_id'],'read','attempt',{'text':'水在蒸发时会从周围环境吸收热量。'})
            assert r['criteria_results'][0]['status']=='correct'
            assert r['status']!='correct',r
            return {'status':r['status'],'criteria':r['criteria_results'],'trace':e.traces(s['session_id'])}
        run('quote_alone_does_not_satisfy_explanation',reading)

        def bank():
            base=dict(class_id='Live',course_id='物理',concept_id='speed',domain='物理',task_type='experiment',objective='计算速度并写单位',
                rubric='用路程除以时间计算速度，并保留m/s单位。',check_requirements=['使用路程与时间求速度','写出速度单位'])
            check=e.publish_assignment(dict(base,question='小车3秒走了12米，速度是多少？写单位。',reference='4 m/s',
                criteria=[dict(id='value',kind='numeric',description='数值正确',expected='4'),dict(id='unit',kind='unit',description='单位正确',expected='m/s')]))
            a=e.publish_assignment(dict(base,question='小车2秒走了6米，速度是多少？写单位。',reference='3 m/s',check_assignment_ids=[check['assignment_id']],
                criteria=[dict(id='value',kind='numeric',description='数值正确',expected='3'),dict(id='unit',kind='unit',description='单位正确',expected='m/s')]))
            s=e.create('physicist',assignment_id=a['assignment_id'])
            r=e.operation(s['session_id'],'first','attempt',{'text':'3 m/s'});assert r['status']=='correct',r
            calls=len(e.model.calls);r=e.operation(s['session_id'],'check','check',{})
            assert len(e.model.calls)==calls
            r=e.operation(s['session_id'],'second','attempt',{'text':'4 m/s'});assert r['status']=='correct',r
            return {'status':r['status'],'evidence':r['evidence'],'trace':e.traces(s['session_id'])}
        run('teacher_bank_physics_independent_check',bank)

        def generated_contract():
            a=e.publish_assignment(dict(class_id='Live',course_id='英语',concept_id='sentence',domain='英语',task_type='writing',objective='用because写出表示原因的英语句子',
                question='用because写一句说明你为什么喜欢阅读的英文句子。',rubric='英语句子完整，使用because引导原因。',
                reference='I like reading because I can learn new things.',check_requirements=['完整英语句子','because引导原因'],
                criteria=[dict(id='reason',kind='model',description='使用because引导原因并形成完整英语句子')]))
            s=e.create('writer',assignment_id=a['assignment_id'])
            r=e.operation(s['session_id'],'answer','attempt',{'text':'I like reading because I can learn new things.'});assert r['status']=='correct',r
            r=e.operation(s['session_id'],'check','check',{})
            assert r['session']['task']['criteria'][0]['id']=='reason'
            assert r['session']['task']['check_requirements']==['完整英语句子','because引导原因']
            return {'task':r['session']['task'],'trace':e.traces(s['session_id'])}
        run('generated_writing_check_preserves_contract',generated_contract)
    report['passed']=all(c['passed'] for c in report['cases'])
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
