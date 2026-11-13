"""Compatibility boundary: new sessions use open tasks; courses are explicit examples."""
from .engine import Engine as ExampleEngine
from .open_engine import OpenEngine


class Gateway:
    def __init__(self,db=None,model=None):
        self.examples=ExampleEngine(db=db)
        self.open=OpenEngine(db=db,model=model)
        self.model=self.open.model
    def create(self,learner,skill): return self.examples.create(learner,skill)
    def create_task(self,**kwargs): return self.open.create(**kwargs)
    def backend(self,sid): return self.open if sid.startswith('open_') else self.examples
    def session(self,sid): return self.backend(sid).session(sid)
    def operation(self,sid,*args,**kwargs): return self.backend(sid).operation(sid,*args,**kwargs)
    def traces(self,sid): return self.backend(sid).traces(sid)
    def replay(self,sid): return self.backend(sid).replay(sid)
    def connect(self): return self.examples.connect()
    def correct(self,*args): return self.examples.correct(*args)
    def report(self,learner=None):
        report=self.examples.report(learner);report['open_tasks']=self.open.report(learner);return report
    def progress(self,learner):
        report=self.examples.progress(learner)
        report['open_tasks']=[{'session_id':x['session_id'],'task':{k:x['task'][k] for k in ('domain','objective','question')} if x['task'] else None,
            'needs_review':x['needs_review'], 'evidence':[{'event_id':t['event_id'],'status':t.get('status'),'evidence':t['evidence']} for t in x['turns']]}
            for x in self.open.report(learner)]
        return report
