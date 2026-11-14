"""Move inline base64 images in learning_events into content-addressed files (v0.8 layout).

Usage: migrate_images.py [DB] [--apply]. Without --apply only reports what would change.
With --apply the database is copied to DB.bak-<time> first. Safe to run repeatedly."""

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harness.resources.images import store_image


def migrate(db, apply=False):
    db = Path(db)
    root = db.parent / (db.stem + '-images')
    c = sqlite3.connect(db)
    rows = [(i, json.loads(p)) for i, p in c.execute("SELECT id,payload FROM learning_events WHERE kind='image'")]
    rows = [(i, p) for i, p in rows if str(p.get('image_url', '')).startswith('data:image/')]
    if apply and rows:
        backup = db.with_name(f'{db.name}.bak-{time.strftime("%Y%m%d%H%M%S")}')
        shutil.copy2(db, backup)
        with c:
            for i, payload in rows:
                fields = store_image(payload.pop('image_url'), root)
                c.execute(
                    'UPDATE learning_events SET payload=? WHERE id=?',
                    (json.dumps({**payload, **fields}, ensure_ascii=False, sort_keys=True), i),
                )
        print(f'backup: {backup}')
    c.close()
    return len(rows)


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--apply']
    apply = '--apply' in sys.argv
    db = args[0] if args else Path(__file__).resolve().parents[1] / 'data/learning.sqlite'
    n = migrate(db, apply)
    print(f'{n} inline image event(s) ' + ('migrated' if apply else 'found; rerun with --apply to migrate'))
