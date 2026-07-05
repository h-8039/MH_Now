# -*- coding: utf-8 -*-
"""モンハンNow 装備プランナー Webアプリ（Streamlit）

最終目標の武器/防具を選択して実行すると、討伐難易度★3以下まで遡った
装備準備ツリー（gear_tree）を表示する。

ローカル実行:
    streamlit run app.py
Streamlit Community Cloud:
    GitHubリポジトリにpushして share.streamlit.io からデプロイ
    （data/mhnow.db が無い環境では初回起動時にCSVから自動構築する）
"""

import sqlite3
from pathlib import Path

import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "data" / "mhnow.db"

SLOTS = ["頭", "胴", "腕", "腰", "脚"]
DEFAULT_LOADOUT = {
    "武器": "吼剣【地咬】",
    "頭": "オーグヘルム",
    "胴": "ウルクメイル",
    "腕": "レイアアーム",
    "腰": "レックスロアコイル",
    "脚": "レックスロアグリーヴ",
}


def db_ready():
    """DBが存在し、装備データが入っているかを確認する。

    ファイルの有無だけでは不十分（構築途中でプロセスが再起動されると
    空のDBファイルが残るため、中身まで検証する）。
    """
    if not DB_FILE.exists():
        return False
    try:
        conn = sqlite3.connect(DB_FILE)
        n = conn.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        conn.close()
        return n > 0
    except sqlite3.Error:
        return False


def ensure_db():
    """クラウド環境などDBが無い/壊れている場合、CSVから自動構築する。"""
    if db_ready():
        return
    with st.spinner("初回起動: データベースを構築しています…（1〜2分）"):
        import build_db
        build_db.main()
    if not db_ready():
        st.error("データベースの構築に失敗しました。raw_pages のCSVが"
                 "リポジトリに含まれているか確認してください。")
        st.stop()
    load_choices.clear()  # 空リストがキャッシュされていた場合に備える


@st.cache_data
def load_choices(db_version: float = 0.0):
    """選択肢（武器種→武器、部位→防具）を読み込む。

    db_version はDB再構築後にキャッシュを無効化するためのキー。
    """
    conn = sqlite3.connect(DB_FILE)
    weapons = {}
    for wtype, name in conn.execute(
            "SELECT weapon_type, name FROM equipment "
            "WHERE category='武器' ORDER BY weapon_type, name"):
        weapons.setdefault(wtype, []).append(name)
    armors = {}
    for slot, name in conn.execute(
            "SELECT slot, name FROM equipment "
            "WHERE category='防具' AND slot IS NOT NULL ORDER BY slot, name"):
        armors.setdefault(slot, []).append(name)
    wtypes = sorted(weapons.keys())
    conn.close()
    return wtypes, weapons, armors


def main():
    st.set_page_config(page_title="モンハンNow 装備プランナー",
                       page_icon="⚔️", layout="centered")
    st.title("⚔️ モンハンNow 装備プランナー")
    st.caption("最終目標の装備を選ぶと、討伐難易度の低いモンスターから順に"
               "「何を狩り、何を作るか」の準備ツリーを表示します。")

    ensure_db()
    wtypes, weapons, armors = load_choices(DB_FILE.stat().st_mtime)
    if not wtypes:
        st.error("装備データが読み込めませんでした。ページを再読み込みしてください。")
        st.stop()

    # ---------------- 装備選択フォーム ----------------
    st.subheader("🎯 最終目標の装備")

    default_wtype = "片手剣" if "片手剣" in wtypes else wtypes[0]
    wtype = st.selectbox("武器種", wtypes, index=wtypes.index(default_wtype))
    wlist = weapons[wtype]
    w_default = DEFAULT_LOADOUT["武器"] if DEFAULT_LOADOUT["武器"] in wlist else wlist[0]
    weapon = st.selectbox("武器", wlist, index=wlist.index(w_default))

    cols = st.columns(2)
    armor_sel = {}
    for i, slot in enumerate(SLOTS):
        alist = armors.get(slot, [])
        default = DEFAULT_LOADOUT[slot] if DEFAULT_LOADOUT[slot] in alist else alist[0]
        with cols[i % 2]:
            armor_sel[slot] = st.selectbox(
                f"{slot}防具", alist, index=alist.index(default))

    with st.expander("詳細設定"):
        max_star = st.slider("この討伐難易度(★)以下は「そのまま狩れる」とみなす",
                             1, 6, 3)
        tree_wtype = st.selectbox(
            "対策装備の武器種（途中で作るつなぎ武器）",
            ["目標武器と同じ"] + wtypes)

    # ---------------- 実行 ----------------
    if st.button("🌲 準備ツリーを生成", type="primary", use_container_width=True):
        from gear_tree import GearTree
        sel_wtype = wtype if tree_wtype == "目標武器と同じ" else tree_wtype
        loadout = [weapon] + [armor_sel[s] for s in SLOTS]
        with st.spinner("ツリーを計算中…"):
            tree = GearTree(max_star, sel_wtype)
            tree.run(loadout)

        st.subheader("📋 準備ツリー")
        st.caption("🎯目標装備 ⚒作成する対策装備 🔴要対策モンスター "
                   f"🟢★{max_star}以下（そのまま狩れる） 🔁前述")
        st.markdown("\n".join(tree.md))

        st.download_button("Markdownをダウンロード",
                           data="\n".join(tree.md),
                           file_name="gear_tree.md",
                           mime="text/markdown",
                           use_container_width=True)

    st.divider()
    st.caption("データ出典: GameWith / アルテマ / game8（個人利用の攻略支援ツール）")


if __name__ == "__main__":
    main()
