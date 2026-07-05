# -*- coding: utf-8 -*-
"""GameWith「最強装備・おすすめ装備まとめ」(記事414964) から推奨装備セットを収集する。

武器種ごとに掲載されている「おすすめ武器＋防具5部位」のセットを抽出し、
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
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
OUT_FILE = BASE_DIR / "data" / "recommended_sets.json"
DB_FILE = BASE_DIR / "data" / "mhnow.db"
URL = "https://gamewith.jp/monsterhunternow/article/show/414964"

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


def parse_sets(html):
    """h2(武器種) → h3(セット) の順に走査してセットを抽出する。"""
    sets = []
    # h2/h3の位置を列挙して区間を切る
    heads = [(m.start(), m.group(1), strip_tags(m.group(2)))
             for m in re.finditer(r"<h([23])[^>]*>(.*?)</h\1>", html, re.S)]
    wtype = None
    for i, (pos, level, title) in enumerate(heads):
        if level == "2":
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
    html = fetch(URL)
    sets = parse_sets(html)
    print(f"抽出: {len(sets)}セット")
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
