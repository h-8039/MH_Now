# -*- coding: utf-8 -*-
"""GameWithの最強装備記事から推奨装備セットを収集する。

対象:
  - まとめ記事414964（全武器種の代表セット）
  - 武器種別の最強装備記事（汎用装備・作りやすい装備などを含む）
「おすすめ武器＋防具5部位」のセットを抽出し、重複を除いて
data/recommended_sets.json に保存する。gear_tree.py が対策装備セットの
基本形として使用する。

使い方:
    python collect_recommended_sets.py
"""

import io
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUT_FILE = BASE_DIR / "data" / "recommended_sets.json"
DB_FILE = BASE_DIR / "data" / "mhnow.db"
URL_FMT = "https://gamewith.jp/monsterhunternow/article/show/{}"
SUMMARY_ID = 414964          # 全武器種まとめ（h2=武器種）
TYPE_ARTICLE_IDS = [         # 武器種別記事（武器種はタイトルから判定）
    415957, 429284, 415960, 415962, 415961, 429285,
    453466, 477863, 439990, 415964, 439991, 415963,
]
FETCH_INTERVAL = 4           # アクセスマナー（秒）

SLOTS = ["頭", "胴", "腕", "腰", "脚"]
WEAPON_TYPES = ["片手剣", "双剣", "大剣", "太刀", "ハンマー", "狩猟笛",
                "ランス", "ガンランス", "スラッシュアックス", "チャージアックス",
                "ライトボウガン", "ヘビィボウガン", "弓", "操虫棍"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def first_name(td_html):
    """<td>内の装備名（先頭リンクのテキスト、無ければ先頭テキスト）を返す。"""
    m = re.search(r"<a[^>]*>(.*?)</a>", td_html, re.S)
    text = strip_tags(m.group(1)) if m else strip_tags(td_html)
    # 「吼剣【地咬】(グレード10)【強化前:...】」「ゴシャガザクゥグレード10 Lv5」等の後置修飾を除去
    text = re.split(r"\(グレード|グレード\d|【強化前", text)[0]
    return text.strip()


# 記事の表記 → DB上の装備名（DBは禍鎧/ミヅハを部位共通の1名称で保持）
NAME_MAP = {
    "禍鎧【胸当て】": "禍鎧", "禍鎧【腰当て】": "禍鎧",
    "ミヅハ【烏帽子】": "ミヅハ", "ミヅハ【丸帯】": "ミヅハ",
    "ミヅハ【胸当て】": "ミヅハ",
}


def parse_sets(html, fixed_wtype=None):
    """h3(セット)ブロックを走査してセットを抽出する。

    fixed_wtype があれば全ブロックをその武器種として扱い（武器種別記事）、
    無ければ h2 見出しから武器種を判定する（まとめ記事414964）。
    """
    sets = []
    # h2/h3の位置を列挙して区間を切る
    heads = [(m.start(), m.group(1), strip_tags(m.group(2)))
             for m in re.finditer(r"<h([23])[^>]*>(.*?)</h\1>", html, re.S)]
    wtype = fixed_wtype
    for i, (pos, level, title) in enumerate(heads):
        if level == "2" and not fixed_wtype:
            wtype = next((w for w in WEAPON_TYPES
                          if title.startswith(w + "の")), None)
            continue
        if level != "3" or not wtype:
            continue
        end = heads[i + 1][0] if i + 1 < len(heads) else len(html)
        block = html[pos:end]

        # 武器名の見出しセルと名前セルは行(<tr>)が分かれている
        m = re.search(r"武器名</th>.*?<td[^>]*>(.*?)</td>", block, re.S)
        if not m:
            continue
        weapon = first_name(m.group(1))
        armor = {}
        for slot in SLOTS:
            am = re.search(rf"<th[^>]*>{slot}</th>\s*<td[^>]*>(.*?)</td>",
                           block, re.S)
            if am:
                armor[slot] = first_name(am.group(1))
        if len(armor) == 5 and weapon:
            armor = {sl: NAME_MAP.get(n, n) for sl, n in armor.items()}
            sets.append({"weapon_type": wtype, "title": title,
                         "weapon": NAME_MAP.get(weapon, weapon),
                         "armor": armor})
    return sets


def validate(sets):
    """装備名がDBに存在するか確認し、不一致を報告する。"""
    conn = sqlite3.connect(DB_FILE)
    known = {r[0] for r in conn.execute("SELECT name FROM equipment")}
    conn.close()
    missing = set()
    for s in sets:
        if s["weapon"] not in known:
            missing.add(s["weapon"])
        for name in s["armor"].values():
            if name not in known:
                missing.add(name)
    return missing


def main():
    sets = parse_sets(fetch(URL_FMT.format(SUMMARY_ID)))
    print(f"まとめ記事{SUMMARY_ID}: {len(sets)}セット")

    for aid in TYPE_ARTICLE_IDS:
        time.sleep(FETCH_INTERVAL)
        html = fetch(URL_FMT.format(aid))
        tm = re.search(r"【モンハンナウ】(.+?)の最強装備", html)
        wtype = tm.group(1) if tm and tm.group(1) in WEAPON_TYPES else None
        if not wtype:
            print(f"⚠ 記事{aid}: 武器種をタイトルから判定できずスキップ")
            continue
        add = parse_sets(html, fixed_wtype=wtype)
        print(f"記事{aid}（{wtype}）: {len(add)}セット")
        sets.extend(add)

    # 重複除去（同一の武器＋防具構成は先勝ち）
    seen, uniq = set(), []
    for s in sets:
        key = (s["weapon"], tuple(s["armor"].get(sl) for sl in SLOTS))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    sets = uniq
    print(f"\n重複除去後: {len(sets)}セット")
    for s in sets:
        print(f"  [{s['weapon_type']}] {s['title']}: {s['weapon']} / "
              + "、".join(s["armor"][sl] for sl in SLOTS))
    missing = validate(sets)
    if missing:
        print(f"\n⚠ DB未登録の装備名 ({len(missing)}件): " + "、".join(sorted(missing)))
    OUT_FILE.write_text(json.dumps(sets, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n保存: {OUT_FILE}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")
    main()
