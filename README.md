# CAE Lab

**A prototype tool for listing and tracking CAE analysis results (OpenFOAM / CalculiX), running entirely in Docker.**
It scans result folders, auto-detects the solver, run date, completion status, computation time, and analysis
settings by parsing each solver's own log/config/input files (no external CAE libraries required), and displays
them in a browsable web UI with per-case notes and an auto-generated mesh-shape thumbnail. Built as a step toward
a lightweight alternative to full CAE-PDM systems for small teams. The app UI itself is Japanese-only for now.

---

以下、日本語での詳細説明です。

## 目的

CAE解析者は解析結果ファイルをフォルダに平置きしがちで、後から見返すのが困難になりやすい。
これを解決するCAE用PDMツールは存在するが、数人規模の小規模チームには導入が重い。
本リポジトリは、その簡易版をOpenFOAM／CalculiXを対象に検証するプロトタイプ。

- ホストのUbuntu本体には何もインストールせず、OpenFOAM・CalculiX・管理ツール自体を
  すべてDockerコンテナ内で完結させる
- 元の解析結果ファイルには一切書き込まない（メモ・サムネイル等は別ディレクトリに保存）
- 解析結果の中身（数値・グラフ）は表示しない。あくまで「一覧管理」に特化

## できること

- 監視フォルダ配下からOpenFOAM／CalculiXのケースを自動検出し、一覧表示
  - ソルバー種別・ケース名・実行日・完走判定・計算時間（CPU時間／実時間）
- ケース名での絞り込み、列見出しクリックでの並べ替え
- 失敗ケースは行を強調表示し、停止理由（ログの該当行）を表示
- 詳細パネル（行クリックで開く）で以下を表示
  - 解析の種類（線形/非線形、定常/非定常、圧縮性、乱流/層流、単相/多相、熱/構造 等。
    ソフトごとに自然な形で解釈）
  - 解析設定（時間刻み・ステップ数・増分制御）
  - CalculiX: 固有値/座屈解析の有無、接触の有無、材料モデル、荷重条件、要素種別
  - OpenFOAM: 乱流モデル名、収束判定基準、メッシュ生成方法、次元（2D/3D/軸対称）
  - メッシュ形状のサムネイル画像（1枚の俯瞰図。ヘッドレスコンテナ内でVTK等を使わず
    `matplotlib`のみで生成）
  - ケースごとのメモ（自由記入、保存される）
  - ホスト側の絶対パス（コピー用ボタン付き）

## アーキテクチャ

```
cae-lab/
  compose.yaml              # openfoam / calculix / app の3サービス
  docker/calculix/          # CalculiXのDockerfile（Ubuntu24.04 + apt）
  app/                      # 解析結果管理ツール本体
    Dockerfile
    backend/main.py         # FastAPI: ケース検出・判定ロジック・API
    backend/mesh_render.py  # メッシュ形状の画像化（polyMesh / .inp を自前パース）
    static/index.html       # フロントエンド（素のHTML/JS、一覧＋詳細パネル）
    data/memos.json         # メモの永続化（ホスト側に残る。元データとは別置き）
    data/thumbnails/        # メッシュ形状サムネイル（同上）
  cases/openfoam/           # OpenFOAM チュートリアル実行結果（サンプルデータ）
  cases/calculix/           # CalculiX サンプル実行結果（サンプルデータ）
  v0-detection-proposal.md  # 初期の判定ロジック検討メモ（設計の経緯資料）
```

| サービス | 内容 |
|---|---|
| `openfoam` | `opencfd/openfoam-default:2406`（公式配布の非GUIイメージ、tutorials同梱） |
| `calculix` | Ubuntu 24.04 + `calculix-ccx`（ソルバー本体）+ `calculix-ccx-test`（標準サンプル集） |
| `app` | FastAPI + 素のHTML/JS。`./cases`を`:ro`（読み取り専用）でマウント |

## セットアップ・起動方法

前提: Docker（Docker Desktop等）が使えること。

```bash
git clone <このリポジトリ>
cd cae-lab
export HOST_UID=$(id -u) HOST_GID=$(id -g) HOST_CASES_DIR="$(pwd)/cases"
docker compose up -d --build
```

ブラウザで `http://localhost:8080/` を開くと一覧画面が表示される。

```bash
docker compose exec calculix bash    # 例: CalculiXコンテナに入る
docker compose exec openfoam bash    # 例: OpenFOAMコンテナに入る
```

### 実運用フォルダを監視対象にする場合

現状は`./cases`のみを`:ro`でマウントしている。実際の解析結果フォルダを監視するには、
`compose.yaml`の`app.volumes`に、そのフォルダを（`:ro`を維持したまま）追加し、
`SCAN_ROOT`環境変数が指すパス配下に見えるようにする必要がある。

## サンプルデータ（計10ケース）

自分の過去の実データではなく、OpenFOAM／CalculiXが**公式に配布している例題**のみを使用。
CalculiXには「チュートリアル」という区分が無いため、開発元（dhondt.de）配布の標準サンプル集
（`calculix-ccx-test`）から代表例を選んでいる。

