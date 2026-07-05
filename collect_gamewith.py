# -*- coding: utf-8 -*-
"""GameWithから装備の不足情報（属性・レア度・攻撃力・派生元）を収集し、
raw_pages/equipment_info.csv / equipment_stats.csv に追記するツール。

■ 認証について
  - login.json にメールアドレスとパスワードを保存しておくと、ログイン画面で自動入力する。
  - GameWithはCloudflare Turnstile（CAPTCHA）を使うため、ログインの最終確定は
    初回のみ手動で行う（--login で表示されるブラウザ上で完了させる）。
    CAPTCHAの自動突破は行わない。
  - セッションは browser_profile/ に保存され、以後は --collect だけで全自動収集できる。

■ 使い方
  1) login.json を作成（login.json.example を参照）
  2) 初回ログイン:        python collect_gamewith.py --login
  3) URL一覧の構築:       python collect_gamewith.py --discover
  4) 収集（例: 50件ずつ）: python collect_gamewith.py --collect --limit 50
     → 実行のたびに未収集の装備から順に処理する（再開可能）
  5) DBへ反映:            python build_db.py

■ マナー
  - ページ取得の間隔は WAIT_SECONDS 秒（既定4秒）空ける。
  - 収集は自分のアカウントで閲覧できる範囲のみ。取得データは個人利用に留めること。
    （GameWithの利用規約上、自動アクセスは禁止されている可能性があります。
      本ツールの使用は自己責任でお願いします）
"""

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOGIN_FILE = BASE_DIR / "login.json"
PROFILE_DIR = BASE_DIR / "browser_profile"
RAW_DIR = BASE_DIR / "raw_pages"
HTML_DIR = RAW_DIR / "html"
URL_MAP_FILE = RAW_DIR / "equipment_urls.json"
INFO_CSV = RAW_DIR / "equipment_info.csv"
STATS_CSV = RAW_DIR / "equipment_stats.csv"
SKILLS_CSV = RAW_DIR / "equipment_skills.csv"
DB_FILE = BASE_DIR / "data" / "mhnow.db"

TOP_URL = "https://gamewith.jp/monsterhunternow/"
LOGIN_URL = "https://gamewith.jp/login"
WAIT_SECONDS = 4.0

INFO_HEADER = ["名前", "属性", "レア度", "派生元", "モンスター", "部位"]
STATS_HEADER = ["名前", "グレード", "レベル", "攻撃力", "属性値", "防御力"]
SKILLS_HEADER = ["名前", "グレード", "レベル", "スキル名", "スキルLv"]

ELEMENTS = ["火", "水", "雷", "氷", "龍", "毒", "麻痺", "睡眠", "無"]


# ------------------------------------------------------------- 共通処理 ---

def load_login():
    if not LOGIN_FILE.exists():
        return None
    with open(LOGIN_FILE, encoding="utf-8") as f:
        d = json.load(f)
    if not d.get("email") or not d.get("password"):
        sys.exit("エラー: login.json に email / password を設定してください。")
    return d


def launch(p, headless):
    """セッションを保存する永続プロファイルでブラウザを起動する。"""
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        locale="ja-JP",
        viewport={"width": 1280, "height": 900},
    )


def db_equipment_names():
    conn = sqlite3.connect(DB_FILE)
    names = {row[0] for row in conn.execute("SELECT name FROM equipment")}
    conn.close()
    return names


def read_csv_names(path, key="名前"):
    if not path.exists():
        return set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        return {row[key] for row in csv.DictReader(f) if row.get(key)}


def append_rows(path, header, rows):
    new_file = not path.exists()
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in header})


# ------------------------------------------------------------ ① ログイン ---

def cmd_login():
    from playwright.sync_api import sync_playwright
    login = load_login()
    with sync_playwright() as p:
        ctx = launch(p, headless=False)  # CAPTCHA対応のため必ず表示モード
        page = ctx.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        # 認証情報の自動入力（見つかった場合のみ。確定はユーザー操作に任せる）
        if login:
            for sel in ("input[type=email]", "input[name*=mail i]", "input[name*=login i]"):
                if page.locator(sel).count():
                    page.locator(sel).first.fill(login["email"])
                    break
            if page.locator("input[type=password]").count():
                page.locator("input[type=password]").first.fill(login["password"])
            print("メールアドレスとパスワードを自動入力しました。")
        print("ブラウザ上でログインを完了させてください（CAPTCHAが出た場合も手動で対応）。")
        input("ログインが完了したら、ここで Enter を押してください > ")
        page.goto(TOP_URL, wait_until="domcontentloaded")
        ctx.close()
    print(f"セッションを {PROFILE_DIR} に保存しました。以後は --collect だけで実行できます。")


# ------------------------------------------- ② 装備ページURLの自動発見 ---

