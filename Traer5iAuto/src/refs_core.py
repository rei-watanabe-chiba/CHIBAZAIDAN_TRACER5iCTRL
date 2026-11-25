"""資料一覧の中身(画面を持たない)。CSV の読み込み、結合列、列の割り当て(ID・項目1・項目2)、あいまい検索、並べ替え。
画面の側(controller.RefsModel)はこれを呼んで表に映すだけにする(2026-11-19: WebView2 へ作り替える前に、処理と画面を分けた)"""
from __future__ import annotations

import csv, io, unicodedata

from gui_config import UI

_IGNORE = str.maketrans("", "", " \t-_‐‑–—―−/.,・")    # 照合で無視する区切り文字(ハイフン・空白など)
NUM = "#0"                                            # 行番号の列(表の木の列)


def norm(s: str) -> str:
    """照合用: 全角/半角・大文字/小文字をそろえ、区切り文字を無視する"""
    return unicodedata.normalize("NFKC", s).casefold().translate(_IGNORE)


def _subseq(q: str, s: str) -> bool:
    """q の文字が s に順番どおり含まれるか(例: "a1" は "a001" に含まれる)"""
    it = iter(s)
    return all(c in it for c in q)


def rank_norm(q: str, v: str, loose: bool = True) -> int | None:
    """norm 済みの q・v の一致の順位(小さいほど上)。一致しなければ None。
    0: 完全一致 / 1: 先頭一致 / 2: 部分一致 / 3: 文字が順番どおり含まれる(loose のときだけ)"""
    if not q:
        return 2
    if v == q:
        return 0
    if v.startswith(q):
        return 1
    if q in v:
        return 2
    if loose and _subseq(q, v):
        return 3
    return None


def match_rank(query: str, value: str) -> int | None:
    """あいまい一致の順位(rank_norm)。一致しなければ None"""
    return rank_norm(norm(query), norm(value))


def sort_key(s: str) -> tuple:
    """並べ替えの順: 数として読めれば数の順、ほかは文字の順(norm)、空欄は最後"""
    t = s.strip().replace(",", "")
    if not t:
        return (2, 0.0, "")
    try:
        return (0, float(t), "")
    except ValueError:
        return (1, 0.0, norm(s))


def read_csv(path: str) -> tuple[list[str], list[list[str]], list[int]]:
    """CSV を読む(UTF-8 / BOM 付き UTF-8 / Shift_JIS。区切りはカンマ・タブ・セミコロンを自動判別)。
    (見出し, 行, 行ごとの元のファイルの行番号) を返す。見出しが空の列は「列1」などにする。空の行は飛ばす"""
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("文字コードが分かりません(UTF-8 か Shift_JIS で保存してください)")
    first = text.lstrip("\r\n").split("\n", 1)[0]
    delim = max(",\t;", key=first.count) if any(d in first for d in ",\t;") else ","   # 見出しの行で一番多い区切り
    rows, lines, end = [], [], 0
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    for r in reader:
        start, end = end + 1, reader.line_num             # この行が始まったファイルの行(引用の中の改行も数える)
        if any(c.strip() for c in r):
            rows.append(r); lines.append(start)
    if not rows:
        raise ValueError("中身が空です")
    head = [h.strip() or f"列{i + 1}" for i, h in enumerate(rows[0])]
    width = max(len(r) for r in rows)
    head += [f"列{i + 1}" for i in range(len(head), width)]
    return head, [r + [""] * (width - len(r)) for r in rows[1:]], lines[1:]


