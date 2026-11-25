# Traer5iAuto: Bruker ハンドヘルド XRF 自動測定

Bruker の蛍光X線装置(Bruker S1 Titan。USB RNDIS)に PC から直接つなぎ(TCP 55100〜55102。RemoteCTRL は使わない)、「測定 → アプリケーション(モード)切替 → 再測定」を繰り返すアプリ。画面は pywebview(WebView2)。ユーザーとのやり取りは日本語。

**このファイルは要点だけ。** 決まりの細部・経緯・実機で確かめた数値は [docs/DETAILS.md](docs/DETAILS.md)(節の名前はこのファイルと同じ)。**その部分を変える前に、DETAILS.md の該当する節を Grep で探して読む。** 決まりを変えたら DETAILS.md を直し、要点が変わるときだけこのファイルも直す。

## ファイル構成

```
TRACERexl/                  開発用(このフォルダ)
  Traer5iAuto/              配布するアプリ。改修はここ
    start_measure_gui.vbs   起動用(`pythonw src/web_app.py`)。ASCII・CRLF。相対パスの基準
    src/                    ソース(コマンドはこのフォルダで実行する)
    config/  TracerData/    初回起動で自動作成(設定・台帳・ログ / 保存先 File.csv・Results.csv・pdz/)
  tests/                    テスト(配布しない。TRACERexl で `python tests/...`)。sensor/・realdevice/(照射するものあり)・demo_web.py・demo_device.py・fake_device.py・build_exe.py(build/・dist/ を直下に作る)
  docs/                     資料。DETAILS.md(決まりの細部)・PROTOCOL_55000.md・PROTOCOL_55100.md・LID_SENSOR.md・WEBVIEW_CHECK.md・messages_review.csv(ユーザーが編集する文言の一覧)
  archive/                  git の外。読まない。backup_tk_2026-11-20/(Tk 版)・snapshot_2026-11-20/(切り替えの途中の Traer5iAuto)・data_2026-11-09/(移す前の config・TracerData)
  CHANGELOG.md / requirements.txt
  Software and Firmware/    Bruker 付属物。触らない
```

- **git で管理する**(2026-11-25〜。GitHub `rei-watanabe-chiba/CHIBAZAIDAN_TRACER5iCTRL`、**公開リポジトリ**、ブランチ main)。`config/`・`TracerData/`(PASS・測定データ)・`build/`・`dist/`・控えのフォルダ・`Software and Firmware/`・借用一覧の CSV は `.gitignore` で外す。公開なので、PASS・測定データ・他の機関の資料をコミットしない。控えはフォルダのコピーではなく git で残す。コミット・push はユーザーに頼まれたときだけ。

## 主なファイル(`Traer5iAuto/src/`)

| ファイル | 役割 |
|---|---|
| `web_app.py` / `web/` | 起動ファイルと画面。`Bridge`(`emit` / `run`)・`Api`・小窓・閉じるときの後始末・`--selftest`。`web/` は映して操作を返すだけ |
| `controller.py` | 測定の進め方(`Controller`・`Modes`・`LedgerModel`・`RefsModel`)。照射してよいかは `confirm_answer` |
| `device_core.py` | 本体画面(`DeviceWatch`): USB を探す・確かめる・「接続」・自動のつなぎ直し・初期化中の見張り |
| `app_rules.py` | 画面の決まり(状態の文言・メインのボタン・入力の不備・セットの終わり方・倍率) |
| `ledger_core.py` / `refs_core.py` / `modes_core.py` | 台帳 / 資料一覧 / 測定モード表の中身(画面を持たない) |
| `auto_measure.py` | 操作の中核(`Operator`・ログイン・切替・測定・計画実行)と CLI |
| `device_link.py` | 機器へ直接つなぐ(`DeviceLink`・`shared_link`・`DirectRemote`・`find_s1_titan`) |
| `screen_match.py` / `screens/` | 画面を見本と照合して読む(OCR なし)/ 見本(作るのは `tests/build_screens.py`) |
| `lid_sensor.py` | 蓋の磁気センサー(closed / open / none) |
| `instrument_files.py` | TCP 55000 のファイル一覧・取り込み(読み取り専用) |
| `storage.py` | フォルダの定数・`write_atomic`・設定・`single_instance`・`append_log` |
| `gui_config.py` | `class UI`(見た目と文言のすべて) |
| `shutdown.py` / `bruker_ui.py` | 閉じるときの照射の止め方 / RemoteCTRL の窓(予備。CLI の `--remotectrl`) |

保存するもの: `config/gui_settings.json`(設定。PASS は `pass` に平文 = ユーザーの判断)・`config/ledger.json`(**台帳の正本**)・`config/measure_log.csv`・`config/app_log.txt`・`config/last_error.png`(異常時 1 枚)・`<保存先>/File.csv`(正本から有効な行だけを毎回書き出す)。

