#!/usr/bin/env python3
"""変換ツール - データ形式の相互変換と Protobuf 解析 (標準ライブラリのみ)

  python converter.py [--port 8410]    ブラウザで http://127.0.0.1:8410 を開く
  python converter.py --selftest       変換の自己診断

形式変換:  CSV / TSV / JSON / JSON Lines / XML / YAML / Markdown表 / HTML表 / SQL INSERT を相互に変換
Protobuf:  .proto なしでもバイナリ(hex / base64 / ファイル)を解析して CSV に変換 (.proto を与えると項目名・型つき)
"""
import argparse
import base64
import binascii
import csv
import html
import io
import json
import math
import re
import struct
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlparse
from xml.sax.saxutils import escape as xesc, quoteattr

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 40 * 1024 * 1024


class ConvError(Exception):
    pass


# ====================================================== 値の推測・フラット化
_INT = re.compile(r"-?(?:0|[1-9]\d{0,17})")
_FLOAT = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


def infer(s):
    """文字列を数値・真偽値・null に変換（先頭ゼロの数字 '007' などは文字列のまま）"""
    if not isinstance(s, str):
        return s
    t = s.strip()
    if t == "" or t == "null":
        return None
    if t in ("true", "TRUE", "True"):
        return True
    if t in ("false", "FALSE", "False"):
        return False
    if _INT.fullmatch(t):
        return int(t)
    if _FLOAT.fullmatch(t) and ("." in t or "e" in t or "E" in t):
        try:
            v = float(t)
            return v if math.isfinite(v) else s
        except ValueError:
            return s
    return s


def infer_rows(rows):
    return [{k: infer(v) for k, v in r.items()} for r in rows]


def flatten(obj, prefix="", out=None):
    """{'a':{'b':[1,2]}} -> {'a.b[0]':1,'a.b[1]':2}"""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        if not obj and prefix:
            out[prefix] = "{}"
        for k, v in obj.items():
            flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(obj, list):
        if not obj and prefix:
            out[prefix] = "[]"
        for i, v in enumerate(obj):
            flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix or "value"] = obj
    return out


_TOK = re.compile(r"\[(\d+)\]|([^.\[\]]+)")


def unflatten(flat):
    root = {}
    for key, val in flat.items():
        toks = [(int(a) if a else b) for a, b in _TOK.findall(str(key))]
        if not toks:
            root[key] = val
            continue
        cur = root
        for i, t in enumerate(toks):
            last = i == len(toks) - 1
            nxt = None if last else toks[i + 1]
            if isinstance(cur, dict):
                if last:
                    cur[t] = val
                else:
                    cur = cur.setdefault(t, [] if isinstance(nxt, int) else {})
            else:  # list
                while len(cur) <= t:
                    cur.append(None)
                if last:
                    cur[t] = val
                else:
                    if cur[t] is None:
                        cur[t] = [] if isinstance(nxt, int) else {}
                    cur = cur[t]
    return root


def to_records(data):
    """データから表にする行（dict のリスト）を取り出す"""
    d = data
    while isinstance(d, dict) and len(d) == 1:
        v = next(iter(d.values()))
        if isinstance(v, (dict, list)):
            d = v
        else:
            break
    if isinstance(d, list):
        return [r if isinstance(r, dict) else {"value": r} for r in d]
    if isinstance(d, dict):
        return [d]
    return [{"value": d}]


def table_of(data, do_flatten=True):
    rows = [flatten(r) if do_flatten else r for r in to_records(data)]
    cols = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols, rows


def cell(v):
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        return repr(v)
    return str(v)


def unique_cols(names):
    out, seen = [], {}
    for i, n in enumerate(names):
        n = (n or "").strip() or f"col{i + 1}"
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 1
        out.append(n)
    return out


def rows_from_matrix(matrix, header=True):
    matrix = [r for r in matrix if any((c or "").strip() for c in r)] if matrix else []
    if not matrix:
        return []
    if header:
        cols = unique_cols(matrix[0])
        body = matrix[1:]
    else:
        width = max(len(r) for r in matrix)
        cols = [f"col{i + 1}" for i in range(width)]
        body = matrix
    width = max([len(cols)] + [len(r) for r in body])
    while len(cols) < width:
        cols.append(f"col{len(cols) + 1}")
    return [{cols[i]: (r[i] if i < len(r) else "") for i in range(width)} for r in body]


# ====================================================== パーサ
DELIMS = {"comma": ",", "tab": "\t", "semicolon": ";", "pipe": "|", ",": ",", "\t": "\t", ";": ";", "|": "|"}


def detect_delim(text):
    lines = [l for l in text.split("\n")[:8] if l.strip()]
    best, bc = ",", 0
    for d in [",", "\t", ";", "|"]:
        counts = [l.count(d) for l in lines]
        if counts and min(counts) > 0 and counts[0] > 0 and counts[0] > bc:
            best, bc = d, counts[0]
    if bc == 0:
        for d in [",", "\t", ";", "|"]:
            if lines and lines[0].count(d) > bc:
                best, bc = d, lines[0].count(d)
    return best


def parse_csv(text, delim=None, header=True):
    d = DELIMS.get(delim) if delim and delim != "auto" else detect_delim(text)
    try:
        matrix = list(csv.reader(io.StringIO(text, newline=""), delimiter=d))
    except csv.Error as e:
        raise ConvError(f"CSVを読み取れません: {e}")
    return rows_from_matrix(matrix, header)


def parse_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ConvError(f"JSONの{e.lineno}行目 {e.colno}文字目でエラー: {e.msg}")


def parse_jsonl(text):
    out = []
    for i, ln in enumerate(text.split("\n"), 1):
        if ln.strip():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError as e:
                raise ConvError(f"JSON Lines の{i}行目でエラー: {e.msg}")
    return out


def _xml_obj(e):
    kids = list(e)
    attrs = {"@" + k: v for k, v in e.attrib.items()}
    txt = (e.text or "").strip()
    if not kids and not attrs:
        return txt
    d = dict(attrs)
    for k in kids:
        tag = k.tag.split("}", 1)[-1]
        v = _xml_obj(k)
        if tag in d:
            if not (isinstance(d[tag], list) and d.get("__list__" + tag)):
                d[tag] = [d[tag]]
                d["__list__" + tag] = True
            d[tag].append(v)
        else:
            d[tag] = v
    for k in [k for k in d if k.startswith("__list__")]:
        del d[k]
    if txt:
        d["#text"] = txt
    return d


def parse_xml(text):
    try:
        root = ET.fromstring(text.encode("utf-8") if text.lstrip().startswith("<?xml") else text)
    except ET.ParseError as e:
        raise ConvError(f"XMLを読み取れません: {e}")
    return {root.tag.split("}", 1)[-1]: _xml_obj(root)}


class _TableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.row, self.cell, self.in_cell, self.depth, self.th = [], None, None, False, 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.tables.append([])
        elif self.depth == 1 and tag == "tr":
            self.row = []
            self.th = False
        elif self.depth == 1 and tag in ("td", "th"):
            self.cell, self.in_cell = [], True
            self.th = self.th or tag == "th"
        elif self.in_cell and tag == "br":
            self.cell.append("\n")

    def handle_endtag(self, tag):
        if tag == "table":
            self.depth -= 1
        elif self.depth == 1 and tag in ("td", "th") and self.in_cell:
            self.row.append("".join(self.cell).strip())
            self.in_cell = False
        elif self.depth == 1 and tag == "tr" and self.row is not None:
            if self.tables:
                self.tables[-1].append((self.row, self.th))
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def parse_html(text, header=True):
    p = _TableHTML()
    p.feed(text)
    if not p.tables or not p.tables[0]:
        raise ConvError("HTMLに <table> が見つかりません")
    matrix = [r for r, _ in p.tables[0]]
    return rows_from_matrix(matrix, header)


def _split_md_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    cells, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line) and line[i + 1] == "|":
            cur += "|"
            i += 2
            continue
        if c == "|":
            cells.append(cur.strip())
            cur = ""
        else:
            cur += c
        i += 1
    cells.append(cur.strip())
    return [c.replace("<br>", "\n").replace("<br/>", "\n") for c in cells]


def parse_md(text, header=True):
    lines = [l for l in text.split("\n") if l.strip() and "|" in l]
    if not lines:
        raise ConvError("Markdownの表（| で区切られた行）が見つかりません")
    matrix = []
    for ln in lines:
        cells = _split_md_row(ln)
        if all(re.fullmatch(r":?-{1,}:?", c) for c in cells if c) and any(cells):
            continue
        matrix.append(cells)
    return rows_from_matrix(matrix, header)


