# -*- coding: utf-8 -*-
"""raw_pages のCSVと data/equipment.json から SQLite データベースを構築する。

テーブル構成（ユーザー設計の正規化スキーマ）:
    equipment       … ① 基本データ（武器・防具を統合。category列で区別）
    equipment_tree  … ② 派生ツリー（parent_id。MH Nowは基本的に独立生産のためNULL、
                        派生関係が判明した装備のみ登録する）
    stats           … ③ グレード別ステータス（攻撃力・属性値・防御力。未収集はNULL）
    upgrade_steps   … ④-a 強化ステップ（グレード/レベルごとのゼニー）
    step_materials  … ④-b ステップごとの必要素材と個数
    skills          … 装備スキル（equipment.json の判明分）

※ ゼニーは「素材ごと」ではなく「強化ステップごと」に1つの値なので、
   ④を steps / materials の2テーブルに分割して重複を排除している。

使い方:
    python build_db.py          # data/mhnow.db を再構築
"""

import io
import json
import sqlite3
import sys
from pathlib import Path

import materials_csv

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"
JSON_FILE = BASE_DIR / "data" / "equipment.json"

SCHEMA = """
DROP TABLE IF EXISTS step_materials;
DROP TABLE IF EXISTS upgrade_steps;
DROP TABLE IF EXISTS stats;
DROP TABLE IF EXISTS skills;
DROP TABLE IF EXISTS equipment_tree;
DROP TABLE IF EXISTS equipment;

CREATE TABLE equipment (
    id          INTEGER PRIMARY KEY,
    name        TEXT UNIQUE NOT NULL,
    category    TEXT NOT NULL,          -- '武器' / '防具'
    weapon_type TEXT,                   -- 片手剣など（武器のみ）
    series      TEXT,                   -- 防具シリーズ（防具のみ）
    slot        TEXT,                   -- 頭/胴/腕/腰/脚（防具のみ）
    element     TEXT,                   -- 属性（未収集はNULL）
    rarity      INTEGER,                -- レア度（未収集はNULL）
    crit        INTEGER,                -- 基礎会心率%（武器のみ。weapon_index.csv由来）
    base_grade  INTEGER,                -- 生産グレード（CSVの最初のステップ）
    monster     TEXT                    -- 素材元モンスター（判明分）
);

CREATE TABLE equipment_tree (
    equipment_id INTEGER PRIMARY KEY REFERENCES equipment(id),
    parent_id    INTEGER REFERENCES equipment(id)  -- NULL = 独立生産/初期武器
);

CREATE TABLE stats (
    equipment_id  INTEGER NOT NULL REFERENCES equipment(id),
    grade         INTEGER NOT NULL,
    level         INTEGER NOT NULL,
    attack        INTEGER,
    element_value INTEGER,
    defense       INTEGER,
    PRIMARY KEY (equipment_id, grade, level)
);

CREATE TABLE upgrade_steps (
    id           INTEGER PRIMARY KEY,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
    grade        INTEGER NOT NULL,
    level        INTEGER NOT NULL,
    zenny        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (equipment_id, grade, level)
);

CREATE TABLE step_materials (
    step_id       INTEGER NOT NULL REFERENCES upgrade_steps(id),
    material_name TEXT NOT NULL,
    count         INTEGER NOT NULL,
    PRIMARY KEY (step_id, material_name)
);

CREATE TABLE skills (
    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
    skill_name   TEXT NOT NULL,
    unlock       TEXT,        -- 'G5' などの解放条件
    level        INTEGER
);

CREATE INDEX idx_steps_equipment ON upgrade_steps(equipment_id, grade, level);
CREATE INDEX idx_materials_name ON step_materials(material_name);
CREATE INDEX idx_equipment_type ON equipment(category, weapon_type, series);
"""

# 防具名の語尾から部位を推定する
SLOT_SUFFIXES = [
    ("ヘルム", "頭"), ("ヘッド", "頭"), ("フード", "頭"), ("がね", "頭"),
    ("メイル", "胴"), ("ベスト", "胴"), ("スーツ", "胴"),
    ("アーム", "腕"), ("ガントレット", "腕"), ("篭手", "腕"),
    ("コイル", "腰"), ("ベルト", "腰"),
    ("グリーヴ", "脚"), ("ブーツ", "脚"), ("パンツ", "脚"),
]