CLI(`auto_measure.py`): `--check` / `--login` / `--switch-only "App"` / `--set-times "A=15+60"`(どれも照射なし)・`--plan "A=30,B=15+60" [--yes]`(**照射あり**)。

## 安全ルール(厳守)

- **X線照射(トリガー / F6 / WM_COMMAND 40011 / 55100 の `TRIGGER_PULL`)を、ユーザーの明示的な同意なしに送らない。** 測定の実行はユーザーが行う。照射を伴わないテスト(`--check`、`--switch-only`、状態の読み取り)だけを使う。
  - 例外は無い。放射線の注意の画面(「トリガを引いて放すと続行します」)も、アプリからは送らない(本体のトリガーを手で引いて放す)。
- **PASS をエージェントが入力しない。** 読み出して表示・送信しない。
- **画像を溜めない。** キャプチャはメモリ上だけ。保存するのは `config/last_error.png`(上書き)と、見本に無い画面(`config/new_screens/`、最大 40 枚)だけ。
- **蓋が閉じていなければ照射しない**([docs/LID_SENSOR.md](docs/LID_SENSOR.md))。センサーが無い・読めないときも照射しない(外す設定は作らない)。照射中に蓋が開いたらすぐトリガーを離す。
- 機器の表示設定(ズームなど)を変えたら元に戻す(既定は 1X、コマンド 104)。
- **プロトコルが分からないポートへ、ユーザーの同意なしにデータを送らない。** `$TS` は照射なので絶対に送らない。
- テストでは本物の `config/`・台帳・機器・蓋のセンサーに触れない。設定のパス(`WebApp(settings_path=…)`)・`am.LOG_CSV`・`am.ERROR_PNG`・`am.TRACER_DIR`・`am.LEDGER_STORE` を一時フォルダへ、機器は偽の機器(`tests/demo_device.py` の `install`・`device_core.connect_link` / `find_devices` の差し替え)、センサーは `lid_sensor._SENSOR = lid_sensor.Fixed`。
- トリガーを引いたあとは、どんな理由で中断してもトリガーを離す。測定の前からトリガーが引かれていたら照射しない(手動動作中)。

## 機器とつなぎ方(要点。細部は DETAILS.md「機器のネットワーク」「機器の操作の仕組み」)

- 機器は 192.168.137.139 が多いが**アドレスは固定しない**: USB の ID `USB\VID_226B` と名前「Bruker S1 Titan」で接続を探し、その接続の DHCP サーバーが機器、送り元はその接続の PC 側のアドレスに固定する(Wi-Fi 側へ流さないため)。
- 55102 = 画面(480x640 RGB565)とクリック・生存の知らせ / 55101 = トリガーの状態 / 55100 = トリガーの命令 / 55000 = ファイル([docs/PROTOCOL_55100.md](docs/PROTOCOL_55100.md)・[docs/PROTOCOL_55000.md](docs/PROTOCOL_55000.md))。機器は同時に 1 つの相手としかつながらない(RemoteCTRL・Instrument Tools は閉じておく)。
- つなぎ直すときは**前と同じ PC 側のポート番号**を使う(新しい番号では数分断られる)。切れた接続はすぐ捨てる。
- GUI でつなぐのは本体画面の「接続」だけ(同じ製造番号の機器が戻れば自動でつなぎ直す)。GUI でログインするのは「ログイン」ボタンだけ。
- 画面は見本との照合で読む。読めない画面は unknown で、測定は先へ進まない。
- 測定の終了は画面(結果画面)で判定する。測定後は次の切替の前に 2 秒待つ。取り込みは一連の測定がすべて終わってからまとめて行う。
- USB が切れると機器は照射を止める。ソフトの側で TCP を切っても照射は止まらない。

## GUI の約束ごと(要点。細部は DETAILS.md「GUI の約束ごと」)

- **決まりは Python の側**(`controller.py`・`device_core.py`・`*_core.py`・`app_rules.py`)。`web/` は映して操作を返すだけ。アプリの状態を変えるのは画面の糸(`Bridge.run`)だけ。裏の糸からは `post`。
- **見た目・文言の変更は `gui_config.py` の `class UI` だけ。** `app.css` には並べ方だけ。色は 青 = GO / 緑 = STAY / 赤 = 危険・停止 / 灰 = 確認中(橙は使わない)。
- 左の列は余白で区切る(線は引かない)。設定の項目を足すときはチェックの行に足す。本体画面の下が窓に収まるかを確かめる。
- メインのボタンは 1 つ(`app_rules.main_button`)。再計測・再開のときだけ右に「終了」。停止は照射中だけ押せる。
- 入力の不備は状態の欄に出す(アラートに出さない)。アラートは要注意の知らせだけ。
- 使わなくなった処理・`UI` の値は削除する。`subprocess` には `creationflags=CREATE_NO_WINDOW`。ファイルの保存は必ず `write_atomic`。
- アプリは 1 つしか起動しない(`storage.single_instance`)。
- 見た目を確かめるときはアプリの窓だけを `PrintWindow` で撮り、見たら画像は消す。

