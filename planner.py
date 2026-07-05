# -*- coding: utf-8 -*-
"""モンハンNow 最適化攻略プランナー CLI

data/mhnow.db（build_db.py で構築）に対して、コアアルゴリズムを提供する。

  ① 派生ツリー探索:        python planner.py route 吼剣【地咬】
  ② 必要素材の総量計算:     python planner.py cost 吼剣【地咬】 --from 5:1 --to 10:5
  ③ フィルタリングとソート:  python planner.py find --weapon-type 片手剣 --sort zenny
     素材の逆引き:           python planner.py material 黒轟竜の逆鱗
"""

import argparse
import io
import sqlite3
import sys
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent / "data" / "mhnow.db"
MONSTERS_FILE = Path(__file__).resolve().parent / "data" / "monsters.json"


def connect():
    if not DB_FILE.exists():
        sys.exit("エラー: data/mhnow.db がありません。先に build_db.py を実行してください。")
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def get_equipment(conn, name):
    row = conn.execute("SELECT * FROM equipment WHERE name = ?", (name,)).fetchone()
    if row is None:
        cand = conn.execute(
            "SELECT name FROM equipment WHERE name LIKE ? LIMIT 5",
            (f"%{name}%",)).fetchall()
        hint = f"（候補: {'、'.join(r['name'] for r in cand)}）" if cand else ""
        sys.exit(f"エラー: 「{name}」が見つかりません{hint}")
    return row


def parse_gl(s):
    """'10:5' → (10, 5)。'10' はグレード10レベル1とみなす。"""
    if ":" in s:
        g, l = s.split(":", 1)
        return int(g), int(l)
    return int(s), 1


# ------------------------------------------------ ① 派生ツリー探索 (DFS) ---

def cmd_route(conn, args):
    eq = get_equipment(conn, args.name)

    # 祖先をたどる（最終武器 → 初期武器の逆算）
    chain = [eq]
    cur = eq
    while True:
        row = conn.execute(
            "SELECT e.* FROM equipment_tree t JOIN equipment e ON e.id = t.parent_id "
            "WHERE t.equipment_id = ?", (cur["id"],)).fetchone()
        if row is None:
            break
        chain.append(row)
        cur = row
    chain.reverse()

    print(f"■ {eq['name']} までの作成ルート")
    if len(chain) == 1:
        print(f"  {eq['name']}（G{eq['base_grade']}で直接生産・派生なし）")
    else:
        print("  " + " → ".join(c["name"] for c in chain))

    # 子孫をDFSで列挙（この武器からの派生先）
    def dfs(eq_id, depth):
        rows = conn.execute(
            "SELECT e.* FROM equipment_tree t JOIN equipment e ON e.id = t.equipment_id "
            "WHERE t.parent_id = ? ORDER BY e.name", (eq_id,)).fetchall()
        for r in rows:
            print("  " + "    " * depth + f"└─ {r['name']}")
            dfs(r["id"], depth + 1)

    print(f"■ {eq['name']} からの派生先")
    has_child = conn.execute(
        "SELECT 1 FROM equipment_tree WHERE parent_id = ? LIMIT 1", (eq["id"],)).fetchone()
    if has_child:
        dfs(eq["id"], 0)
    else:
        print("  （登録された派生先はありません）")


# --------------------------------------- ② 必要素材の総量計算（再帰合算） ---

def collect_cost(conn, eq_id, gl_from, gl_to):
    """(grade,level] 区間の強化ステップを集計する。gl_from は現状（含まない）。"""
    rows = conn.execute(
        "SELECT s.id, s.grade, s.level, s.zenny FROM upgrade_steps s "
        "WHERE s.equipment_id = ? ORDER BY s.grade, s.level", (eq_id,)).fetchall()
    total_z = 0
    mats = {}
    used = []
    for r in rows:
        key = (r["grade"], r["level"])
        if key <= gl_from or key > gl_to:
            continue
        total_z += r["zenny"]
        used.append(key)
        for m in conn.execute(
                "SELECT material_name, count FROM step_materials WHERE step_id = ?",
                (r["id"],)):
            mats[m["material_name"]] = mats.get(m["material_name"], 0) + m["count"]
    return total_z, mats, used


