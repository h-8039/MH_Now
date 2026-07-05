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
import re
import sqlite3
import sys
from pathlib import Path

from optimize import Evaluator, load_equipment, load_skill_effects, merge_skills

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"
MONSTERS_FILE = BASE_DIR / "data" / "monsters.json"
MAT_SRC_FILE = BASE_DIR / "data" / "material_sources.json"
REC_FILE = BASE_DIR / "data" / "recommended_sets.json"
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
        # GameWith推奨装備セット（collect_recommended_sets.py で収集）
        self.rec_sets = (json.loads(REC_FILE.read_text(encoding="utf-8"))
                         if REC_FILE.exists() else [])
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

    def eval_set(self, ev, gear):
        """セット全体（武器＋防具）のダメージ期待値。"""
        skills = dict(gear[0]["skills"])
        for p in gear[1:]:
            skills = merge_skills(skills, p["skills"])
        return ev.evaluate(gear[0], skills)[0]

    def auto_weapon(self, star, ev):
        weapons = [e for e in self.equipment.values()
                   if e["category"] == "武器" and e["attack"] > 0
                   and (not self.weapon_type or e["weapon_type"] == self.weapon_type)
                   and self.craftable_before(e["name"], star)]
        if not weapons:
            return None
        return max(weapons, key=lambda w: ev.evaluate(w, dict(w["skills"]))[0])

    def auto_armor(self, slot, star, ev, weapon):
        pieces = [e for e in self.equipment.values()
                  if e["category"] == "防具" and e["slot"] == slot
                  and self.craftable_before(e["name"], star)]
        if not pieces:
            return None
        return max(pieces, key=lambda p: ev.evaluate(
            weapon, merge_skills(weapon["skills"], p["skills"]))[0])

    def adjust_recommended(self, rs, star, ev):
        """GameWith推奨セットを基本に、難易度制約で作れない部位だけ代替する。

        戻り値: (gear, subs) — subs は代替した装備名のリスト。
        武器がどうしても用意できない場合は (None, None)。
        """
        subs = []
        w = self.equipment.get(rs["weapon"])
        if not (w and w["category"] == "武器" and w["attack"] > 0
                and self.craftable_before(rs["weapon"], star)):
            w = self.auto_weapon(star, ev)
            if not w:
                return None, None
            subs.append(w["name"])
        gear = [w]
        base = ev.evaluate(w, dict(w["skills"]))[0]
        for slot in SLOTS:
            name = rs["armor"].get(slot)
            p = self.equipment.get(name)
            if p and self.craftable_before(name, star):
                if p["slot"] != slot:  # 禍鎧/ミヅハ等はDB上部位なしのため補完
                    p = dict(p, slot=slot)
                gear.append(p)
                continue
            alt = self.auto_armor(slot, star, ev, w)
            if alt and ev.evaluate(w, merge_skills(
                    w["skills"], alt["skills"]))[0] > base:
                gear.append(alt)
                subs.append(alt["name"])
        return gear, subs

    def select_set(self, mon):
        """モンスター討伐用の武器+防具5部位を選定する。

        GameWith推奨セット（recommended_sets.json）を基本とし、
        討伐難易度の制約（craftable_before）で作れない装備だけ
        代替品に置き換える。推奨セットが使えない場合は従来の自動選定。
        戻り値: (gear, base_title, subs) または None。
        """
        star = self.min_star(mon)
        weaknesses = self.monsters[mon]["weakness"]
        ev = Evaluator(self.effects, weaknesses, optimistic=False)

        cands = []
        for rs in self.rec_sets:
            if self.weapon_type and rs["weapon_type"] != self.weapon_type:
                continue
            gear, subs = self.adjust_recommended(rs, star, ev)
            if gear:
                cands.append((self.eval_set(ev, gear), gear, rs["title"], subs))
        if cands:
            _, gear, title, subs = max(cands, key=lambda c: c[0])
            # 記事由来の部位が少ない（ほぼ代替）場合はベース表記しない
            if len(gear) - len(subs) < 3:
                title = None
            return gear, title, subs

        # フォールバック: 従来の自動選定
        weapon = self.auto_weapon(star, ev)
        if not weapon:
            return None
        base = ev.evaluate(weapon, dict(weapon["skills"]))[0]
        chosen = [weapon]
        for slot in SLOTS:
            best = self.auto_armor(slot, star, ev, weapon)
            if best and ev.evaluate(weapon, merge_skills(
                    weapon["skills"], best["skills"]))[0] > base:
                chosen.append(best)
        return chosen, None, []

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
        res = self.select_set(mon)
        if not res:
            self.emit(buf, indent, f"🔴 {mon}（{stars_s}／弱点:{weak_s}）"
                                   f"→ ⚠ ★{star}未満の素材で作れる適合武器なし"
                                   f"（汎用素材装備で挑戦）")
            return
        gear, base_title, subs = res
        # 装備セットを登録（初出時にセット番号を採番。同一セットは討伐対象を追記）
        key = tuple(g["name"] for g in gear)
        first = key not in self.set_no
        if first:
            self.set_no[key] = len(self.set_no) + 1
        entry = self.sets.setdefault(key, {"gear": gear, "targets": [],
                                           "base": base_title, "subs": subs})
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
        self.compute_flow()
        self.build_flow(loadout)
        self.build_flow_dot(loadout)
        self.build_set_list(loadout)
        self.build_md(loadout)
        return all_targets

    # ---------------------------------------------- ④ 討伐フローチャート ---

    def mon_stars(self, m):
        return self.monsters.get(m, {}).get("stars", "★?")

    def compute_flow(self):
        """セット間の依存関係（素材モンスター→討伐セット）から作成順を求め、
        セット番号を作成順に振り直す。

        DFS展開の登場順では「後のセットが先のセットの素材討伐に必要」という
        逆転が起こり得るため、トポロジカルソートで実際に作れる順に並べる。
        """
        self.mon_set = {}   # 赤モンスター → 討伐に使うセットkey
        for key, e in self.sets.items():
            for m in e["targets"]:
                self.mon_set[m] = key
        self.set_mats = {}  # セットkey → 素材モンスター集合
        deps = {}
        for key in self.sets:
            need = set()
            for gname in key:
                need |= self.monsters_for(gname)
            self.set_mats[key] = need
            deps[key] = {self.mon_set[m] for m in need
                         if m in self.mon_set and self.mon_set[m] != key}
        order = []
        remaining = set(deps)
        while remaining:
            ready = sorted((k for k in remaining if not (deps[k] & remaining)),
                           key=lambda k: self.set_no[k])
            if not ready:   # 想定外の循環時は登場順で1つずつ取り出す
                ready = [min(remaining, key=lambda k: self.set_no[k])]
            for k in ready:
                order.append(k)
                remaining.discard(k)
        # ツリー本文中の「セットN」表記も新番号に書き換える
        renum = {self.set_no[k]: i + 1 for i, k in enumerate(order)}
        pat = re.compile(r"セット(\d+)")
        fix = lambda s: pat.sub(lambda m: f"セット{renum[int(m.group(1))]}", s)
        self.lines = [fix(s) for s in self.lines]
        for sec in self.sections:
            sec["md"] = [fix(s) for s in sec["md"]]
        self.set_no = {k: i + 1 for i, k in enumerate(order)}
        self.set_order = order

    def build_flow(self, loadout):
        """作成順に「セット作成→討伐」を並べたフロー（Markdown＋手順データ）。"""
        steps = []
        for k in self.set_order:
            greens = sorted((m for m in self.set_mats[k]
                             if self.min_star(m) <= self.max_star), key=self.min_star)
            reds = sorted((m for m in self.set_mats[k]
                           if self.min_star(m) > self.max_star), key=self.min_star)
            steps.append({
                "no": self.set_no[k],
                "label": self.set_label(k),
                "greens": greens,
                "reds": [(m, self.set_no.get(self.mon_set.get(m))) for m in reds],
                "targets": self.sets[k]["targets"],
            })
        self.flow_steps = steps

        md = ["### 🧭 討伐フロー（上から順に実行）"]
        for s in steps:
            md.append(f"{s['no']}. **⚒ {s['label']}を作成**")
            if s["greens"]:
                md.append("   - 素材集め: 🟢 " + "、".join(s["greens"])
                          + "（そのまま討伐）")
            if s["reds"]:
                md.append("   - 素材集め: 🔴 " + "、".join(
                    f"{m}（セット{n}で討伐）" if n else f"{m}（⚠汎用装備で挑戦）"
                    for m, n in s["reds"]))
            md.append("   - ⚔ **このセットで討伐 → " + "、".join(
                f"{m}（{self.mon_stars(m)}）" for m in s["targets"]) + "**")
        md.append(f"{len(steps) + 1}. **🎯 目標装備を製作: {'、'.join(loadout)}**")
        goal_mats = []
        for name in loadout:
            for m in sorted(self.monsters_for(name), key=self.min_star):
                if self.min_star(m) <= self.max_star:
                    goal_mats.append(f"🟢 {m}")
                else:
                    n = self.set_no.get(self.mon_set.get(m))
                    goal_mats.append(f"{m}（セット{n}で討伐済み）" if n else f"{m}（⚠）")
        if goal_mats:
            md.append("   - 素材: " + "、".join(dict.fromkeys(goal_mats)))
        self.flow_md = md
        self.lines.append("")
        self.lines.extend(s.replace("**", "") for s in md)

    def build_flow_dot(self, loadout):
        """Streamlit の st.graphviz_chart 用の DOT フローチャートを組み立てる。"""
        def esc(t):
            return t.replace('"', '\\"')

        def clip(names, n=4):
            names = list(names)
            return "、".join(names[:n]) + (f" 他{len(names) - n}体"
                                           if len(names) > n else "")

        d = ["digraph flow {",
             "  rankdir=TB;",
             '  node [fontname="sans-serif", fontsize=12, shape=box, '
             'style="rounded,filled", fillcolor="#dbeafe", color="#64748b"];',
             '  edge [color="#64748b"];',
             '  start [label="スタート（手持ち装備）", fillcolor="#e2e8f0"];']
        prev = "start"
        for s in self.flow_steps:
            sid, kid = f"s{s['no']}", f"k{s['no']}"
            lab = [f"⚒ {s['label']}を作成"]
            if s["greens"]:
                lab.append("素材: " + clip(s["greens"]))
            if s["reds"]:
                lab.append("要: " + clip(
                    [f"{m}(セット{n})" if n else f"{m}(⚠)" for m, n in s["reds"]]))
            label = "\\n".join(esc(x) for x in lab)
            d.append(f'  {sid} [label="{label}"];')
            tgt = clip([f"{m}（{self.mon_stars(m)}）" for m in s["targets"]], 3)
            d.append(f'  {kid} [shape=ellipse, fillcolor="#fee2e2", '
                     f'label="{esc("討伐: " + tgt)}"];')
            d.append(f"  {prev} -> {sid} -> {kid};")
            prev = kid
        d.append(f'  goal [fillcolor="#dcfce7", '
                 f'label="{esc("🎯 " + "、".join(loadout) + " を製作")}"];')
        d.append(f"  {prev} -> goal;")
        d.append("}")
        self.flow_dot = "\n".join(d)

    def build_set_list(self, loadout):
        """重複を除いた装備セット一覧（番号順=作成順）をMarkdown行で作る。"""
        out = [f"### 📦 装備セット一覧（全{len(self.sets) + 1}セット・番号順に作成）"]
        ordered = sorted(self.sets.items(), key=lambda kv: self.set_no[kv[0]])
        for key, entry in ordered:
            targets = "、".join(
                f"{m}（{self.monsters[m].get('stars', '★?')}）" for m in entry["targets"])
            src = (f"GameWith「{entry['base']}」ベース" if entry.get("base")
                   else "自動選定")
            out.append(f"- **{self.set_label(key)}**（{src}） 討伐対象 → {targets}")
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
                mark = " ※代替" if eq["name"] in entry.get("subs", []) else ""
                out.append(f"  - 【{slot}】{eq['name']}"
                           + (f"（{'／'.join(info)}）" if info else "") + mark)
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
        md = list(self.flow_md) + [""]
        md += self.set_md + [""]
        md += [f"### 🌲 詳細ツリー（討伐難易度★{self.max_star}以下まで遡り）", ""]
        for sec in self.sections:
            md += ["<details>", f"<summary>{sec['title']}</summary>", ""]
            md += sec["md"]
            md += ["", "</details>", ""]
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