def guess_slot(name):
    for suffix, slot in SLOT_SUFFIXES:
        if name.endswith(suffix):
            return slot
    return None


def load_wide_rows(path, name_col, extra_col):
    """CSVを (装備名, 区分列の値, steps) の形で返す。"""
    import csv as _csv
    result = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in _csv.DictReader(f):
            name = (row.get(name_col) or "").strip()
            if name and name not in result:
                result[name] = (row.get(extra_col) or "").strip()
    return result


def load_skills_csv():
    """equipment_skills.csv を読み、{装備名: {(スキル名, Lv): 最小グレード}} を返す。

    ステータス表はグレードごとに同じスキルを繰り返し記載するため、
    「そのスキルLvが最初に登場するグレード」= 解放グレード として集約する。
    """
    import csv as _csv
    path = materials_csv.CSV_DIR / "equipment_skills.csv"
    result = {}
    if not path.exists():
        return result
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in _csv.DictReader(f):
            name = (row.get("名前") or "").strip()
            skill = (row.get("スキル名") or "").strip()
            if not name or not skill:  # 空行マーカーは無視
                continue
            grade = int(float(row.get("グレード") or 0))
            lv = int(float(row.get("スキルLv") or 0))
            key = (skill, lv)
            bucket = result.setdefault(name, {})
            if key not in bucket or grade < bucket[key]:
                bucket[key] = grade
    return result


def load_optional_csvs(conn):
    """追加情報CSVがあれば取り込む（無ければ何もしない）。

    raw_pages/equipment_info.csv:
        名前,属性,レア度,派生元,モンスター,部位
        → equipment の属性・レア度・モンスター・部位、equipment_tree の派生元を更新
    raw_pages/equipment_stats.csv:
        名前,グレード,レベル,攻撃力,属性値,防御力
        → stats テーブルへ投入（空欄はNULL）
    """
    import csv as _csv

    info_csv = materials_csv.CSV_DIR / "equipment_info.csv"
    if info_csv.exists():
        n_upd = n_tree = 0
        unknown = []
        with open(info_csv, encoding="utf-8-sig", newline="") as f:
            for row in _csv.DictReader(f):
                name = (row.get("名前") or "").strip()
                if not name:
                    continue
                eq = conn.execute("SELECT id FROM equipment WHERE name = ?", (name,)).fetchone()
                if eq is None:
                    unknown.append(name)
                    continue
                sets, params = [], []
                for col, key in (("element", "属性"), ("monster", "モンスター"), ("slot", "部位")):
                    v = (row.get(key) or "").strip()
                    if v:
                        sets.append(f"{col} = ?"); params.append(v)
                rarity = (row.get("レア度") or "").strip()
                if rarity:
                    sets.append("rarity = ?"); params.append(int(float(rarity)))
                if sets:
                    conn.execute(f"UPDATE equipment SET {', '.join(sets)} WHERE id = ?",
                                 params + [eq[0]])
                    n_upd += 1
                parent = (row.get("派生元") or "").strip()
                if parent:
                    p = conn.execute("SELECT id FROM equipment WHERE name = ?", (parent,)).fetchone()
                    if p:
                        conn.execute("UPDATE equipment_tree SET parent_id = ? WHERE equipment_id = ?",
                                     (p[0], eq[0]))
                        n_tree += 1
                    else:
                        unknown.append(parent)
        print(f"equipment_info.csv: {n_upd} 件更新 / 派生 {n_tree} 件登録"
              + (f" / 未登録名 {len(unknown)} 件（例: {'、'.join(unknown[:3])}）" if unknown else ""))

    index_csv = materials_csv.CSV_DIR / "weapon_index.csv"
    if index_csv.exists():
        n_upd = 0
        with open(index_csv, encoding="utf-8-sig", newline="") as f:
            for row in _csv.DictReader(f):
                name = (row.get("名前") or "").strip()
                if not name:
                    continue
                cur = conn.execute(
                    "UPDATE equipment SET crit = ?, "
                    " monster = COALESCE(NULLIF(?, ''), monster), "
                    " element = COALESCE(element, NULLIF(?, '')) "
                    "WHERE name = ?",
                    (int(float(row.get("会心率") or 0)),
                     (row.get("モンスター") or "").strip(),
                     (row.get("属性") or "").strip(), name))
                n_upd += cur.rowcount
        print(f"weapon_index.csv: {n_upd} 件更新（会心率・素材元モンスター）")

    stats_csv = materials_csv.CSV_DIR / "equipment_stats.csv"
    if stats_csv.exists():
        n_stats = 0
        with open(stats_csv, encoding="utf-8-sig", newline="") as f:
            for row in _csv.DictReader(f):
                name = (row.get("名前") or "").strip()
                grade = (row.get("グレード") or "").strip()
                if not name or not grade:
                    continue
                eq = conn.execute("SELECT id FROM equipment WHERE name = ?", (name,)).fetchone()
                if eq is None:
                    continue

                def num(key):
                    v = (row.get(key) or "").strip()
                    return int(float(v)) if v else None

                conn.execute(
                    "INSERT OR REPLACE INTO stats "
                    "(equipment_id, grade, level, attack, element_value, defense) "
                    "VALUES (?,?,?,?,?,?)",
                    (eq[0], int(float(grade)), num("レベル") or 1,
                     num("攻撃力"), num("属性値"), num("防御力")))
                n_stats += 1
        print(f"equipment_stats.csv: {n_stats} 行投入")


