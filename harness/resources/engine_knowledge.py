"""Suggest and validate knowledge-library nodes.

Internal OpenEngine mixin; uses its transaction, model and session primitives.
"""

from harness.core.evidence import KNOWLEDGE_ID, KNOWLEDGE_LICENSE
from harness.persistence.replay import retry_stale
from harness.resources.knowledge import SUBJECT_IDS, describe, tag


class KnowledgeMixin:
    @retry_stale
    def suggest_knowledge(self, question, subject, stage=None):
        kb = self.knowledge
        if not kb.available():
            raise ValueError('知识点库未导入：运行 python scripts/import_kgraph.py')
        if not (question or '').strip():
            raise ValueError('请先填写题目')
        if subject and subject not in SUBJECT_IDS:
            raise ValueError('知识点库目前只覆盖人教版数学、物理、化学、生物；学科可留空')
        result = tag(self.model, kb, question, subject, [stage] if stage else None)
        return {
            'source': result['source'],
            'guesses': result['guesses'],
            'license': KNOWLEDGE_LICENSE,
            'items': [
                {
                    'id': n['id'],
                    'name': n['name'],
                    'label': describe(n),
                    'prerequisites': [{'id': p['id'], 'name': p['name']} for p in kb.prerequisites(n['id'])],
                }
                for n in result['nodes']
            ],
        }

    def knowledge_node(self, node_id):
        kb = self.knowledge
        node = kb.get(node_id) if node_id and kb.available() else None
        return node and {**node, 'label': describe(node), 'prerequisites': kb.prerequisites(node_id)}

    def check_knowledge_ids(self, ids):
        unknown = [
            i for i in ids if i and KNOWLEDGE_ID.match(i) and self.knowledge.available() and not self.knowledge.get(i)
        ]
        if unknown:
            raise ValueError('知识点库中没有这些编号：' + '、'.join(unknown))