def cmd_discover(limit_pages):
    """トップページから『一覧』系記事をたどり、DB装備名→記事URL の対応表を作る。"""
    from playwright.sync_api import sync_playwright
    names = db_equipment_names()
    url_map = {}
    if URL_MAP_FILE.exists():
        url_map = json.loads(URL_MAP_FILE.read_text(encoding="utf-8"))

    def harvest(page):
        n = 0
        for a in page.eval_on_selector_all(
                "a[href*='/monsterhunternow/article/show/']",
                "els => els.map(e => [e.textContent.trim(), e.href])"):
            text, href = a[0], a[1].split("?")[0]
            # リンクテキストの揺れを吸収（前後の飾り文字を除去）
            text = re.sub(r"(の(性能|スキル|評価)|と(必要)?(強化)?素材.*)$", "", text)
            if text in names and text not in url_map:
                url_map[text] = href
                n += 1
        return n

    with sync_playwright() as p:
        ctx = launch(p, headless=True)
        page = ctx.new_page()
        print("トップページから一覧記事を探索中...")
        page.goto(TOP_URL, wait_until="domcontentloaded")
        harvest(page)
        list_links = page.eval_on_selector_all(
            "a[href*='/monsterhunternow/article/show/']",
            "els => [...new Set(els.filter(e => /一覧/.test(e.textContent))"
            ".map(e => e.href.split('?')[0]))]")
        print(f"一覧系記事: {len(list_links)} 件")
        for i, url in enumerate(list_links[:limit_pages]):
            time.sleep(WAIT_SECONDS)
            try:
                page.goto(url, wait_until="domcontentloaded")
                got = harvest(page)
                print(f"  [{i + 1}/{min(len(list_links), limit_pages)}] {url} → {got} 件発見")
            except Exception as e:
                print(f"  [{i + 1}] {url} → エラー: {e}")
        ctx.close()

    URL_MAP_FILE.write_text(json.dumps(url_map, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    print(f"URL対応表: {len(url_map)} / {len(names)} 件 → {URL_MAP_FILE}")
    print("※ 足りない分は --discover の再実行や、equipment_urls.json への手動追記で補えます。")


# ------------------------------------------------- ③ 装備ページから収集 ---

def parse_tables(page):
    """ページ内の全テーブルを [[セル,...], ...] の形で取り出す。
    セル内の画像は alt テキストで補う（属性アイコン対策）。"""
    return page.eval_on_selector_all(
        "table",
        "tables => tables.map(t => [...t.rows].map(r => [...r.cells].map(c => "
        "(c.textContent.trim() + ' ' + [...c.querySelectorAll('img')]"
        ".map(i => i.alt || '').join(' ')).trim())))")


def to_int(s):
    m = re.search(r"-?\d[\d,]*", s or "")
    return int(m.group().replace(",", "")) if m else None


def extract_info(name, tables, stats_rows, element):
    """属性・レア度などの基本情報を組み立てる。"""
    info = {"名前": name, "属性": "", "レア度": "", "派生元": "", "モンスター": "", "部位": ""}
    if element:
        info["属性"] = element
    elif any(r["攻撃力"] != "" for r in stats_rows):
        # ステータス表があり属性表記が無い武器は無属性
        info["属性"] = "無"
    for t in tables:
        for row in t:
            if len(row) == 2 and "レア" in row[0]:
                v = to_int(row[1])
                if v is not None and not info["レア度"]:
                    info["レア度"] = v
    return info


def extract_stats(name, tables):
    """武器/防具ステータス表からグレード別の数値を抽出する。

    GameWithの実構造: ヘッダー = [(空), 攻撃, 会心, 属性, SP, 装備スキル] など、
    1列目にグレード（「6」「10Lv5」）が入る。防具は [(空), 防御力, スキル]。
    戻り値: (statsの行リスト, 検出した属性 or None, スキルの行リスト)
    """
    rows = []
    skill_rows = []
    element = None
    for t in tables:
        if not t or len(t) < 2:
            continue
        header = t[0]
        col = {}
        for i, h in enumerate(header):
            if "攻撃" in h and "スキル" not in h:
                col["attack"] = i
            elif h.startswith("属性"):
                col["element"] = i
            elif "防御" in h:
                col["defense"] = i
            elif "スキル" in h:
                col["skills"] = i
        if "attack" not in col and "defense" not in col:
            continue

        def cell(r, key):
            if key not in col or len(r) <= col[key]:
                return ""
            v = to_int(r[col[key]])
            return v if v is not None else ""

        for r in t[1:]:
            # 武器は「6」「10Lv5」、防具は「グレード3」形式
            m = re.match(r"^(?:グレード)?\s*(\d+)", r[0].strip()) if r else None
            if not m:
                continue
            grade = int(m.group(1))
            lv = re.search(r"Lv\s*(\d)", r[0])
            level = int(lv.group(1)) if lv else 1
            rows.append({
                "名前": name, "グレード": grade, "レベル": level,
                "攻撃力": cell(r, "attack"),
                "属性値": cell(r, "element"),
                "防御力": cell(r, "defense"),
            })
            # 属性列のテキスト（アイコンalt含む）から属性名を検出
            if "element" in col and len(r) > col["element"] and element is None:
                for el in ELEMENTS:
                    if el != "無" and el in r[col["element"]]:
                        element = el
                        break
            # スキル列「攻撃Lv1連撃Lv2」のような連結表記を分解
            if "skills" in col and len(r) > col["skills"]:
                for sk_name, sk_lv in re.findall(r"([^\s/、]+?)\s*Lv\s*(\d)",
                                                 r[col["skills"]]):
                    skill_rows.append({
                        "名前": name, "グレード": grade, "レベル": level,
                        "スキル名": sk_name, "スキルLv": int(sk_lv),
                    })
    return rows, element, skill_rows


def cmd_collect(limit, dump, only=None, missing_stats=False, missing_skills=False):
    from playwright.sync_api import sync_playwright
    if not URL_MAP_FILE.exists():
        sys.exit("エラー: 先に --discover でURL対応表を作成してください。")
    url_map = json.loads(URL_MAP_FILE.read_text(encoding="utf-8"))
    done = read_csv_names(INFO_CSV)
    if only:
        targets = [(n, u) for n, u in url_map.items() if n == only]
        if not targets:
            sys.exit(f"エラー: 「{only}」はURL対応表にありません。")
    elif missing_stats:
        stats_done = read_csv_names(STATS_CSV)
        targets = [(n, u) for n, u in url_map.items() if n not in stats_done][:limit]
    elif missing_skills:
        skills_done = read_csv_names(SKILLS_CSV)
        targets = [(n, u) for n, u in url_map.items() if n not in skills_done][:limit]
    else:
        targets = [(n, u) for n, u in url_map.items() if n not in done][:limit]
    if not targets:
        print("未収集の装備はありません。")
        return
    print(f"収集対象: {len(targets)} 件（収集済み {len(done)} 件はスキップ）")

    HTML_DIR.mkdir(exist_ok=True)
    stats_done = read_csv_names(STATS_CSV)
    skills_done = read_csv_names(SKILLS_CSV)
    errors = 0
    with sync_playwright() as p:
        ctx = launch(p, headless=True)
        page = ctx.new_page()
        for i, (name, url) in enumerate(targets, 1):
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)  # 動的描画の待機
                body_text = page.inner_text("body")
                if "ログイン" in body_text and "会員限定" in body_text:
                    print(f"  [{i}] {name}: 会員限定表示を検出。--login でログインし直してください。")
                    errors += 1
                    if errors >= 3:
                        print("エラーが続くため中断します。")
                        break
                    continue
                tables = parse_tables(page)
                stats, element, skills = extract_stats(name, tables)
                info = extract_info(name, tables, stats, element)
                # 再走査時の重複追記を防ぐ（既出の装備はスキップ）
                if name not in done:
                    append_rows(INFO_CSV, INFO_HEADER, [info])
                    done.add(name)
                if stats and name not in stats_done:
                    append_rows(STATS_CSV, STATS_HEADER, stats)
                    stats_done.add(name)
                if name not in skills_done:
                    if not skills:
                        # スキル無し装備も訪問済みとして記録（空行マーカー）
                        skills = [{"名前": name, "グレード": 0, "レベル": 0,
                                   "スキル名": "", "スキルLv": ""}]
                    append_rows(SKILLS_CSV, SKILLS_HEADER, skills)
                    skills_done.add(name)
                if dump:
                    (HTML_DIR / f"{re.sub(r'[\\\\/:*?\"<>|]', '_', name)}.html").write_text(
                        page.content(), encoding="utf-8")
                print(f"  [{i}/{len(targets)}] {name}: 属性={info['属性'] or '?'} "
                      f"ステータス{len(stats)}行 スキル{len(skills)}行")
                errors = 0
            except Exception as e:
                errors += 1
                print(f"  [{i}/{len(targets)}] {name}: エラー {e}")
                if errors >= 3:
                    print("エラーが3回連続したため中断します。")
                    break
            time.sleep(WAIT_SECONDS)
        ctx.close()
    print(f"\n出力: {INFO_CSV}\n      {STATS_CSV}")
    print("build_db.py を再実行するとDBに反映されます。")


def main():
    parser = argparse.ArgumentParser(description="GameWith 装備情報収集ツール")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--login", action="store_true", help="ブラウザを開いてログイン（初回のみ）")
    g.add_argument("--discover", action="store_true", help="装備名→記事URLの対応表を構築")
    g.add_argument("--collect", action="store_true", help="不足情報を収集してCSVに追記")
    parser.add_argument("--limit", type=int, default=50, help="1回の実行で処理する最大件数")
    parser.add_argument("--dump", action="store_true", help="取得HTMLを raw_pages/html/ に保存（デバッグ用）")
    parser.add_argument("--only", help="指定した装備1件だけ収集（検証用）")
    parser.add_argument("--missing-stats", action="store_true",
                        help="ステータス未取得の装備だけ再収集する")
    parser.add_argument("--missing-skills", action="store_true",
                        help="スキル未取得の装備だけ再収集する")
    args = parser.parse_args()

    if args.login:
        cmd_login()
    elif args.discover:
        cmd_discover(args.limit)
    elif args.collect:
        cmd_collect(args.limit, args.dump, args.only, args.missing_stats,
                    args.missing_skills)


if __name__ == "__main__":
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
