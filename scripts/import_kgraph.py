"""Build data/knowledge.sqlite from K12-KGraph (CC BY-NC-SA 4.0; research and non-commercial teaching only)."""
import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.knowledge import DEFAULT_DB, import_kgraph  # noqa: E402

URL = 'https://huggingface.co/datasets/lhpku20010120/K12-KGraph/resolve/main/global_KG/{}'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', default=str(ROOT / 'references/k12-kgraph/global_KG'))
    p.add_argument('--db', default=str(DEFAULT_DB))
    args = p.parse_args()
    source = Path(args.source)
    for name in ('nodes.json', 'edges.json'):
        if not (source / name).exists():
            source.mkdir(parents=True, exist_ok=True)
            urllib.request.urlretrieve(URL.format(name), source / name)
    print(import_kgraph(source / 'nodes.json', source / 'edges.json', args.db), '->', args.db)


if __name__ == '__main__':
    main()