def cmd_cost(conn, args):
    eq = get_equipment(conn, args.name)
    first = conn.execute(
        "SELECT grade, level FROM upgrade_steps WHERE equipment_id = ? "
        "ORDER BY grade, level LIMIT 1", (eq["id"],)).fetchone()
    last = conn.execute(
        "SELECT grade, level FROM upgrade_steps WHERE equipment_id = ? "
        "ORDER BY grade DESC, level DESC LIMIT 1", (eq["id"],)).fetchone()
    if first is None:
        sys.exit(f"エラー: 「{eq['name']}」の強化データがありません。")

    # 既定: 未生産（生産ステップ含む）〜 最大強化
    gl_from = parse_gl(args.from_gl) if args.from_gl else (first["grade"], first["level"] - 1)
    gl_to = parse_gl(args.to_gl) if args.to_gl else (last["grade"], last["level"])

    # 派生元武器の費用も合算（--with-parents）
    targets = [eq]
    if args.with_parents:
        cur = eq
        while True:
            row = conn.execute(
                "SELECT e.* FROM equipment_tree t JOIN equipment e ON e.id = t.parent_id "
                "WHERE t.equipment_id = ?", (cur["id"],)).fetchone()
            if row is None:
                break
            targets.append(row)
            cur = row
        targets.reverse()

    grand_z = 0
    grand_mats = {}
    for t in targets:
        if t["id"] == eq["id"]:
            z, mats, used = collect_cost(conn, t["id"], gl_from, gl_to)
        else:  # 派生元は全区間（生産〜派生に必要な段階まで）を計上
            z, mats, used = collect_cost(conn, t["id"], (0, 0), (99, 99))
        grand_z += z
        for k, v in mats.items():
            grand_mats[k] = grand_mats.get(k, 0) + v
        label = f"G{gl_from[0]}:{gl_from[1]} → G{gl_to[0]}:{gl_to[1]}" \
            if t["id"] == eq["id"] else "全区間"
        print(f"■ {t['name']}（{label}、{len(used)}ステップ）: {z:,} ゼニー")

    print(f"\n必要ゼニー合計: {grand_z:,}")
    print("必要素材合計:")
    for mat, cnt in sorted(grand_mats.items(), key=lambda kv: -kv[1]):
        print(f"  {mat} ×{cnt:,}")


# ------------------------------------- ③ 条件付きフィルタリングとソート ---

def cmd_find(conn, args):
    where, params = [], []
    if args.category:
        where.append("e.category = ?"); params.append(args.category)
    if args.weapon_type:
        where.append("e.weapon_type = ?"); params.append(args.weapon_type)
    if args.series:
        where.append("e.series = ?"); params.append(args.series)
    if args.slot:
        where.append("e.slot = ?"); params.append(args.slot)
    if args.element:
        where.append("e.element = ?"); params.append(args.element)
    if args.name:
        where.append("e.name LIKE ?"); params.append(f"%{args.name}%")
    if args.skill:
        where.append("EXISTS (SELECT 1 FROM skills sk WHERE sk.equipment_id = e.id"
                     " AND sk.skill_name LIKE ?)")
        params.append(f"%{args.skill}%")

    sql = ("SELECT e.*, "
           " (SELECT SUM(zenny) FROM upgrade_steps s WHERE s.equipment_id = e.id) AS total_zenny,"
           " (SELECT MAX(attack) FROM stats st WHERE st.equipment_id = e.id) AS max_attack,"
           " (SELECT MAX(defense) FROM stats st WHERE st.equipment_id = e.id) AS max_defense "
           "FROM equipment e")
    if where:
        sql += " WHERE " + " AND ".join(where)
    if args.sort == "attack":
        sql += " ORDER BY COALESCE(max_attack, max_defense) DESC NULLS LAST"
    elif args.sort == "zenny":
        sql += " ORDER BY total_zenny DESC"
    else:
        sql += " ORDER BY e.name"
    sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("該当する装備はありません。")
        return
    print(f"{'名称':<24} {'区分':<4} {'種別/シリーズ':<12} {'部位':<4} "
          f"{'生産G':<5} {'攻撃/防御':<8} {'総ゼニー':>10}")
    print("-" * 78)
    for r in rows:
        kind = r["weapon_type"] or r["series"] or "—"
        power = r["max_attack"] if r["category"] == "武器" else r["max_defense"]
        power = power if power is not None else "未収集"
        tz = f"{r['total_zenny']:,}" if r["total_zenny"] else "—"
        print(f"{r['name']:<24} {r['category']:<4} {kind:<12} {r['slot'] or '—':<4} "
              f"G{r['base_grade'] or '?':<4} {power!s:<8} {tz:>10}")
    if args.sort == "attack" and all(
            r["max_attack"] is None and r["max_defense"] is None for r in rows):
        print("\n※ 攻撃力データ（statsテーブル）は未収集です。データ追加後にソートが機能します。")