def main():
    conn = sqlite3.connect(DB_FILE)
    conn.executescript(SCHEMA)

    with open(JSON_FILE, encoding="utf-8") as f:
        json_data = json.load(f)
    json_eq = json_data["equipment"]

    steps_by_name = materials_csv.load_all()
    weapon_types = load_wide_rows(materials_csv.WEAPON_CSV, "武器名", "武器種")
    armor_series = load_wide_rows(materials_csv.ARMOR_CSV, "防具名", "シリーズ")
    collected_skills = load_skills_csv()

    n_eq = n_steps = n_mats = 0
    for name, steps in steps_by_name.items():
        if name in weapon_types:
            category, wtype, series, slot = "武器", weapon_types[name], None, None
        else:
            category, wtype = "防具", None
            series = armor_series.get(name)
            slot = guess_slot(name)

        meta = json_eq.get(name, {})
        # JSON側のslot（武器以外）はCSV推定より優先
        if meta.get("slot") in ("頭", "胴", "腕", "腰", "脚"):
            slot = meta["slot"]

        cur = conn.execute(
            "INSERT INTO equipment (name, category, weapon_type, series, slot,"
            " element, rarity, base_grade, monster) VALUES (?,?,?,?,?,?,?,?,?)",
            (name, category, wtype, series, slot,
             meta.get("element"), meta.get("rarity"),
             steps[0]["grade"] if steps else None, meta.get("monster")))
        eq_id = cur.lastrowid
        n_eq += 1

        # 派生ツリー: 判明分のみ。既定は独立生産（parent_id = NULL）
        conn.execute("INSERT INTO equipment_tree (equipment_id, parent_id) VALUES (?, NULL)",
                     (eq_id,))

        for s in steps:
            cur = conn.execute(
                "INSERT INTO upgrade_steps (equipment_id, grade, level, zenny) VALUES (?,?,?,?)",
                (eq_id, s["grade"], s["level"], s["zenny"]))
            step_id = cur.lastrowid
            n_steps += 1
            for mat, cnt in s["materials"].items():
                conn.execute(
                    "INSERT INTO step_materials (step_id, material_name, count) VALUES (?,?,?)",
                    (step_id, mat, cnt))
                n_mats += 1

        # スキル: 収集CSV（equipment_skills.csv）を優先し、無ければ equipment.json
        if name in collected_skills:
            for (skill_name, lv), grade in collected_skills[name].items():
                conn.execute(
                    "INSERT INTO skills (equipment_id, skill_name, unlock, level) VALUES (?,?,?,?)",
                    (eq_id, skill_name, f"G{grade}", lv))
        else:
            for sk in meta.get("skills", []):
                for unlock, lv in sk.get("levels", {}).items():
                    conn.execute(
                        "INSERT INTO skills (equipment_id, skill_name, unlock, level) VALUES (?,?,?,?)",
                        (eq_id, sk["name"], unlock, lv))

    load_optional_csvs(conn)

    conn.commit()
    print(f"構築完了: {DB_FILE}")
    print(f"  装備: {n_eq} 件 / 強化ステップ: {n_steps} 件 / 素材行: {n_mats} 件")
    for cat, cnt in conn.execute(
            "SELECT category, COUNT(*) FROM equipment GROUP BY category"):
        print(f"  {cat}: {cnt} 件")
    conn.close()


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
