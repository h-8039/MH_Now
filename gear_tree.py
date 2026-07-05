# -*- coding: utf-8 -*-
"""装備準備ツリー生成 — 目標装備から遡って「何を狩り、何を作るか」を木構造で示す。

手順:
  ① 目標装備の素材から、討伐が必要なモンスターを抽出
     （素材→入手元モンスター対応表 data/material_sources.json を使用）
  ② 各モンスターに対し、討伐用の武器/防具セットを選定
     （optimize.py のダメージ期待値モデルを流用。ただし候補は
       「そのモンスターより討伐難易度(min_star)が低いモンスターの素材だけで
        作れる装備」に限定 → ツリーが必ず易しい方向へ収束する）
  ③ ②の装備の素材モンスターを抽出し、①へ再帰
  終了条件: モンスターの討伐難易度が --max-star 以下（既定★3）になったら
            「序盤装備のまま狩れる」とみなして展開を止める

使い方:
    python gear_tree.py                          # 既定の片手剣無属性最強装備
    python gear_tree.py --max-star 3 --weapon-type 片手剣
出力: コンソール + output/gear_tree.md
"""

import argparse
import io
import json
import sqlite3
import sys
from pathlib import Path

from optimize import Evaluator, load_equipment, load_skill_effects, merge_skills

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"
MONSTERS_FILE = BASE_DIR / "data" / "monsters.json"
MAT_SRC_FILE = BASE_DIR / "data" / "material_sources.json"
OUT_FILE = BASE_DIR / "output" / "gear_tree.md"

SLOTS = ["頭", "胴", "腕", "腰", "脚"]
DEFAULT_LOADOUT = ["吼剣【地咬】", "オーグヘルム", "ウルクメイル",
                   "レイアアーム", "レックスロアコイル", "レックスロアグリーヴ"]
MAX_DEPTH = 6