class RefBook:
    """資料一覧の中身。表の列 cols = 結合列(あれば)+ CSV の列、表の行 rows = 結合列の値 + CSV の行。
    roles: 役割(UI.REF_ROLES)-> 列。sort: (列, 降順か) / None = 元の順(列 NUM は元の CSV の行番号の順)"""

    def __init__(self):
        self.path = ""
        self.head: list[str] = []                    # CSV の列
        self.base: list[list[str]] = []              # CSV の行
        self.lines: list[int] = []                   # 行ごとの元の CSV の行番号
        self.join: tuple[str, str, str] | None = None   # 結合列 (列A, 区切り, 列B)
        self.cols: list[str] = []
        self.rows: list[list[str]] = []
        self.roles: dict[str, str] = {}
        self.sort: tuple[str, bool] | None = None
        self._norms: dict[int, list[str]] = {}        # 列の番号 -> 行ごとの norm 済みの値(絞り込みの度に作らない)

    def load(self, path: str, roles: dict | None = None, join=None):
        """CSV を読む(読めなければ OSError / ValueError / csv.Error)。roles: 役割 -> 列(無い列は外す。None なら ID に
        「ID」を含む列、それも無ければ1列目)。join: 結合列 (列A, 区切り, 列B)(列が無ければ作らない)"""
        head, rows, lines = read_csv(path)
        self.path, self.head, self.base, self.lines = path, head, rows, lines
        self.join = tuple(join) if join and len(join) == 3 and join[0] in head and join[2] in head else None
        self.sort = None
        self.rebuild()
        if roles is None:
            roles = {"ID": next((h for h in head if "id" in norm(h)), head[0])}
        self.roles = {r: c for r, c in roles.items() if r in UI.REF_ROLES and c in self.cols}

    @property
    def join_name(self) -> str | None:
        if not self.join:
            return None
        name = UI.REF_JOIN_NAME.format(a=self.join[0], b=self.join[2])
        return name if name not in self.head else name + UI.REF_JOIN_DUP

    def rebuild(self):
        """表の列と行を作り直す(CSV の列 + 結合列)"""
        jn = self.join_name
        self.cols = ([jn] if jn else []) + self.head      # 結合列は # の右(一番左のデータの列)
        if jn:
            ia, ib, sep = self.head.index(self.join[0]), self.head.index(self.join[2]), self.join[1]
            self.rows = [[r[ia] + sep + r[ib]] + r for r in self.base]
        else:
            self.rows = self.base
        self._norms.clear()

    # ---- 結合列(1 列だけ)
    def can_join(self, a: str, sep: str, b: str) -> bool:
        """作れる(列A・列B が CSV の列)で、今の結合列と違うか(「作成 / 更新」を押せるか)"""
        return a in self.head and b in self.head and self.join != (a, sep, b)

    def set_join(self, join) -> tuple[str | None, str | None]:
        """結合列を作る / 作り直す / 消す(None)。割り当て・並べ替えは新しい列へ移す(消したら外す)。(前の名前, 新しい名前)を返す"""
        old = self.join_name
        self.join = tuple(join) if join else None
        self.rebuild()
        new = self.join_name
        self.roles = {r: (new if c == old else c) for r, c in self.roles.items() if c != old or new}
        if self.sort and self.sort[0] == old:
            self.sort = (new, self.sort[1]) if new else None
        return old, new

    # ---- 列の割り当て
    def role_of(self, col: str) -> str | None:
        return next((r for r, c in self.roles.items() if c == col), None)

    def assign(self, col: str, role: str | None):
        """列 col を役割 role に使う(role=None で外す)。1つの役割は1列だけ、1つの列は1つの役割だけ"""
        self.roles = {r: c for r, c in self.roles.items() if c != col and r != role}
        if role:
            self.roles[role] = col

    def values(self, n: int) -> dict[str, str]:
        """行 n の、割り当てた役割ごとの値(計測ファイルの欄へ入れる)"""
        return {role: self.rows[n][self.cols.index(col)] for role, col in self.roles.items() if col in self.cols}

    # ---- 並べ替え・絞り込み
    def sort_by(self, col: str):
        """見出しのクリック: 昇順 → 降順 → 元の順(# の列も同じ)"""
        if self.sort and self.sort[0] == col:
            self.sort = None if self.sort[1] else (col, True)
        else:
            self.sort = (col, False)

    def _norm_col(self, c: int) -> list[str]:
        if c not in self._norms:
            self._norms[c] = [norm(r[c]) for r in self.rows]
        return self._norms[c]

    def matches(self, query: str, target: str = UI.REF_ALL) -> list[int]:
        """target の列(全列なら どれかの列)が query にあいまい一致する行の番号を、一致の良い順に(同じ順位は CSV の順)。
        全列のときは 文字が順番どおり含まれる(順位 3)は使わない(どの列にも当たって絞り込めないため)"""
        q = norm(query)
        if not q:
            return list(range(len(self.rows)))
        if target in self.cols:
            vals = self._norm_col(self.cols.index(target))
            scored = [(rk, n) for n, v in enumerate(vals) if (rk := rank_norm(q, v)) is not None]
        else:
            cols = [self._norm_col(c) for c in range(len(self.cols))]
            scored = []
            for n in range(len(self.rows)):
                ranks = [rk for vals in cols if (rk := rank_norm(q, vals[n], loose=False)) is not None]
                if ranks:
                    scored.append((min(ranks), n))
        return [n for _, n in sorted(scored)]

    def view(self, query: str = "", target: str = UI.REF_ALL) -> list[int]:
        """表に出す行の番号(絞り込み → 並べ替え)。query は検索が ON のときの文字列(OFF なら "")"""
        idx = self.matches(query, target)
        if self.sort and self.sort[0] == NUM:
            idx.sort(key=lambda n: self.lines[n], reverse=self.sort[1])
        elif self.sort and self.sort[0] in self.cols:
            c = self.cols.index(self.sort[0])
            idx.sort(key=lambda n: sort_key(self.rows[n][c]), reverse=self.sort[1])
        return idx