def _sql_values(s, i):
    """s[i] が '(' の位置。(値のリスト, 次の位置) を返す"""
    assert s[i] == "("
    i += 1
    vals = []
    while True:
        while i < len(s) and s[i] in " \t\r\n":
            i += 1
        if i >= len(s):
            raise ConvError("SQLの括弧が閉じていません")
        c = s[i]
        if c == "'":
            j, buf = i + 1, []
            while True:
                if j >= len(s):
                    raise ConvError("SQLの文字列が閉じていません")
                if s[j] == "'":
                    if j + 1 < len(s) and s[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                if s[j] == "\\" and j + 1 < len(s) and s[j + 1] in "'\\":
                    buf.append(s[j + 1])
                    j += 2
                    continue
                buf.append(s[j])
                j += 1
            vals.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < len(s) and s[j] not in ",)":
                j += 1
            tok = s[i:j].strip()
            up = tok.upper()
            if up == "NULL":
                v = None
            elif up == "TRUE":
                v = True
            elif up == "FALSE":
                v = False
            else:
                v = infer(tok) if re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?", tok) else tok
            vals.append(v)
            i = j
        while i < len(s) and s[i] in " \t\r\n":
            i += 1
        if i < len(s) and s[i] == ",":
            i += 1
            continue
        if i < len(s) and s[i] == ")":
            return vals, i + 1
        raise ConvError("SQLの値を読み取れません")


def _unq(n):
    n = n.strip()
    if len(n) >= 2 and n[0] in '`"[' and n[-1] in '`"]':
        n = n[1:-1]
    return n


def parse_sql(text):
    rows = []
    pat = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([^\s(]+)\s*(?:\(([^)]*)\))?\s*VALUES\s*", re.I)
    pos = 0
    while True:
        m = pat.search(text, pos)
        if not m:
            break
        cols = [_unq(c) for c in m.group(2).split(",")] if m.group(2) else None
        i = m.end()
        while True:
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
            if i >= len(text) or text[i] != "(":
                break
            vals, i = _sql_values(text, i)
            names = cols or [f"col{k + 1}" for k in range(len(vals))]
            if len(names) != len(vals):
                raise ConvError(f"列数({len(names)})と値の数({len(vals)})が一致しません")
            rows.append(dict(zip(names, vals)))
            while i < len(text) and text[i] in " \t\r\n":
                i += 1
            if i < len(text) and text[i] == ",":
                i += 1
                continue
            break
        pos = i
    if not rows:
        raise ConvError("INSERT INTO ... VALUES (...) が見つかりません")
    return rows


# ---- 最小限の YAML (ブロック形式の辞書・リスト・スカラー・フロー形式・|/> ブロック)
def _strip_comment(s):
    q = None
    for i, c in enumerate(s):
        if q:
            if c == q and s[i - 1] != "\\":
                q = None
        elif c in "'\"" and (i == 0 or s[i - 1] in " \t[{,:"):
            q = c
        elif c == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _yaml_scalar(t):
    t = t.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    if t[0] == '"' and t[-1] == '"' and len(t) >= 2:
        try:
            return json.loads(t)
        except ValueError:
            return t[1:-1]
    if t[0] == "'" and t[-1] == "'" and len(t) >= 2:
        return t[1:-1].replace("''", "'")
    if t[0] in "[{":
        return _yaml_flow(t)
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", t):
        return float(t)
    return t


def _split_top(s):
    parts, cur, depth, q = [], "", 0, None
    for c in s:
        if q:
            cur += c
            if c == q:
                q = None
            continue
        if c in "'\"":
            q = c
            cur += c
        elif c in "[{":
            depth += 1
            cur += c
        elif c in "]}":
            depth -= 1
            cur += c
        elif c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        parts.append(cur)
    return parts


def _yaml_flow(t):
    t = t.strip()
    if t.startswith("["):
        if not t.endswith("]"):
            raise ConvError("YAMLの [ ] が閉じていません")
        return [_yaml_scalar(p) for p in _split_top(t[1:-1])]
    if not t.endswith("}"):
        raise ConvError("YAMLの { } が閉じていません")
    d = {}
    for p in _split_top(t[1:-1]):
        k, sep, v = _kv(p.strip())
        if not sep:
            raise ConvError(f"YAMLのフロー形式を読み取れません: {p}")
        d[k] = _yaml_scalar(v)
    return d


_KEY = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s:#\-?!&*|>'"%@`\[\]{},][^:]*?|-[^\s:][^:]*?)\s*:(?:\s+(.*))?$""")


def _kv(s):
    m = _KEY.match(s)
    if not m:
        return None, "", ""
    k = m.group(1)
    return _yaml_scalar(k) if k[0] in "'\"" else k.strip(), ":", (m.group(2) or "")


def parse_yaml(text):
    raw = []
    started = False
    for ln in text.replace("\t", "  ").split("\n"):
        if ln.strip() in ("---", "...") and not started:
            continue
        if ln.strip() == "---" and started:
            raise ConvError("複数ドキュメントの YAML には対応していません")
        s = _strip_comment(ln)
        if not s.strip():
            raw.append((None, ln))
            continue
        started = True
        raw.append((len(s) - len(s.lstrip()), s.strip()))
    full = raw

    def block_scalar(style, parent_indent, idx):
        """| や > の中身を集める。idx は raw 上の次の行"""
        buf, base = [], None
        while idx < len(full):
            i, s = full[idx]
            if i is None:
                buf.append("")
                idx += 1
                continue
            if i <= parent_indent:
                break
            if base is None:
                base = i
            buf.append(" " * (i - base) + s)
            idx += 1
        while buf and buf[-1] == "":
            buf.pop()
        text_ = "\n".join(buf) if style.startswith("|") else " ".join(x for x in buf)
        return text_ + ("\n" if not style.endswith("-") and buf else ""), idx

    # raw のインデックスで再帰下降
    items = [(k, i, s) for k, (i, s) in enumerate(full) if i is not None]

    def parse(ix, indent):
        """items[ix] から indent の塊を読む。(値, 次のix)"""
        if ix >= len(items):
            return None, ix
        k, i, s = items[ix]
        if s.startswith("- ") or s == "-":
            out = []
            while ix < len(items) and items[ix][1] == indent and (items[ix][2].startswith("- ") or items[ix][2] == "-"):
                k, i, s = items[ix]
                rest = s[1:].strip()
                if not rest:
                    if ix + 1 < len(items) and items[ix + 1][1] > indent:
                        v, ix = parse(ix + 1, items[ix + 1][1])
                    else:
                        v, ix = None, ix + 1
                    out.append(v)
                    continue
                kk, sep, vv = _kv(rest)
                if rest.startswith("- ") or rest == "-":
                    items[ix] = (k, indent + 2, rest)
                    v, ix = parse(ix, indent + 2)
                    out.append(v)
                elif sep and not rest.startswith(("[", "{", '"', "'")) or (sep and rest[0] in "\"'" and _KEY.match(rest)):
                    items[ix] = (k, indent + 2, rest)
                    v, ix = parse(ix, indent + 2)
                    out.append(v)
                else:
                    out.append(_yaml_scalar(rest))
                    ix += 1
            return out, ix
        kk, sep, vv = _kv(s)
        if sep:
            d = {}
            while ix < len(items) and items[ix][1] == indent:
                k, i, s = items[ix]
                kk, sep, vv = _kv(s)
                if not sep:
                    raise ConvError(f"YAMLの{k + 1}行目を読み取れません: {s}")
                vv = vv.strip()
                if vv and vv[0] in "|>":
                    txt, nk = block_scalar(vv, indent, k + 1)
                    d[kk] = txt
                    ix += 1
                    while ix < len(items) and items[ix][0] < nk:
                        ix += 1
                elif vv == "" or vv[0] in "&!":
                    if ix + 1 < len(items) and items[ix + 1][1] > indent:
                        d[kk], ix = parse(ix + 1, items[ix + 1][1])
                    elif ix + 1 < len(items) and items[ix + 1][1] == indent and (items[ix + 1][2].startswith("- ") or items[ix + 1][2] == "-"):
                        d[kk], ix = parse(ix + 1, indent)
                    else:
                        d[kk] = None
                        ix += 1
                else:
                    d[kk] = _yaml_scalar(vv)
                    ix += 1
            if ix < len(items) and items[ix][1] > indent:
                raise ConvError(f"YAMLの{items[ix][0] + 1}行目のインデントが正しくありません")
            return d, ix
        return _yaml_scalar(s), ix + 1

    if not items:
        return None
    v, ix = parse(0, items[0][1])
    if ix < len(items):
        raise ConvError(f"YAMLの{items[ix][0] + 1}行目を読み取れません")
    return v


PARSERS_TABULAR = {"csv", "tsv", "html", "md", "sql"}
FORMAT_NAMES = {"csv": "CSV", "tsv": "TSV", "json": "JSON", "jsonl": "JSON Lines", "xml": "XML", "yaml": "YAML",
                "md": "Markdown表", "html": "HTML表", "sql": "SQL INSERT"}


def detect_format(text):
    t = text.lstrip("\ufeff").strip()
    if not t:
        return "csv"
    if t[0] == "<":
        return "html" if re.search(r"<table\b", t, re.I) else "xml"
    if t[0] in "[{":
        try:
            json.loads(t)
            return "json"
        except ValueError:
            lines = [l for l in t.split("\n") if l.strip()]
            if len(lines) > 1 and all(l.lstrip().startswith("{") for l in lines):
                return "jsonl"
            return "json"
    if re.match(r"INSERT\s", t, re.I):
        return "sql"
    lines = [l for l in t.split("\n") if l.strip()]
    if len(lines) >= 2 and "|" in lines[0] and re.fullmatch(r"\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*", lines[1]):
        return "md"
    if t.startswith("---") or re.match(r"\s*-\s+\S", lines[0]) or re.match(r"^[\w\"'.\-]+\s*:(\s|$)", lines[0]):
        if "," not in lines[0] or re.match(r"^[\w\"'.\-]+\s*:(\s|$)", lines[0]):
            return "yaml"
    if lines and lines[0].count("\t") > 0 and lines[0].count("\t") >= lines[0].count(","):
        return "tsv"
    return "csv"


def parse_any(fmt, text, opts):
    text = text.lstrip("\ufeff")
    header = opts.get("header", True)
    if fmt == "csv":
        return parse_csv(text, opts.get("in_delim") or "auto", header)
    if fmt == "tsv":
        return parse_csv(text, "tab", header)
    if fmt == "json":
        return parse_json(text)
    if fmt == "jsonl":
        return parse_jsonl(text)
    if fmt == "xml":
        return parse_xml(text)
    if fmt == "yaml":
        return parse_yaml(text)
    if fmt == "md":
        return parse_md(text, header)
    if fmt == "html":
        return parse_html(text, header)
    if fmt == "sql":
        return parse_sql(text)
    raise ConvError(f"未対応の入力形式です: {fmt}")


# ====================================================== ライタ
def write_csv(cols, rows, delim=",", crlf=True, quote_all=False):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delim, lineterminator="\r\n" if crlf else "\n", quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL)
    w.writerow(cols)
    for r in rows:
        w.writerow([cell(r.get(c)) for c in cols])
    return buf.getvalue()


