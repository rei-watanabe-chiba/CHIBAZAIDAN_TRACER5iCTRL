"""機器(TCP 55000)のファイル一覧の取得とファイルのダウンロード。読み取り専用(形式は PROTOCOL_55000.md)

    python instrument_files.py list "\\BRUKER\\Data"                  # フォルダの一覧
    python instrument_files.py get "\\BRUKER\\Data\\Results.csv" [保存先フォルダ]   # 1ファイルをダウンロード
    python instrument_files.py fetch 723,724 [保存先フォルダ]     # pdz を 保存先/pdz へ、Results.csv を保存先へ(上書き)

Instrument Tools が接続中だと、機器は応答しない。先に Instrument Tools を終了しておくこと。

安全策:
  - 送れるのは 接続時の挨拶 / $OW BrukerCommandFile.CMD / $FW(READ_COMMANDS だけ)/ $CF / $OR / $FR のみ。
    $TS(X線照射)・削除・書き込み系は、組み立てる前に拒否する。
  - 機器上の BrukerCommandFile.CMD は、コマンドを渡すために書き換わる(Instrument Tools も同じ動作)。
"""
from __future__ import annotations

import csv, datetime as dt, io, os, socket, struct, sys, time
from dataclasses import dataclass

from storage import TRACER_DIR, use_base, write_atomic

HOST, PORT = "192.168.137.139", 55000
MAGIC = 0xDEADFEED                       # BRKR_REMOTE_S1INFO.dwMagic(通信の記録で確認)
CMD_FILE = "BrukerCommandFile.CMD"
READ_COMMANDS = ("GetFileList ", "GetFileStatus ", "GetSerialNumber")   # $FW で送ってよいコマンド
ALLOWED_PREFIXES = (b"$OW ", b"$FW ", b"$CF", b"$OR ", b"$FR ")
CR = b"\r"
RECORD = 300                             # 一覧の1件(WIN32_FIND_DATA 相当、ファイル名は ASCII 260)


def _crc(data: bytes) -> int:
    return sum(data) & 0xFF              # CRCModulo256


def _filetime(v: int) -> dt.datetime | None:
    return dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=v // 10) if v else None


@dataclass
class Entry:
    name: str
    size: int
    modified: dt.datetime | None
    is_dir: bool


