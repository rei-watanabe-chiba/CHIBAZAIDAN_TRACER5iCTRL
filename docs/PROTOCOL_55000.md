# 機器 TCP 55000 の通信

2026-11-09 調査。二段階で調べた。

1. **静的解析**: `Bruker Instrument Tools.exe`(内部名 S1Sync、v1.9.0.146、.NET x86)の設定・文字列・IL を読んだ。
2. **実際の通信の記録**: Instrument Tools が一覧を取り、`Results.csv` をダウンロードしたときの通信を `pktmon` で記録した(記録ファイルは解析後に削除)。
   - 下の形式は、この記録で**確認済み**。

実装は [Traer5iAuto/src/instrument_files.py](Traer5iAuto/src/instrument_files.py)。

## 位置づけ

- 55000 は Instrument Tools のメイン接続ポート(`S1Sync.exe.config`: `ConnectionType=Network`, `PortNo=55000`, `XrayPortNo=54990`)。
  - 54990 は X線関係のポートと思われる。調査時は閉じていた。
- **同時に1つの相手としかやり取りしない**。Instrument Tools が接続中だと、機器は TCP 接続を受け付けるが、何も応答しない。
- 実装クラスは `BrukerCommunication.TcpCommManager` / `CommunicationManager` / `S1Sync.BrukerCommWrapper`。
- `XRFConnectLib.dll` と 55500 番(`ConnectivityServer`)は PC 内の Bruker Data Stream 用で、機器との通信には関係しない。

## 接続時の挨拶

接続したら、最初に構造体 `BRKR_REMOTE_S1INFO`(304 バイト)を送る。

- 中身は `dwSize=0`、**`dwMagic=0xDEADFEED`**(リトルエンディアンで `ED FE AD DE`)、残りはすべて 0。
- 機器からの返事はない。
- 静的解析では `0xDEADBEED` と読み違えていた。その値では機器は何も応答しない。
- Instrument Tools は挨拶の約 0.1 秒後に 4 バイト(`3A 35 32 1F`)を送っている。意味は不明。送らなくても動くかは未確認。

## メッセージ形式

テキスト形式で、末尾は CR(0x0D)。返事は `!` で始まる。CRC は全バイトの和の下位 8 ビット(`CRCModulo256`)。

| 送信 | 返事 | 意味 |
|---|---|---|
| `$OW <名前の長さ>, <名前>\r` | `!OW 0\r` | ファイルを書き込みで開く |
| `$FW <長さ>, ` + CRC(1バイト)+ `\r` + データ | `!FW ...` | 書き込む。コマンドファイルの場合はその結果が返る |
| `$CF\r` | `!CF\r` | ファイルを閉じる |
| `$OR <名前の長さ>, <名前>\r` | `!OR <サイズ>\r` | ファイルを読み取りで開く |
| `$FR <サイズ>\r` | `!FR <サイズ>, ` + CRC + `\r` + データ | 読む(35KB のファイルも1回で返ってきた) |
| `$TS` / `$TE` | `!TS` / `!TE` | **トリガー開始 / 終了(X線照射)。送らない** |

### 機器コマンドの実行

コマンドは、機器上のファイル `BrukerCommandFile.CMD` に書き込んで実行させる。手順は `$OW BrukerCommandFile.CMD` → `$FW <コマンド>` → 返事 → `$CF`。
そのため、読み取りだけのコマンドでも、機器上のこのファイルは書き換わる。

確認済みのコマンド:

- `GetSerialNumber` → `!FW 900G12901\r`
- `GetFileList <フォルダ>\*.*` → `!FW \r` の後に、1件 300 バイトの記録が並ぶ。
  - 1件の中身(WIN32_FIND_DATA 相当、リトルエンディアン):

    | 位置 | 内容 |
    |---|---|
    | 0 | 属性(4)。0x10 ならフォルダ |
    | 4 | 作成日時(FILETIME、8) |
    | 12 | アクセス日時(FILETIME、8) |
    | 20 | 更新日時(FILETIME、8) |
    | 28 | サイズ 上位(4) |
    | 32 | サイズ 下位(4) |
    | 36 | ID(4) |
    | 40 | ファイル名(ASCII、260) |

- `GetFileStatus <ファイル>` → `!FW` + 28 バイトのバイナリ。属性と日時(SYSTEMTIME)らしいが、未解析。

### ファイルのダウンロード(Instrument Tools の手順)

`GetFileStatus` → `$OR` → `$FR <サイズ>` → CRC を確認 → `$CF`。

## 機器のフォルダ(2026-11-09 時点)

- `\`: `BRUKER`, `Temp`, `Windows`, `Program Files`, `My Documents` など。
- `\BRUKER`: `Data`、アプリ定義(`Obsidian.aen` など)、`InstrumentDef.idf`、ログ(`tracelog*.txt`, `eventlog0.txt`)など。
- **`\BRUKER\Data`: 測定データ。** 82件(`*.pdz` 81件 + `Results.csv`)。
  - `NNNNN-<アプリ名>.pdz` がスペクトル。番号は結果画面のファイル No。
    - サイズの例: Obsidian 9,388 バイト、Mudrock Air 18,628 バイト。
  - `Results.csv` が全測定の結果表。
    - 項目は File #, DateTime, Application, 元素ごとの濃度と誤差など。
    - アプリごとに見出し行が入る。
- 機器の時計は 2006 年になっている(ファイルの日時もそれに従う)。
- 一覧の更新日時(FILETIME)は、`Results.csv` の DateTime より 8 時間進んでいる(例: No.704 は一覧 00:05、CSV 16:05)。時差の扱いは未確認。
- 2026-11-09 に `instrument_files.py` から実機で一覧(82件)と `Results.csv`・`.pdz` のダウンロードを確認した(Instrument Tools は終了した状態)。

## 送ってはいけないコマンド

- `$TS`: X線照射の開始。`$TE`: 停止
- `DeleteFile` / `DeleteFolder` / ファイルのアップロード / `UncompressFile`(ファームウェア更新に使う)
- `SetSerialNumber`
