# -*- coding: utf-8 -*-
"""④ 装備最適化アルゴリズム — ダメージ期待値が最大になる編成を探索する。

■ ダメージモデル（1モーションあたりの期待値の比較用指標）
    攻撃力合計 = 武器攻撃力 × (1 + Σ攻撃力%/100) + Σ攻撃力実数
    会心補正   = 1 + 0.25 × min(Σ会心率, 100)/100          … 会心は+25%として期待値化
    属性合計   = (武器属性値 + Σ属性攻撃力) × 弱点係数
                 弱点係数: 弱点属性=1.0 / 非弱点=0.3 / 無属性武器=属性ダメージなし
    期待値EV  = (攻撃力合計 × 会心補正 + 属性合計) × (1 + Σ与ダメージ%/100)

■ 前提・制約
    - 全装備をグレード10最大強化（statsテーブルの最大値）と仮定
    - 発動条件付きスキル（ジャスト巧撃・火事場力など）は既定で除外。
      --optimistic で「常時発動」とみなして含める
    - 「攻撃・境地」は無属性武器装備時のみ有効（ゲーム仕様）
    - 同名スキルは装備間でレベル加算され、スキルの最大Lvでキャップ
    - 武器の基礎会心率は未収集のため0%と仮定（既知の制限）

■ 使い方
    python optimize.py --monster ティガレックス亜種 --weapon-type 片手剣
    python optimize.py --element 水 --element 龍 --weapon-type 太刀 --optimistic
"""

import argparse
import io
import json
import sqlite3
import sys
from itertools import product
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"
SKILL_FILE = BASE_DIR / "data" / "skill_effects.json"
META_FILE = BASE_DIR / "data" / "equipment.json"

SLOTS = ["頭", "胴", "腕", "腰", "脚"]
CRIT_BONUS = 0.25          # 会心1回あたりのダメージ倍率増分
OFF_ELEMENT_FACTOR = 0.3   # 非弱点属性のダメージ係数


