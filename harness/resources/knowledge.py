"""Curriculum knowledge-point library built from K12-KGraph (CC BY-NC-SA 4.0, non-commercial use).

Concept and Skill nodes of the 人教版 math/physics/chemistry/biology textbooks become concept ids;
prerequisites_for edges (source is a prerequisite for target) give prerequisites."""

import json
import math
import os
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from harness.paths import ROOT
from harness.resources.toolbox import terms

DEFAULT_DB = ROOT / 'data/knowledge.sqlite'
SUBJECTS = {'math': '数学', 'physics': '物理', 'chemistry': '化学', 'biology': '生物'}
SUBJECT_IDS = {v: k for k, v in SUBJECTS.items()} | {k: k for k in SUBJECTS}
TAGGABLE = ('Concept', 'Skill')
SCHEMA = """
create table if not exists kp_nodes(id text primary key,label text not null,name text not null,subject text not null,
    stage text not null,book text,book_name text,chapter_name text,section_name text);
create table if not exists kp_edges(source text not null,target text not null,type text not null);
create index if not exists kp_edges_target on kp_edges(target,type);
create index if not exists kp_edges_source on kp_edges(source,type);
create table if not exists kp_meta(key text primary key,value text not null);
"""


def stage(book):
    grade = book.split('_')[1]
    if grade[0].isdigit():
        return '小学' if int(grade[0]) <= 6 else '初中'
    return '高中'


def import_kgraph(nodes_path, edges_path, db=DEFAULT_DB):
    nodes = {n['id']: n for n in json.loads(Path(nodes_path).read_text(encoding='utf-8'))}
    edges = json.loads(Path(edges_path).read_text(encoding='utf-8'))
    parent = {e['source']: e['target'] for e in edges if e['type'] == 'is_part_of'}
    section = {}
    for e in edges:
        if e['type'] == 'appears_in':
            section.setdefault(e['source'], e['target'])
    rows = []
    for i, n in nodes.items():
        sec = n['id'] if n['label'] == 'Section' else section.get(i)
        chapter = parent.get(sec) if sec else (i if n['label'] == 'Chapter' else None)
        book = '_'.join(i.split('_')[:3])
        rows.append(
            (
                i,
                n['label'],
                n['name'],
                i.split('_')[0],
                stage(book),
                book,
                nodes.get(book, {}).get('name'),
                nodes.get(chapter, {}).get('name'),
                nodes.get(sec, {}).get('name') if n['label'] != 'Section' else None,
            )
        )
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA + 'delete from kp_nodes;delete from kp_edges;')
        c.executemany('insert into kp_nodes values(?,?,?,?,?,?,?,?,?)', rows)
        c.executemany(
            'insert into kp_edges values(?,?,?)',
            [(e['source'], e['target'], e['type']) for e in edges if e['source'] in nodes and e['target'] in nodes],
        )
        c.executemany(
            'insert or replace into kp_meta values(?,?)',
            [
                ('source', 'K12-KGraph (huggingface.co/datasets/lhpku20010120/K12-KGraph)'),
                ('license', 'CC BY-NC-SA 4.0'),
                ('nodes', str(len(rows))),
                ('edges', str(len(edges))),
            ],
        )
    return {'nodes': len(rows), 'edges': len(edges)}


class Knowledge:
    def __init__(self, db=None):
        self.db = Path(db or os.getenv('OMNIEDU_KNOWLEDGE_DB') or DEFAULT_DB)
        self._index = None

    def available(self):
        return self.db.exists()

    def _rows(self, sql, args=()):
        with sqlite3.connect(self.db) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args)]

    def get(self, node_id):
        rows = self._rows('select * from kp_nodes where id=?', (node_id,))
        return rows[0] if rows else None

    def related(self, node_id, kind, reverse=False):
        a, b = ('source', 'target') if reverse else ('target', 'source')
        return self._rows(
            f'select n.* from kp_edges e join kp_nodes n on n.id=e.{b} where e.{a}=? and e.type=? order by n.id',
            (node_id, kind),
        )

    def prerequisites(self, node_id):
        return self.related(node_id, 'prerequisites_for')

    def meta(self):
        return {r['key']: r['value'] for r in self._rows('select * from kp_meta')}

    def index(self):
        if self._index is None:
            docs = self._rows(
                f'select * from kp_nodes where label in ({",".join("?" * len(TAGGABLE))}) order by id', TAGGABLE
            )
            bags = [Counter(terms(d['name']) + terms(d['section_name'] or '')) for d in docs]
            df, postings = Counter(), defaultdict(list)
            for i, bag in enumerate(bags):
                for t in bag:
                    df[t] += 1
                    postings[t].append(i)
            lengths = [sum(b.values()) for b in bags]
            self._index = docs, bags, df, postings, lengths, sum(lengths) / max(len(lengths), 1)
        return self._index

    def search(self, query, subject=None, stages=None, limit=20, exclude=(), k1=1.2, b=0.75):
        docs, bags, df, postings, lengths, avg = self.index()
        subject = SUBJECT_IDS.get(subject) if subject else None
        scores = defaultdict(float)
        for t in set(terms(query)):
            idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5)) if t in df else 0
            for i in postings.get(t, ()):
                tf = bags[i][t]
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[i] / avg))
        ranked = sorted(scores, key=lambda i: (-scores[i], docs[i]['id']))
        keep = [
            docs[i] | {'score': round(scores[i], 3)}
            for i in ranked
            if (not subject or docs[i]['subject'] == subject)
            and (not stages or docs[i]['stage'] in stages)
            and docs[i]['id'] not in exclude
        ]
        return keep[:limit]


def _norm(text):
    return unicodedata.normalize('NFKC', text).replace(' ', '').lower()


def tag(model, kb, question, subject, stages=None, shortlist=30, per_guess=5):
    """Suggest up to three knowledge points; the model only chooses among library ids."""
    from harness.llm.model import ModelUnavailable

    subject_name = SUBJECTS.get(SUBJECT_IDS.get(subject, subject), subject)
    try:
        guesses = model.guess_knowledge(question, subject_name)
    except ModelUnavailable:
        guesses = []
    pool = {}
    for g in guesses:
        for node in kb.search(g, subject, stages, per_guess):
            pool.setdefault(node['id'], node)
    for node in kb.search(question, subject, stages, shortlist):
        pool.setdefault(node['id'], node)
    candidates = list(pool.values())
    if not candidates:
        return {'ids': [], 'nodes': [], 'guesses': guesses, 'candidates': [], 'source': 'none'}
    named = {_norm(c['name']): c['id'] for c in reversed(candidates)}
    exact = [named[_norm(g)] for g in guesses if _norm(g) in named]
    try:
        chosen = [i for i in model.choose_knowledge(question, subject_name, candidates) if i in pool]
        ids, source = list(dict.fromkeys(exact + chosen))[:3], 'model'
    except ModelUnavailable:
        ids = []
    if not ids:
        ranked = kb.search(question, subject, stages, 3)
        ids, source = [n['id'] for n in ranked], 'bm25'
    return {
        'ids': ids,
        'nodes': [pool.get(i) or kb.get(i) for i in ids],
        'guesses': guesses,
        'candidates': [c['id'] for c in candidates],
        'source': source,
    }


def describe(node):
    where = ' / '.join(x for x in (node.get('book_name'), node.get('chapter_name'), node.get('section_name')) if x)
    return f'{node["name"]}（{SUBJECTS.get(node["subject"], node["subject"])}·{where}）'
