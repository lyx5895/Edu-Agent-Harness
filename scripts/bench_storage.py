"""Measure transaction cost and concurrency on a large synthetic class (fake model, no GPU).

    .envs/harness/bin/python scripts/bench_storage.py --learners 1000 --db /tmp/bench/big.sqlite
    .envs/harness/bin/python scripts/bench_storage.py --db /tmp/bench/big.sqlite --concurrent 40

The first command builds learners x 3 sessions (later sessions import earlier evidence).
The second copies that database and reports single-operation latency, then runs
``--concurrent`` simultaneous attempts whose model call sleeps ``--model-seconds``,
reporting wall time, stale-snapshot reruns and peak memory.
"""

import argparse
import resource
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from test_open_tasks import FakeOpenModel

from harness.core.open_engine import OpenEngine
from harness.persistence import storage

ASSIGNMENT = dict(
    class_id='C', course_id='M', concept_id='k', domain='科学', objective='解释月相',
    question='月相为何变化', rubric='解释原因', reference='参考结论',
)  # fmt: skip


def build(db, learners):
    engine = OpenEngine(db, FakeOpenModel())
    aid = engine.publish_assignment(ASSIGNMENT)['assignment_id']
    started = time.time()
    for i in range(learners):
        for j in range(3):
            s = engine.create(f'L{i}', assignment_id=aid)
            engine.operation(s['session_id'], uuid.uuid4().hex, 'attempt', {'text': '错误解释'})
            engine.operation(s['session_id'], uuid.uuid4().hex, 'attempt', {'text': '正确解释' if j else '错误'})
    size = Path(db).stat().st_size / 1e6
    print(f'built {learners * 3} sessions ({size:.0f} MB) in {time.time() - started:.0f}s')


def measure(db, concurrent, model_seconds):
    class SlowModel(FakeOpenModel):
        def assess(self, *args):
            time.sleep(model_seconds)
            return super().assess(*args)

    work = f'{db}.bench'
    shutil.copy(db, work)
    engine = OpenEngine(work, SlowModel())
    started = time.time()
    dashboard = engine.dashboard('C')
    print(f'dashboard: {time.time() - started:.2f}s for {len(dashboard["learning"])} rows')
    sessions = [engine.create(f'bench{i}', '解释月相变化') for i in range(concurrent)]
    latencies = []
    for s in sessions[:5]:
        started = time.time()
        engine.operation(s['session_id'], uuid.uuid4().hex, 'hint', {})
        latencies.append(time.time() - started)
    stale = [0]
    original = storage.StaleSnapshot.__init__

    def count(self, *args, **kwargs):
        stale[0] += 1
        original(self, *args, **kwargs)

    storage.StaleSnapshot.__init__ = count
    results = []

    def attempt(s):
        try:
            results.append(
                engine.operation(s['session_id'], uuid.uuid4().hex, 'attempt', {'text': '错误解释'})['status']
            )
        except Exception as error:  # report, do not hide, failures under load
            results.append(repr(error))

    threads = [threading.Thread(target=attempt, args=(s,)) for s in sessions]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    ok = sum(r == 'incorrect' for r in results)
    print(f'single operation (median of 5): {sorted(latencies)[2] * 1000:.0f} ms')
    print(
        f'{concurrent} concurrent attempts: {ok}/{concurrent} ok, {wall:.1f}s, {stale[0]} stale reruns, peak RSS {peak:.0f} MB'
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--db', required=True)
    parser.add_argument('--learners', type=int, help='build a database with this many learners')
    parser.add_argument('--concurrent', type=int, default=40)
    parser.add_argument('--model-seconds', type=float, default=0.5)
    args = parser.parse_args()
    if args.learners:
        Path(args.db).parent.mkdir(parents=True, exist_ok=True)
        build(args.db, args.learners)
    else:
        measure(args.db, args.concurrent, args.model_seconds)


if __name__ == '__main__':
    main()