| フォルダ | ソフト | 内容 | 結果 |
|---|---|---|---|
| `openfoam/01_icoFoam_cavity` | OpenFOAM | cavity（層流・非定常・非圧縮） | 正常終了 |
| `openfoam/02_simpleFoam_pitzDaily` | OpenFOAM | pitzDaily（乱流kEpsilon・定常・非圧縮） | 正常終了 |
| `openfoam/03_icoFoam_cavity_FAILED` | OpenFOAM | cavityの動粘性係数を負値に改変 | **意図的失敗**（発散・FPEクラッシュ、endTime未到達） |
| `openfoam/04_sonicFoam_shockTube` | OpenFOAM | shockTube（圧縮性・層流） | 正常終了 |
| `openfoam/05_interFoam_damBreak` | OpenFOAM | damBreak（多相流VOF・層流） | 正常終了 |
| `calculix/01_beamlin_cantilever` | CalculiX | beamlin（片持ち梁、B32、線形静解析） | 正常終了 |
| `calculix/02_beamtor_torsion` | CalculiX | beamtor（片持ち梁ねじり、B32、非線形幾何） | 正常終了 |
| `calculix/03_beamlin_BROKEN` | CalculiX | beamlinの境界条件が存在しない節点を参照するよう改変 | **意図的失敗**（入力エラーで計算開始前に停止） |
| `calculix/04_oneel20fi_thermal` | CalculiX | oneel20fi（対流境界条件付き熱伝達、C3D20R、1要素） | 正常終了 |
| `calculix/05_shell2_hinge` | CalculiX | shell2（片持ちシェル、S8×2枚でヒンジ形状、非線形幾何） | 正常終了 |

意図的失敗ケースは2種類の異なる壊れ方（計算途中の発散 / 入力エラーによる開始前停止）を
意図的に用意し、完走判定ロジックが両方のパターンに対応できることを確認している。

## 判定ロジックの詳細

いずれも「元ファイルを書き換えない」制約のもと、結果フォルダ内の既存ファイル
（ログ・設定・入力ファイル）を読むだけで判定する。

### ソルバー種別

- OpenFOAM: `system/controlDict`ファイル と `constant/`ディレクトリの両方が存在
- CalculiX: `*.inp`ファイルが存在
- 両方に該当／どちらにも該当しない場合は「想定外のソフト」として非表示

### 実行日

両ソフトとも、結果フォルダ内の全ファイルの**最も新しいmtime（更新日時）**を採用。
OpenFOAMのログには実際の実行日時が埋め込まれているが、CalculiXのログには無いため、
実装を1本化する目的で両ソフトともmtimeベースに統一している。

最古のmtimeではなく最新のmtimeを採用しているのは、過去のケースフォルダを
コピーして再実行する運用を想定しているため。最古のmtimeだと、コピー元の
メッシュ・設定ファイルの古いタイムスタンプを拾ってしまい、実際の実行日が
数ヶ月前と誤表示されるおそれがある。最新のmtime（＝最後に書き込まれた
ログ・結果ファイルの時刻）であれば、再実行のたびに更新される。

### 完走判定（正常終了したか）

エラーの有無ではなく「最後まで走ったか」のみを見る（結果の物理的妥当性は見ない）。

- OpenFOAM: ログ（`log.<ソルバー名>`）の**最後の非空行が`End`**かどうか
- CalculiX: ログ（`log.ccx`等）に**`Job finished`**の文字列があるかどうか
- 該当ログが無い場合は「判定不能」として扱う

### 計算時間

- OpenFOAM: ログ中最後に出現する`ExecutionTime = X s  ClockTime = Y s`から
  CPU時間・実時間の両方を取得（`ClockTime`は整数秒刻みで精度は粗い）
- CalculiX: ログ末尾の`Total CalculiX Time: X`から取得（CPU/実時間の区別なし、1値のみ）

### 解析の種類・解析設定

設定・入力ファイルを読んで判定する（ログは見ない）。ソフトごとに自然な形で解釈しており、
一方のソフトにしか無い概念（圧縮性・乱流/層流・単相/多相はOpenFOAMのみ、熱/構造は
CalculiXのみ）は無理に共通軸へ押し込めていない。

| 項目 | OpenFOAM | CalculiX |
|---|---|---|
| ソルバー名／解析手続き | `controlDict`の`application` | `*STEP`直後の手続きキーワード（`*STATIC`等） |
| 定常/非定常・静解析/動解析 | `fvSchemes`の`ddtSchemes.default`が`steadyState`か | 手続きキーワードが`DYNAMIC`系か等 |
| 線形/非線形 | （概念なし） | `*STEP`行に`NLGEOM`パラメータがあるか |
| 圧縮性 | `constant/thermophysicalProperties`の有無 | （概念なし） |
| 層流/乱流 | `constant/turbulenceProperties`の`simulationType` | （概念なし） |
| 単相/多相 | `transportProperties`に`phases (...)`があるか | （概念なし） |
| 熱/構造 | （概念なし） | 手続きキーワードに`HEAT TRANSFER`等が含まれるか |
| 時間刻み | `controlDict`の`deltaT` | 手続きキーワード直後の増分制御数値行（無ければ「自動」） |
| ステップ数 | `(endTime-startTime)/deltaT`（`adjustTimeStep true`なら「可変」） | `*STEP`〜`*END STEP`ブロックの数 |

