"""管理用 CLI。

    classbot run                       ボットを起動
    classbot scan                      materials/ と DB を照合し、変更をジョブ登録
    classbot reindex [--id N] [--force] [--inline]
    classbot materials                 登録資料一覧
    classbot usage                     利用状況（推定費用）
    classbot backup                    SQLite のオンラインバックアップ
    classbot healthcheck               ヘルスチェック（異常時 終了コード 1）

ジョブは DB に登録され、稼働中のボットが処理する。ボット停止中に処理したい場合は
reindex に --inline を付ける。
"""

from __future__ import annotations

import argparse
import sys

from classbot.config import get_settings
from classbot.db import Database
from classbot.jobs import KIND_INGEST, KIND_REINDEX_ALL, JobQueue


def _db():
    s = get_settings()
    s.ensure_dirs()
    db = Database(s.db_path)
    db.init_schema()
    return s, db


def cmd_scan(_args) -> int:
    from classbot.app import make_enqueue_ingest
    from classbot.materials import MaterialStore
    from classbot.scanner import Scanner

    s, db = _db()
    report = Scanner(db, s, MaterialStore(db, s), make_enqueue_ingest(JobQueue(db))).scan()
    print(report.summary())
    return 0


def cmd_reindex(args) -> int:
    s, db = _db()
    if args.inline:
        from classbot.indexer import Indexer
        from classbot.materials import MaterialStore

        store = MaterialStore(db, s)
        indexer = Indexer(db, s, store)
        ids = [args.id] if args.id else [r["id"] for r in db.query("SELECT id FROM materials ORDER BY id")]
        for mid in ids:
            o = indexer.index_material(mid, force=args.force)
            print(f"#{mid}: {o.status}{' (skip)' if o.skipped else ''} {o.message} chunks={o.chunk_count}")
        return 0
    q = JobQueue(db)
    if args.id:
        if db.query_one("SELECT 1 FROM materials WHERE id=?", (args.id,)) is None:
            print(f"ID {args.id} の資料はありません", file=sys.stderr)
            return 1
        q.enqueue(KIND_INGEST, {"material_id": args.id, "force": args.force}, dedupe_key=f"ingest:{args.id}")
    else:
        q.enqueue(KIND_REINDEX_ALL, {"force": args.force}, dedupe_key="reindex_all")
    print("ジョブを登録しました（稼働中のボットが処理します）")
    return 0


def cmd_materials(_args) -> int:
    from classbot.materials import MaterialStore

    s, db = _db()
    rows, total = MaterialStore(db, s).list(0, 100000)
    print(f"{total} 件")
    for r in rows:
        err = f"  ! {r['last_error']}" if r["last_error"] else ""
        print(f"#{r['id']:<5} {r['status']:<9} {r['source']:<6} {r['size_bytes']:>10}B  "
              f"{r['chunk_count']:>5}chunks  {r['display_name']}{err}")
    return 0


def cmd_usage(_args) -> int:
    from classbot.budget import BudgetManager
    from classbot.pricing import PriceTable
    from classbot.status import format_usage

    s, db = _db()
    from classbot.images import ImageStore

    bm = BudgetManager(db, s, PriceTable.load(s.pricing_file))
    images = ImageStore(db, s).stats(bm.month_start_iso())
    print(format_usage(bm.summary(), s, top=50, images=images).replace("*", ""))
    return 0


def cmd_backup(_args) -> int:
    from classbot.app import backup_now

    s, db = _db()
    print(backup_now(s, db))
    return 0


def cmd_healthcheck(_args) -> int:
    from classbot.healthcheck import check

    ok, msg = check(get_settings())
    print(msg)
    return 0 if ok else 1


def cmd_run(_args) -> int:
    from classbot.app import run_bot

    run_bot()
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="classbot", description="授業用 Slack AI アシスタント")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="ボットを起動").set_defaults(fn=cmd_run)
    sub.add_parser("scan", help="materials/ を照合").set_defaults(fn=cmd_scan)
    r = sub.add_parser("reindex", help="再インデックス")
    r.add_argument("--id", type=int)
    r.add_argument("--force", action="store_true", help="内容が変わっていなくても再処理する")
    r.add_argument("--inline", action="store_true", help="ジョブ登録せずこのプロセスで処理する")
    r.set_defaults(fn=cmd_reindex)
    sub.add_parser("materials", help="資料一覧").set_defaults(fn=cmd_materials)
    sub.add_parser("usage", help="利用状況").set_defaults(fn=cmd_usage)
    sub.add_parser("backup", help="DB バックアップ").set_defaults(fn=cmd_backup)
    sub.add_parser("healthcheck", help="ヘルスチェック").set_defaults(fn=cmd_healthcheck)
    args = p.parse_args(argv)
    sys.exit(args.fn(args))
