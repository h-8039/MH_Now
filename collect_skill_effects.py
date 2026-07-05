# -*- coding: utf-8 -*-
"""GameWith記事に埋め込まれた tooltipDatas（全スキルのレベル別効果）を解析し、
data/skill_effects.json を生成する。

tooltipDatas は各装備ページに全スキル分が埋め込まれているため、
raw_pages/html/ に保存済みのHTMLが1つあれば全スキルを抽出できる。

ダメージ計算用に、効果テキストから以下の数値をパースする:
    attack_pct   … 攻撃力+N%
    attack_flat  … 攻撃力+N（実数）
    crit_rate    … 会心率+N%
    elem_flat    … 属性攻撃力+N（スキル名の属性と武器属性が一致する場合のみ有効）
    damage_pct   … 与ダメージ+N%
効果に発動条件があるスキルは condition にテキストを保持する（最適化時の扱いは optimize.py 側）。
"""

import io
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HTML_DIR = BASE_DIR / "raw_pages" / "html"
OUT_FILE = BASE_DIR / "data" / "skill_effects.json"

# 発動条件付きと判定するキーワード（スキル概要 kinds に含まれる場合）
CONDITION_KEYWORDS = [
    "体力", "確率", "経過", "ガードに成功", "ジャスト回避", "ジャストガード",
    "撃墜", "部位破壊", "旋律", "討伐", "SPゲージ", "チャージ", "溜め",
    "回避した", "命中", "怒り", "罠", "アイテム", "採集",
]

# キーワードで拾えない状況限定スキルの明示指定（スキル名 → 条件ラベル）
FORCE_CONDITIONAL = {
    "目覚めの一撃": "睡眠中の初撃のみ",
    "追い打ち【毒】": "毒状態中のみ",
    "追い打ち【麻痺】": "麻痺状態中のみ",
    "グループハント強化【攻撃】": "マルチプレイ時のみ",
    "闇討ち": "背後からの攻撃のみ",
    "軽巧": "空中攻撃のみ",
    "逆恨み": "被弾後の一定時間のみ",
    "不屈": "力尽きた後のみ",
    "死中に活": "自身が状態異常中のみ",
    "状態異常蓄積時威力UP": "状態異常蓄積発生時のみ",
    "災禍転福": "状態異常解除後のみ",
    "邁進": "前方回避後の数秒のみ",
    "SPスキル威力アップ": "SPスキル使用時のみ",
    "鬼火纏": "鬼火やられのデメリット付き",
    "攻撃増強【会心】": "武器の基礎会心率依存（未収集のため除外）",
}

# 発動条件はあるが実戦でほぼ常時見なせるため無条件扱いにするスキル
FORCE_UNCONDITIONAL = {"連撃", "弱点特効"}

ELEMENT_PREFIX = ["火", "水", "雷", "氷", "龍"]


def parse_level_effect(text):
    """1レベル分の効果テキストから数値効果を抽出する。"""
    eff = {}
    m = re.search(r"属性攻撃力\+(\d+)([%％]?)", text)
    if m:
        if m.group(2):
            eff["elem_pct"] = int(m.group(1))    # 会心撃【属性】等の倍率系
        else:
            eff["elem_flat"] = int(m.group(1))   # 属性攻撃強化等の実数加算
    m = re.search(r"(?<!属性)攻撃力\+(\d+)([%％]?)", text)
    if m:
        if m.group(2):
            eff["attack_pct"] = int(m.group(1))
        else:
            eff["attack_flat"] = int(m.group(1))
    m = re.search(r"会心率([+-])(\d+)[%％]", text)
    if m:
        eff["crit_rate"] = int(m.group(2)) * (1 if m.group(1) == "+" else -1)
    m = re.search(r"会心ダメージ\+(\d+)[%％]", text)
    if m:
        eff["crit_dmg_pct"] = int(m.group(1))    # 超会心（会心部分のみ強化）
    m = re.search(r"(?<![被心])ダメージ\+(\d+)[%％]", text)
    if m:
        eff["damage_pct"] = int(m.group(1))
    return eff


def main():
    src = next(HTML_DIR.glob("*.html"), None)
    if src is None:
        sys.exit("エラー: raw_pages/html/ に保存済みHTMLがありません。"
                 "collect_gamewith.py --collect --only 任意の装備 --dump を実行してください。")

    html = src.read_text(encoding="utf-8")
    m = re.search(r"tooltipDatas\s*=\s*\[(.*?)\];", html, re.S)
    if m is None:
        sys.exit(f"エラー: {src.name} に tooltipDatas が見つかりません。")

    skills = {}
    entry_re = re.compile(r"\{name:'([^']*)',aid:'[^']*',kinds:'([^']*)',txt:'([^']*)'\}")
    for name, kinds, txt in entry_re.findall(m.group(1)):
        levels = {}
        parsed = {}
        for lv, eff_text in re.findall(r"Lv(\d)[：:]\s*([^<]+)", txt):
            eff_text = eff_text.strip()
            levels[lv] = eff_text
            parsed[lv] = parse_level_effect(eff_text)
        if name in FORCE_CONDITIONAL:
            condition = FORCE_CONDITIONAL[name]
        elif name in FORCE_UNCONDITIONAL:
            condition = None
        else:
            condition = next((kw for kw in CONDITION_KEYWORDS if kw in kinds), None)
        # 対象属性の判定: スキル名の接頭辞 → 効果文中の「X属性攻撃力」→ 概要の「X属性を弱点」
        element = next((el for el in ELEMENT_PREFIX if name.startswith(el + "属性")), None)
        if element is None:
            m2 = re.search(r"([火水雷氷龍])属性攻撃力", txt + kinds)
            if m2:
                element = m2.group(1)
        if element is None:
            m2 = re.search(r"([火水雷氷龍])属性を弱点", kinds)
            if m2:
                element = m2.group(1)
        skills[name] = {
            "description": kinds,
            "levels": levels,          # レベル別効果（原文）
            "effects": parsed,         # レベル別効果（パース済み数値）
            "condition": condition,    # None なら常時発動
            "element": element,        # 属性攻撃強化系スキルの対象属性
        }

    OUT_FILE.write_text(json.dumps(skills, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    n_damage = sum(1 for s in skills.values()
                   if any(e for e in s["effects"].values()))
    print(f"抽出元: {src.name}")
    print(f"スキル {len(skills)} 件を {OUT_FILE} に出力（うち数値効果あり {n_damage} 件）")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
