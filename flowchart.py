# -*- coding: utf-8 -*-
"""モンスターハンターNow 装備作成フローチャート生成ツール

武器・頭・胴・腕・腰・脚に作りたい装備を指定すると、
素材元モンスターの狩猟フェーズ順に並べた作成フローチャートを
Mermaid (Markdown) と 自己完結HTML の2形式で出力する。

素材データの優先順位:
    1. raw_pages/*.csv （全グレード・全レベルの実数データ）
    2. data/equipment.json の craft_materials / upgrade_key_materials （概略）

使い方:
    python flowchart.py                       # 既定の片手剣無属性最強装備
    python flowchart.py --list                # 登録装備の一覧を表示
    python flowchart.py --weapon 吼剣【地咬】 --head オーグヘルム ...
"""

import argparse
import io
import json
import sys
from collections import defaultdict
from pathlib import Path

import materials_csv

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "equipment.json"
OUT_DIR = BASE_DIR / "output"

SLOT_ORDER = ["武器", "頭", "胴", "腕", "腰", "脚"]

# 既定の編成（片手剣・無属性最強装備）
DEFAULT_LOADOUT = {
    "武器": "吼剣【地咬】",
    "頭": "オーグヘルム",
    "胴": "ウルクメイル",
    "腕": "レイアアーム",
    "腰": "レックスロアコイル",
    "脚": "レックスロアグリーヴ",
}


def load_data():
    with open(DATA_FILE, encoding="utf-8") as f:
        return json.load(f)


def fmt_count(n):
    """素材個数を表示用に整形。未確認(null)は ×? とする。"""
    return f"×{n}" if n is not None else "×?"


def fmt_materials(materials):
    return "、".join(f"{name}{fmt_count(cnt)}" for name, cnt in materials.items())


def resolve_loadout(args, data):
    """CLI引数から部位→装備名の辞書を作り、データに存在するか検証する。"""
    loadout = {
        "武器": args.weapon,
        "頭": args.head,
        "胴": args.chest,
        "腕": args.arm,
        "腰": args.waist,
        "脚": args.leg,
    }
    equipment = data["equipment"]
    for slot, name in loadout.items():
        if name is None:
            continue
        if name not in equipment:
            sys.exit(f"エラー: 「{name}」はデータ未登録です。--list で登録装備を確認するか、"
                     f"data/equipment.json に追加してください。")
        actual = equipment[name]["slot"]
        if actual != slot:
            sys.exit(f"エラー: 「{name}」は{actual}装備です（{slot}に指定されています）。")
    return {s: n for s, n in loadout.items() if n is not None}


def build_phases(loadout, data):
    """選択装備をモンスターの狩猟フェーズ順にグループ化する。

    戻り値: [(phase番号, モンスター名, モンスター情報, [(部位, 装備名, 装備情報), ...]), ...]
    """
    monsters = data["monsters"]
    equipment = data["equipment"]
    by_monster = defaultdict(list)
    for slot in SLOT_ORDER:
        if slot not in loadout:
            continue
        name = loadout[slot]
        eq = equipment[name]
        by_monster[eq["monster"]].append((slot, name, eq))

    phases = []
    for mon, items in by_monster.items():
        if mon not in monsters:
            sys.exit(f"エラー: モンスター「{mon}」が data/equipment.json の monsters に未登録です。")
        phases.append((monsters[mon]["phase"], mon, monsters[mon], items))
    phases.sort(key=lambda p: p[0])
    return phases


def build_summaries(loadout, csv_data):
    """選択装備ごとのCSV集計（生産／グレードアップ／総計）を返す。CSVに無い場合は None。"""
    return {name: materials_csv.summarize(csv_data.get(name, []))
            for name in loadout.values()}


def craft_info(name, eq, summaries):
    """生産素材・ゼニー・生産グレードを (materials, zenny, grade, 実数か) で返す。"""
    s = summaries.get(name)
    if s:
        c = s["craft"]
        return c["materials"], c["zenny"], c["grade"], True
    return eq["craft_materials"], eq.get("craft_zenny"), eq["craft_grade"], False


