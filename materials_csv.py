# -*- coding: utf-8 -*-
"""raw_pages のワイド形式CSV（武器・防具の強化素材表）を読み込むモジュール。

CSV形式:
    武器種/シリーズ, 武器名/防具名, グレード, レベル, ゼニー, 素材1, 個数1, ... 素材5, 個数5

グレードg・レベル1の行は「グレードgへのグレードアップ（または生産）」、
レベル2〜5の行は「グレードg内のレベル強化」を表す。
"""

import csv
from pathlib import Path

CSV_DIR = Path(__file__).resolve().parent / "raw_pages"
WEAPON_CSV = CSV_DIR / "monsterhunternow_weapon_materials_wide.csv"
ARMOR_CSV = CSV_DIR / "monsterhunternow_armor_materials_wide.csv"


def _load_wide_csv(path, name_col):
    """ワイド形式CSVを {装備名: [step, ...]} に変換する。

    step = {"grade": int, "level": int, "zenny": int, "materials": {素材名: 個数}}
    """
    steps = {}
    seen = set()
    if not path.exists():
        return steps
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get(name_col) or "").strip()
            grade = (row.get("グレード") or "").strip()
            level = (row.get("レベル") or "").strip()
            if not name or not grade or not level:
                continue
            # 同名装備の重複行対策（例:「禍鎧」は5部位が同名でCSVに部位列がない）
            key = (name, grade, level)
            if key in seen:
                continue
            seen.add(key)
            materials = {}
            for i in range(1, 6):
                mat = (row.get(f"素材{i}") or "").strip()
                cnt = (row.get(f"個数{i}") or "").strip()
                if mat and cnt:
                    materials[mat] = materials.get(mat, 0) + int(float(cnt))
            step = {
                "grade": int(float(grade)),
                "level": int(float(level)),
                "zenny": int(float(row["ゼニー"] or 0)),
                "materials": materials,
            }
            steps.setdefault(name, []).append(step)
    for name in steps:
        steps[name].sort(key=lambda s: (s["grade"], s["level"]))
    return steps


def _merge_materials(dst, src):
    for mat, cnt in src.items():
        dst[mat] = dst.get(mat, 0) + cnt


def load_all():
    """武器・防具CSVを読み込み、装備名→ステップ一覧の辞書を返す。

    補完CSV（*_supplement.csv。game8由来）がある場合、
    素材が空の装備をそのデータで上書きする。
    """
    data = _load_wide_csv(WEAPON_CSV, "武器名")
    data.update(_load_wide_csv(ARMOR_CSV, "防具名"))
    for path, col in ((CSV_DIR / "weapon_materials_supplement.csv", "武器名"),
                      (CSV_DIR / "armor_materials_supplement.csv", "防具名")):
        for name, steps in _load_wide_csv(path, col).items():
            existing = data.get(name, [])
            if not any(s["materials"] for s in existing):
                data[name] = steps
    return data


def summarize(steps):
    """ステップ一覧を「生産／グレードアップ区間／総計」に集計する。

    戻り値:
        {
          "craft": {"grade": g, "zenny": z, "materials": {...}},
          "grade_ups": [{"label": "G5→G6", "zenny": z, "materials": {...}}, ...],
          "total": {"zenny": z, "materials": {...}},
          "max_step": "G10 Lv5",
        }
    """
    if not steps:
        return None
    craft = steps[0]
    result = {
        "craft": {"grade": craft["grade"], "zenny": craft["zenny"],
                  "materials": dict(craft["materials"])},
        "grade_ups": [],
        "total": {"zenny": 0, "materials": {}},
        "max_step": f"G{steps[-1]['grade']} Lv{steps[-1]['level']}",
    }
    for s in steps:
        result["total"]["zenny"] += s["zenny"]
        _merge_materials(result["total"]["materials"], s["materials"])

    # 「Gg→Gg+1」= グレードg内のレベル強化(Lv2〜5) + グレードg+1へのグレードアップ(Lv1)
    grades = sorted({s["grade"] for s in steps})
    for g in grades:
        levelups = [s for s in steps if s["grade"] == g and s["level"] >= 2]
        gradeup = [s for s in steps if s["grade"] == g + 1 and s["level"] == 1]
        if gradeup:
            bucket = {"label": f"G{g}→G{g + 1}", "zenny": 0, "materials": {}}
            for s in levelups + gradeup:
                bucket["zenny"] += s["zenny"]
                _merge_materials(bucket["materials"], s["materials"])
            result["grade_ups"].append(bucket)
        elif levelups:  # 最終グレードのレベル強化のみ
            bucket = {"label": f"G{g} Lv2〜{levelups[-1]['level']}", "zenny": 0, "materials": {}}
            for s in levelups:
                bucket["zenny"] += s["zenny"]
                _merge_materials(bucket["materials"], s["materials"])
            result["grade_ups"].append(bucket)
    return result