def write_md(cols, rows):
    esc = lambda s: cell(s).replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")
    out = ["| " + " | ".join(esc(c) for c in cols) + " |", "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows:
        out.append("| " + " | ".join(esc(r.get(c)) for c in cols) + " |")
    return "\n".join(out) + "\n"


def write_html(cols, rows):
    e = lambda v: html.escape(cell(v)).replace("\n", "<br>")
    out = ["<table>", "  <thead>", "    <tr>" + "".join(f"<th>{e(c)}</th>" for c in cols) + "</tr>", "  </thead>", "  <tbody>"]
    for r in rows:
        out.append("    <tr>" + "".join(f"<td>{e(r.get(c))}</td>" for c in cols) + "</tr>")
    out += ["  </tbody>", "</table>"]
    return "\n".join(out) + "\n"


def _sql_id(n):
    return n if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) else '"' + n.replace('"', '""') + '"'


def _sql_val(v):
    if v is None or v == "":
        return "NULL" if v is None else "''"
    if v is True:
        return "TRUE"
    if v is False:
        return "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + cell(v).replace("'", "''") + "'"


def write_sql(cols, rows, table="my_table"):
    head = f"INSERT INTO {_sql_id(table)} (" + ", ".join(_sql_id(c) for c in cols) + ") VALUES "
    return "".join(head + "(" + ", ".join(_sql_val(r.get(c)) for c in cols) + ");\n" for r in rows)


def jdump(o, indent):
    if indent == "min":
        return json.dumps(o, ensure_ascii=False, separators=(",", ":"), default=str)
    return json.dumps(o, ensure_ascii=False, indent="\t" if indent == "tab" else int(indent or 2), default=str)


_YAML_SPECIAL = re.compile(r"^(?:[-?:,\[\]{}#&*!|>'\"%@`]|\s)|(?:\s|:)$|: | #|^(?:~|null|Null|NULL|true|True|TRUE|false|False|FALSE|yes|no|on|off)$", re.I)


def _y_str(s):
    if s == "" or _YAML_SPECIAL.search(s) or re.fullmatch(r"[-+]?(\d[\d_]*\.?\d*|\.\d+)([eE][-+]?\d+)?|0x[0-9a-fA-F]+", s) or "\n" in s or "\t" in s:
        return json.dumps(s, ensure_ascii=False)
    return s


def _y_scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return _y_str(str(v))


def dump_yaml(o, ind=0):
    pad = "  " * ind
    out = []
    if isinstance(o, dict):
        if not o:
            return [pad + "{}"]
        for k, v in o.items():
            key = _y_str(str(k))
            if isinstance(v, dict) and v:
                out.append(f"{pad}{key}:")
                out += dump_yaml(v, ind + 1)
            elif isinstance(v, list) and v:
                out.append(f"{pad}{key}:")
                out += dump_yaml(v, ind)
            else:
                out.append(f"{pad}{key}: " + ("{}" if v == {} and isinstance(v, dict) else "[]" if isinstance(v, list) else _y_scalar(v)))
    elif isinstance(o, list):
        if not o:
            return [pad + "[]"]
        for v in o:
            if isinstance(v, dict) and v:
                sub = dump_yaml(v, ind + 1)
                out.append(f"{pad}- " + sub[0].lstrip())
                out += sub[1:]
            elif isinstance(v, list) and v:
                sub = dump_yaml(v, ind + 1)
                out.append(f"{pad}- " + sub[0].lstrip())
                out += sub[1:]
            else:
                out.append(f"{pad}- " + ("{}" if isinstance(v, dict) else "[]" if isinstance(v, list) else _y_scalar(v)))
    else:
        out.append(pad + _y_scalar(o))
    return out


def _xname(n):
    n = re.sub(r"[^\w.\-]", "_", str(n), flags=re.U)
    if not n or not (n[0].isalpha() or n[0] == "_"):
        n = "_" + n
    if n.lower().startswith("xml"):
        n = "_" + n
    return n


def _xml_write(name, v, ind, out, step="  "):
    pad = step * ind
    if isinstance(v, list):
        for x in v:
            _xml_write(name, x, ind, out, step)
        return
    tag = _xname(name)
    if isinstance(v, dict):
        attrs = "".join(f" {_xname(k[1:])}={quoteattr(cell(x))}" for k, x in v.items() if k.startswith("@"))
        kids = [(k, x) for k, x in v.items() if not k.startswith("@") and k != "#text"]
        text = v.get("#text")
        if not kids:
            out.append(f"{pad}<{tag}{attrs}>{xesc(cell(text))}</{tag}>" if text is not None else f"{pad}<{tag}{attrs}/>")
            return
        out.append(f"{pad}<{tag}{attrs}>")
        if text is not None:
            out.append(f"{pad}{step}{xesc(cell(text))}")
        for k, x in kids:
            _xml_write(k, x, ind + 1, out, step)
        out.append(f"{pad}</{tag}>")
    elif v is None or v == "":
        out.append(f"{pad}<{tag}/>")
    else:
        out.append(f"{pad}<{tag}>{xesc(cell(v))}</{tag}>")


def write_xml(data, root="root", row="item", decl=True):
    out = ['<?xml version="1.0" encoding="UTF-8"?>'] if decl else []
    if isinstance(data, dict) and len(data) == 1 and not next(iter(data)).startswith(("@", "#")):
        k, v = next(iter(data.items()))
        if isinstance(v, (dict, str, int, float, bool)) or v is None:
            _xml_write(k, v, 0, out)
            return "\n".join(out) + "\n"
    if isinstance(data, list):
        out.append(f"<{_xname(root)}>")
        for r in data:
            _xml_write(row, r, 1, out)
        out.append(f"</{_xname(root)}>")
    else:
        _xml_write(root, data, 0, out)
    return "\n".join(out) + "\n"


# ====================================================== 変換
def convert(src, dst, text, opts=None):
    opts = opts or {}
    warnings = []
    if src in (None, "", "auto"):
        src = detect_format(text)
    if not text.strip():
        return {"text": "", "src": src, "count": 0, "cols": 0, "warnings": []}
    data = parse_any(src, text, opts)
    if src in PARSERS_TABULAR and opts.get("infer", False):
        data = infer_rows(data)
    tabular_out = dst in ("csv", "tsv", "md", "html", "sql")
    nested_src = src in ("json", "jsonl", "xml", "yaml")
    if tabular_out:
        cols, rows = table_of(data, True)
        if nested_src and any(("." in c or "[" in c) for c in cols):
            warnings.append("ネストした項目は「a.b[0]」の形の列に展開しました")
        if dst == "csv":
            out = write_csv(cols, rows, DELIMS.get(opts.get("out_delim") or ",", ","), opts.get("eol", "crlf") != "lf", bool(opts.get("quote_all")))
        elif dst == "tsv":
            out = write_csv(cols, rows, "\t", opts.get("eol", "crlf") != "lf", bool(opts.get("quote_all")))
        elif dst == "md":
            out = write_md(cols, rows)
        elif dst == "html":
            out = write_html(cols, rows)
        else:
            out = write_sql(cols, rows, opts.get("table") or "my_table")
        return {"text": out, "src": src, "count": len(rows), "cols": len(cols), "warnings": warnings}
    # 構造を保つ形式
    if src in PARSERS_TABULAR and opts.get("unflatten", True):
        rows = data
        if any(("." in str(k) or "[" in str(k)) for r in rows for k in r):
            data = [unflatten(r) for r in rows]
    count = len(data) if isinstance(data, list) else 1
    if dst == "json":
        out = jdump(data, opts.get("indent", "2")) + "\n"
    elif dst == "jsonl":
        recs = data if isinstance(data, list) else to_records(data) if isinstance(data, dict) and len(data) == 1 else [data]
        out = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=str) + "\n" for r in recs)
        count = len(recs)
    elif dst == "yaml":
        out = "\n".join(dump_yaml(data)) + "\n"
    elif dst == "xml":
        out = write_xml(data, opts.get("root") or "root", opts.get("row") or "item", opts.get("decl", True))
    else:
        raise ConvError(f"未対応の出力形式です: {dst}")
    return {"text": out, "src": src, "count": count, "cols": 0, "warnings": warnings}


# ====================================================== Protobuf
class PbError(ConvError):
    pass


def read_varint(buf, i):
    v = shift = 0
    while True:
        if i >= len(buf):
            raise PbError("varint の途中でデータが終わっています")
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7
        if shift > 70:
            raise PbError("varint が長すぎます")


def parse_wire(buf, depth=0):
    """[(field_no, wire_type, value)] value: int(varint/fixed) / bytes(LEN)。group は未対応"""
    i, out = 0, []
    while i < len(buf):
        key, i = read_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if fn == 0 or fn >= 1 << 29:
            raise PbError(f"不正なフィールド番号です: {fn}")
        if wt == 0:
            v, i = read_varint(buf, i)
        elif wt == 1:
            if i + 8 > len(buf):
                raise PbError("fixed64 の途中でデータが終わっています")
            v, i = bytes(buf[i:i + 8]), i + 8
        elif wt == 2:
            n, i = read_varint(buf, i)
            if i + n > len(buf):
                raise PbError("長さ指定がデータの終わりを超えています")
            v, i = bytes(buf[i:i + n]), i + n
        elif wt == 5:
            if i + 4 > len(buf):
                raise PbError("fixed32 の途中でデータが終わっています")
            v, i = bytes(buf[i:i + 4]), i + 4
        else:
            raise PbError(f"未対応のワイヤタイプです: {wt}")
        out.append((fn, wt, v))
    return out