def grand_totals(loadout, summaries):
    """選択装備全体の必要素材・ゼニー総計（CSVに実数がある装備のみ）。"""
    total_mats = {}
    total_zenny = 0
    missing = []
    for name in loadout.values():
        s = summaries.get(name)
        if not s:
            missing.append(name)
            continue
        total_zenny += s["total"]["zenny"]
        for mat, cnt in s["total"]["materials"].items():
            total_mats[mat] = total_mats.get(mat, 0) + cnt
    ordered = dict(sorted(total_mats.items(), key=lambda kv: -kv[1]))
    return ordered, total_zenny, missing


# ---------------------------------------------------------------- Mermaid ---

def gen_mermaid(loadout, phases, data, summaries):
    tips = data.get("phase_tips", {})
    lines = ["flowchart TD"]
    lines.append('    START(["狩り開始（初期装備）"])')
    prev_tail = "START"

    for idx, (phase_no, mon, mon_info, items) in enumerate(phases, start=1):
        m_id = f"M{idx}"
        sg_id = f"P{idx}"
        lines.append(f'    subgraph {sg_id}["フェーズ{idx}：{mon}を狩る（{mon_info["stars"]}）"]')
        lines.append(f'        {m_id}["{mon} 討伐<br/>弱点: {mon_info["weakness"]}<br/>{mon_info["unlock"]}"]')
        for j, (slot, name, eq) in enumerate(items, start=1):
            e_id = f"E{idx}_{j}"
            mats, zenny, grade, exact = craft_info(name, eq, summaries)
            zenny_s = f"<br/>ゼニー{zenny:,}" if zenny else ""
            skills = " / ".join(s["name"] for s in eq["skills"])
            lines.append(
                f'        {e_id}["【{slot}】{name}<br/>G{grade}で生産'
                f'<br/>素材: {fmt_materials(mats)}{zenny_s}<br/>スキル: {skills}"]'
            )
            lines.append(f"        {m_id} --> {e_id}")
        lines.append("    end")
        lines.append(f"    {prev_tail} --> {m_id}")
        prev_tail = f"E{idx}_{len(items)}"

    lines.append('    GOAL(["装備完成 → 各装備をG10へ強化"])')
    lines.append(f"    {prev_tail} --> GOAL")

    # Markdown 全体を組み立て
    md = ["# モンハンNow 装備作成フローチャート", ""]
    md.append("## 目標装備")
    md.append("")
    md.append("| 部位 | 装備 | 入手元 | 主要スキル | G10までの総ゼニー |")
    md.append("| --- | --- | --- | --- | --- |")
    for slot in SLOT_ORDER:
        if slot not in loadout:
            continue
        name = loadout[slot]
        eq = data["equipment"][name]
        skills = " / ".join(s["name"] for s in eq["skills"])
        s = summaries.get(name)
        zenny = f"{s['total']['zenny']:,}" if s else "—"
        md.append(f"| {slot} | {name} | {eq['monster']} | {skills} | {zenny} |")
    md.append("")
    md.append("```mermaid")
    md.extend(lines)
    md.append("```")
    md.append("")

    md.append("## フェーズ別の進め方")
    md.append("")
    for idx, (phase_no, mon, mon_info, items) in enumerate(phases, start=1):
        md.append(f"### フェーズ{idx}：{mon}（{mon_info['stars']}／弱点: {mon_info['weakness']}）")
        md.append("")
        md.append(f"- 出現: {mon_info['habitat']}／{mon_info['unlock']}")
        md.append(f"- 攻略メモ: {mon_info['advice']}")
        tip = tips.get(str(phase_no))
        if tip:
            md.append(f"- 進め方: {tip}")
        md.append("")
        for slot, name, eq in items:
            mats, zenny, grade, exact = craft_info(name, eq, summaries)
            md.append(f"#### 【{slot}】{name}")
            md.append("")
            md.append(f"- 生産: グレード{grade}／素材: {fmt_materials(mats)}"
                      + (f"／ゼニー{zenny:,}" if zenny else ""))
            for milestone in eq.get("milestones", []):
                md.append(f"- {milestone}")
            s = summaries.get(name)
            if s:
                md.append(f"- 強化素材（実数・{s['max_step']}まで）:")
                md.append("")
                md.append("  | 区間 | ゼニー | 素材 |")
                md.append("  | --- | --- | --- |")
                for b in s["grade_ups"]:
                    md.append(f"  | {b['label']} | {b['zenny']:,} | {fmt_materials(b['materials'])} |")
                md.append("")
            else:
                up = eq.get("upgrade_key_materials", {})
                if up:
                    md.append("- 強化の主要素材（概略・個数未確認あり）:")
                    for step, m in up.items():
                        md.append(f"    - {step}: {fmt_materials(m)}")
            md.append(f"- 出典: {eq['source']}")
            md.append("")

    mats_total, zenny_total, missing = grand_totals(loadout, summaries)
    if mats_total:
        md.append("## 必要素材の総計（生産〜最大強化）")
        md.append("")
        md.append(f"総ゼニー: **{zenny_total:,}**")
        md.append("")
        md.append("| 素材 | 合計個数 |")
        md.append("| --- | --- |")
        for mat, cnt in mats_total.items():
            md.append(f"| {mat} | {cnt:,} |")
        if missing:
            md.append("")
            md.append(f"※ CSV未収録のため総計に含まれない装備: {'、'.join(missing)}")
        md.append("")

    md.append("---")
    md.append("※ 素材個数は raw_pages のCSV（全グレード実数）を優先し、CSVに無い装備は概略表示（×?）。")
    md.append(f"※ 情報源: {'、'.join(data['meta']['sources'])}")
    return "\n".join(md) + "\n"


