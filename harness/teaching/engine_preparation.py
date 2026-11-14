"""Draft, edit, publish and distribute preparation packs.

Internal OpenEngine mixin; uses its transaction, model and session primitives.
"""
from harness.teaching import preparation
from harness.core.evidence import dump
from harness.persistence.replay import retry_stale
from harness.resources.toolbox import chunks


class PreparationMixin:
    @retry_stale
    def save_pack(self, data, request_id, pack_id=None, version=None):
        with self.transaction() as c:
            scope='pack:'+pack_id if pack_id else 'packs:'+data['class_id']
            digest,old=self.cached(c,scope,request_id,['save',data,version])
            if old is not None:return old
            result=preparation.save(c,data,pack_id,version)
            self.remember(c,scope,request_id,digest,result)
            return result

    @retry_stale
    def generate_pack(self, data, request_id):
        body=preparation.GeneratePack.model_validate(data).model_dump()
        with self.transaction() as c:
            scope='packs:'+body['class_id']
            digest,old=self.cached(c,scope,request_id,['generate',body])
            if old is not None:return old
            items=[]
            for goal in body['goals']:
                request=goal['objective']+'\n教学要求：'+dump(goal)
                draft=self.build(request,chunks(body['material']),'learn')
                item={k:draft[k] for k in ('question','rubric','reference','prerequisite')}
                item.update(goal,class_id=body['class_id'],material=body['material'],
                            criteria=draft.get('criteria',[]),reference_origin='model_generated_unverified')
                # Generated drafts become teacher-reviewed only upon explicit publication.
                items.append(item)
            result=preparation.save(c,dict(class_id=body['class_id'],title=body['title'],items=items))
            preparation.event(c,result['pack_id'],'model_drafted',{'requires_teacher_review':True})
            self.remember(c,scope,request_id,digest,result)
            return result

    def packs(self,class_id):
        with self.connect() as c:
            return [preparation.load(c,r['id']) for r in c.execute(
                'SELECT id FROM preparation_packs WHERE class_id=? ORDER BY rowid DESC',(class_id,))]

    def load_pack(self,pack_id):
        with self.connect() as c:return preparation.load(c,pack_id)

    @retry_stale
    def publish_pack(self,pack_id,version,distribution,request_id,edited_body=None):
        with self.transaction() as c:
            scope='pack:'+pack_id
            digest,old=self.cached(c,scope,request_id,['publish',version,distribution,edited_body])
            if old is not None:return old
            if edited_body is not None:
                saved=preparation.save(c,edited_body,pack_id,version)
                version=saved['version']
            result=preparation.publish(c,pack_id,version,distribution)
            self.remember(c,scope,request_id,digest,result)
            return result