def load_skill_effects():
    with open(SKILL_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_equipment(conn):
    """装備ごとの最大ステータスとスキル構成（最大Lv）を読み込む。"""
    eqs = {}
    for r in conn.execute(
            "SELECT e.id, e.name, e.category, e.weapon_type, e.slot, e.element, e.crit, "
            " e.monster, "
            " (SELECT MAX(attack) FROM stats s WHERE s.equipment_id=e.id) AS atk, "
            " (SELECT MAX(element_value) FROM stats s WHERE s.equipment_id=e.id) AS elem "
            "FROM equipment e"):
        eqs[r[0]] = {"id": r[0], "name": r[1], "category": r[2], "weapon_type": r[3],
                     "slot": r[4], "element": r[5], "crit": r[6] or 0, "monster": r[7],
                     "attack": r[8] or 0, "elem_value": r[9] or 0, "skills": {}}
    for eq_id, sk, lv in conn.execute(
            "SELECT equipment_id, skill_name, MAX(level) FROM skills "
            "GROUP BY equipment_id, skill_name"):
        if eq_id in eqs:
            eqs[eq_id]["skills"][sk] = lv
    return list(eqs.values())


class Evaluator:
    def __init__(self, effects, weaknesses, optimistic):
        self.effects = effects
        self.weaknesses = weaknesses
        self.optimistic = optimistic

    def skill_active(self, name, weapon, skill_totals):
        """スキルが評価対象か（条件付きスキルの扱い）。"""
        info = self.effects.get(name)
        if info is None or not any(info["effects"].values()):
            return False
        # 「◯◯・境地」系は基底スキルLv5の発動が条件（例: 攻撃・境地 ← 攻撃Lv5）
        if "・境地" in name:
            base = name.split("・境地")[0]
            if skill_totals.get(base, 0) < 5:
                return False
        # 属性攻撃強化は武器属性が一致する場合のみ
        if info.get("element") and info["element"] != weapon["element"]:
            return False
        if info.get("condition") and not self.optimistic:
            return False
        return True

    def evaluate(self, weapon, skill_totals):
        """スキル合計から期待値EVと内訳を計算する。"""
        attack_pct = attack_flat = crit_dmg = 0
        crit = weapon.get("crit", 0)  # 武器の基礎会心率から開始
        elem_flat = elem_pct = elem_pct_crit = damage_pct = 0
        used = []
        for name, lv in skill_totals.items():
            if not self.skill_active(name, weapon, skill_totals):
                continue
            info = self.effects[name]
            max_lv = max((int(k) for k in info["effects"]), default=0)
            lv = min(lv, max_lv)
            eff = info["effects"].get(str(lv), {})
            if not eff:
                continue
            attack_pct += eff.get("attack_pct", 0)
            attack_flat += eff.get("attack_flat", 0)
            crit += eff.get("crit_rate", 0)
            crit_dmg += eff.get("crit_dmg_pct", 0)
            elem_flat += eff.get("elem_flat", 0)
            # 属性倍率系: 会心撃【属性】は会心発生時のみ乗る
            if "会心撃" in name:
                elem_pct_crit += eff.get("elem_pct", 0)
            else:
                elem_pct += eff.get("elem_pct", 0)
            damage_pct += eff.get("damage_pct", 0)
            used.append((name, lv, info.get("condition")))

        attack_total = weapon["attack"] * (1 + attack_pct / 100) + attack_flat
        crit = max(-100, min(crit, 100))
        pos_crit = max(crit, 0)
        neg_crit = max(-crit, 0)
        # 会心期待値: 正会心は(25%+超会心)増し、マイナス会心は25%減（75%ダメージ）
        crit_mult = (1 + (CRIT_BONUS + crit_dmg / 100) * pos_crit / 100
                     - CRIT_BONUS * neg_crit / 100)
        element = weapon["element"] or "無"
        if element == "無":
            elem_total = 0.0
        else:
            factor = 1.0 if element in self.weaknesses else OFF_ELEMENT_FACTOR
            elem_total = ((weapon["elem_value"] + elem_flat)
                          * (1 + elem_pct / 100 + elem_pct_crit / 100 * pos_crit / 100)
                          * factor)
        ev = (attack_total * crit_mult + elem_total) * (1 + damage_pct / 100)
        return ev, {"attack_total": attack_total, "crit": crit,
                    "elem_total": elem_total, "damage_pct": damage_pct,
                    "used_skills": used}


def merge_skills(*skill_dicts):
    total = {}
    for d in skill_dicts:
        for k, v in d.items():
            total[k] = total.get(k, 0) + v
    return total


def main():
    parser = argparse.ArgumentParser(description="モンハンNow 装備最適化（ダメージ期待値最大化）")
    parser.add_argument("--monster", help="対象モンスター名（data/equipment.json収録分）")
    parser.add_argument("--element", action="append", default=[],
                        help="弱点属性を直接指定（複数可: --element 水 --element 龍）")
    parser.add_argument("--weapon-type", help="武器種で絞り込み（例: 片手剣）")
    parser.add_argument("--optimistic", action="store_true",
                        help="発動条件付きスキルも常時発動とみなす")
    parser.add_argument("--top", type=int, default=3, help="表示する編成数")
    parser.add_argument("--weapon-candidates", type=int, default=12,
                        help="探索対象とする武器の上位数")
    parser.add_argument("--per-slot", type=int, default=8,
                        help="部位ごとの防具候補数")
    args = parser.parse_args()

    weaknesses = list(args.element)
    if args.monster:
        monsters = {}
        monsters_file = BASE_DIR / "data" / "monsters.json"
        if monsters_file.exists():  # collect_index_data.py が生成する全モンスター弱点
            with open(monsters_file, encoding="utf-8") as f:
                monsters = json.load(f)
        if args.monster in monsters:
            weaknesses += monsters[args.monster]["weakness"]
        else:  # フォールバック: equipment.json の攻略メタデータ
            with open(META_FILE, encoding="utf-8") as f:
                meta_monsters = json.load(f)["monsters"]
            if args.monster not in meta_monsters:
                cand = [n for n in monsters if args.monster in n][:5]
                hint = f"（候補: {'、'.join(cand)}）" if cand else ""
                sys.exit(f"エラー: 「{args.monster}」の弱点データがありません{hint}。"
                         f"--element で直接指定するか、collect_index_data.py を実行してください。")
            weaknesses += meta_monsters[args.monster]["weakness"].replace("・", " ").split()
    if not weaknesses and not args.monster:
        print("※ 弱点属性の指定なし → 無属性・物理重視の編成を探索します。")

    conn = sqlite3.connect(DB_FILE)
    equipment = load_equipment(conn)
    conn.close()
    ev = Evaluator(load_skill_effects(), weaknesses, args.optimistic)

    # --- 武器候補: 単体EV上位 ---
    weapons = [e for e in equipment if e["category"] == "武器" and e["attack"] > 0]
    if args.weapon_type:
        weapons = [w for w in weapons if w["weapon_type"] == args.weapon_type]
        if not weapons:
            sys.exit(f"エラー: 武器種「{args.weapon_type}」の武器がありません。")
    weapons.sort(key=lambda w: ev.evaluate(w, dict(w["skills"]))[0], reverse=True)
    weapons = weapons[:args.weapon_candidates]
    ref_weapon = weapons[0]

    # --- 防具候補: 基準武器に対する単体寄与の上位（部位別） ---
    base_ev = ev.evaluate(ref_weapon, dict(ref_weapon["skills"]))[0]
    armor_by_slot = {}
    for slot in SLOTS:
        pieces = [e for e in equipment if e["category"] == "防具" and e["slot"] == slot]
        scored = []
        for p in pieces:
            gain = ev.evaluate(ref_weapon,
                               merge_skills(ref_weapon["skills"], p["skills"]))[0] - base_ev
            scored.append((gain, p))
        scored.sort(key=lambda x: -x[0])
        armor_by_slot[slot] = [p for gain, p in scored[:args.per_slot]]

    # --- 全探索（武器 × 部位別候補の直積） ---
    results = []
    for w in weapons:
        for combo in product(*(armor_by_slot[s] for s in SLOTS)):
            totals = merge_skills(w["skills"], *(p["skills"] for p in combo))
            score, detail = ev.evaluate(w, totals)
            results.append((score, w, combo, detail))
    results.sort(key=lambda x: -x[0])

    # --- 出力 ---
    mode = "楽観モード（条件付きスキル込み）" if args.optimistic else "安定モード（常時発動スキルのみ）"
    target = args.monster or ("弱点: " + "・".join(weaknesses) if weaknesses else "指定なし")
    print(f"■ 最適編成探索  対象: {target} ／ {mode}")
    print(f"  探索範囲: 武器{len(weapons)}種 × 防具{args.per_slot}候補^5部位"
          f"（全装備G10最大強化・基礎会心率は収集値を使用）\n")

    seen = set()
    rank = 0
    for score, w, combo, detail in results:
        key = (w["name"],) + tuple(p["name"] for p in combo)
        if key in seen:
            continue
        seen.add(key)
        rank += 1
        if rank > args.top:
            break
        elem_s = f"{w['element']}{w['elem_value']}" if (w["element"] or "無") != "無" else "無属性"
        crit_s = f"／会心{w['crit']:+d}%" if w.get("crit") else ""
        print(f"【第{rank}位】期待値 {score:,.0f}")
        print(f"  武器: {w['name']}（{w['weapon_type']}／攻撃{w['attack']}／{elem_s}{crit_s}）")
        for slot, p in zip(SLOTS, combo):
            sk = "、".join(f"{k}Lv{v}" for k, v in p["skills"].items()) or "—"
            print(f"  {slot}: {p['name']}（{sk}）")
        print(f"  内訳: 攻撃力{detail['attack_total']:,.0f} × 会心{detail['crit']}%補正"
              f" + 属性{detail['elem_total']:,.0f} × 与ダメ+{detail['damage_pct']}%")
        act = "、".join(f"{n}Lv{l}" + (f"[{c}]" if c else "") for n, l, c in detail["used_skills"])
        print(f"  発動スキル: {act or 'なし'}\n")

    if not args.optimistic:
        print("※ 条件付きスキル（ジャスト巧撃など）は除外しています。--optimistic で含められます。")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
