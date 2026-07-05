# -*- coding: utf-8 -*-
"""game8で取得できなかった防具の強化素材をアルテマから補完する。

アルテマの防具ページ (altema.jp/mhnow/bogu/<id>) は
「グレードN」ごとのアコーディオン内に アップグレード / Lv2〜Lv5 の
レベル別素材表を持つため、既存CSVと同じ粒度で取得できる。

出力: raw_pages/armor_materials_supplement.csv に追記
実行後は build_db.py で反映すること。
"""

import csv
import io
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"
A_SUP = BASE_DIR / "raw_pages" / "armor_materials_supplement.csv"
LIST_URL = "https://altema.jp/mhnow/bogulist"
WAIT = 3.0

LINK_RE = re.compile(
    r'<a[^>]+href="(/mhnow/bogu/\d+)">\s*(?:<img[^>]*>)?\s*<span class="b"[^>]*>([^<]+)</span>')
DL_RE = re.compile(r'<dl class="acMenu grade(\d+)">.*?<dd[^>]*>(.*?)</dd>', re.S)
ROW_RE = re.compile(r"<th[^>]*>\s*(アップ<br>グレード|Lv\d)\s*</th>\s*<td(.*?)</td>", re.S)
MAT_RE = re.compile(r">\s*([^<×]{1,25}?)×(\d+)\s*</a>")
ZENNY_RE = re.compile(r"([\d,]+)ゼニー")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as res:
        return res.read().decode("utf-8", errors="replace")


def parse_page(html):
    """[(グレード, レベル, ゼニー, {素材: 個数}), ...] を返す。"""
    steps = []
    for grade_s, body in DL_RE.findall(html):
        grade = int(grade_s)
        for label, td in ROW_RE.findall(body):
            level = 1 if "グレード" in label else int(label[2])
            mats = {}
            for m, n in MAT_RE.findall(td):
                mats[m.strip()] = mats.get(m.strip(), 0) + int(n)
            zm = ZENNY_RE.search(td)
            zenny = int(zm.group(1).replace(",", "")) if zm else 0
            if mats:
                steps.append((grade, level, zenny, mats))
    return steps


def main():
    conn = sqlite3.connect(DB_FILE)
    empty = {r[0]: r[1] for r in conn.execute(
        "SELECT e.name, e.series FROM equipment e WHERE e.category='防具' "
        "AND NOT EXISTS (SELECT 1 FROM step_materials m JOIN upgrade_steps s "
        "ON s.id=m.step_id WHERE s.equipment_id=e.id)")}
    conn.close()
    done = set()
    if A_SUP.exists():
        with open(A_SUP, encoding="utf-8-sig", newline="") as f:
            done = {r["防具名"] for r in csv.DictReader(f)}
    targets = {n: s for n, s in empty.items() if n not in done}
    if not targets:
        print("補完対象の防具はありません。")
        return
    print(f"補完対象: {len(targets)} 件（アルテマから取得）")

    url_map = {}
    for href, label in LINK_RE.findall(fetch(LIST_URL)):
        label = label.strip()
        if label in targets and label not in url_map:
            url_map[label] = "https://altema.jp" + href
    print(f"URL対応: {len(url_map)} / {len(targets)} 件")

    cols = ["シリーズ", "防具名", "グレード", "レベル", "ゼニー"] + \
        [c for k in range(1, 6) for c in (f"素材{k}", f"個数{k}")]
    new_file = not A_SUP.exists()
    with open(A_SUP, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new_file:
            w.writeheader()
        for i, (name, series) in enumerate(targets.items(), 1):
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
                print(f"  [{i}/{len(targets)}] {name}: 素材テーブルなし")
                continue
            for grade, lv, zenny, mats in steps:
                row = {"シリーズ": series or "", "防具名": name,
                       "グレード": grade, "レベル": lv, "ゼニー": zenny}
                for j, (m, n) in enumerate(list(mats.items())[:5], 1):
                    row[f"素材{j}"] = m
                    row[f"個数{j}"] = n
                w.writerow(row)
            print(f"  [{i}/{len(targets)}] {name}: {len(steps)} ステップ取得")
    print(f"→ {A_SUP}")
    print("build_db.py を再実行するとDBに反映されます。")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
