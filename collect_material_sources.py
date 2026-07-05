# -*- coding: utf-8 -*-
"""GameWith 素材一覧 (413328) から「素材 → 入手元モンスター」対応表を作る。

ページ本文は「素材名RARE n 入手場所 モンスター名(★x〜★y)から入手」という
テキスト構造なので、DBに存在する素材名をキーに前方一致で照合する。
モンスター名が見つからない素材（鉱石・植物・小型モンスター素材など）は
入手元 [] = どこでも集められる汎用素材として扱う。

出力: data/material_sources.json  { 素材名: [モンスター名, ...] }
"""

import io
import json
import re
import sqlite3
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"
OUT_FILE = BASE_DIR / "data" / "material_sources.json"
URL = "https://gamewith.jp/monsterhunternow/article/show/413328"


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as res:
        html = res.read().decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", "", html)  # タグ除去して素のテキストにする

    conn = sqlite3.connect(DB_FILE)
    materials = [r[0] for r in conn.execute(
        "SELECT DISTINCT material_name FROM step_materials")]
    conn.close()

    # 既知モンスター名（monsters.json）の「名前(★」を直接照合する。
    # 正規表現だと「全エリアオドガロン亜種」のように場所名のカタカナが
    # モンスター名に連結して誤抽出されるため。
    monsters_file = BASE_DIR / "data" / "monsters.json"
    if not monsters_file.exists():
        sys.exit("エラー: data/monsters.json がありません。collect_index_data.py を先に実行してください。")
    known_monsters = list(json.loads(monsters_file.read_text(encoding="utf-8")))

    def find_entry(mat):
        """素材名+RAREの位置を返す。より長い別素材名の末尾に一致する位置は除外する
        （例: 「轟竜の尻尾」が「黒轟竜の尻尾」の内部にマッチするのを防ぐ）。"""
        longer = [y for y in materials if y != mat and y.endswith(mat)]
        start = 0
        while True:
            idx = text.find(mat + "RARE", start)
            if idx < 0:
                return -1
            if not any(text[idx - (len(y) - len(mat)): idx + len(mat)] == y
                       for y in longer if idx >= len(y) - len(mat)):
                return idx
            start = idx + 1

    result = {}
    n_hit = n_mon = 0
    for mat in materials:
        idx = find_entry(mat)
        if idx < 0:
            result[mat] = []  # ページ未掲載 → 汎用扱い
            continue
        n_hit += 1
        window = text[idx + len(mat): idx + len(mat) + 250]
        # 次の素材エントリ（"RARE n"の2回目）以降は見ない（はみ出しゼロで切る）
        second = re.search(r"RARE\s*\d", window[10:])
        if second:
            window = window[:10 + second.start()]
        monsters = [name for name in known_monsters if f"{name}(★" in window]
        result[mat] = monsters
        if monsters:
            n_mon += 1

    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    print(f"素材 {len(materials)} 件中、ページ照合 {n_hit} 件／モンスター入手 {n_mon} 件")
    print(f"→ {OUT_FILE}")


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