class GearTree:
    def __init__(self, max_star, weapon_type):
        self.max_star = max_star
        self.weapon_type = weapon_type
        conn = sqlite3.connect(DB_FILE)
        self.equipment = {e["name"]: e for e in load_equipment(conn)}
        # 装備ごとの素材一覧（全ステップ分）を先読み
        self.eq_materials = {}
        for name, eq_id in [(n, e["id"]) for n, e in self.equipment.items()]:
            mats = [r[0] for r in conn.execute(
                "SELECT DISTINCT m.material_name FROM step_materials m "
                "JOIN upgrade_steps s ON s.id = m.step_id WHERE s.equipment_id = ?",
                (eq_id,))]
            self.eq_materials[name] = mats
        conn.close()
        self.monsters = json.loads(MONSTERS_FILE.read_text(encoding="utf-8"))
        self.mat_src = json.loads(MAT_SRC_FILE.read_text(encoding="utf-8"))
        self.effects = load_skill_effects()
        self.visited = {}   # モンスター名 → 展開済みか
        self.lines = []     # 出力バッファ（コンソール用テキスト）
        self.md = []        # 出力バッファ（Markdown全体・ファイル/DL用）
        self.sets = {}      # 装備セット(タプル) → {"gear": [...], "targets": [モンスター...]}
        self.set_no = {}    # 装備セット(タプル) → セット番号（登場順=作成順）
        self.sections = []  # [{"title": 装備見出し, "md": [ツリー行...]}] 折りたたみ表示用
        self.set_md = []    # 装備セット一覧のMarkdown行

    # ------------------------------------------------ ① 素材→モンスター ---

    def min_star(self, mon):
        return self.monsters.get(mon, {}).get("min_star", 99)

    def monsters_for(self, eq_name):
        """装備の素材から必要モンスター集合を返す（汎用素材は除外）。

        複数モンスターから入手できる素材（古龍の血など）は、
        最も討伐難易度の低い1体から集めるものとして扱う。
        """
        mons = set()
        for mat in self.eq_materials.get(eq_name, []):
            sources = [m for m in self.mat_src.get(mat, []) if m in self.monsters]
            if sources:
                mons.add(min(sources, key=self.min_star))
        eq = self.equipment.get(eq_name)
        # 素材データ欠損時のフォールバック: 装備の主モンスター
        if eq and eq.get("monster") and eq["monster"] in self.monsters:
            mons.add(eq["monster"])
        return mons

    # ------------------------------------------------ ② 討伐装備の選定 ---

    def has_source_info(self, eq_name):
        """素材または主モンスターの情報があるか（無い装備は入手難度を検証できない）。"""
        eq = self.equipment.get(eq_name, {})
        return bool(self.eq_materials.get(eq_name)) or bool(eq.get("monster"))

    def craftable_before(self, eq_name, star):
        """その装備が「min_star < star のモンスター素材だけ」で作れるか。
        素材情報の無い装備は難易度を検証できないため候補から外す。"""
        if not self.has_source_info(eq_name):
            return False
        return all(self.min_star(m) < star for m in self.monsters_for(eq_name))

    def select_set(self, mon):
        """モンスター討伐用の武器+防具5部位を選定する。"""
        star = self.min_star(mon)
        weaknesses = self.monsters[mon]["weakness"]
        ev = Evaluator(self.effects, weaknesses, optimistic=False)

        weapons = [e for e in self.equipment.values()
                   if e["category"] == "武器" and e["attack"] > 0
                   and (not self.weapon_type or e["weapon_type"] == self.weapon_type)
                   and self.craftable_before(e["name"], star)]
        if not weapons:
            return None
        weapon = max(weapons, key=lambda w: ev.evaluate(w, dict(w["skills"]))[0])
        base = ev.evaluate(weapon, dict(weapon["skills"]))[0]

        chosen = [weapon]
        for slot in SLOTS:
            pieces = [e for e in self.equipment.values()
                      if e["category"] == "防具" and e["slot"] == slot
                      and self.craftable_before(e["name"], star)]
            if not pieces:
                continue
            best = max(pieces, key=lambda p: ev.evaluate(
                weapon, merge_skills(weapon["skills"], p["skills"]))[0])
            gain = ev.evaluate(weapon,
                               merge_skills(weapon["skills"], best["skills"]))[0] - base
            if gain > 0:
                chosen.append(best)
        return chosen

    # ---------------------------------------------------- ③ 再帰でツリー ---

    def emit(self, buf, indent, text):
        self.lines.append("  " * indent + text)
        buf.append("  " * indent + "- " + text)

    def set_label(self, key):
        """ツリー内での装備セットの簡潔表示（セット番号＋属性のみ）。"""
        el = self.sets[key]["gear"][0]["element"] or "無"
        return f"セット{self.set_no[key]}（{el}属性）"

    def expand_monster(self, mon, indent, depth, buf):
        star = self.min_star(mon)
        stars_s = self.monsters[mon].get("stars", "★?")
        weak_s = "・".join(self.monsters[mon]["weakness"])
        if star <= self.max_star:
            self.emit(buf, indent, f"🟢 {mon}（{stars_s}／弱点:{weak_s}）"
                                   f"→ そのまま討伐可能")
            return
        if self.visited.get(mon):
            self.emit(buf, indent, f"🔁 {mon}（{stars_s}）→ 前述のセットで討伐")
            return
        self.visited[mon] = True
        if depth >= MAX_DEPTH:
            self.emit(buf, indent, f"🔴 {mon}（{stars_s}）…（深さ上限）")
            return
        gear = self.select_set(mon)
        if not gear:
            self.emit(buf, indent, f"🔴 {mon}（{stars_s}／弱点:{weak_s}）"
                                   f"→ ⚠ ★{star}未満の素材で作れる適合武器なし"
                                   f"（汎用素材装備で挑戦）")
            return
        # 装備セットを登録（初出時にセット番号を採番。同一セットは討伐対象を追記）
        key = tuple(g["name"] for g in gear)
        first = key not in self.set_no
        if first:
            self.set_no[key] = len(self.set_no) + 1
        entry = self.sets.setdefault(key, {"gear": gear, "targets": []})
        entry["targets"].append(mon)
        label = self.set_label(key)
        if not first:
            self.emit(buf, indent, f"🔴 {mon}（{stars_s}／弱点:{weak_s}）"
                                   f"→ ⚒ {label}で討伐（作成済み）")
            return
        self.emit(buf, indent, f"🔴 {mon}（{stars_s}／弱点:{weak_s}）"
                               f"→ ⚒ {label}を作成して討伐:")
        need = set()
        for eq in gear:
            need |= self.monsters_for(eq["name"])
        for m in sorted(need, key=self.min_star):
            self.expand_monster(m, indent + 1, depth + 1, buf)

    def difficulty(self, name):
        """装備の製作難易度 = 必要モンスターの最大討伐難易度。"""
        return max((self.min_star(m) for m in self.monsters_for(name)), default=0)

    def run(self, loadout):
        all_targets = set()
        # 製作しやすい順（必要討伐難易度が低い順）に装備ごとのツリーを作る
        for name in loadout:
            if name not in self.equipment:
                sys.exit(f"エラー: 「{name}」はデータ未登録です。")
        for name in sorted(loadout, key=self.difficulty):
            eq = self.equipment[name]
            slot = eq["weapon_type"] if eq["category"] == "武器" else eq["slot"]
            d = self.difficulty(name)
            title = (f"🎯 【{slot}】{name}"
                     f"（必要討伐難易度 {'★' + str(d) if d else '情報なし'}）")
            self.lines.append(title)
            buf = []
            need = sorted(self.monsters_for(name), key=self.min_star)
            all_targets.update(need)
            for m in need:
                self.expand_monster(m, 1, 1, buf)
            if not buf:
                self.emit(buf, 1, "（必要モンスター情報なし）")
            self.sections.append({"title": title, "md": buf})
        self.build_set_list(loadout)
        self.build_md(loadout)
        return all_targets

    def build_set_list(self, loadout):
        """重複を除いた装備セット一覧（番号順=作成順）をMarkdown行で作る。"""
        out = [f"### 📦 装備セット一覧（全{len(self.sets) + 1}セット・番号順に作成）"]
        ordered = sorted(self.sets.items(), key=lambda kv: self.set_no[kv[0]])
        for key, entry in ordered:
            targets = "、".join(
                f"{m}（{self.monsters[m].get('stars', '★?')}）" for m in entry["targets"])
            out.append(f"- **{self.set_label(key)}** 討伐対象 → {targets}")
            for eq in entry["gear"]:
                slot = eq["weapon_type"] if eq["category"] == "武器" else eq["slot"]
                info = []
                if eq["category"] == "武器":
                    el = eq["element"] or "無"
                    info.append(f"攻撃{eq['attack']}／{el}"
                                + (str(eq["elem_value"]) if el != "無" else ""))
                sk = "、".join(f"{k}Lv{v}" for k, v in list(eq["skills"].items())[:3])
                if sk:
                    info.append(sk)
                out.append(f"  - 【{slot}】{eq['name']}"
                           + (f"（{'／'.join(info)}）" if info else ""))
        out.append("- **最終セット（目標装備）**")
        for name in loadout:
            eq = self.equipment[name]
            slot = eq["weapon_type"] if eq["category"] == "武器" else eq["slot"]
            out.append(f"  - 【{slot}】{name}")
        self.set_md = out
        self.lines.append("")
        self.lines.extend(s.replace("**", "") for s in out)

    def build_md(self, loadout):
        """ファイル/ダウンロード用のMarkdown全体（<details>折りたたみ付き）を組み立てる。"""
        md = [f"## 🌲 準備ツリー（製作しやすい順・討伐難易度★{self.max_star}以下まで遡り）", ""]
        for sec in self.sections:
            md += ["<details>", f"<summary>{sec['title']}</summary>", ""]
            md += sec["md"]
            md += ["", "</details>", ""]
        md += self.set_md
        self.md = md