def port_in_use(host=HOST, port=PORT) -> bool:
    """他のソフト(Instrument Tools)が機器の 55000 に接続中か。接続中だと機器はこちらに応答しない"""
    import subprocess
    try:
        out = subprocess.run(["netstat", "-n", "-p", "TCP"], capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout   # 黒い窓を出さない
    except Exception:
        return False
    return any(f"{host}:{port}" in line and "ESTABLISHED" in line for line in out.splitlines())


class Instrument:
    def __init__(self, host=HOST, port=PORT, timeout=5.0, verbose=False, source=None):
        """source: 送り元の PC 側のアドレス(その機器の USB の接続のもの。本体画面の接続のときに覚えたもの)"""
        if port_in_use(host, port):
            raise RuntimeError("Instrument Tools が機器に接続中です。終了してからもう一度実行してください")
        self.verbose = verbose
        self.sock = socket.create_connection((host, port), timeout=timeout,
                                             source_address=(source, 0) if source else None)
        hello = struct.pack("<IIII", 0, MAGIC, 0, 0) + bytes(128 + 128 + 32)    # 304 バイト
        self._send(hello, hello=True)

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---- 低レベル
    def _send(self, data: bytes, hello=False):
        if not hello and not data.startswith(ALLOWED_PREFIXES):
            raise ValueError(f"許可していない送信です: {data[:20]!r}")      # $TS(照射)などは送らない
        if self.verbose:
            print(f"  >> {data[:80]!r}")
        self.sock.sendall(data)

    def _recv(self, until=None, size: int | None = None, timeout=10.0, idle=1.0) -> bytes:
        """until(末尾)か size バイトまで読む。どちらも無ければ idle 秒途切れるまで読む"""
        end, buf = time.time() + timeout, b""
        while time.time() < end:
            if (until and buf.endswith(until)) or (size is not None and len(buf) >= size):
                break
            self.sock.settimeout(idle if buf and not until and size is None else max(0.1, end - time.time()))
            try:
                b = self.sock.recv(65536)
            except socket.timeout:
                if buf and not until and size is None:
                    break
                continue
            if not b:
                break
            buf += b
        if self.verbose:
            print(f"  << {len(buf)} バイト: {buf[:100]!r}")
        return buf

    def _expect(self, prefix: bytes, resp: bytes) -> bytes:
        if not resp.startswith(prefix):
            raise RuntimeError(f"想定外の応答({prefix!r} を期待): {resp[:80]!r}")
        return resp

    # ---- コマンド($OW → $FW → 応答 → $CF)
    def command(self, cmd: str) -> bytes:
        if not cmd.startswith(READ_COMMANDS):
            raise ValueError(f"読み取り系以外のコマンドは送りません: {cmd!r}")
        name = CMD_FILE.encode("ascii")
        self._send(b"$OW %d, " % len(name) + name + CR)
        self._expect(b"!OW", self._recv(until=CR, timeout=5))
        data = cmd.encode("ascii")
        self._send(b"$FW %d, " % len(data) + bytes([_crc(data)]) + CR + data)
        resp = self._expect(b"!FW", self._recv(timeout=20, idle=1.0))
        self._send(b"$CF" + CR)
        self._recv(until=CR, timeout=5)
        return resp[4:]

    def list(self, folder: str) -> list[Entry]:
        body = self.command(f"GetFileList {folder.rstrip(chr(92))}\\*.*")
        body = body[1:] if body.startswith(CR) else body
        out = []
        for i in range(0, len(body) - RECORD + 1, RECORD):
            r = body[i:i + RECORD]
            attr, _, _, mtime, hi, lo, _ = struct.unpack_from("<IQQQIII", r, 0)
            name = r[40:].split(b"\0", 1)[0].decode("ascii", "replace")
            if name:
                out.append(Entry(name, (hi << 32) | lo, _filetime(mtime), bool(attr & 0x10)))
        return out

    # ---- ファイル取得($OR → $FR → $CF)
    def get(self, remote: str) -> bytes:
        name = remote.encode("ascii")
        self._send(b"$OR %d, " % len(name) + name + CR)
        size = int(self._expect(b"!OR", self._recv(until=CR, timeout=5))[4:].strip())
        self._send(b"$FR %d" % size + CR)
        head = self._recv(size=16, timeout=10)                  # "!FR <size>, " + CRC(1バイト) + CR + データ
        self._expect(b"!FR", head)
        p = head.index(b", ") + 2                               # CRC の位置(CRC 自体が 0x0D のこともある)
        n, crc = int(head[4:p - 2]), head[p]
        data = head[p + 2:]
        data += self._recv(size=n - len(data), timeout=60) if len(data) < n else b""
        self._send(b"$CF" + CR)
        self._recv(until=CR, timeout=5)
        data = data[:n]
        if len(data) != size or _crc(data) != crc:
            raise RuntimeError(f"受信データが壊れています(サイズ {len(data)}/{size}, CRC {_crc(data)}/{crc})")
        return data


DATA_DIR = r"\BRUKER\Data"
RESULTS = "Results.csv"
PDZ_DIR = "pdz"                          # 保存先の中の pdz 置き場


def fetch_measurements(folder: str, sample_nos: list[str], retries=5, wait=1.0,
                       connect=None, host=HOST, source=None) -> tuple[list[str], list[str]]:
    """一連の測定のデータを保存先に取り込む(1回の接続で)。(保存したパス, 機器に無かった No) を返す
      - <folder>/pdz/NNNNN-<アプリ名>.pdz : ファイル No が sample_nos の pdz(一覧に現れるまで retries 回確かめる)
      - <folder>/Results.csv             : 機器の結果表(既存は上書き)。途中で止まった回の pdz・行もそのまま(分けるのは台帳だけ)"""
    want = {int(n): str(n) for n in sample_nos}
    connect = connect or (lambda: Instrument(host, source=source))   # 本体画面の接続と同じ機器・同じ接続へ
    pdz_dir = os.path.join(folder, PDZ_DIR)
    os.makedirs(pdz_dir, exist_ok=True)
    with connect() as inst:
        found: dict[int, str] = {}
        for i in range(retries):
            for e in inst.list(DATA_DIR):
                head = e.name.split("-", 1)[0]
                if not e.is_dir and e.name.lower().endswith(".pdz") and head.isdigit() and int(head) in want:
                    found[int(head)] = e.name
            if len(found) == len(want):
                break
            if i < retries - 1:
                time.sleep(wait)
        saved = []
        for no in sorted(found):
            saved.append(os.path.join(pdz_dir, found[no]))
            write_atomic(saved[-1], inst.get(f"{DATA_DIR}\\{found[no]}"))
        saved.append(os.path.join(folder, RESULTS))
        write_atomic(saved[-1], inst.get(f"{DATA_DIR}\\{RESULTS}"))
    return saved, [want[n] for n in want if n not in found]


def results_rows(connect=None, host=HOST, source=None) -> list[dict]:
    """機器で測った回の一覧(読むだけ。保存しない)。結果表(Results.csv)の行(列は File #, Application, ElapsedTime など)に、
    結果表に載らない回(pdz だけがある。Spectrometer Mode の回は結果表に行が無い: 2026-11-13 実機)を
    {"File #": No, "Application": pdz の名前のモード, "ElapsedTime": ""} として足し、No の順に並べる。
    測定の途中で接続が切れたとき、その回が終わっていたかを確かめるのに使う(auto_measure.resume_step。開始のときの最後の No も)"""
    connect = connect or (lambda: Instrument(host, source=source))
    with connect() as inst:
        data = inst.get(f"{DATA_DIR}\\{RESULTS}")
        entries = inst.list(DATA_DIR)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp932", "replace")
    rows = [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(io.StringIO(text))]
    rows = [r for r in rows if file_no(r) is not None]          # 結果表には途中に見出しの行が繰り返し入る
    have = {file_no(r) for r in rows}
    for e in entries:
        head, _, rest = e.name.partition("-")
        if not e.is_dir and e.name.lower().endswith(".pdz") and head.isdigit() and int(head) not in have:
            rows.append({"File #": str(int(head)), "Application": rest[:-4], "ElapsedTime": ""})
    return sorted(rows, key=file_no)


def file_no(row: dict) -> int | None:
    v = row.get("File #", "")
    return int(v) if v.isdigit() else None


def main():
    a = [x for x in sys.argv[1:] if x not in ("-v", "--verbose")]
    verbose = len(a) != len(sys.argv) - 1
    cmd = a[0] if a else "list"
    use_base()                           # 相対パスの基準を VBS のフォルダにする
    dest = a[2] if len(a) > 2 else TRACER_DIR                           # 既定の保存先
    if cmd == "fetch":                   # 測定した分(pdz と Results.csv)を保存先へ
        saved, missing = fetch_measurements(dest, a[1].split(","), connect=lambda: Instrument(verbose=verbose))
        for p in saved:
            print(f"保存しました: {p}")
        if missing:
            print(f"機器にありません: No.{', '.join(missing)}")
        return
    with Instrument(verbose=verbose) as inst:
        if cmd == "list":
            folder = a[1] if len(a) > 1 else r"\BRUKER\Data"
            entries = inst.list(folder)
            print(f"{folder}: {len(entries)} 件")
            for e in entries:
                when = e.modified.strftime("%Y-%m-%d %H:%M") if e.modified else "-"
                print(f"  {'<DIR>' if e.is_dir else f'{e.size:>9,}'}  {when}  {e.name}")
        elif cmd == "get":
            remote = a[1]
            os.makedirs(dest, exist_ok=True)
            data = inst.get(remote)
            path = os.path.join(dest, remote.split("\\")[-1])
            with open(path, "wb") as f:
                f.write(data)
            print(f"保存しました: {path} ({len(data):,} バイト)")
        else:
            print(__doc__)


if __name__ == "__main__":
    main()
