# 実機の確認用スクリプト(2026-11-13 の確認で使ったもの)

配布しない。TRACERexl で `python tests/realdevice/<名前>.py ...` と実行する(`PYTHONIOENCODING=utf-8` を付けると文字化けしない)。
機器へ直接つなぐ(本体画面と同じ)。アプリ・RemoteCTRL・Instrument Tools は閉じておく。

**⚠ 照射するものがある。ユーザーの同意と、試料・周囲の安全(カバーなど)を確かめてから使う。**

| ファイル | 照射 | 内容 |
|---|---|---|
| `resume_check.py` | なし | 機器の結果表で `resume_step` の判断(測り終えた・照射されていない・合わない)を確かめる。トリガーの命令は送れないようにしてある |
| `read_times.py "Mudrock Air" "Obsidian" ...` | なし | 各モードの設定画面の時間欄を読むだけ(キャンセルで閉じる)。書き換える前に元の値を控える |
| `measure_plan.py "Obsidian=12,..." <結果.json> [--no-set]` | **あり** | 計画を測り、結果の行(失敗・ずれ・題名)と測定中の生存の知らせ(0x19)の間隔を出す |
| `cut_resume.py "Obsidian=12" <照射N秒で切る> <N秒後につなぐ>` | **あり** | 照射中にソフトの側で接続を切り、つなぎ直して再開の流れを見る(ソフトで切っても照射は止まらない) |

- `measure_plan.py`・`cut_resume.py`・`--set-times` は機器の測定時間を書き換える。終わったら元に戻す(2026-11-13 時点: Mudrock Air 10+10・Obsidian 15・Glass Dual 30+50・Spectrometer Mode 10)。
- `measure_plan.py`・`cut_resume.py` は本物の `config/measure_log.csv` に行を足す。
- `gui_stop.py`(Tk 版の画面で狙った瞬間に「停止」を押す)は 2026-11-20 に Tk 版と一緒に削除した(控えは `backup_tk_2026-11-20/tests/realdevice/`)。
