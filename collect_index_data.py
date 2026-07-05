# -*- coding: utf-8 -*-
"""GameWithの一覧ページ（静的HTML）から以下を一括取得する。

1. 全武器一覧 (413709) の埋め込みJS配列
   → raw_pages/weapon_index.csv（名前,武器種,攻撃力,属性値,会心率,属性,モンスター）
   ※ 会心率（基礎値）と素材元モンスターはここでしか取れない

2. モンスター一覧 (413311) の data-filter 属性
   → data/monsters.json（モンスター名 → 弱点属性リスト・個別ページURL）

3. --difficulty 指定時: 各モンスターページの「ハンターランクと入手ゼニー」表から
   出現★範囲（討伐難易度）を取得して monsters.json に追記（66ページ・約3分）

1と2はログイン・ブラウザ描画不要（1リクエストずつ）。
"""

import csv
import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WEAPON_INDEX_CSV = BASE_DIR / "raw_pages" / "weapon_index.csv"
MONSTERS_JSON = BASE_DIR / "data" / "monsters.json"

WEAPON_LIST_URL = "https://gamewith.jp/monsterhunternow/article/show/413709"
MONSTER_LIST_URL = "https://gamewith.jp/monsterhunternow/article/show/413311"

# JS配列の atr / data-filter のキー → 属性名
ELEMENT_MAP = {
    "fire": "火", "water": "水", "thun": "雷", "thunder": "雷", "ice": "氷",
    "dra": "龍", "dragon": "龍", "dok": "毒", "doku": "毒", "poison": "毒",
    "mah": "麻痺", "para": "麻痺", "paralysis": "麻痺",
    "sleep": "睡眠", "bakuha": "爆破", "blast": "爆破",
}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8", errors="replace")


def collect_weapons():
    html = fetch(WEAPON_LIST_URL)
    # 例: {id:'946',n:'吼剣【地咬】',k:'...',f:'...',s1:'2198',s2:'0',s3:'-10',
    #      atr:'',aid:'525239',m:'ティガレックス亜種',mid:'521134',i:'...',t:'片手剣',...}
    entry_re = re.compile(
        r"\{id:'\d+',n:'([^']*)',k:'[^']*',f:'[^']*',"
        r"s1:'([^']*)',s2:'([^']*)',s3:'([^']*)',atr:'([^']*)',"
        r"aid:'[^']*',m:'([^']*)',mid:'[^']*',i:'[^']*',t:'([^']*)'")
    rows = []
    for name, atk, elem_v, crit, atr, monster, wtype in entry_re.findall(html):
        rows.append({
            "名前": name,
            "武器種": wtype,
            "攻撃力": atk or 0,
            "属性値": elem_v or 0,
            "会心率": crit or 0,
            "属性": ELEMENT_MAP.get(atr, "無") if atr else "無",
            "モンスター": monster,
        })
    if not rows:
        sys.exit("エラー: 武器一覧の埋め込みデータが見つかりません（ページ構造変更の可能性）。")
    with open(WEAPON_INDEX_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    n_crit = sum(1 for r in rows if str(r["会心率"]) not in ("0", ""))
    print(f"武器一覧: {len(rows)} 件 → {WEAPON_INDEX_CSV}（会心率≠0: {n_crit}件）")


def collect_monsters():
    html = fetch(MONSTER_LIST_URL)
    # li単位で 名前・弱点(data-filter)・個別ページURL を取得
    li_re = re.compile(
        r'<li data-id="\d+" data-name="([^"]+)"[^>]*data-filter="([^"]*)"'
        r'.*?href=["\'](https://gamewith\.jp/monsterhunternow/article/show/\d+)["\']',
        re.S)
    old = {}
    if MONSTERS_JSON.exists():  # 収集済みの難易度等を保持
        old = json.loads(MONSTERS_JSON.read_text(encoding="utf-8"))
    monsters = {}
    for name, filters, url in li_re.findall(html):
        weakness = []
        for key in filters.split():
            el = ELEMENT_MAP.get(key)
            if el and el not in weakness:
                weakness.append(el)
        if weakness:
            monsters[name] = old.get(name, {})
            monsters[name].update({"weakness": weakness, "url": url})
    if not monsters:
        sys.exit("エラー: モンスター一覧の弱点データが見つかりません（ページ構造変更の可能性）。")
    MONSTERS_JSON.write_text(json.dumps(monsters, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(f"モンスター弱点: {len(monsters)} 体 → {MONSTERS_JSON}")


def collect_difficulty(wait=3.0):
    """各モンスターページから出現★範囲（討伐難易度）を取得する。

    「ハンターランクと入手ゼニー」表の「★4 60 / 40」のような行から★を拾い、
    最小〜最大を discovery する。表が無いページは素材一覧の「★6〜7」表記に回帰。
    """
    monsters = json.loads(MONSTERS_JSON.read_text(encoding="utf-8"))
    star_re = re.compile(r"★(10|[1-9])(\d+)\s*/\s*\d+")        # ★4 60 / 40（HRP/ゼニー行）
    star_range_re = re.compile(r"★(10|[1-9])(?:〜(10|[1-9]))?")  # 素材見出しの ★6〜7 等
    n_done = 0
    for i, (name, info) in enumerate(monsters.items(), 1):
        if info.get("stars"):
            continue
        url = info.get("url")
        if not url:
            continue
        try:
            html = fetch(url)
        except Exception as e:
            print(f"  [{i}] {name}: 取得失敗 {e}")
            continue
        stars = {int(m.group(1)) for m in star_re.finditer(html)}
        if not stars:  # フォールバック: ★n〜m 表記から収集
            for m in star_range_re.finditer(html):
                stars.add(int(m.group(1)))
                if m.group(2):
                    stars.add(int(m.group(2)))
        if stars:
            lo, hi = min(stars), max(stars)
            info["stars"] = f"★{lo}〜★{hi}"
            info["min_star"] = lo
            n_done += 1
            print(f"  [{i}/{len(monsters)}] {name}: ★{lo}〜★{hi}")
        else:
            print(f"  [{i}/{len(monsters)}] {name}: ★情報なし")
        time.sleep(wait)
    MONSTERS_JSON.write_text(json.dumps(monsters, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    print(f"討伐難易度: {n_done} 体を更新 → {MONSTERS_JSON}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if "--difficulty" in sys.argv:
        collect_difficulty()
    else:
        collect_weapons()
        collect_monsters()