CalculiX側の詳細項目（詳細パネルにのみ表示）:

- 固有値解析／座屈解析の有無（手続きキーワードに`FREQUENCY`/`BUCKLE`が含まれるか）
- 接触の有無（`*CONTACT PAIR`の有無）
- 材料モデル（`*MATERIAL`名＋プロパティ種別。詳細は後述の堅牢性の項を参照）
- 荷重条件の種類（`*CLOAD`/`*DLOAD`/`*BOUNDARY`/`*FILM`等）
- 要素種別（`*ELEMENT`の`TYPE=`一覧）

OpenFOAM側の詳細項目:

- 乱流モデルの具体名（`RASModel`/`LESModel`）
- 収束判定基準（`fvSolution`の`residualControl`を表示のみ、判定はしない）
- メッシュ生成方法（`blockMeshDict`/`snappyHexMeshDict`の有無）
- 次元（`polyMesh/boundary`のパッチtypeから2D/3D/軸対称を推定）

### メッシュ形状のサムネイル

VTK/ParaViewのような重量級ライブラリやヘッドレス3D特有のOSMesa/EGL設定を使わず、
`matplotlib`（Aggバックエンド）+`numpy`のみで実現している。

- **OpenFOAM**: `constant/polyMesh`の`points`/`faces`/`boundary`をテキストとして自前
  パースし、境界パッチのfaceだけを外殻サーフェスとして描画（内部セルは描かない）。
  2次元押し出しケース（チュートリアルの大半が該当）は斜め俯瞰だと薄すぎて形状が
  潰れて見えるため、次元判定が「2D」のケースは自動的に真上からの視点に切り替える。
  軸対称（wedge）ケースも同様の課題があると想定されるが、検証用の実例が無く未対応。
- **CalculiX**: `.inp`の`*NODE`/`*ELEMENT`カードを自前パースし、要素タイプを3系統に
  分類して描画方式を切り替える（行連続＝末尾カンマにも対応）。
  - ソリッド要素（C3D8/C3D20R等）: コーナー節点＋辺トポロジ表でワイヤーフレーム
  - シェル要素（S3/S4/S8等）・膜要素: コーナー節点を1枚の面として塗りつぶし
  - 線要素（B32等）: 節点を記載順につないだパス
  - 対応表に無い要素タイプは**黙ってスキップしない**。種別・件数を記録し、
    詳細パネルの画像直下に警告として表示する
- 画像は`app/data/thumbnails/`にキャッシュ（元の解析結果ファイルとは別置き）

## 動作確認済みの範囲

このツールの判定ロジックは、以下の組み合わせで**実際に動作を確認**している
（`cases/`配下の10ケース）。それ以外の組み合わせも、想定外の値でクラッシュしたり
誤った情報を表示したりしないように作っているが（後述の堅牢性チェック参照）、
分類が「不明」表示になる、または期待と異なる分類になる可能性がある。

- **OpenFOAMソルバー**: icoFoam・simpleFoam・sonicFoam・interFoam
- **乱流モデル**: kEpsilon（RAS）。LESや他のRASモデルは未検証
  （`LESModel`キーワード自体には対応済みだが実例なし）
- **CalculiX解析手続き**: STATIC（線形・非線形幾何）、HEAT TRANSFER（定常）。
  DYNAMIC・FREQUENCY・BUCKLE・接触解析（`*CONTACT PAIR`）は判定ロジックとしては
  実装済みだが、実例での動作確認はできていない
- **CalculiX要素種別**: B32（梁）、C3D20R（六面体20節点）、S8（シェル）
- **CalculiX材料プロパティ**: ELASTIC、CONDUCTIVITY

一方で、**基本情報**（ソルバー種別・ケース名・実行日・完走判定・計算時間）は、
上記の組み合わせに関わらず動作する設計になっている（ログファイルの有無や末尾の
記述パターンだけを見ており、ソルバー名や要素種別には依存しないため）。

## 既知の制約・今後の課題

- 軸対称（wedge）ケースのメッシュ可視化ビュー（検証用の実例が無く未対応）
- 実運用フォルダをマウントしての動作確認
- DYNAMIC・FREQUENCY・BUCKLE・接触解析など、未検証のCalculiX解析手続きの実例確認
- 3面図化、メッシュ規模・サイズの統計表示などの拡張
- アプリ画面自体の多言語対応（未着手）

## ライセンス

[MIT License](LICENSE)