def main():
    parser = argparse.ArgumentParser(description="装備準備ツリー生成")
    parser.add_argument("--weapon", default=DEFAULT_LOADOUT[0])
    parser.add_argument("--head", default=DEFAULT_LOADOUT[1])
    parser.add_argument("--chest", default=DEFAULT_LOADOUT[2])
    parser.add_argument("--arm", default=DEFAULT_LOADOUT[3])
    parser.add_argument("--waist", default=DEFAULT_LOADOUT[4])
    parser.add_argument("--leg", default=DEFAULT_LOADOUT[5])
    parser.add_argument("--max-star", type=int, default=3,
                        help="この討伐難易度以下は展開を止める（既定: 3）")
    parser.add_argument("--weapon-type", default="片手剣",
                        help="対策武器の武器種（既定: 片手剣。空文字で全種）")
    args = parser.parse_args()

    tree = GearTree(args.max_star, args.weapon_type or None)
    loadout = [args.weapon, args.head, args.chest, args.arm, args.waist, args.leg]
    tree.run(loadout)

    print("\n".join(tree.lines))
    print("\n凡例: 🎯目標装備 ⚒作成する対策装備 🔴要対策モンスター "
          f"🟢★{args.max_star}以下（そのまま狩れる） 🔁前述")

    OUT_FILE.parent.mkdir(exist_ok=True)
    md = ["# 装備準備ツリー", "",
          f"目標: {'、'.join(loadout)}（討伐難易度★{args.max_star}以下まで遡り）", ""]
    md += tree.md
    md += ["", "凡例: 🎯目標装備 ⚒対策装備 🔴要対策 🟢そのまま狩れる 🔁前述"]
    OUT_FILE.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\n出力: {OUT_FILE}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
