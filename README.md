# モンスターハンターNow 装備攻略プランナー

モンハンNowの装備作成を支援するツール群。作りたい装備を選ぶと最適な作成順序のフローチャートを生成し、
全装備データベースに対して派生探索・素材計算・条件検索を行える。

- 情報源: [GameWith](https://gamewith.jp/monsterhunternow/)、[アルテマ](https://altema.jp/mhnow/)、[Gamerch](https://gamerch.com/mh-now/)
- 実行環境: Windows / Python 3.14（uv管理の `.venv`）

---

## Webアプリ（スマホ対応）

```
.venv\Scripts\python -m streamlit run app.py     # ローカル起動
```

[app.py](app.py) は装備選択→実行ボタン→準備ツリー表示のStreamlitアプリ。
Streamlit Community Cloud にデプロイするとスマホのブラウザから利用できる。

デプロイ手順:
1. GitHubにリポジトリを作成し、このプロジェクトをpush
   （login.json / browser_profile / data/mhnow.db は .gitignore 済み。
     raw_pages のCSVと data/*.json は必要なので含めること）
2. https://share.streamlit.io にGitHubアカウントでログイン
3. 「Create app」→ リポジトリと `app.py` を指定してデプロイ
4. 発行されたURL（https://～.streamlit.app）にスマホからアクセス

data/mhnow.db が無い環境では、アプリが初回起動時にCSVから自動構築する。

## ファイル構成

```
c:\python\MH_now\
├── app.py                 # Webアプリ（Streamlit。装備選択→準備ツリー表示）
├── requirements.txt       # デプロイ用依存パッケージ
├── flowchart.py           # 装備作成フローチャート生成（Mermaid + HTML）
├── planner.py             # 攻略プランナーCLI（探索・素材計算・検索）
├── optimize.py            # ④ 装備最適化（ダメージ期待値最大の編成探索）
├── gear_tree.py           # 装備準備ツリー（目標装備→対策装備→…を★3以下まで遡る）
├── build_db.py            # SQLiteデータベース構築
├── collect_gamewith.py    # GameWithから不足情報を収集するクローラ
├── collect_skill_effects.py # スキル効果（レベル別数値）の抽出
├── collect_index_data.py  # 武器一覧（会心率・素材元）とモンスター弱点の一括取得
├── collect_material_sources.py # 素材→入手元モンスター対応表の取得
├── collect_game8_materials.py  # 素材欠損装備の補完（game8から収集）
├── collect_altema_materials.py # 素材欠損防具の補完（アルテマから収集）
├── collect_recommended_sets.py # GameWith推奨装備セット（記事414964）の収集
├── materials_csv.py       # 素材CSV読み込みモジュール
├── login.json.example     # GameWith認証情報のテンプレート（login.jsonは.gitignore対象）
├── data\
│   ├── equipment.json     # フローチャート用メタデータ（モンスター・フェーズ・攻略メモ）
│   ├── skill_effects.json # 全スキルのレベル別効果（collect_skill_effects.pyで生成）
│   ├── monsters.json      # 全モンスターの弱点属性（collect_index_data.pyで生成）
│   ├── recommended_sets.json # GameWith推奨装備セット（collect_recommended_sets.pyで生成）
│   └── mhnow.db           # SQLiteデータベース（build_db.pyで再構築可能）
├── raw_pages\             # 収集データ（CSV）
│   ├── monsterhunternow_weapon_materials_wide.csv   # 武器の全強化素材（手動入手）
│   ├── monsterhunternow_armor_materials_wide.csv    # 防具の全強化素材（手動入手）
│   ├── equipment_urls.json     # 装備名→GameWith記事URLの対応表（--discoverで生成）
│   ├── equipment_info.csv      # 属性・レア度など（--collectで生成）
│   ├── equipment_stats.csv     # グレード別 攻撃力/属性値/防御力（--collectで生成）
│   └── equipment_skills.csv    # グレード別スキル（--collectで生成）
└── output\
    ├── flowchart.md            # Mermaidフローチャート入りMarkdown
    └── flowchart.html          # ブラウザで開ける自己完結HTML
```

---

## データ収録状況（2026-07-05時点）

| データ | 収録率 | 内容 |
| --- | --- | --- |
| 装備 | 1,516件（武器1,176・防具340） | 名称・区分・武器種/シリーズ・部位 |
| 強化素材 | 52,400ステップ / 166,670素材行 | 生産〜G10 Lv5の全素材・ゼニー実数 |
| 武器の属性 | 1,176 / 1,176（100%） | 無377・雷171・火156・氷124・水122・龍100・毒75・睡眠30・麻痺21 |
| グレード別ステータス | 1,516 / 1,516（100%） | 武器=攻撃力・属性値、防具=防御力（各グレードLv1時点の下限値） |
| スキル | 1,514 / 1,516（99.9%） | 121種類・2,363行。スキルLvごとの解放グレード付き |

未収録: 「禍鎧」「ミヅハ」のスキル（5部位同名の特殊装備でページ構造が異なるため）

---

## 使い方

以下、`python` は `.venv\Scripts\python.exe` を指す。

### 1. フローチャート生成（flowchart.py）

部位ごとに目標装備を指定すると、素材元モンスターの狩猟フェーズ順に作成手順を出力する。

```
python flowchart.py                    # 既定: 片手剣無属性最強装備
python flowchart.py --list             # 登録装備の一覧
python flowchart.py --weapon 吼剣【地咬】 --head オーグヘルム --chest ウルクメイル ^
                    --arm レイアアーム --waist レックスロアコイル --leg レックスロアグリーヴ
```

- 出力: `output/flowchart.md`（Mermaid）と `output/flowchart.html`（自己完結HTML）
- 素材個数は raw_pages のCSV実数を優先。フェーズ分け・攻略メモは `data/equipment.json` で管理
  （新しい装備をフローチャート対象にするには equipment.json への登録が必要）

既定編成の生成結果: 全6装備の最大強化に **総ゼニー2,554,000**。
フェーズ: ①ウルクスス（ウルクメイル）→ ②リオレイア（レイアアーム）→ ③ネルギガンテ（オーグヘルム）→ ④ティガレックス亜種（吼剣【地咬】・レックスロアコイル・レックスロアグリーヴ）

### 2. データベース構築（build_db.py）

raw_pages のCSVと data/equipment.json から `data/mhnow.db` を再構築する。CSVを更新したら再実行。

```
python build_db.py
```

### 3. 攻略プランナー（planner.py）

```
# ① 派生ツリー探索（作成ルート逆算・派生先一覧）
python planner.py route 吼剣【地咬】

# ② 必要素材の総量計算（現在→目標グレード、--with-parentsで派生元も合算）
python planner.py cost 吼剣【地咬】 --from 5:1 --to 10:5

# ③ 条件付きフィルタリングとソート
python planner.py find --weapon-type 太刀 --element 水 --sort attack
python planner.py find --slot 胴 --skill 攻撃
python planner.py find --category 防具 --series レックスロア

# スキルの表示（解放グレード付き）
python planner.py skills オーグヘルム

# 素材の逆引き（どの装備がその素材を使うか）
python planner.py material 黒轟竜の逆鱗

# モンスター一覧（弱点・討伐難易度・関連装備数。難易度順に表示）
python planner.py monsters
python planner.py monsters --element 水
```

### 4. データ収集（collect_gamewith.py）

GameWithの装備ページからデータを収集し raw_pages のCSVに追記する。

```
python collect_gamewith.py --login                  # 初回のみ: ブラウザでログイン（CAPTCHA手動対応）
python collect_gamewith.py --discover --limit 42    # 装備名→記事URL対応表の構築
python collect_gamewith.py --collect --limit 100    # 未収集の装備を収集（再開可能）
python collect_gamewith.py --collect --missing-stats    # ステータス未取得分のみ再収集
python collect_gamewith.py --collect --missing-skills   # スキル未取得分のみ再収集
python collect_gamewith.py --collect --only 装備名 --dump  # 1件だけ収集しHTMLも保存（検証用）
```

- **属性・ステータス・スキルはログイン不要で取得可能**（会員限定なのは素材テーブルのみ）
- 認証情報は `login.json`（gitignore対象）。CAPTCHA（Cloudflare Turnstile）の自動突破は行わない設計。
  ログイン後のセッションは `browser_profile/` に保存され再利用される
- マナー対策: ページ間4秒待機、連続エラー3回で自動中断、収集済みスキップ
- **注意**: GameWithの利用規約上、自動アクセスは禁止されている可能性がある。個人利用の範囲で自己責任で使用すること

---

## データベーススキーマ（data/mhnow.db）

正規化された6テーブル構成。

| テーブル | 内容 |
| --- | --- |
| `equipment` | ① 基本データ。武器・防具統合（category列で区別）。名称・武器種/シリーズ・部位・属性・レア度・生産グレード・素材元モンスター |
| `equipment_tree` | ② 派生ツリー（parent_id）。**MH Nowは全装備が直接生産のため現状すべてNULL**。派生関係が判明したら equipment_info.csv の「派生元」列で登録可能 |
| `stats` | ③ グレード別ステータス（攻撃力・属性値・防御力） |
| `upgrade_steps` | ④-a 強化ステップ（装備×グレード×レベルごとのゼニー） |
| `step_materials` | ④-b ステップごとの必要素材と個数 |
| `skills` | 装備スキル（スキル名・スキルLv・解放グレード） |

設計メモ: ゼニーは素材ごとではなく強化ステップごとの値のため、④を steps / materials の2テーブルに分割して重複を排除している。

### 追加データの投入方法

`raw_pages/` に以下の形式のCSVを置いて `build_db.py` を再実行すると自動で取り込まれる。

```
equipment_info.csv  … 名前,属性,レア度,派生元,モンスター,部位
equipment_stats.csv … 名前,グレード,レベル,攻撃力,属性値,防御力
equipment_skills.csv … 名前,グレード,レベル,スキル名,スキルLv
```

---

## 実装済みアルゴリズム

1. **派生ツリー探索（DFS）** — `planner.py route`。parent_idを再帰的にたどり、作成ルートの逆算と派生先一覧を表示
2. **必要素材の総量計算** — `planner.py cost`。指定区間の強化ステップを合算し、素材名をキーにグループ化。`--with-parents` で派生元装備の費用も再帰的に合算
3. **条件付きフィルタリングとソート** — `planner.py find`。区分・武器種・シリーズ・部位・属性・スキル・名称で絞り込み、攻撃力（防具は防御力）・総ゼニー順でソート
4. **装備最適化** — `optimize.py`。武器×防具5部位の組み合わせからダメージ期待値最大の編成を探索

### ④ 装備最適化（optimize.py）

```
python optimize.py --monster ティガレックス亜種 --weapon-type 片手剣
python optimize.py --element 水 --element 龍 --optimistic --top 5
```

ダメージモデル:

```
攻撃力合計 = 武器攻撃力 × (1 + Σ攻撃力%) + Σ攻撃力実数
会心補正   = 1 + (25% + 超会心) × 正会心率 − 25% × マイナス会心率
属性合計   = (武器属性値 + Σ属性攻撃力) × (1 + 属性倍率 + 会心撃【属性】×会心率) × 弱点係数
             弱点係数: 弱点属性 1.0 / 非弱点 0.3 / 無属性武器は属性ダメージなし
期待値EV  = (攻撃力合計 × 会心補正 + 属性合計) × (1 + Σ与ダメージ%)
```

ルール:
- 全装備G10最大強化と仮定。同名スキルはレベル加算しスキル上限でキャップ
- 「◯◯・境地」系は基底スキルLv5の発動が条件（例: 攻撃・境地 ← 攻撃Lv5）
- 属性攻撃強化系は武器属性一致時のみ有効（幻獣の疾雷=雷、溟龍の波雷=水なども判別）
- 発動条件付きスキル（ジャスト巧撃・追い打ち等）は既定で除外、`--optimistic` で常時発動扱い
- 「連撃」「弱点特効」は実戦でほぼ常時発動とみなし既定で有効
- 探索: 武器候補（単体EV上位12）× 部位別防具候補（寄与上位8）の全組み合わせ

スキル効果の元データは `collect_skill_effects.py` がGameWithページ埋め込みの
tooltipDatas（全150スキルのレベル別効果）から抽出して `data/skill_effects.json` に生成する。

### 装備準備ツリー（gear_tree.py）

「①目標装備の素材モンスター抽出 → ②そのモンスター討伐用の装備セット選定 →
③その装備の素材モンスター抽出 → ①へ再帰」を、討伐難易度が `--max-star`（既定★3）
以下になるまで繰り返し、狩る順序が分かる木構造を出力する。

```
python gear_tree.py                          # 既定の片手剣無属性最強装備
python gear_tree.py --max-star 3 --weapon-type 片手剣
```

- 素材→入手元は `data/material_sources.json`（`collect_material_sources.py` で生成）
- 対策装備の候補は「対象モンスターより討伐難易度の低いモンスター素材だけで作れる装備」に
  限定するため、ツリーは必ず易しい方向へ収束して停止する
- 複数モンスターから入手できる素材（古龍の血など）は最も難易度の低い1体で集める扱い
- 素材情報の無い装備は対策候補から除外される
- ツリー末尾に重複除去済みの「装備セット一覧」を出力（同一セットで狩れる🔴モンスターを集約し、
  作成順＝討伐対象の難易度が低い順に並ぶ）
- 出力: コンソール（🎯目標 ⚒対策装備 🔴要対策 🟢そのまま狩れる）+ output/gear_tree.md

### 素材欠損データの補完

元CSVで素材欄が空だった59装備のうち45防具は補完済み
（game8: 30件 → `collect_game8_materials.py`、アルテマ: 15件 → `collect_altema_materials.py`。
いずれも `raw_pages/*_materials_supplement.csv` に保存され、materials_csv.py が自動で優先読込）。
残るケマトリス武器14種（ケマス系・炎尾系）は公開サイトに素材表が存在しないため未補完。
装備の主モンスター（ケマトリス）情報でツリー生成には支障なし。正確な素材が必要な場合は
ゲーム内確認かGameWith会員ページの手動保存で補える。

---

## 既知の制限

- 防具の防御力は「49〜60」のような範囲表記のうち下限値（各グレードLv1時点）のみ記録
- 「禍鎧」「ミヅハ」はスキル未収録、素材CSVでも5部位が同名のため部位別に区別できない
- 武器の基礎会心率は `collect_index_data.py`（全武器一覧の埋め込みデータ）から取得済みで、
  optimize.py の会心期待値に反映される（equipment.crit列）
- モンスター弱点は data/monsters.json に66体分収録（`optimize.py --monster 名前` で利用可能。
  弱点は「弱点で絞り込む」フィルタ由来のため複数属性を含む。--element で単一指定も可能）
- 討伐難易度は「出現する★の範囲」（各モンスターページのHRP/ゼニー表から取得）。
  再収集は `python collect_index_data.py --difficulty`（66ページ・約4分・再開可能）
- optimize.py のダメージモデルは比較用の近似値。モーション値・肉質・武器種係数は含まない
- 超会心・会心撃【属性】は正会心時のみ、力任せのマイナス会心はダメージ75%として期待値化済み