# ------------------------------------------------------------------- HTML ---

HTML_STYLE = """
:root {
  --bg: #f5f2ea; --card: #ffffff; --ink: #2d2a24; --sub: #6b6558;
  --line: #d8d2c4; --accent: #a05a2c; --accent2: #47632a; --warn: #9c3b1e;
  --phase-bg: #ece7db;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1d1b17; --card: #2a2721; --ink: #ece7db; --sub: #a89f8d;
    --line: #4a453a; --accent: #e0a068; --accent2: #a2c47e; --warn: #e0785a;
    --phase-bg: #24211c;
  }
}
:root[data-theme="dark"] {
  --bg: #1d1b17; --card: #2a2721; --ink: #ece7db; --sub: #a89f8d;
  --line: #4a453a; --accent: #e0a068; --accent2: #a2c47e; --warn: #e0785a;
  --phase-bg: #24211c;
}
:root[data-theme="light"] {
  --bg: #f5f2ea; --card: #ffffff; --ink: #2d2a24; --sub: #6b6558;
  --line: #d8d2c4; --accent: #a05a2c; --accent2: #47632a; --warn: #9c3b1e;
  --phase-bg: #ece7db;
}
body { background: var(--bg); color: var(--ink);
  font-family: "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif;
  line-height: 1.65; margin: 0; padding: 24px 16px 48px; }
.wrap { max-width: 860px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 4px; }
h2.sec { font-size: 1.15rem; margin: 32px 0 10px; color: var(--accent); }
.lead { color: var(--sub); margin: 0 0 20px; font-size: .92rem; }
table.grid { width: 100%; border-collapse: collapse; margin-bottom: 8px;
  background: var(--card); border: 1px solid var(--line); font-size: .9rem; }
table.grid th, table.grid td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
table.grid th { background: var(--phase-bg); white-space: nowrap; }
table.grid td.num { text-align: right; font-variant-numeric: tabular-nums; }
.node { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 16px; }
.terminal { text-align: center; font-weight: 700; border-radius: 999px;
  border: 2px solid var(--accent); max-width: 420px; margin: 0 auto; }
.arrow { text-align: center; color: var(--sub); font-size: 1.3rem; line-height: 1; padding: 8px 0; }
.phase { background: var(--phase-bg); border: 1px solid var(--line); border-radius: 14px;
  padding: 14px 16px 16px; margin: 0 auto; }
.phase h2 { font-size: 1.08rem; margin: 0 0 8px; color: var(--accent); }
.monster { border-left: 4px solid var(--warn); }
.monster .name { font-weight: 700; }
.meta { color: var(--sub); font-size: .86rem; margin: 2px 0 0; }
.equips { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 10px; margin-top: 10px; }
.equip { border-left: 4px solid var(--accent2); }
.equip .slot { display: inline-block; font-size: .78rem; font-weight: 700;
  color: var(--accent2); border: 1px solid var(--accent2); border-radius: 4px;
  padding: 0 6px; margin-right: 6px; }
.equip .name { font-weight: 700; }
.equip ul { margin: 6px 0 0; padding-left: 18px; font-size: .86rem; color: var(--sub); }
.tip { font-size: .86rem; margin-top: 10px; color: var(--ink); }
.tip::before { content: "📌 "; }
details.gradeup { margin-top: 12px; font-size: .88rem; }
details.gradeup summary { cursor: pointer; font-weight: 700; color: var(--accent); }
details.gradeup .inner { overflow-x: auto; margin-top: 8px; }
.foot { color: var(--sub); font-size: .8rem; margin-top: 28px; }
.foot a { color: var(--accent); }
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def gen_html_fragment(loadout, phases, data, summaries, title):
    tips = data.get("phase_tips", {})
    h = []
    h.append(f"<title>{esc(title)}</title>")
    h.append(f"<style>{HTML_STYLE}</style>")
    h.append('<div class="wrap">')
    h.append(f"<h1>{esc(title)}</h1>")
    h.append('<p class="lead">素材元モンスターの狩猟フェーズ順に、装備を作成する流れを示します。'
             '素材個数は保存済みCSV（全グレード実数）に基づきます。</p>')

    h.append('<table class="grid"><tr><th>部位</th><th>装備</th><th>入手元</th>'
             '<th>主要スキル</th><th>G10までの総ゼニー</th></tr>')
    for slot in SLOT_ORDER:
        if slot not in loadout:
            continue
        name = loadout[slot]
        eq = data["equipment"][name]
        skills = " / ".join(s["name"] for s in eq["skills"])
        s = summaries.get(name)
        zenny = f"{s['total']['zenny']:,}" if s else "—"
        h.append(f"<tr><td>{esc(slot)}</td><td>{esc(name)}</td>"
                 f"<td>{esc(eq['monster'])}</td><td>{esc(skills)}</td>"
                 f'<td class="num">{zenny}</td></tr>')
    h.append("</table>")

    h.append('<div class="node terminal">狩り開始（初期装備）</div>')
    for idx, (phase_no, mon, mon_info, items) in enumerate(phases, start=1):
        h.append('<div class="arrow">▼</div>')
        h.append('<div class="phase">')
        h.append(f"<h2>フェーズ{idx}：{esc(mon)}を狩る（{esc(mon_info['stars'])}）</h2>")
        h.append('<div class="node monster">')
        h.append(f'<span class="name">{esc(mon)} 討伐</span>')
        h.append(f'<p class="meta">弱点: {esc(mon_info["weakness"])}／出現: {esc(mon_info["habitat"])}'
                 f'<br>{esc(mon_info["unlock"])}<br>{esc(mon_info["advice"])}</p>')
        h.append("</div>")
        h.append('<div class="equips">')
        for slot, name, eq in items:
            mats, zenny, grade, exact = craft_info(name, eq, summaries)
            zenny_s = f"／ゼニー{zenny:,}" if zenny else ""
            h.append('<div class="node equip">')
            h.append(f'<span class="slot">{esc(slot)}</span><span class="name">{esc(name)}</span>')
            h.append("<ul>")
            h.append(f"<li>グレード{grade}で生産</li>")
            h.append(f"<li>素材: {esc(fmt_materials(mats))}{esc(zenny_s)}</li>")
            for m in eq.get("milestones", []):
                h.append(f"<li>{esc(m)}</li>")
            h.append("</ul>")
            s = summaries.get(name)
            if s:
                h.append('<details class="gradeup"><summary>強化素材の実数'
                         f'（{esc(s["max_step"])}まで）</summary><div class="inner">')
                h.append('<table class="grid"><tr><th>区間</th><th>ゼニー</th><th>素材</th></tr>')
                for b in s["grade_ups"]:
                    h.append(f'<tr><td>{esc(b["label"])}</td><td class="num">{b["zenny"]:,}</td>'
                             f'<td>{esc(fmt_materials(b["materials"]))}</td></tr>')
                h.append("</table></div></details>")
            h.append("</div>")
        h.append("</div>")
        tip = tips.get(str(phase_no))
        if tip:
            h.append(f'<p class="tip">{esc(tip)}</p>')
        h.append("</div>")

    h.append('<div class="arrow">▼</div>')
    h.append('<div class="node terminal">装備完成 → 各装備をG10へ強化</div>')

    mats_total, zenny_total, missing = grand_totals(loadout, summaries)
    if mats_total:
        h.append('<h2 class="sec">必要素材の総計（生産〜最大強化）</h2>')
        h.append(f'<p class="lead">総ゼニー: <strong>{zenny_total:,}</strong>'
                 + (f'（CSV未収録のため対象外: {esc("、".join(missing))}）' if missing else "")
                 + "</p>")
        h.append('<table class="grid"><tr><th>素材</th><th>合計個数</th></tr>')
        for mat, cnt in mats_total.items():
            h.append(f'<tr><td>{esc(mat)}</td><td class="num">{cnt:,}</td></tr>')
        h.append("</table>")

    srcs = "、".join(f'<a href="{esc(u)}">{esc(u)}</a>' for u in data["meta"]["sources"])
    h.append(f'<p class="foot">情報源: {srcs}<br>素材実数: raw_pages CSV／'
             f'生成: flowchart.py（{esc(data["meta"]["updated"])} 時点データ）</p>')
    h.append("</div>")
    return "\n".join(h)


def gen_html(fragment):
    return ('<!doctype html>\n<html lang="ja">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "</head>\n<body>\n" + fragment + "\n</body>\n</html>\n")


# ------------------------------------------------------------------- main ---

def print_summary(loadout, phases, summaries):
    print("=" * 60)
    print("装備作成フローチャート（概要）")
    print("=" * 60)
    for idx, (phase_no, mon, mon_info, items) in enumerate(phases, start=1):
        print(f"\n[フェーズ{idx}] {mon} を狩る（{mon_info['stars']}／弱点: {mon_info['weakness']}）")
        for slot, name, eq in items:
            s = summaries.get(name)
            note = f"　総ゼニー{s['total']['zenny']:,}（{s['max_step']}まで）" if s else "　（CSV未収録）"
            mats, zenny, grade, exact = craft_info(name, eq, summaries)
            print(f"    → 【{slot}】{name}（G{grade}生産）{note}")
    mats_total, zenny_total, missing = grand_totals(loadout, summaries)
    if mats_total:
        print(f"\n[総計] 全装備を最大強化するのに必要なゼニー: {zenny_total:,}")
    print("[最終] 全装備をG10へ強化して完成")


def main():
    parser = argparse.ArgumentParser(description="モンハンNow 装備作成フローチャート生成")
    parser.add_argument("--weapon", default=DEFAULT_LOADOUT["武器"], help="武器名")
    parser.add_argument("--head", default=DEFAULT_LOADOUT["頭"], help="頭防具名")
    parser.add_argument("--chest", default=DEFAULT_LOADOUT["胴"], help="胴防具名")
    parser.add_argument("--arm", default=DEFAULT_LOADOUT["腕"], help="腕防具名")
    parser.add_argument("--waist", default=DEFAULT_LOADOUT["腰"], help="腰防具名")
    parser.add_argument("--leg", default=DEFAULT_LOADOUT["脚"], help="脚防具名")
    parser.add_argument("--list", action="store_true", help="登録済み装備の一覧を表示して終了")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="出力先ディレクトリ")
    args = parser.parse_args()

    data = load_data()

    if args.list:
        print("登録済み装備一覧:")
        for slot in SLOT_ORDER:
            names = [n for n, e in data["equipment"].items() if e["slot"] == slot]
            print(f"  {slot}: {'、'.join(names) if names else '（未登録）'}")
        return

    loadout = resolve_loadout(args, data)
    phases = build_phases(loadout, data)
    csv_data = materials_csv.load_all()
    summaries = build_summaries(loadout, csv_data)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    md = gen_mermaid(loadout, phases, data, summaries)
    (out_dir / "flowchart.md").write_text(md, encoding="utf-8")

    title = f"モンハンNow 装備作成フロー：{loadout.get('武器', '防具のみ')}編成"
    fragment = gen_html_fragment(loadout, phases, data, summaries, title)
    (out_dir / "flowchart.html").write_text(gen_html(fragment), encoding="utf-8")
    (out_dir / "flowchart_fragment.html").write_text(fragment, encoding="utf-8")

    print_summary(loadout, phases, summaries)
    print(f"\n出力先:")
    print(f"  {out_dir / 'flowchart.md'}   （Mermaid入りMarkdown）")
    print(f"  {out_dir / 'flowchart.html'} （ブラウザで開ける自己完結HTML）")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