def cmd_skills(conn, args):
    eq = get_equipment(conn, args.name)
    rows = conn.execute(
        "SELECT skill_name, unlock, level FROM skills WHERE equipment_id = ? "
        "ORDER BY skill_name, level", (eq["id"],)).fetchall()
    if not rows:
        print(f"「{eq['name']}」のスキルデータはありません。")
        return
    print(f"■ {eq['name']} のスキル（解放グレード順）")
    for r in rows:
        print(f"  {r['skill_name']} Lv{r['level']}（{r['unlock']}〜）")


def cmd_monsters(conn, args):
    """モンスター一覧（弱点・討伐難易度・作成できる装備数）を表示する。"""
    import json as _json
    if not MONSTERS_FILE.exists():
        sys.exit("エラー: data/monsters.json がありません。collect_index_data.py を実行してください。")
    monsters = _json.loads(MONSTERS_FILE.read_text(encoding="utf-8"))

    rows = []
    for name, info in monsters.items():
        if args.element and args.element not in info.get("weakness", []):
            continue
        n_eq = conn.execute(
            "SELECT COUNT(*) FROM equipment WHERE monster = ?", (name,)).fetchone()[0]
        rows.append((info.get("min_star", 99), name,
                     "・".join(info.get("weakness", [])),
                     info.get("stars", "未収集"), n_eq))
    rows.sort(key=lambda r: (r[0], r[1]))

    print(f"{'モンスター':<14} {'討伐難易度':<10} {'弱点属性':<14} {'関連装備数':>5}")
    print("-" * 52)
    for _, name, weak, stars, n_eq in rows:
        print(f"{name:<14} {stars:<10} {weak:<14} {n_eq:>5}")
    print(f"\n計 {len(rows)} 体（討伐難易度=出現する★の範囲。低い★から出るほど早期に狩猟可能）")


# ------------------------------------------------------ 素材の逆引き検索 ---

def cmd_material(conn, args):
    rows = conn.execute(
        "SELECT e.name, e.category, SUM(m.count) AS total "
        "FROM step_materials m "
        "JOIN upgrade_steps s ON s.id = m.step_id "
        "JOIN equipment e ON e.id = s.equipment_id "
        "WHERE m.material_name LIKE ? "
        "GROUP BY e.id ORDER BY total DESC LIMIT ?",
        (f"%{args.name}%", args.limit)).fetchall()
    if not rows:
        print("該当素材を使う装備はありません。")
        return
    print(f"「{args.name}」を最大強化までに使う装備（上位{args.limit}件）:")
    for r in rows:
        print(f"  {r['name']}（{r['category']}）: ×{r['total']:,}")


def main():
    parser = argparse.ArgumentParser(description="モンハンNow 最適化攻略プランナー")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("route", help="① 派生ツリー探索（作成ルートの逆算と派生先一覧）")
    p.add_argument("name")
    p.set_defaults(func=cmd_route)

    p = sub.add_parser("cost", help="② 現在→目標グレードの必要素材・ゼニー総量計算")
    p.add_argument("name")
    p.add_argument("--from", dest="from_gl", metavar="G:L",
                   help="現在の強化段階（例 5:1）。省略時は未生産")
    p.add_argument("--to", dest="to_gl", metavar="G:L",
                   help="目標の強化段階（例 10:5）。省略時は最大")
    p.add_argument("--with-parents", action="store_true",
                   help="派生元武器の生産・強化費用も合算する")
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("find", help="③ 条件付きフィルタリングとソート")
    p.add_argument("--category", choices=["武器", "防具"])
    p.add_argument("--weapon-type", help="片手剣、大剣など")
    p.add_argument("--series", help="防具シリーズ名")
    p.add_argument("--slot", choices=["頭", "胴", "腕", "腰", "脚"])
    p.add_argument("--element", help="属性（無、火、水…）※未収集分はNULL")
    p.add_argument("--name", help="名称の部分一致")
    p.add_argument("--skill", help="指定スキルを持つ装備に絞り込み（部分一致）")
    p.add_argument("--sort", choices=["name", "attack", "zenny"], default="name")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("skills", help="装備のスキルと解放グレードを表示")
    p.add_argument("name")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("monsters", help="モンスター一覧（弱点・討伐難易度）")
    p.add_argument("--element", help="この属性が弱点のモンスターだけ表示（例: 水）")
    p.set_defaults(func=cmd_monsters)

    p = sub.add_parser("material", help="素材の逆引き（どの装備が使うか）")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_material)

    args = parser.parse_args()
    conn = connect()
    try:
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
