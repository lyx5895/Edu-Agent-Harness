"""Manage teacher accounts for EDU_AUTH=1 (design doc 10). Prints a new user's access code once.

python scripts/edu_users.py add 张老师            # teacher
python scripts/edu_users.py add 管理员 --admin
python scripts/edu_users.py list
python scripts/edu_users.py disable 张老师
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edu_core.config import load_settings
from edu_core.storage import Store
from edu_core.workflow import Workspace


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=None, help='默认使用 EDU_DB 或 data/edu.sqlite')
    sub = ap.add_subparsers(dest='cmd', required=True)
    add = sub.add_parser('add')
    add.add_argument('name')
    add.add_argument('--admin', action='store_true')
    sub.add_parser('list')
    dis = sub.add_parser('disable')
    dis.add_argument('name')
    args = ap.parse_args()
    settings = load_settings(**({'db': args.db} if args.db else {}))
    ws = Workspace(Store(settings.db), None, settings)
    if args.cmd == 'add':
        u = ws.create_user(args.name, 'admin' if args.admin else 'teacher')
        print(f'{u["name"]}（{u["role"]}）的访问码：{u["token"]}\n只显示这一次，请转交本人保存。')
    elif args.cmd == 'list':
        for u in ws.users():
            print(f'{u["name"]}\t{u["role"]}\t{"停用" if u["disabled"] else "启用"}')
    else:
        match = [u for u in ws.users() if u['name'] == args.name]
        if not match:
            sys.exit(f'没有这个用户：{args.name}')
        ws.set_user_disabled(match[0]['id'], True)
        print(f'已停用 {args.name}')


if __name__ == '__main__':
    main()