def _is_text(b):
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s or any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in s):
        return None
    return s


def _try_msg(b):
    if not b:
        return None
    try:
        f = parse_wire(b)
    except PbError:
        return None
    return f or None


# ---- .proto の最小パーサ
SCALARS = {"int32", "int64", "uint32", "uint64", "sint32", "sint64", "bool", "fixed32", "sfixed32", "float", "fixed64", "sfixed64", "double", "string", "bytes"}


class ProtoSchema:
    def __init__(self):
        self.messages = {}   # フルネーム -> {num: (name, type, repeated)}
        self.enums = {}      # フルネーム -> {value: name}
        self.order = []


def _tokenize_proto(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return re.findall(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[A-Za-z_][\w.]*|-?\d+|[{}\[\]();=<>,]', text)


def parse_proto(text):
    try:
        return _parse_proto(text)
    except (ValueError, IndexError):
        raise PbError(".proto を読み取れません（番号や構文を確認してください）")


def _parse_proto(text):
    toks = _tokenize_proto(text)
    sch = ProtoSchema()
    pkg = [""]
    i = [0]

    def peek():
        return toks[i[0]] if i[0] < len(toks) else None

    def nxt():
        t = peek()
        if t is None:
            raise PbError(".proto が途中で終わっています")
        i[0] += 1
        return t

    def expect(x):
        t = nxt()
        if t != x:
            raise PbError(f".proto の構文エラー: '{x}' が必要な位置に '{t}' があります")

    def skip_stmt():
        depth = 0
        while True:
            t = nxt()
            if t in "{[":
                depth += 1
            elif t in "}]":
                depth -= 1
            elif t == ";" and depth <= 0:
                return

    def parse_enum(prefix):
        name = nxt()
        full = prefix + name
        expect("{")
        vals = {}
        while peek() != "}":
            t = nxt()
            if t in ("option", "reserved"):
                skip_stmt()
                continue
            expect("=")
            vals[int(nxt())] = t
            if peek() == "[":
                while nxt() != "]":
                    pass
            expect(";")
        expect("}")
        sch.enums[full] = vals
        sch.enums.setdefault("." + name, vals)

    def parse_msg(prefix):
        name = nxt()
        full = prefix + name
        fields = {}
        sch.messages[full] = fields
        sch.order.append(full)
        expect("{")
        parse_body(full + ".", fields)
        expect("}")

    def parse_body(prefix, fields, oneof=False):
        while peek() not in ("}", None):
            t = nxt()
            if t == ";":
                continue
            if t == "message":
                parse_msg(prefix)
            elif t == "enum":
                parse_enum(prefix)
            elif t in ("option", "reserved", "extensions", "extend"):
                skip_stmt() if t != "extend" else (nxt(), skip_block())
            elif t == "oneof":
                nxt()
                expect("{")
                parse_body(prefix, fields, True)
                expect("}")
            else:
                rep = False
                if t in ("repeated", "optional", "required"):
                    rep = t == "repeated"
                    t = nxt()
                if t == "map":
                    expect("<")
                    k = nxt()
                    expect(",")
                    v = nxt()
                    expect(">")
                    fname = nxt()
                    expect("=")
                    num = int(nxt())
                    if peek() == "[":
                        while nxt() != "]":
                            pass
                    expect(";")
                    fields[num] = (fname, ("map", k, v), True)
                    continue
                if t == "group":
                    raise PbError("group には対応していません")
                fname = nxt()
                expect("=")
                num = int(nxt())
                if peek() == "[":
                    while nxt() != "]":
                        pass
                expect(";")
                fields[num] = (fname, t, rep)

    def skip_block():
        while nxt() != "{":
            pass
        depth = 1
        while depth:
            t = nxt()
            depth += (t == "{") - (t == "}")

    while peek() is not None:
        t = nxt()
        if t == "package":
            pkg[0] = nxt() + "."
            expect(";")
        elif t == "message":
            parse_msg("")
        elif t == "enum":
            parse_enum("")
        elif t in ("syntax", "import", "option"):
            skip_stmt()
        elif t == ";":
            continue
        elif t in ("service", "extend"):
            skip_block()
        else:
            raise PbError(f".proto を読み取れません: '{t}'")
    if not sch.messages:
        raise PbError(".proto に message が見つかりません")
    return sch


def _resolve(sch, scope, typ):
    """型名を message / enum のフルネームに解決"""
    parts = scope.split(".") if scope else []
    for n in range(len(parts), -1, -1):
        cand = ".".join(parts[:n] + [typ])
        if cand in sch.messages:
            return "msg", cand
        if cand in sch.enums:
            return "enum", cand
    if typ in sch.messages:
        return "msg", typ
    if "." + typ in sch.enums:
        return "enum", "." + typ
    return None, typ


# ---- 値の変換
def _signed(v, bits):
    return v - (1 << bits) if v >= 1 << (bits - 1) else v


def _varint_val(v, mode):
    if mode == "signed":
        return _signed(v & ((1 << 64) - 1), 64)
    if mode == "zigzag":
        return (v >> 1) ^ -(v & 1)
    return v


def _fixed_val(b, opt):
    if opt == "float":
        return struct.unpack("<f", b)[0] if len(b) == 4 else struct.unpack("<d", b)[0]
    return int.from_bytes(b, "little")


def _typed(sch, typ, wt, v, o):
    """スキーマの型に従って値を変換"""
    t = typ
    if t in ("int32", "int64"):
        return _signed(v & ((1 << 64) - 1), 64) if t == "int64" else _signed(v & 0xFFFFFFFF, 32)
    if t in ("uint32", "uint64"):
        return v
    if t in ("sint32", "sint64"):
        return (v >> 1) ^ -(v & 1)
    if t == "bool":
        return bool(v)
    if t == "fixed32":
        return int.from_bytes(v, "little")
    if t == "sfixed32":
        return _signed(int.from_bytes(v, "little"), 32)
    if t == "fixed64":
        return int.from_bytes(v, "little")
    if t == "sfixed64":
        return _signed(int.from_bytes(v, "little"), 64)
    if t == "float":
        return struct.unpack("<f", v)[0]
    if t == "double":
        return struct.unpack("<d", v)[0]
    if t == "string":
        return v.decode("utf-8", "replace")
    if t == "bytes":
        return v.hex()
    return None


_PACKABLE = {"int32": 0, "int64": 0, "uint32": 0, "uint64": 0, "sint32": 0, "sint64": 0, "bool": 0, "enum": 0,
             "fixed32": 5, "sfixed32": 5, "float": 5, "fixed64": 1, "sfixed64": 1, "double": 1}


def decode_message(buf, o, sch=None, mname=None, depth=0):
    """-> OrderedDict風 dict。同じ番号が複数あればリスト化。キーは 項目名 or 番号(文字列)"""
    if depth > 30:
        raise PbError("入れ子が深すぎます")
    fields = sch.messages.get(mname) if (sch and mname) else None
    out, repeated_keys = {}, set()

    def put(key, val, force_list=False):
        if key in out:
            if key not in repeated_keys:
                out[key] = [out[key]]
                repeated_keys.add(key)
            if isinstance(val, list) and force_list:
                out[key].extend(val)
            else:
                out[key].append(val)
        elif force_list:
            out[key] = val if isinstance(val, list) else [val]
            repeated_keys.add(key)
        else:
            out[key] = val

    for fn, wt, v in parse_wire(buf):
        spec = fields.get(fn) if fields else None
        if spec:
            name, typ, rep = spec
            if isinstance(typ, tuple):  # map
                ent = _decode_map_entry(v, typ, sch, mname, o, depth)
                put(name, ent, True)
                continue
            kind, full = (None, typ)
            if typ not in SCALARS:
                kind, full = _resolve(sch, mname, typ)
            if kind == "msg":
                if wt != 2:
                    put(name, _raw(wt, v, o), rep)
                    continue
                put(name, decode_message(v, o, sch, full, depth + 1), rep)
            elif kind == "enum":
                if wt == 2:  # packed
                    vals = _packed(v, 0)
                    put(name, [sch.enums[full].get(x, x) for x in vals], True)
                else:
                    put(name, sch.enums[full].get(v, v), rep)
            elif typ in SCALARS:
                if wt == 2 and typ in _PACKABLE:
                    for x in _unpack(v, _PACKABLE[typ]):
                        put(name, _typed(sch, typ, _PACKABLE[typ], x, o), True)
                else:
                    try:
                        put(name, _typed(sch, typ, wt, v, o), rep)
                    except (struct.error, ValueError, TypeError):
                        put(name, _raw(wt, v, o), rep)
            else:
                put(name, _raw(wt, v, o), rep)
            continue
        key = str(fn)
        if wt == 2:
            put(key, _guess_len(v, o, sch, depth))
        else:
            put(key, _raw(wt, v, o))
    return out


def _packed(b, wt):
    i, out = 0, []
    while i < len(b):
        v, i = read_varint(b, i)
        out.append(v)
    return out


def _unpack(b, wt):
    if wt == 0:
        return _packed(b, 0)
    n = 4 if wt == 5 else 8
    if len(b) % n:
        raise PbError("packed のサイズが不正です")
    return [b[i:i + n] for i in range(0, len(b), n)]


def _decode_map_entry(b, typ, sch, scope, o, depth):
    _, kt, vt = typ
    d = {}
    for fn, wt, v in parse_wire(b):
        t = kt if fn == 1 else vt
        if t in SCALARS:
            d["key" if fn == 1 else "value"] = _typed(sch, t, wt, v, o)
        else:
            kind, full = _resolve(sch, scope, t)
            if kind == "msg":
                d["value"] = decode_message(v, o, sch, full, depth + 1)
            elif kind == "enum":
                d["value"] = sch.enums[full].get(v, v)
            else:
                d["key" if fn == 1 else "value"] = _raw(wt, v, o)
    return d


def _raw(wt, v, o):
    if wt == 0:
        return _varint_val(v, o.get("varint", "unsigned"))
    if wt in (1, 5):
        return _fixed_val(v, o.get("fixed", "int"))
    return v.hex()


def _guess_len(b, o, sch, depth):
    mode = o.get("len", "auto")
    if mode == "bytes":
        return b.hex()
    if mode == "string":
        s = b.decode("utf-8", "replace")
        return s
    txt = _is_text(b)
    msg = _try_msg(b) if depth < 20 else None
    if mode == "message" and msg:
        return decode_message(b, o, sch, None, depth + 1)
    if mode == "auto":
        if msg and not (txt is not None and b[:1] not in (b"\n", b"\t", b"\r")):
            return decode_message(b, o, sch, None, depth + 1)
        if txt is not None:
            return txt
        if not b:
            return ""
        return b.hex()
    if txt is not None:
        return txt
    return b.hex() if not msg else decode_message(b, o, sch, None, depth + 1)


# ---- 入力の取り込み
def pb_bytes(text, enc="auto"):
    t = text.strip()
    if not t:
        raise PbError("データが空です")
    if enc == "auto":
        h = re.sub(r"0x|[\s,:\-]", "", t)
        enc = "hex" if re.fullmatch(r"[0-9a-fA-F]+", h) and len(h) % 2 == 0 else "base64"
    if enc == "hex":
        h = re.sub(r"0x|[\s,:\-]", "", t)
        try:
            return binascii.unhexlify(h)
        except (binascii.Error, ValueError):
            raise PbError("16進数として読み取れません（奇数桁か、使えない文字があります）")
    try:
        b = t.replace("-", "+").replace("_", "/")
        b = re.sub(r"\s+", "", b)
        b += "=" * (-len(b) % 4)
        return base64.b64decode(b, validate=True)
    except (binascii.Error, ValueError):
        raise PbError("Base64として読み取れません")


def split_frames(buf, framing):
    if framing == "none":
        return [buf]
    out, i = [], 0
    while i < len(buf):
        if framing == "varint":
            n, i = read_varint(buf, i)
        else:  # grpc: 1byte flag + 4byte BE length
            if i + 5 > len(buf):
                raise PbError("gRPCフレームのヘッダが途中で終わっています")
            if buf[i] not in (0, 1):
                raise PbError("gRPCフレームの形式ではありません")
            if buf[i] == 1:
                raise PbError("圧縮されたgRPCフレームには対応していません")
            n = int.from_bytes(buf[i + 1:i + 5], "big")
            i += 5
        if i + n > len(buf):
            raise PbError("フレームの長さがデータを超えています")
        out.append(buf[i:i + n])
        i += n
    return out


# ---- CSV 化
def _walk_leaves(obj, path, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _walk_leaves(v, path + [k], out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk_leaves(v, path + [i], out)
    else:
        out.append((path, obj))


def _path_str(path):
    s = ""
    for p in path:
        s += f"[{p}]" if isinstance(p, int) else (("." if s else "") + str(p))
    return s


def _get_path(obj, name):
    for part in name.split("."):
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            return None
    return obj


def messages_to_csv(msgs, o, sch=None, root=None):
    """msgs: decode 済みの dict のリスト -> (列, 行)"""
    mode = o.get("layout", "wide")
    if mode == "long":
        cols = ["message", "path", "value", "type"]
        rows = []
        for mi, m in enumerate(msgs, 1):
            leaves = []
            _walk_leaves(m, [], leaves)
            for path, v in leaves:
                rows.append({"message": mi, "path": _path_str(path), "value": v,
                             "type": "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float" if isinstance(v, float) else "string"})
        return cols, rows
    explode = (o.get("explode") or "").strip()
    flat_rows = []
    for m in msgs:
        if explode:
            items = _get_path(m, explode)
            if items is None:
                flat_rows.append(flatten(m))
                continue
            items = items if isinstance(items, list) else [items]
            base = _drop_path(m, explode)
            bflat = flatten(base) if base else {}
            if not items:
                flat_rows.append(bflat)
            for it in items:
                r = dict(bflat)
                r.update(flatten(it, explode) if isinstance(it, (dict, list)) else {explode: it})
                flat_rows.append(r)
        else:
            flat_rows.append(flatten(m))
    cols, seen = [], set()
    for r in flat_rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    return cols, flat_rows


def _drop_path(obj, name):
    import copy
    o = copy.deepcopy(obj)
    parts = name.split(".")
    cur = o
    for p in parts[:-1]:
        cur = cur.get(p) if isinstance(cur, dict) else None
        if cur is None:
            return o
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)
    return o


def protobuf_to_csv(text, o=None):
    o = o or {}
    buf = pb_bytes(text, o.get("input", "auto")) if not o.get("raw") else o["raw"]
    frames = split_frames(buf, o.get("framing", "none"))
    sch = None
    root = None
    warnings = []
    if (o.get("proto") or "").strip():
        sch = parse_proto(o["proto"])
        root = o.get("message") or None
        if root and root not in sch.messages:
            cands = [m for m in sch.messages if m.split(".")[-1] == root]
            if cands:
                root = cands[0]
            else:
                raise PbError(f"メッセージ「{root}」が .proto にありません")
        if not root:
            root = sch.order[0]
            warnings.append(f"メッセージ名の指定がないため {root} を使いました")
    msgs = []
    for i, f in enumerate(frames):
        try:
            msgs.append(decode_message(f, o, sch, root))
        except PbError as e:
            raise PbError(f"{i + 1}件目の解析に失敗しました: {e}" if len(frames) > 1 else f"解析に失敗しました: {e}")
    cols, rows = messages_to_csv(msgs, o, sch, root)
    csv_text = write_csv(cols, rows, DELIMS.get(o.get("out_delim") or ",", ","), o.get("eol", "crlf") != "lf", bool(o.get("quote_all")))
    return {"csv": csv_text, "tree": jdump(msgs if len(msgs) != 1 else msgs[0], "2"), "count": len(rows), "cols": len(cols),
            "messages": len(msgs), "bytes": len(buf), "warnings": warnings,
            "names": list(sch.messages) if sch else []}


def proto_messages(text):
    """UI用: .proto 内のメッセージ名一覧"""
    return list(parse_proto(text).messages)


# ---- テスト用エンコーダ（サンプル生成にも使用）
def _venc(v):
    v &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_enc(fields):
    """[(番号, 'v'|'s'|'m'|'f32'|'f64'|'d', 値)] -> bytes。's'=文字列 'm'=入れ子(bytes) 'd'=double"""
    out = bytearray()
    for fn, kind, v in fields:
        if kind == "v":
            out += _venc(fn << 3) + _venc(v)
        elif kind in ("s", "m", "b"):
            data = v.encode("utf-8") if isinstance(v, str) else v
            out += _venc(fn << 3 | 2) + _venc(len(data)) + data
        elif kind == "f32":
            out += _venc(fn << 3 | 5) + struct.pack("<f", v)
        elif kind == "i32":
            out += _venc(fn << 3 | 5) + struct.pack("<I", v)
        elif kind == "d":
            out += _venc(fn << 3 | 1) + struct.pack("<d", v)
    return bytes(out)


SAMPLE_PROTO = """syntax = "proto3";
package shop;

enum Status { UNKNOWN = 0; ACTIVE = 1; CLOSED = 2; }

message Item {
  int32 id = 1;
  string name = 2;
  double price = 3;
  repeated string tags = 4;
}

message Order {
  int64 order_id = 1;
  string customer = 2;
  Status status = 3;
  repeated Item items = 4;
  repeated int32 scores = 5;
}
"""


def sample_hex():
    def item(i, name, price, tags):
        return pb_enc([(1, "v", i), (2, "s", name), (3, "d", price)] + [(4, "s", t) for t in tags])
    order = pb_enc([(1, "v", 10001), (2, "s", "山田商事"), (3, "v", 1),
                    (4, "m", item(1, "ボールペン", 120.5, ["文具", "黒"])),
                    (4, "m", item(2, "ノート", 300.0, ["文具"])),
                    (4, "m", item(3, "付箋", 180.0, [])),
                    (5, "m", b"".join(_venc(x) for x in (90, 75, 100)))])
    return order.hex(" ")


PAGE = r"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>変換ツール</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--ink:#222831;--sub:#6b7480;--line:#d9dee5;--accent:#1f6feb;--hi:#e8f0fe;--err:#c0392b;--ok:#1a7f4b;--code:#f3f5f8}
@media (prefers-color-scheme:dark){:root{--bg:#171a1f;--card:#20242b;--ink:#e6e9ee;--sub:#9aa4b2;--line:#363c46;--accent:#6aa2ff;--hi:#26334d;--err:#ff8a7a;--ok:#5fd39a;--code:#2a2f38}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic UI",Meiryo,sans-serif;background:var(--bg);color:var(--ink);font-size:14px}
header{display:flex;align-items:center;gap:14px;padding:10px 18px;background:var(--card);border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:18px;margin:0}
.tabs{display:flex;gap:4px}.tabs button{border:none;background:none;padding:8px 14px;border-radius:8px 8px 0 0;cursor:pointer;color:var(--sub);font-size:14px;border-bottom:3px solid transparent}
.tabs button.on{color:var(--accent);border-bottom-color:var(--accent);font-weight:700}
main{padding:14px 18px;max-width:1700px;margin:0 auto}
input,select,button,textarea{font:inherit;color:var(--ink)}
button{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:5px 11px;cursor:pointer}button:hover{border-color:var(--accent)}button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
select,input[type=text]{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 6px;min-width:0}
.bar{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin-bottom:12px}
.bar label{display:flex;align-items:center;gap:5px;color:var(--sub);font-size:13px;white-space:nowrap}.bar .grp{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.bar .tt{font-weight:700;color:var(--ink);font-size:13px}.sep{width:1px;height:22px;background:var(--line)}
.cols{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
@media (max-width:900px){.cols{grid-template-columns:minmax(0,1fr)}}
.pane{background:var(--card);border:1px solid var(--line);border-radius:10px;display:flex;flex-direction:column;min-width:0}
.pane .hd{display:flex;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--line);flex-wrap:wrap}.pane .hd b{margin-right:auto}
textarea.io{width:100%;height:56vh;min-height:260px;border:none;border-radius:0 0 10px 10px;padding:10px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace;font-size:13px;line-height:1.5;background:var(--card);white-space:pre;overflow:auto;tab-size:4}
textarea.io:focus{outline:2px solid var(--accent);outline-offset:-2px}
.st{padding:5px 10px;font-size:12px;color:var(--sub);border-top:1px solid var(--line);min-height:26px}.st.err{color:var(--err)}.st.ok{color:var(--ok)}
.drop{outline:3px dashed var(--accent)!important;outline-offset:-4px}
.mid{display:flex;justify-content:center;margin:-2px 0 8px}
.small{font-size:12px;color:var(--sub)}
.tbl{overflow:auto;max-height:56vh;min-height:260px}.tbl table{border-collapse:collapse;font-size:12.5px}.tbl th,.tbl td{border:1px solid var(--line);padding:3px 8px;white-space:nowrap;text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}.tbl th{background:var(--code);position:sticky;top:0}
.sub{display:flex;gap:2px}.sub button{border-radius:6px 6px 0 0;border-bottom:none;padding:3px 10px;font-size:13px}.sub button.on{background:var(--hi);border-color:var(--accent);color:var(--accent)}
#proto{width:100%;height:210px;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;border:1px solid var(--line);border-radius:6px;padding:6px;background:var(--card);resize:vertical;white-space:pre}
textarea#pin{height:150px;min-height:100px;border-bottom:1px solid var(--line);border-radius:0}
.note{background:var(--hi);border-radius:8px;padding:8px 10px;font-size:12.5px;line-height:1.6;margin-bottom:10px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#222;color:#fff;padding:8px 14px;border-radius:8px;opacity:0;pointer-events:none;transition:opacity .2s;z-index:9;max-width:90vw}#toast.on{opacity:.95}
</style></head><body>
<header><h1>🔄 変換ツール</h1>
<div class="tabs"><button id="tb1" class="on">形式変換</button><button id="tb2">Protobuf → CSV</button></div></header>
<main>
<section id="t1">
  <div class="bar">
    <div class="grp"><span class="tt">入力</span>
      <select id="src" title="入力の形式">
        <option value="auto">自動判定</option><option value="csv">CSV</option><option value="tsv">TSV</option><option value="json">JSON</option><option value="jsonl">JSON Lines</option><option value="xml">XML</option><option value="yaml">YAML</option><option value="md">Markdown表</option><option value="html">HTML表</option><option value="sql">SQL INSERT</option></select>
      <label id="lDelim">区切り <select id="inDelim"><option value="auto">自動</option><option value=",">, カンマ</option><option value="tab">タブ</option><option value=";">; セミコロン</option><option value="|">| パイプ</option></select></label>
      <label id="lHeader"><input type="checkbox" id="header" checked>1行目は見出し</label>
      <label id="lInfer" title="数字→数値、true/false→真偽値、空欄→null に変換します。'007' のような先頭0つきは文字列のままです"><input type="checkbox" id="infer" checked>型を推測</label>
    </div>
    <div class="sep"></div>
    <div class="grp"><span class="tt">出力</span>
      <select id="dst"><option value="json">JSON</option><option value="csv">CSV</option><option value="tsv">TSV</option><option value="jsonl">JSON Lines</option><option value="xml">XML</option><option value="yaml">YAML</option><option value="md">Markdown表</option><option value="html">HTML表</option><option value="sql">SQL INSERT</option></select>
      <label id="lOd">区切り <select id="outDelim"><option value=",">, カンマ</option><option value="tab">タブ</option><option value=";">; セミコロン</option><option value="|">| パイプ</option></select></label>
      <label id="lEol">改行 <select id="eol"><option value="crlf">CRLF (Windows)</option><option value="lf">LF</option></select></label>
      <label id="lQa"><input type="checkbox" id="qa">すべて引用符で囲む</label>
      <label id="lInd">字下げ <select id="indent"><option value="2">スペース2</option><option value="4">スペース4</option><option value="tab">タブ</option><option value="min">圧縮</option></select></label>
      <label id="lRoot">ルート <input type="text" id="root" value="root" size="8"></label>
      <label id="lRow">行 <input type="text" id="row" value="item" size="8"></label>
      <label id="lDecl"><input type="checkbox" id="decl" checked>XML宣言</label>
      <label id="lTable">テーブル名 <input type="text" id="table" value="my_table" size="12"></label>
      <label id="lUnf" title="見出しが a.b や items[0] の形なら、入れ子の構造に戻します"><input type="checkbox" id="unf" checked>a.b 形式の見出しを入れ子に戻す</label>
    </div>
  </div>
  <div class="cols">
    <div class="pane"><div class="hd"><b>入力</b><span class="small" id="detected"></span>
      <button id="open1">ファイルを開く</button><button id="paste1">貼り付け</button><button id="sample1">サンプル</button><button id="clear1">クリア</button><input type="file" id="file1" hidden></div>
      <textarea class="io" id="in1" spellcheck="false" placeholder="ここにデータを貼り付ける、またはファイルをドラッグ&ドロップ（CSV / TSV / JSON / JSON Lines / XML / YAML / Markdown表 / HTML表 / SQL INSERT）"></textarea>
      <div class="st" id="st1"></div></div>
    <div class="pane"><div class="hd"><b>出力</b>
      <button id="swap" title="出力を入力に移して、形式も入れ替えます">⇄ 入れ替え</button><button id="copy1">コピー</button>
      <select id="enc1" title="保存時の文字コード"><option value="utf-8-sig">UTF-8 (BOM付き・Excel向け)</option><option value="utf-8">UTF-8</option><option value="cp932">Shift_JIS (CP932)</option><option value="utf-16">UTF-16</option><option value="euc-jp">EUC-JP</option></select>
      <button id="dl1" class="primary">ダウンロード</button></div>
      <textarea class="io" id="out1" spellcheck="false" readonly></textarea>
      <div class="st" id="st1o"></div></div>
  </div>
</section>

<section id="t2" hidden>
  <div class="note">Protobuf のバイナリを <b>.proto なしでも</b>解析して CSV にします（項目名は番号になります）。<b>.proto を貼る</b>と項目名・型・enum つきで正確に読めます。
    .proto なしでは、「文字列かメッセージか」「整数か小数か」は推測です（packed の整数配列は文字列に見えることがあります）。うまく読めない時は下のオプションか .proto で調整してください。</div>
  <div class="cols">
    <div>
      <div class="pane" style="margin-bottom:12px"><div class="hd"><b>バイナリ（16進 または Base64）</b>
        <button id="open2">ファイルを開く</button><button id="sample2">サンプル</button><button id="clear2">クリア</button><input type="file" id="file2" hidden></div>
        <textarea class="io" id="pin" spellcheck="false" placeholder="例: 08 91 4e 12 0c ...  /  CJFOEgzl..."></textarea>
        <div class="st" id="st2"></div></div>
      <div class="bar">
        <label>入力形式 <select id="pEnc"><option value="auto">自動判定</option><option value="hex">16進(hex)</option><option value="base64">Base64</option></select></label>
        <label>まとまり <select id="pFrame"><option value="none">1メッセージ</option><option value="varint">長さ付きの連続（writeDelimited）</option><option value="grpc">gRPCフレーム</option></select></label>
        <label>CSVの形 <select id="pLayout"><option value="wide">横持ち（1メッセージ=1行）</option><option value="long">縦持ち（値ごとに1行）</option></select></label>
        <label id="lExp" title="繰り返しフィールドの名前（または番号）。要素ごとに1行になり、親の項目は各行に繰り返されます">行に展開する項目 <input type="text" id="pExp" size="10" placeholder="例: items / 4"></label>
        <label>整数(varint) <select id="pVar"><option value="unsigned">符号なし</option><option value="signed">符号付き</option><option value="zigzag">ZigZag(sint)</option></select></label>
        <label>長さ付き(LEN) <select id="pLen"><option value="auto">自動</option><option value="string">文字列</option><option value="message">メッセージ</option><option value="bytes">バイト列(hex)</option></select></label>
        <label>fixed32/64 <select id="pFix"><option value="int">整数</option><option value="float">小数(float/double)</option></select></label>
      </div>
      <div class="pane"><div class="hd"><b>.proto（任意）</b><label class="small">メッセージ <select id="pMsg"><option value="">（先頭）</option></select></label><button id="sampleProto">サンプル</button></div>
        <div style="padding:8px"><textarea id="proto" spellcheck="false" placeholder='syntax = "proto3";&#10;message Order { int64 order_id = 1; string customer = 2; repeated Item items = 4; }'></textarea></div>
        <div class="st" id="stp"></div></div>
    </div>
    <div class="pane"><div class="hd"><div class="sub"><button id="rv1" class="on">CSV</button><button id="rv2">表</button><button id="rv3">ツリー(JSON)</button></div><span class="small" style="margin-right:auto" id="pinfo"></span>
      <button id="copy2">コピー</button>
      <select id="enc2" title="保存時の文字コード"><option value="utf-8-sig">UTF-8 (BOM付き・Excel向け)</option><option value="utf-8">UTF-8</option><option value="cp932">Shift_JIS (CP932)</option></select>
      <button id="dl2" class="primary">CSVをダウンロード</button></div>
      <textarea class="io" id="out2" spellcheck="false" readonly></textarea>
      <div class="tbl" id="tbl2" hidden></div>
      <div class="st" id="st2o"></div></div>
  </div>
</section>
</main>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
function h(tag, attrs, ...kids){ const e = document.createElement(tag); for(const k in (attrs || {})) e.setAttribute(k, attrs[k]); for(const c of kids.flat()) e.append(c instanceof Node ? c : document.createTextNode(String(c))); return e; }
let tt; function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('on'); clearTimeout(tt); tt = setTimeout(() => t.classList.remove('on'), 2800); }
async function post(path, body){ const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}); if(!r.ok) throw new Error('通信エラー ' + r.status); return r; }
const pj = async (p, b) => (await post(p, b)).json();
function debounce(f, ms){ let t; return (...a) => { clearTimeout(t); t = setTimeout(() => f(...a), ms); }; }
const LS = { get(k, d){ try{ const v = localStorage.getItem('cv.' + k); return v == null ? d : JSON.parse(v); }catch(e){ return d; } }, set(k, v){ try{ localStorage.setItem('cv.' + k, JSON.stringify(v)); }catch(e){} } };
const b64 = buf => { let s = ''; const u = new Uint8Array(buf); for(let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000)); return btoa(s); };
async function copyText(t){ try{ await navigator.clipboard.writeText(t); toast('コピーしました'); }catch(e){ toast('コピーできませんでした（出力欄を選択して Ctrl+C）'); } }
async function download(text, name, enc){ const r = await post('/api/download', {text, filename:name, encoding:enc}); const blob = await r.blob(), a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 3000); if(r.headers.get('X-Replaced') === '1') toast('この文字コードで表せない文字は「?」に置き換えました'); }
function setSt(el, msg, cls){ el.textContent = msg || ''; el.className = 'st' + (cls ? ' ' + cls : ''); }

// ---------- タブ
function showTab(n){ $('#t1').hidden = n !== 1; $('#t2').hidden = n !== 2; $('#tb1').classList.toggle('on', n === 1); $('#tb2').classList.toggle('on', n === 2); LS.set('tab', n); }
$('#tb1').onclick = () => showTab(1); $('#tb2').onclick = () => showTab(2);

// ---------- 形式変換
const TABULAR = ['csv', 'tsv', 'md', 'html', 'sql'], NESTED = ['json', 'jsonl', 'xml', 'yaml'];
const in1 = $('#in1'), out1 = $('#out1'); let tok = 0, lastExt = 'json', last1 = '';
const EXT = {csv:'csv', tsv:'tsv', json:'json', jsonl:'jsonl', xml:'xml', yaml:'yaml', md:'md', html:'html', sql:'sql'};
function opts1(){ const d = $('#dst').value, s = $('#src').value;
  return {header:$('#header').checked, infer:$('#infer').checked, in_delim:$('#inDelim').value === 'tab' ? 'tab' : $('#inDelim').value,
    out_delim:$('#outDelim').value === 'tab' ? 'tab' : $('#outDelim').value, eol:$('#eol').value, quote_all:$('#qa').checked, indent:$('#indent').value,
    root:$('#root').value.trim(), row:$('#row').value.trim(), decl:$('#decl').checked, table:$('#table').value.trim(), unflatten:$('#unf').checked}; }
function syncOptVis(){ const d = $('#dst').value, s = $('#src').value, sTab = s === 'auto' || ['csv','tsv','md','html'].includes(s);
  $('#lDelim').hidden = !(s === 'auto' || s === 'csv'); $('#lHeader').hidden = !sTab; $('#lInfer').hidden = !(s === 'auto' || ['csv','tsv','md','html'].includes(s));
  $('#lOd').hidden = d !== 'csv'; $('#lEol').hidden = !(d === 'csv' || d === 'tsv'); $('#lQa').hidden = !(d === 'csv' || d === 'tsv'); $('#lInd').hidden = d !== 'json';
  $('#lRoot').hidden = d !== 'xml'; $('#lRow').hidden = d !== 'xml'; $('#lDecl').hidden = d !== 'xml'; $('#lTable').hidden = d !== 'sql'; $('#lUnf').hidden = !NESTED.includes(d); }
const run1 = debounce(async () => {
  syncOptVis(); const my = ++tok, text = in1.value;
  if(!text.trim()){ out1.value = ''; last1 = ''; setSt($('#st1'), ''); setSt($('#st1o'), ''); $('#detected').textContent = ''; return; }
  let r; try{ r = await pj('/api/convert', {src:$('#src').value, dst:$('#dst').value, text, opts:opts1()}); }catch(e){ if(my === tok) setSt($('#st1o'), e.message, 'err'); return; }
  if(my !== tok) return; $('#detected').textContent = $('#src').value === 'auto' && r.src ? '判定: ' + ({csv:'CSV',tsv:'TSV',json:'JSON',jsonl:'JSON Lines',xml:'XML',yaml:'YAML',md:'Markdown表',html:'HTML表',sql:'SQL INSERT'}[r.src] || r.src) : '';
  if(!r.ok){ setSt($('#st1o'), '⚠ ' + r.error, 'err'); return; }
  out1.value = r.text; last1 = r.text; lastExt = EXT[$('#dst').value];
  setSt($('#st1o'), `${r.count} 件` + (r.cols ? ` / ${r.cols} 列` : '') + ` / ${r.text.length.toLocaleString()} 文字` + (r.warnings.length ? '　' + r.warnings.join('、') : ''), 'ok');
}, 220);
['src','dst','inDelim','outDelim','eol','qa','indent','decl','unf','header','infer'].forEach(i => $('#' + i).addEventListener('change', () => { LS.set('o_' + i, $('#' + i).type === 'checkbox' ? $('#' + i).checked : $('#' + i).value); run1(); }));
['root','row','table'].forEach(i => $('#' + i).addEventListener('input', () => { LS.set('o_' + i, $('#' + i).value); run1(); }));
for(const i of ['src','dst','inDelim','outDelim','eol','qa','indent','decl','unf','header','infer','root','row','table']){ const v = LS.get('o_' + i, null), el = $('#' + i); if(v != null){ if(el.type === 'checkbox') el.checked = !!v; else if([...(el.options || [])].some(o => o.value === v) || el.type === 'text') el.value = v; } }
in1.addEventListener('input', run1);
$('#open1').onclick = () => $('#file1').click();
async function loadFile(f){ const buf = await f.arrayBuffer(); const r = await pj('/api/readfile', {b64:b64(buf), encoding:'auto'}); if(!r.ok) return toast(r.error); in1.value = r.text; setSt($('#st1'), `${f.name} を読み込みました（文字コード: ${r.encoding}）`, 'ok');
  $('#src').value = 'auto'; run1(); }
$('#file1').onchange = e => { if(e.target.files[0]) loadFile(e.target.files[0]); e.target.value = ''; };
['dragover','dragenter'].forEach(ev => in1.addEventListener(ev, e => { e.preventDefault(); in1.classList.add('drop'); }));
['dragleave','drop'].forEach(ev => in1.addEventListener(ev, e => { e.preventDefault(); in1.classList.remove('drop'); }));
in1.addEventListener('drop', e => { const f = e.dataTransfer.files[0]; if(f) loadFile(f); });
$('#paste1').onclick = async () => { try{ in1.value = await navigator.clipboard.readText(); run1(); }catch(e){ toast('貼り付けできませんでした。入力欄をクリックして Ctrl+V を押してください'); } };
$('#clear1').onclick = () => { in1.value = ''; run1(); };
$('#sample1').onclick = () => { in1.value = 'id,name,age,city,tags\n1,山田 太郎,34,東京,"営業,新規"\n2,佐藤 花子,28,大阪,開発\n3,鈴木 一郎,,名古屋,\n'; $('#src').value = 'auto'; run1(); };
$('#copy1').onclick = () => last1 ? copyText(last1) : toast('出力がありません');
$('#dl1').onclick = () => last1 ? download(last1, 'converted.' + lastExt, $('#enc1').value).catch(e => toast(e.message)) : toast('出力がありません');
$('#swap').onclick = () => { if(!last1) return; const d = $('#dst').value; const prev = $('#src').value === 'auto' ? (($('#detected').textContent.match(/判定: (.+)/) || [])[1]) : null;
  in1.value = last1; $('#src').value = d; const names = {'CSV':'csv','TSV':'tsv','JSON':'json','JSON Lines':'jsonl','XML':'xml','YAML':'yaml','Markdown表':'md','HTML表':'html','SQL INSERT':'sql'}; if(prev && names[prev]) $('#dst').value = names[prev]; else if(d !== 'json') $('#dst').value = 'json'; else $('#dst').value = 'csv'; run1(); };

// ---------- Protobuf
const pin = $('#pin'); let pview = 1, pres = null;
function opts2(){ return {input:$('#pEnc').value, framing:$('#pFrame').value, layout:$('#pLayout').value, explode:$('#pExp').value.trim(), varint:$('#pVar').value, len:$('#pLen').value, fixed:$('#pFix').value, proto:$('#proto').value, message:$('#pMsg').value, out_delim:',', eol:'crlf'}; }
function showPView(n){ pview = n; [1,2,3].forEach(i => $('#rv' + i).classList.toggle('on', i === n)); $('#out2').hidden = n === 2; $('#tbl2').hidden = n !== 2; if(pres) fillP(); }
function fillP(){ if(!pres) return; if(pview === 1) $('#out2').value = pres.csv; else if(pview === 3) $('#out2').value = pres.tree; else drawTable(); }
function drawTable(){ const box = $('#tbl2'); box.textContent = ''; if(!pres) return; const rows = parseCsv(pres.csv), N = 300; const t = h('table'), head = h('tr'); (rows[0] || []).forEach(c => head.append(h('th', {}, c))); t.append(head);
  rows.slice(1, N + 1).forEach(r => { const tr = h('tr'); r.forEach(c => tr.append(h('td', {title:c}, c))); t.append(tr); }); box.append(t); if(rows.length - 1 > N) box.append(h('div', {class:'st'}, `先頭 ${N} 行を表示しています（全 ${rows.length - 1} 行）`)); }
function parseCsv(s){ const out = []; let row = [], f = '', q = false; for(let i = 0; i < s.length; i++){ const c = s[i]; if(q){ if(c === '"'){ if(s[i + 1] === '"'){ f += '"'; i++; } else q = false; } else f += c; } else if(c === '"') q = true; else if(c === ','){ row.push(f); f = ''; } else if(c === '\n'){ row.push(f); out.push(row); row = []; f = ''; } else if(c !== '\r') f += c; } if(f || row.length){ row.push(f); out.push(row); } return out; }
let ptok = 0;
const run2 = debounce(async () => {
  const my = ++ptok, text = pin.value; $('#lExp').style.opacity = $('#pLayout').value === 'wide' ? 1 : .4;
  if(!text.trim()){ pres = null; $('#out2').value = ''; $('#tbl2').textContent = ''; setSt($('#st2o'), ''); $('#pinfo').textContent = ''; return; }
  let r; try{ r = await pj('/api/protobuf', {text, opts:opts2()}); }catch(e){ if(my === ptok) setSt($('#st2o'), e.message, 'err'); return; }
  if(my !== ptok) return; if(!r.ok){ pres = null; setSt($('#st2o'), '⚠ ' + r.error, 'err'); return; }
  pres = r; fillP(); $('#pinfo').textContent = `${r.bytes.toLocaleString()} バイト / ${r.messages} メッセージ`;
  setSt($('#st2o'), `${r.count} 行 / ${r.cols} 列` + (r.warnings.length ? '　' + r.warnings.join('、') : ''), 'ok');
}, 260);
const protoInfo = debounce(async () => { const p = $('#proto').value; const sel = $('#pMsg'), keep = sel.value; if(!p.trim()){ sel.replaceChildren(h('option', {value:''}, '（先頭）')); setSt($('#stp'), ''); run2(); return; }
  let r; try{ r = await pj('/api/protoinfo', {proto:p}); }catch(e){ return; }
  if(!r.ok){ setSt($('#stp'), '⚠ ' + r.error, 'err'); return; } setSt($('#stp'), `メッセージ ${r.messages.length} 個を認識しました`, 'ok');
  sel.replaceChildren(h('option', {value:''}, '（先頭）'), ...r.messages.map(m => h('option', {value:m}, m))); if(r.messages.includes(keep)) sel.value = keep; run2(); }, 300);
pin.addEventListener('input', run2); $('#proto').addEventListener('input', protoInfo);
['pEnc','pFrame','pLayout','pVar','pLen','pFix','pMsg'].forEach(i => $('#' + i).addEventListener('change', run2)); $('#pExp').addEventListener('input', run2);
[1,2,3].forEach(i => $('#rv' + i).onclick = () => showPView(i));
$('#open2').onclick = () => $('#file2').click();
$('#file2').onchange = async e => { const f = e.target.files[0]; e.target.value = ''; if(!f) return; if(f.size > 20e6) return toast('ファイルが大きすぎます（20MBまで）'); const buf = await f.arrayBuffer(); pin.value = b64(buf); $('#pEnc').value = 'base64'; setSt($('#st2'), `${f.name}（${f.size.toLocaleString()} バイト）を Base64 で読み込みました`, 'ok'); run2(); };
['dragover','dragenter'].forEach(ev => pin.addEventListener(ev, e => { e.preventDefault(); pin.classList.add('drop'); }));
['dragleave','drop'].forEach(ev => pin.addEventListener(ev, e => { e.preventDefault(); pin.classList.remove('drop'); }));
pin.addEventListener('drop', async e => { const f = e.dataTransfer.files[0]; if(!f) return; const buf = await f.arrayBuffer(); pin.value = b64(buf); $('#pEnc').value = 'base64'; setSt($('#st2'), `${f.name} を読み込みました`, 'ok'); run2(); });
$('#clear2').onclick = () => { pin.value = ''; setSt($('#st2'), ''); run2(); };
async function loadSample(){ const r = await (await fetch('/api/sample')).json(); return r; }
$('#sample2').onclick = async () => { const r = await loadSample(); pin.value = r.hex; $('#pEnc').value = 'auto'; run2(); };
$('#sampleProto').onclick = async () => { const r = await loadSample(); if(!pin.value.trim()) pin.value = r.hex; $('#proto').value = r.proto; await protoInfo(); setTimeout(() => { $('#pMsg').value = 'Order'; $('#pExp').value = 'items'; run2(); }, 450); };
$('#copy2').onclick = () => pres ? copyText(pview === 3 ? pres.tree : pres.csv) : toast('結果がありません');
$('#dl2').onclick = () => pres ? download(pres.csv, 'protobuf.csv', $('#enc2').value).catch(e => toast(e.message)) : toast('結果がありません');
showTab(LS.get('tab', 1)); syncOptVis();
</script></body></html>
"""


# ====================================================== サーバ
ENCODINGS = {"utf-8": "utf-8", "utf-8-sig": "utf-8-sig", "cp932": "cp932", "utf-16": "utf-16", "euc-jp": "euc_jp"}


def decode_bytes(b, enc="auto"):
    if enc and enc != "auto":
        return b.decode(ENCODINGS.get(enc, enc), "replace"), enc
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8"), "utf-8-sig"
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return b.decode("utf-16"), "utf-16"
    try:
        return b.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return b.decode("cp932"), "cp932"
    except UnicodeDecodeError:
        return b.decode("utf-8", "replace"), "utf-8"


def encode_text(text, enc):
    codec = ENCODINGS.get(enc, "utf-8")
    replaced = False
    try:
        data = text.encode(codec)
    except UnicodeEncodeError:
        data = text.encode(codec, "replace")
        replaced = True
    if codec == "utf-8-sig" and not data.startswith(b"\xef\xbb\xbf"):
        data = b"\xef\xbb\xbf" + data
    return data, replaced


def api_post(path, body):
    if path == "/api/convert":
        return {"ok": True, **convert(body.get("src"), body["dst"], str(body.get("text", "")), body.get("opts") or {})}
    if path == "/api/protobuf":
        o = dict(body.get("opts") or {})
        return {"ok": True, **protobuf_to_csv(str(body.get("text", "")), o)}
    if path == "/api/protoinfo":
        return {"ok": True, "messages": proto_messages(str(body.get("proto", "")))}
    if path == "/api/readfile":
        try:
            raw = base64.b64decode(body.get("b64", ""), validate=True)
        except (binascii.Error, ValueError):
            raise ConvError("ファイルを読み取れません")
        text, enc = decode_bytes(raw, body.get("encoding", "auto"))
        return {"ok": True, "text": text, "encoding": enc}
    raise KeyError(path)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, write=False):
        try:
            host = urlparse("//" + (self.headers.get("Host") or "")).hostname
        except ValueError:
            host = None
        origin = self.headers.get("Origin")
        if host not in ALLOWED_HOSTS or (write and origin and urlparse(origin).netloc != self.headers.get("Host")):
            self._send(403, "Forbidden", "text/plain; charset=utf-8")
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        p = urlparse(self.path).path
        if p == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/api/sample":
            return self._send(200, json.dumps({"hex": sample_hex(), "proto": SAMPLE_PROTO}, ensure_ascii=False))
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._guard(write=True):
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY:
                return self._send(413, '{"ok":false,"error":"データが大きすぎます"}')
            body = json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self._send(400, '{"ok":false,"error":"invalid"}')
        path = urlparse(self.path).path
        if path == "/api/download":
            data, replaced = encode_text(str(body.get("text", "")), body.get("encoding", "utf-8"))
            name = re.sub(r'[\\/:*?"<>|\r\n]', "_", str(body.get("filename") or "converted.txt"))
            return self._send(200, data, "application/octet-stream", {
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "X-Replaced": "1" if replaced else "0"})
        try:
            res = api_post(path, body)
        except KeyError:
            return self._send(404, '{"ok":false,"error":"not found"}')
        except ConvError as e:
            res = {"ok": False, "error": str(e)}
        except (ValueError, TypeError, AttributeError, struct.error, RecursionError) as e:
            res = {"ok": False, "error": f"処理できませんでした: {e}"}
        self._send(200, json.dumps(res, ensure_ascii=False))

    def log_message(self, fmt, *args):
        pass


def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("OK  " if cond else "NG  ") + name)
        ok = ok and cond

    t = 'id,name,tags\n1,山田,"a,b"\n2,佐藤,x\n'
    for dst in ["json", "jsonl", "xml", "yaml", "md", "html", "sql", "tsv"]:
        out = convert("csv", dst, t, {"infer": True})["text"]
        back = convert(dst, "csv", out, {"infer": False})["text"].replace("\r\n", "\n")
        chk(f"CSV→{FORMAT_NAMES[dst]}→CSV", [l.split(",")[0] for l in back.strip().split("\n")] == ["id", "1", "2"])
    r = protobuf_to_csv(sample_hex(), {"proto": SAMPLE_PROTO, "message": "Order", "explode": "items"})
    chk("Protobuf(.protoあり)→CSV", r["count"] == 3 and "ボールペン" in r["csv"])
    r = protobuf_to_csv(sample_hex(), {})
    chk("Protobuf(.protoなし)→CSV", r["count"] == 1 and "山田商事" in r["csv"])
    print("すべて正常です" if ok else "異常があります")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="変換ツール")
    ap.add_argument("--port", type=int, default=8410)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        raise SystemExit(selftest())
    try:
        server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        raise SystemExit(f"ポート {a.port} を使えません（{e}）")
    print(f"変換ツールを起動しました: http://127.0.0.1:{a.port}  (終了は Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