## 測定の決まり(要点。細部は DETAILS.md「途中で止まった回」「ターンと再計測」「照射前の確認」)

- 画面での名前は「セット」(コードでは `turn`)。照射した回はどんな終わり方でも台帳に載せる。失敗は 接続エラー / 停止エラー / 機器エラー / 手動変更 / ターン中断(`_judge` → `failure`)。失敗の行は File.csv に出さない。
- 失敗した回はその場で測り直さない。セットの後に「再計測」/「再開」、やめるなら「終了」(済みの回も無効)。有効な行ができるのは正常に終わったセットだけ。
- 再計測の判定は開いているセットの台帳の行が基準(`LedgerBook.pair`)。計測中・開いているセットの間は、計測ファイルの欄・資料一覧からの転記・測定モード表・そのセットの台帳の行を変えられない。
- 照射の前に入力の不備を調べ(値が変わるたび)、確認のダイアログ(安全確認のチェック 1 つ)でユーザーが同意してから照射する。チェックは閉じる処理の中でも確かめる。

## テスト(TRACERexl で実行)

| 変えたもの | 実行 |
|---|---|
| `*_core.py`・`app_rules.py` | `python tests/core_test.py` |
| 測定の流れ | `python tests/measure_logic_test.py` |
| `controller.py`・`device_core.py` | `python tests/controller_test.py`(2〜3 分) |
| `web_app.py`・`web/` | `python tests/web_smoke_test.py`(約 1 分) |

**ふつうは `python tests/run_changed.py` で走らせる**(まだコミットしていない変更から上の表のとおりに選ぶ。`main` を付けると main からの変更も、`--all` で全部、`--list` で選ぶだけ)。テストは手元だけで走らせる(GitHub Actions は 2026-11-25 にやめた: 毎回ここで走らせているので重なるため)。照射に関わらないテストと git の読み取りは `.claude/settings.json` で確認なしに許可し、`auto_measure.py` の CLI・`tests/realdevice/`・`git push` はいつも確認する。

新しい挙動には確認項目を足す。新しいファイルを足したら `run_changed.py` の `RULES` にも足す。テスト中に出た確認ダイアログを人が押すと結果が変わりうるので、テストはボタンや API を直接操作して判定する。

## 配布(細部は DETAILS.md「配布」)

- exe: `python tests/build_exe.py` → `dist/Traer5iAuto/`(フォルダごと配る。1 ファイル形式にしない)。`--selftest` で起動だけ確かめる。
- Python で配るなら `Traer5iAuto/`(`start_measure_gui.vbs` と `src/`)だけ。`config/` を渡すと PASS も渡るので、配る前に消すか空にする。
- 配布先に要るもの: Windows 10/11・WebView2 Runtime・Bruker の USB ドライバー(Bruker Toolbox)。

## サブエージェントの使い分け(トークン節約)

| 名前 | 使う場面 |
|---|---|
| `code-locator`(haiku、読み取り専用) | 処理や設定値の場所を探すとき。ファイルを丸ごと Read する前に使う |
| `gui-tester`(sonnet) | 画面・Controller を変えたあとの確認。テストの実行と項目の追加をして、OK/NG の要約だけを返す |

使わずにメインで済ませるもの: 場所が分かっている小さな修正(Read の offset/limit で該当部分だけ読む)・改修ログの追記・機器の確認(安全ルールに関わるため)。

## 改修ログ(必須)

コードや仕様を変えたら、作業の最後に必ず `CHANGELOG.md` に記録する。

- いちばん上の日付見出し(`## YYYY-MM-DD`)の下に追記する。新しい順。その日の見出しが無ければ作る。
- 1 回の改修を `### 見出し` 1 つにまとめる。中身は 追加 / 変更 / 修正 / 削除 / 整理 のうち該当するものを、ユーザーにとっての変化として短く書く。必要なら対象のファイル名や関数名を添える。
- 実機で確認したことは「実機確認」、テスト用データでしか確認していないことはそうと明記する。まだのものは「未確認」と書き、確認できたら「実機確認」として追記する。
- 「ユーザーの判断」で決めた決まりは、日付を添えて DETAILS.md の該当する節に書く(このファイルには書かない)。
- 改修ログ自体の書き方を変えたら、この節も合わせて直す。
