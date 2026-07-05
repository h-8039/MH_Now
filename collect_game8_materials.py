# -*- coding: utf-8 -*-
"""素材データが空の装備について、game8の装備ページから強化素材を補完する。

GameWithのCSVエクスポートで素材欄が空だった装備（ケマトリス武器・レイア防具等）を対象に、
game8 (game8.jp/monsterhunternow) の「グレードg→g+1の強化素材」テーブルを解析し、
既存CSVと同じワイド形式の補完CSVを出力する:

    raw_pages/weapon_materials_supplement.csv
    raw_pages/armor_materials_supplement.csv

materials_csv.load_all() が補完CSVを自動で読み込み、該当装備のステップを上書きする。
実行後は build_db.py で反映すること。

使い方:
    python collect_game8_materials.py            # URL探索 + 収集
"""

import csv
import io
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"
RAW_DIR = BASE_DIR / "raw_pages"
URL_MAP_FILE = RAW_DIR / "game8_urls.json"
W_SUP = RAW_DIR / "weapon_materials_supplement.csv"
A_SUP = RAW_DIR / "armor_materials_supplement.csv"

TOP_URL = "https://game8.jp/monsterhunternow"
WAIT = 3.0


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8", errors="replace")


def targets_from_db():
    """素材行が空の装備一覧（名前 → 属性情報）を返す。"""
    conn = sqlite3.connect(DB_FILE)
    rows = conn.execute(
        "SELECT e.name, e.category, e.weapon_type, e.series FROM equipment e "
        "WHERE NOT EXISTS (SELECT 1 FROM step_materials m JOIN upgrade_steps s "
        "ON s.id=m.step_id WHERE s.equipment_id=e.id)").fetchall()
    conn.close()
    return {r[0]: {"category": r[1], "weapon_type": r[2], "series": r[3]} for r in rows}


def discover_urls(targets):
    """game8の一覧記事から装備名→記事URLを探す。"""
    url_map = {}
    if URL_MAP_FILE.exists():
        url_map = json.loads(URL_MAP_FILE.read_text(encoding="utf-8"))
    remaining = set(targets) - set(url_map)
    if not remaining:
        return url_map

    # hrefは相対（トップページ）と絶対（一覧ページ）の両形式がある
    link_re = re.compile(
        r'<a[^>]+href="(?:https://game8\.jp)?(/monsterhunternow/\d+)"[^>]*>(.*?)</a>', re.S)

    def harvest(html):
        n = 0
        for href, label in link_re.findall(html):
            label = re.sub(r"<[^>]+>", "", label).strip()
            label = re.sub(r"の(装備スキル|性能)と.*$", "", label)
            if label in remaining and label not in url_map:
                url_map[label] = "https://game8.jp" + href
                n += 1
        return n

    print("game8のURL探索中...")
    top = fetch(TOP_URL)
    harvest(top)
    # 「一覧」を含むリンク先も探索
    list_pages = sorted({href for href, label in link_re.findall(top)
                         if "一覧" in re.sub(r"<[^>]+>", "", label)})
    for i, href in enumerate(list_pages[:30]):
        time.sleep(WAIT)
        try:
            got = harvest(fetch("https://game8.jp" + href))
            print(f"  [{i + 1}] {href} → {got} 件")
        except Exception as e:
            print(f"  [{i + 1}] {href} → エラー: {e}")
        if not (set(targets) - set(url_map)):
            break
    URL_MAP_FILE.write_text(json.dumps(url_map, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"URL対応: {len([n for n in targets if n in url_map])} / {len(targets)} 件")
    return url_map


SECTION_RE = re.compile(
    r"グレード(\d+)→\d+の強化素材</h3>(.*?)(?=<h3|<h2|\Z)", re.S)
ROW_RE = re.compile(
    r"<th>(\d)</th>\s*<td>(.*?)</td>\s*<td[^>]*>([\d,]+)</td>", re.S)
MAT_RE = re.compile(r"・([^<×]+)×(\d+)")


def parse_page(html):
    """game8装備ページから [(グレード, レベル, ゼニー, {素材: 個数}), ...] を返す。"""
    steps = []
    for grade_s, body in SECTION_RE.findall(html):
        grade = int(grade_s)
        for lv_s, td, zenny_s in ROW_RE.findall(body):
            mats = {}
            for m, n in MAT_RE.findall(td):
                mats[m.strip()] = mats.get(m.strip(), 0) + int(n)
            if mats:
                steps.append((grade, int(lv_s), int(zenny_s.replace(",", "")), mats))
    return steps


def main():
    targets = targets_from_db()
    if not targets:
        print("素材が空の装備はありません。")
        return
    print(f"補完対象: {len(targets)} 件")
    url_map = discover_urls(targets)

    w_rows, a_rows = [], []
    for i, (name, info) in enumerate(targets.items(), 1):
        url = url_map.get(name)
        if not url:
            print(f"  [{i}/{len(targets)}] {name}: URL不明のためスキップ")
            continue
        time.sleep(WAIT)
        try:
            steps = parse_page(fetch(url))
        except Exception as e:
            print(f"  [{i}/{len(targets)}] {name}: エラー {e}")
            continue
        if not steps:
            print(f"  [{i}/{len(targets)}] {name}: 素材テーブルなし（未追記ページ）")
            continue
        for grade, lv, zenny, mats in steps:
            base = {"グレード": grade, "レベル": lv, "ゼニー": zenny}
            pairs = list(mats.items())[:5]
            for j, (m, n) in enumerate(pairs, 1):
                base[f"素材{j}"] = m
                base[f"個数{j}"] = n
            if info["category"] == "武器":
                base["武器種"] = info["weapon_type"] or ""
                base["武器名"] = name
                w_rows.append(base)
            else:
                base["シリーズ"] = info["series"] or ""
                base["防具名"] = name
                a_rows.append(base)
        print(f"  [{i}/{len(targets)}] {name}: {len(steps)} ステップ取得")

    def write(path, rows, key_cols):
        if not rows:
            return
        cols = key_cols + ["グレード", "レベル", "ゼニー"] + \
            [c for k in range(1, 6) for c in (f"素材{k}", f"個数{k}")]
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})
        print(f"→ {path}（{len(rows)} 行）")

    write(W_SUP, w_rows, ["武器種", "武器名"])
    write(A_SUP, a_rows, ["シリーズ", "防具名"])
    print("build_db.py を再実行するとDBに反映されます。")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
