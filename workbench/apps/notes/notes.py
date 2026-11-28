#!/usr/bin/env python3
"""メモ - Markdown対応のローカルメモ。メモ同士を [[リンク]] でつないで考えを整理する (標準ライブラリのみ)

    python3 notes.py                # http://127.0.0.1:8409
    python3 notes.py --port 9000

主な機能
    ・Markdown記述（見出し・リスト・チェックボックス・表・引用・コード・画像・取り消し線 など）。編集 / 分割 / プレビュー切替
    ・[[メモ名]] でメモをリンク。[[ と打つと候補が出ます。存在しないメモへのリンクは、クリックで新規作成
    ・バックリンク（このメモを参照しているメモ）、リンク先、未リンクの言及（タイトルが本文に出てくるのにリンクしていないメモ）
    ・#タグ、全文検索、グラフ表示（メモのつながりを図で見る）、今日のメモ（日次ノート）、ピン留め
    ・メモの名前を変えると、他のメモ内の [[リンク]] も自動で書き換え
    ・自動保存、Markdownファイルの書き出し（zip）と読み込み

ショートカット: Ctrl+K=メモ検索/新規作成  Ctrl+N=新規メモ  Ctrl+E=表示切替  Ctrl+B/I=太字/斜体  Ctrl+クリック(編集欄)=リンクを開く
データは同じフォルダの notes.db (SQLite) に保存されます（環境変数 NOTES_DB で変更可）。127.0.0.1 からのアクセスのみ受け付けます。
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sqlite3
import threading
import zipfile
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("NOTES_DB") or os.path.join(HERE, "notes.db")
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 2_000_000
WD = "月火水木金土日"


class NoteError(Exception):
    pass


class Conflict(NoteError):
    pass


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 解析
FENCE = re.compile(r"^(`{3,}|~{3,}).*?^\1[ \t]*$|^(`{3,}|~{3,}).*\Z", re.S | re.M)
INLINE_CODE = re.compile(r"`[^`\n]*`")
WIKI = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]*))?\]\]")
TAG = re.compile(r"(?:^|(?<=\s))#([^\s#.,;:!?()\[\]{}\"'<>`]+)")


def strip_code(text):
    return INLINE_CODE.sub(" ", FENCE.sub(" ", text))


def extract_links(body):
    seen, out = set(), []
    for m in WIKI.finditer(strip_code(body)):
        t = m.group(1).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def extract_tags(body):
    seen, out = set(), []
    for m in TAG.finditer(strip_code(body)):
        t = m.group(1)
        if t.isdigit() or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append(t)
    return out


def clean_title(t):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    if len(t) > 100:
        raise NoteError("タイトルは100文字以内にしてください")
    if re.search(r"[\[\]|]", t):
        raise NoteError("タイトルに [ ] | は使えません")
    return t


def snippet(text, needle=None, width=50):
    text = re.sub(r"[#>*`~=\-]+|\[\[|\]\]", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if needle:
        i = text.lower().find(needle.lower())
        if i >= 0:
            s = max(0, i - width // 2)
            return ("…" if s else "") + text[s:s + width * 2]
    return text[:width * 2]


def like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ---------------------------------------------------------------- ストア
class Store:
    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        with self.db() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL COLLATE NOCASE UNIQUE,
              body TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS links(src INTEGER NOT NULL, target TEXT NOT NULL COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS tags(note INTEGER NOT NULL, tag TEXT NOT NULL COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS i_links_t ON links(target); CREATE INDEX IF NOT EXISTS i_links_s ON links(src);
            CREATE INDEX IF NOT EXISTS i_tags_t ON tags(tag); CREATE INDEX IF NOT EXISTS i_tags_n ON tags(note);""")
            if not con.execute("SELECT 1 FROM notes").fetchone():
                self.seed(con)

    def db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        return _Ctx(con)

    def seed(self, con):
        t = now()
        docs = [("ようこそ", "# ようこそ 👋\n\nここは **リンクでつなぐメモ** です。\n\n- [[使い方]] を読む\n- 左上の「＋」で新しいメモを作る\n- `Ctrl+K` でメモを検索 / 新規作成\n\n#はじめに\n"),
                ("使い方", "# 使い方\n\n## メモをつなぐ\n`[[` と打つとメモの候補が出ます。例: [[ようこそ]] へ戻る。\n存在しないメモへのリンク（例: [[アイデア置き場]]）は、クリックすると作成できます。\n\n## 書式\n- **太字** / *斜体* / ~~取り消し~~ / ==強調==\n- [ ] チェックボックス（プレビューでクリックして切替）\n- [x] 完了したタスク\n\n> 引用もできます\n\n| 列A | 列B |\n|---|---|\n| 表 | も書けます |\n\n## 整理する\n- 右のパネルに **バックリンク**（このメモを参照しているメモ）が出ます\n- #タグ を付けると左のタグで絞り込めます\n- 右上の 🕸 でメモ同士のつながりを図で確認できます\n\n#はじめに\n")]
        for title, body in docs:
            cur = con.execute("INSERT INTO notes(title,body,created,updated) VALUES(?,?,?,?)", (title, body, t, t))
            self.reindex(con, cur.lastrowid, body)

    @staticmethod
    def reindex(con, nid, body):
        con.execute("DELETE FROM links WHERE src=?", (nid,))
        con.execute("DELETE FROM tags WHERE note=?", (nid,))
        con.executemany("INSERT INTO links(src,target) VALUES(?,?)", [(nid, t) for t in extract_links(body)])
        con.executemany("INSERT INTO tags(note,tag) VALUES(?,?)", [(nid, t) for t in extract_tags(body)])

    # ---- 一覧・検索
    def list(self, q="", tag="", sort="updated"):
        with self.db() as con:
            terms = [t for t in (q or "").split() if t]
            sql, args = "SELECT id,title,body,updated,created,pinned FROM notes WHERE 1=1", []
            for t in terms:
                sql += " AND (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
                args += [like(t), like(t)]
            if tag:
                sql += " AND id IN (SELECT note FROM tags WHERE tag=?)"
                args.append(tag)
            rows = con.execute(sql, args).fetchall()
            tmap = {}
            for r in con.execute("SELECT note,tag FROM tags"):
                tmap.setdefault(r["note"], []).append(r["tag"])
        key = {"title": lambda r: r["title"].lower(), "created": lambda r: r["created"]}.get(sort)
        rows.sort(key=lambda r: r["updated"], reverse=True)
        if key:
            rows.sort(key=key, reverse=(sort == "created"))
        rows.sort(key=lambda r: -r["pinned"])
        needle = terms[0] if terms else None
        return [{"id": r["id"], "title": r["title"], "updated": r["updated"], "pinned": bool(r["pinned"]), "tags": tmap.get(r["id"], []),
                 "snippet": snippet(r["body"], needle)} for r in rows]

    def titles(self):
        with self.db() as con:
            return [{"id": r["id"], "title": r["title"]} for r in con.execute("SELECT id,title FROM notes ORDER BY updated DESC")]

    def tag_counts(self):
        with self.db() as con:
            return [{"tag": r["tag"], "count": r["c"]} for r in con.execute("SELECT tag,COUNT(*) c FROM tags GROUP BY tag ORDER BY c DESC, tag")]

    # ---- 取得
    def get(self, nid):
        with self.db() as con:
            n = con.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
            if not n:
                raise NoteError("メモが見つかりません")
            title = n["title"]
            back = con.execute("SELECT n.id,n.title,n.body FROM notes n WHERE n.id!=? AND n.id IN (SELECT src FROM links WHERE target=?) ORDER BY n.updated DESC", (nid, title)).fetchall()
            out = con.execute("SELECT l.target, n.id FROM links l LEFT JOIN notes n ON n.title=l.target WHERE l.src=?", (nid,)).fetchall()
            ment = []
            if len(title) >= 2:
                ids = {b["id"] for b in back}
                for r in con.execute("SELECT id,title,body FROM notes WHERE id!=? AND body LIKE ? ESCAPE '\\' ORDER BY updated DESC LIMIT 50", (nid, like(title))):
                    if r["id"] not in ids and title.lower() in strip_code(WIKI.sub(" ", r["body"])).lower():
                        ment.append({"id": r["id"], "title": r["title"], "snippet": snippet(r["body"], title)})
            tags = [r["tag"] for r in con.execute("SELECT tag FROM tags WHERE note=?", (nid,))]
        return {"id": n["id"], "title": title, "body": n["body"], "created": n["created"], "updated": n["updated"], "pinned": bool(n["pinned"]),
                "rev": n["rev"], "tags": tags,
                "backlinks": [{"id": b["id"], "title": b["title"], "snippet": snippet(b["body"], "[[" + title)} for b in back],
                "outlinks": [{"title": o[0], "id": o[1]} for o in out], "mentions": ment}

    def by_title(self, title):
        with self.db() as con:
            r = con.execute("SELECT id FROM notes WHERE title=?", (clean_title(title),)).fetchone()
        return r["id"] if r else None

    # ---- 作成・更新
    def unique_title(self, con, base):
        base, n, t = base or "無題", 1, base or "無題"
        while con.execute("SELECT 1 FROM notes WHERE title=?", (t,)).fetchone():
            n += 1
            t = f"{base} {n}"
        return t

    def create(self, title="", body=""):
        with self.lock, self.db() as con:
            title = clean_title(title)
            if title and con.execute("SELECT 1 FROM notes WHERE title=?", (title,)).fetchone():
                raise NoteError("同じ名前のメモがあります")
            title = title or self.unique_title(con, "無題")
            t = now()
            cur = con.execute("INSERT INTO notes(title,body,created,updated) VALUES(?,?,?,?)", (title, body[:MAX_BODY], t, t))
            self.reindex(con, cur.lastrowid, body)
            return cur.lastrowid

    @staticmethod
    def rename_in(body, old, new):
        pat = re.compile(r"\[\[\s*" + re.escape(old) + r"\s*(\|[^\[\]\n]*)?\]\]", re.I)
        return pat.sub(lambda m: "[[" + new + (m.group(1) or "") + "]]", body)

    def update(self, nid, title=None, body=None, rev=None, pinned=None):
        with self.lock, self.db() as con:
            n = con.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
            if not n:
                raise NoteError("メモが見つかりません")
            if rev is not None and int(rev) != n["rev"]:
                raise Conflict("別の画面でこのメモが更新されています。読み込み直してください")
            new_title = n["title"] if title is None else clean_title(title)
            if not new_title:
                raise NoteError("タイトルを入力してください")
            new_body = n["body"] if body is None else str(body)[:MAX_BODY]
            renamed = 0
            if new_title != n["title"]:
                if con.execute("SELECT 1 FROM notes WHERE title=? AND id!=?", (new_title, nid)).fetchone():
                    raise NoteError("同じ名前のメモがあります")
                if new_title.lower() != n["title"].lower() or True:
                    new_body = self.rename_in(new_body, n["title"], new_title)
                    for r in con.execute("SELECT id,body FROM notes WHERE id!=? AND id IN (SELECT src FROM links WHERE target=?)", (nid, n["title"])).fetchall():
                        nb = self.rename_in(r["body"], n["title"], new_title)
                        if nb != r["body"]:
                            con.execute("UPDATE notes SET body=?, rev=rev+1 WHERE id=?", (nb, r["id"]))
                            self.reindex(con, r["id"], nb)
                            renamed += 1
            changed = new_title != n["title"] or new_body != n["body"]
            con.execute("UPDATE notes SET title=?, body=?, updated=?, pinned=?, rev=rev+1 WHERE id=?",
                        (new_title, new_body, now() if changed else n["updated"], n["pinned"] if pinned is None else int(bool(pinned)), nid))
            self.reindex(con, nid, new_body)
            r = con.execute("SELECT rev,updated,title,body FROM notes WHERE id=?", (nid,)).fetchone()
            return {"rev": r["rev"], "updated": r["updated"], "title": r["title"], "body": r["body"], "renamed": renamed}

    def delete(self, nid):
        with self.lock, self.db() as con:
            con.execute("DELETE FROM notes WHERE id=?", (nid,))
            con.execute("DELETE FROM links WHERE src=?", (nid,))
            con.execute("DELETE FROM tags WHERE note=?", (nid,))

    def graph(self):
        with self.db() as con:
            notes = con.execute("SELECT id,title FROM notes").fetchall()
            ids = {n["title"].lower(): n["id"] for n in notes}
            nodes = [{"id": n["id"], "title": n["title"], "ghost": False} for n in notes]
            ghosts, edges = {}, set()
            for l in con.execute("SELECT src,target FROM links"):
                t = l["target"].lower()
                if t in ids:
                    if ids[t] != l["src"]:
                        edges.add((l["src"], ids[t]))
                else:
                    gid = ghosts.setdefault(t, -(len(ghosts) + 1))
                    edges.add((l["src"], gid))
            nodes += [{"id": gid, "title": t, "ghost": True} for t, gid in ghosts.items()]
        return {"nodes": nodes, "edges": [list(e) for e in edges]}

    def daily(self):
        d = dt.date.today()
        title = d.isoformat()
        nid = self.by_title(title)
        if nid is None:
            nid = self.create(title, f"# {d.year}年{d.month}月{d.day}日({WD[d.weekday()]})\n\n")
        return nid

    def link_mention(self, src, target):
        with self.lock, self.db() as con:
            s = con.execute("SELECT * FROM notes WHERE id=?", (src,)).fetchone()
            t = con.execute("SELECT title FROM notes WHERE id=?", (target,)).fetchone()
            if not s or not t:
                raise NoteError("メモが見つかりません")
            title, body = t["title"], s["body"]
            spans = [m.span() for m in WIKI.finditer(body)] + [m.span() for m in FENCE.finditer(body)] + [m.span() for m in INLINE_CODE.finditer(body)]
            for m in re.finditer(re.escape(title), body, re.I):
                if any(a <= m.start() < b for a, b in spans):
                    continue
                text = m.group(0)
                link = f"[[{title}]]" if text == title else f"[[{title}|{text}]]"
                nb = body[:m.start()] + link + body[m.end():]
                con.execute("UPDATE notes SET body=?, updated=?, rev=rev+1 WHERE id=?", (nb, now(), src))
                self.reindex(con, src, nb)
                return {"ok": True}
        raise NoteError("リンクできる箇所が見つかりませんでした")

    def export_zip(self):
        buf, used = io.BytesIO(), set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, self.db() as con:
            for r in con.execute("SELECT title,body FROM notes ORDER BY title"):
                name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", r["title"]).strip(" .") or "note"
                base, i = name, 1
                while name.lower() in used:
                    i += 1
                    name = f"{base}_{i}"
                used.add(name.lower())
                z.writestr(name + ".md", r["body"])
        return buf.getvalue()

    def import_files(self, files):
        done = 0
        for f in files[:500]:
            name = re.sub(r"\.(md|markdown|txt)$", "", str(f.get("name") or "取り込み"), flags=re.I)
            title = re.sub(r"[\[\]|]", "_", re.sub(r"\s+", " ", name)).strip()[:100] or "取り込み"
            with self.lock, self.db() as con:
                title = self.unique_title(con, title)
            self.create(title, str(f.get("body") or ""))
            done += 1
        return done


class _Ctx:
    def __init__(self, con):
        self.con = con

    def __enter__(self):
        return self.con

    def __exit__(self, et, ev, tb):
        try:
            self.con.commit() if et is None else self.con.rollback()
        finally:
            self.con.close()


# ---------------------------------------------------------------- API
def get_api(store, path, qs):
    q = lambda k, d="": (qs.get(k) or [d])[0]
    if path == "/api/notes":
        return store.list(q("q"), q("tag"), q("sort", "updated"))
    m = re.fullmatch(r"/api/notes/(\d+)", path)
    if m:
        return store.get(int(m.group(1)))
    if path == "/api/titles":
        return store.titles()
    if path == "/api/tags":
        return store.tag_counts()
    if path == "/api/graph":
        return store.graph()
    raise KeyError(path)


def post_api(store, method, path, b):
    m = re.fullmatch(r"/api/notes/(\d+)", path)
    if m and method == "PUT":
        r = store.update(int(m.group(1)), b.get("title"), b.get("body"), b.get("rev"), b.get("pinned"))
        r["ok"] = True
        return r
    if m and method == "DELETE":
        store.delete(int(m.group(1)))
        return {"ok": True}
    if path == "/api/notes" and method == "POST":
        return {"ok": True, "id": store.create(b.get("title", ""), str(b.get("body") or ""))}
    if path == "/api/open" and method == "POST":  # タイトルで開く（無ければ作る）
        title = clean_title(b.get("title"))
        if not title:
            raise NoteError("タイトルを入力してください")
        nid = store.by_title(title)
        return {"ok": True, "id": nid if nid is not None else store.create(title), "created": nid is None}
    if path == "/api/daily" and method == "POST":
        return {"ok": True, "id": store.daily()}
    if path == "/api/link_mention" and method == "POST":
        return store.link_mention(int(b["src"]), int(b["target"]))
    if path == "/api/import" and method == "POST":
        return {"ok": True, "done": store.import_files(b.get("files") or [])}
    raise KeyError(path)


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>メモ</title>
<style>
:root{--bg:#f6f5f2;--side:#efede8;--card:#fff;--ink:#26231f;--sub:#7a746b;--line:#dedad2;--accent:#b4531a;--hi:#fbeee3;--link:#1d63c4;--miss:#c0392b;--code:#f1efe9}
@media (prefers-color-scheme:dark){:root{--bg:#1a1917;--side:#211f1d;--card:#262421;--ink:#e9e5de;--sub:#a29b90;--line:#3a3733;--accent:#f0925a;--hi:#3a2a20;--link:#7fb0ff;--miss:#ff8a7a;--code:#2f2c28}}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic UI",Meiryo,sans-serif;background:var(--bg);color:var(--ink);font-size:14px;overflow:hidden}
input,select,button,textarea{font:inherit;color:var(--ink)}
button{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--accent)}button.on{background:var(--hi);border-color:var(--accent)}button.ghost{border:none;background:none;padding:4px 7px}
button:disabled{opacity:.4;cursor:default}
#app{display:grid;grid-template-columns:290px minmax(0,1fr) 280px;height:100vh}
#side{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
#side .top{padding:10px;display:grid;grid-template-columns:minmax(0,1fr);gap:8px;border-bottom:1px solid var(--line)}
#side .row{display:flex;gap:6px;min-width:0}#side .row select{min-width:0;max-width:100px}
#q{flex:1;min-width:0;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card)}
#tags{display:flex;flex-wrap:wrap;gap:4px;max-height:84px;overflow:auto}
.tg{font-size:12px;padding:1px 8px;border-radius:99px;background:var(--card);border:1px solid var(--line);cursor:pointer}.tg.on{background:var(--accent);color:#fff;border-color:var(--accent)}
#list{overflow:auto;flex:1;padding:4px 6px}
.item{padding:8px 10px;border-radius:8px;cursor:pointer;margin:2px 0}.item:hover{background:var(--hi)}.item.cur{background:var(--hi);outline:1px solid var(--accent)}
.item b{display:block;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item small{display:block;color:var(--sub);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
#main{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--card)}
#bar{display:flex;gap:6px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
#title{flex:1;min-width:140px;font-size:19px;font-weight:700;border:1px solid transparent;border-radius:7px;padding:4px 8px;background:none}
#title:hover,#title:focus{border-color:var(--line);background:var(--bg);outline:none}
#status{font-size:12px;color:var(--sub);min-width:62px;text-align:right}
#panes{flex:1;display:flex;min-height:0;position:relative}
#ed{flex:1;min-width:0;resize:none;border:none;outline:none;padding:18px 22px;line-height:1.75;font-family:Consolas,"Yu Gothic UI",Meiryo,monospace;font-size:14px;background:var(--card);tab-size:2}
#pv{flex:1;min-width:0;overflow:auto;padding:18px 26px;line-height:1.8}
#panes.split #ed{border-right:1px solid var(--line)}
#panes.edit #pv,#panes.view #ed{display:none}
#mm{flex:1;position:relative;display:none;min-width:0;background:var(--card);overflow:hidden}#panes.map #mm,#panes.splitmap #mm{display:block}#panes.map #ed,#panes.map #pv,#panes.splitmap #pv{display:none}#panes.splitmap #ed{border-right:1px solid var(--line);order:-1}
.mmghost{position:absolute;z-index:4;pointer-events:none;background:var(--card);border:2px solid var(--accent);border-radius:9px;padding:4px 10px;font-size:13px;opacity:.92;max-width:280px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;box-shadow:0 4px 14px rgba(0,0,0,.25)}
.mmedit{position:absolute;z-index:3;font-size:13px;padding:5px 8px;border:2px solid var(--accent);border-radius:9px;background:var(--card);min-width:160px;box-shadow:0 4px 16px rgba(0,0,0,.25)}
#mmtools [hidden]{display:none!important}#mmtools select{padding:2px 4px;font-size:12px}#mmtools .sep{width:1px;height:18px;background:var(--line)}#mmZ{width:90px}#mmsvg:focus{outline:none}
#mmsvg{width:100%;height:100%;display:block;cursor:grab;touch-action:none;user-select:none}#mmsvg.drag{cursor:grabbing}
#mmtools{position:absolute;top:8px;left:10px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:5px 8px;z-index:2;max-width:calc(100% - 20px)}#mmtools button{padding:3px 9px;font-size:12px}#mmtools label{font-size:12px;color:var(--sub);display:flex;gap:4px;align-items:center}
.mmn{cursor:pointer}.mmn:hover rect.mb{stroke-width:2.4}
#empty{margin:auto;color:var(--sub);text-align:center;line-height:2}
#info{background:var(--side);border-left:1px solid var(--line);overflow:auto;padding:10px 12px}
#info h4{margin:14px 0 6px;font-size:12px;color:var(--sub);font-weight:600}#info h4:first-child{margin-top:2px}
.ln{display:block;padding:6px 8px;border-radius:7px;cursor:pointer;margin:2px 0;border:1px solid var(--line);background:var(--card)}
.ln:hover{border-color:var(--accent)}.ln b{display:block;font-weight:600}.ln small{display:block;color:var(--sub);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ln.miss b{color:var(--miss)}.sub{color:var(--sub);font-size:12px}
.mini{font-size:11px;padding:1px 7px;margin-top:3px}
/* markdown */
#pv h1,#pv h2,#pv h3,#pv h4{line-height:1.35;margin:1.2em 0 .5em}#pv h1{font-size:1.7em;border-bottom:1px solid var(--line);padding-bottom:.2em}#pv h2{font-size:1.4em}#pv h3{font-size:1.18em}
#pv>*:first-child{margin-top:0}#pv p{margin:.6em 0}
#pv code{background:var(--code);padding:1px 5px;border-radius:4px;font-family:Consolas,monospace;font-size:.92em}
#pv pre{background:var(--code);padding:12px 14px;border-radius:8px;overflow:auto}#pv pre code{background:none;padding:0}
#pv blockquote{margin:.8em 0;padding:.1em 1em;border-left:4px solid var(--accent);color:var(--sub);background:var(--hi);border-radius:0 8px 8px 0}
#pv table{border-collapse:collapse;margin:.8em 0}#pv th,#pv td{border:1px solid var(--line);padding:5px 12px}#pv th{background:var(--code)}
#pv ul,#pv ol{padding-left:1.5em;margin:.4em 0}#pv li.task{list-style:none;margin-left:-1.3em}#pv li.task input{margin-right:6px}#pv li.done{color:var(--sub);text-decoration:line-through}
#pv img{max-width:100%;border-radius:6px}#pv hr{border:none;border-top:1px solid var(--line);margin:1.4em 0}#pv mark{background:#ffe58a;color:#222;padding:0 2px;border-radius:3px}
a.wl{color:var(--link);cursor:pointer;text-decoration:none;border-bottom:1px solid currentColor}a.wl.miss{color:var(--miss);border-bottom-style:dashed}
a.tag{color:var(--accent);cursor:pointer;text-decoration:none;background:var(--hi);padding:0 6px;border-radius:99px;font-size:.92em}
#pv a[target]{color:var(--link)}
#ac{position:fixed;z-index:20;background:var(--card);border:1px solid var(--accent);border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.25);min-width:200px;max-width:340px;max-height:260px;overflow:auto;display:none}
#ac div{padding:6px 10px;cursor:pointer}#ac div.sel{background:var(--hi)}#ac small{color:var(--sub)}
dialog{border:none;border-radius:12px;padding:0;background:var(--card);color:var(--ink);box-shadow:0 10px 40px rgba(0,0,0,.35)}
dialog::backdrop{background:rgba(10,10,10,.5)}
#qs{width:min(560px,94vw);padding:12px}#qs input{width:100%;padding:10px 12px;font-size:16px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
#qsl{max-height:50vh;overflow:auto;margin-top:8px}#qsl div{padding:8px 10px;border-radius:7px;cursor:pointer}#qsl div.sel{background:var(--hi)}
#gd{width:min(1000px,96vw);height:min(720px,90vh)}#gd .gh{display:flex;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line)}
#gsvg{width:100%;height:calc(100% - 52px);display:block}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#2b2723;color:#fff;padding:9px 16px;border-radius:99px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:50}#toast.show{opacity:.95}
#menu{position:absolute;right:12px;top:48px;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.2);padding:6px;display:none;z-index:15}
#menu button{display:block;width:100%;text-align:left;border:none;background:none;padding:7px 12px}#menu button:hover{background:var(--hi)}
.only-s{display:none}
@media(max-width:980px){#app{grid-template-columns:1fr}#side,#info{position:fixed;top:0;bottom:0;z-index:30;width:min(320px,88vw);display:none}#side{left:0}#info{right:0}
 #side.show,#info.show{display:flex;flex-direction:column}#info.show{display:block}.only-s{display:inline-block}#panes.split #pv{display:none}#panes.split #ed{border-right:none}#panes.splitmap #ed{display:none}}
</style></head><body>
<div id="app">
  <aside id="side"><div class="top">
    <div class="row"><input id="q" type="search" placeholder="🔍 検索（スペースでAND）"><button id="bNew" title="新規メモ (Ctrl+N)">＋</button></div>
    <div class="row"><button id="bDaily" style="flex:1" title="今日の日付のメモを開く（なければ作成）">📅 今日のメモ</button>
      <select id="sort" title="並び順"><option value="updated">更新順</option><option value="created">作成順</option><option value="title">名前順</option></select></div>
    <div id="tags"></div></div>
    <div id="list"></div></aside>
  <section id="main">
    <div id="bar">
      <button class="ghost only-s" id="bSide">☰</button>
      <button class="ghost" id="bBack" title="戻る" disabled>←</button><button class="ghost" id="bFwd" title="進む" disabled>→</button>
      <input id="title" placeholder="タイトル" disabled><span id="status"></span>
      <button class="ghost" id="bPin" title="ピン留め">📌</button>
      <button id="mEdit" title="テキストのみ">編集</button><button id="mSplit" title="左: テキスト / 右: Markdownプレビュー">分割:表示</button><button id="mSplitMap" title="左: テキスト / 右: マインドマップ">分割:🧠</button><button id="mView" title="プレビューのみ">表示</button><button id="mMap" title="マインドマップのみ">🧠 マップ</button>
      <button class="ghost" id="bGraph" title="グラフ">🕸</button><button class="ghost" id="bQs" title="メモ検索 (Ctrl+K)">🔎</button>
      <button class="ghost" id="bMenu">⋯</button><button class="ghost only-s" id="bInfo">ℹ︎</button>
      <div id="menu"><button id="mExport">Markdownを書き出し (zip)</button><button id="mImport">Markdownを読み込み…</button><button id="mDel" style="color:var(--miss)">このメモを削除</button></div>
      <input type="file" id="file" accept=".md,.markdown,.txt" multiple hidden>
    </div>
    <div id="panes" class="split"><div id="empty">メモを選ぶか、<b>＋</b> で新規作成してください<br><span class="sub">Ctrl+K でメモを検索 / 新規作成</span></div>
      <div id="mm"><div id="mmtools">
        <select id="mmView" title="表示する内容"><option value="outline">このメモの構造</option><option value="notes">メモ間のつながり</option></select>
        <label id="mmOutOpts"><input type="checkbox" id="mmLink"> リンク先のメモも展開</label>
        <span id="mmNoteOpts" hidden><label>深さ <select id="mmDepth"><option>1</option><option>2</option><option>3</option><option>4</option></select></label> <label><input type="checkbox" id="mmBack"> 被リンクも表示</label></span>
        <span class="sep"></span><button id="mmOut" title="縮小">－</button><input type="range" id="mmZ" min="10" max="300" step="1"><button id="mmIn" title="拡大">＋</button><span id="mmZv" class="sub">100%</span>
        <button id="mmFit">全体表示</button><button id="mmAll">すべて展開</button><button id="mmLess">折りたたむ</button>
        <span class="sep"></span><button id="mmAddC" title="選択中のノードの子を追加 (Tab)">＋子</button><button id="mmAddS" title="兄弟を追加 (Enter)">＋兄弟</button><button id="mmEd" title="編集 (ダブルクリック / F2)">✎</button><button id="mmDel" title="削除 (Delete)">🗑</button><span class="sep"></span><button id="mmCopy" title="選択ノードを枝ごとコピー (Ctrl+C)">コピー</button><button id="mmCut" title="枝ごと切り取り (Ctrl+X)">切取</button><button id="mmPaste" title="選択ノードの子として貼り付け (Ctrl+V)">貼付(子)</button><button id="mmPasteS" title="選択ノードの兄弟として貼り付け (Ctrl+Shift+V)">貼付(兄弟)</button><span class="sep"></span><button id="mmSave">SVG保存</button>
        <button id="mmHelp" title="操作方法">？</button></div><svg id="mmsvg" tabindex="0"></svg></div>
      <textarea id="ed" spellcheck="false" placeholder="ここにMarkdownで書きます。[[ でメモをリンク" hidden></textarea><div id="pv" hidden></div></div>
  </section>
  <aside id="info"></aside>
</div>
<div id="ac"></div>
<dialog id="qs"><input id="qsi" placeholder="メモ名を入力（なければ新規作成）"><div id="qsl"></div></dialog>
<dialog id="gd"><div class="gh"><b>🕸 つながり</b><label class="sub"><input type="checkbox" id="gLocal"> 開いているメモの周辺だけ</label><span style="flex:1"></span><button id="gClose">閉じる</button></div><svg id="gsvg"></svg></dialog>
<div id="toast"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function h(tag, props = {}, ...kids){
  const e = document.createElement(tag);
  for(const [k,v] of Object.entries(props)){ if(v == null || v === false) continue;
    if(k === 'class') e.className = v; else if(['checked','value','disabled'].includes(k)) e[k] = v; else if(k.startsWith('on')) e[k] = v; else e.setAttribute(k, v); }
  for(const c of kids.flat()) if(c != null) e.append(c.nodeType ? c : document.createTextNode(c));
  return e;
}
const lsGet = k => { try{ return localStorage.getItem(k); }catch(e){ return null; } };
const lsSet = (k,v) => { try{ localStorage.setItem(k,v); }catch(e){} };
function toast(m){ const t = $('#toast'); t.textContent = m; t.classList.add('show'); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove('show'), 2400); }
async function api(method, path, body){
  try{ const r = await fetch(path, {method, headers:{'Content-Type':'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)}); return await r.json(); }
  catch(e){ return {ok:false, error:String(e)}; }
}

// ================= Markdown
let TITLES = new Map();
const INL = String.raw`\x60([^\x60\n]+)\x60|\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]*))?\]\]|!\[([^\]\n]*)\]\((https?:\/\/[^\s)]+)\)|\[([^\]\n]+)\]\(([^\s)]+)\)|\*\*([^*\n]+?)\*\*|~~([^~\n]+?)~~|==([^=\n]+?)==|(?<![\w*])\*([^*\s][^*\n]*?)\*(?!\*)|(?<=^|\s)#([^\s#.,;:!?()\[\]{}"'<>\x60]+)|(https?:\/\/[^\s<>"')\]]+)`;
function inline(t){
  const re = new RegExp(INL, 'g'); let out = '', last = 0, m;
  while((m = re.exec(t))){
    out += esc(t.slice(last, m.index)); last = re.lastIndex;
    if(m[1] !== undefined) out += '<code>' + esc(m[1]) + '</code>';
    else if(m[2] !== undefined){ const title = m[2].trim(), label = (m[3] !== undefined && m[3].trim()) ? m[3].trim() : title;
      out += `<a class="wl${TITLES.has(title.toLowerCase()) ? '' : ' miss'}" data-t="${esc(title)}">${esc(label)}</a>`; }
    else if(m[5] !== undefined) out += `<img src="${esc(m[5])}" alt="${esc(m[4])}" loading="lazy">`;
    else if(m[7] !== undefined){ if(/^(https?:\/\/|mailto:)/i.test(m[7])) out += `<a href="${esc(m[7])}" target="_blank" rel="noopener noreferrer">${inline(m[6])}</a>`; else out += esc(m[0]); }
    else if(m[8] !== undefined) out += '<strong>' + inline(m[8]) + '</strong>';
    else if(m[9] !== undefined) out += '<del>' + inline(m[9]) + '</del>';
    else if(m[10] !== undefined) out += '<mark>' + inline(m[10]) + '</mark>';
    else if(m[11] !== undefined) out += '<em>' + inline(m[11]) + '</em>';
    else if(m[12] !== undefined){ if(/^\d+$/.test(m[12])) out += esc(m[0]); else out += `<a class="tag" data-tag="${esc(m[12])}">#${esc(m[12])}</a>`; }
    else if(m[13] !== undefined){ const u = m[13].replace(/[.,;:!?]+$/, ''); out += `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(u)}</a>` + esc(m[13].slice(u.length)); }
  }
  return out + esc(t.slice(last));
}
const LISTRE = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/, HR = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/, HEAD = /^(#{1,6})\s+(.*?)\s*#*\s*$/, FEN = /^(`{3,}|~{3,})\s*[\w+-]*\s*$/, TSEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const cells = l => l.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
function isStart(ln, nx){ return HEAD.test(ln) || FEN.test(ln) || HR.test(ln) || /^\s{0,3}>/.test(ln) || LISTRE.test(ln) || (ln.includes('|') && nx !== undefined && TSEP.test(nx)); }
function buildList(items, pos, tasks){
  const ind = items[pos[0]].indent, tag = items[pos[0]].ordered ? 'ol' : 'ul'; let out = `<${tag}>`;
  while(pos[0] < items.length && items[pos[0]].indent === ind){
    const it = items[pos[0]++]; let li, cls = '';
    const tm = /^\[([ xX])\]\s+(.*)$/s.exec(it.text);
    if(tm){ const done = tm[1] !== ' '; cls = done ? ' class="task done"' : ' class="task"';
      li = `<input type="checkbox" data-line="${it.line}"${done ? ' checked' : ''}${tasks ? '' : ' disabled'}>` + inline(tm[2]); }
    else li = inline(it.text);
    if(pos[0] < items.length && items[pos[0]].indent > ind) li += buildList(items, pos, tasks);
    out += `<li${cls}>${li}</li>`;
  }
  return out + `</${tag}>`;
}
function blocks(lines, base, tasks){
  let html = '', i = 0, m;
  while(i < lines.length){
    const ln = lines[i];
    if(/^\s*$/.test(ln)){ i++; continue; }
    if((m = FEN.exec(ln))){ const buf = []; let j = i + 1; while(j < lines.length && !lines[j].startsWith(m[1])) buf.push(lines[j++]); html += `<pre><code>${esc(buf.join('\n'))}</code></pre>`; i = j + 1; continue; }
    if((m = HEAD.exec(ln))){ html += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; i++; continue; }
    if(HR.test(ln)){ html += '<hr>'; i++; continue; }
    if(/^\s{0,3}>/.test(ln)){ const buf = []; while(i < lines.length && /^\s{0,3}>/.test(lines[i])) buf.push(lines[i++].replace(/^\s{0,3}>\s?/, '')); html += `<blockquote>${blocks(buf, 0, false)}</blockquote>`; continue; }
    if(LISTRE.test(ln)){ const items = [];
      while(i < lines.length){ const lm = LISTRE.exec(lines[i].replace(/\t/g, '    '));
        if(lm) items.push({indent:lm[1].length, ordered:/\d/.test(lm[2]), text:lm[3], line:base + i});
        else if(items.length && /^\s{2,}\S/.test(lines[i])) items[items.length - 1].text += '\n' + lines[i].trim();
        else break; i++; }
      const pos = [0]; while(pos[0] < items.length) html += buildList(items, pos, tasks); continue; }
    if(ln.includes('|') && i + 1 < lines.length && TSEP.test(lines[i + 1])){
      const head = cells(ln); i += 2; let t = '<table><thead><tr>' + head.map(c => `<th>${inline(c)}</th>`).join('') + '</tr></thead><tbody>';
      while(i < lines.length && lines[i].includes('|') && !/^\s*$/.test(lines[i])) t += '<tr>' + cells(lines[i++]).map(c => `<td>${inline(c)}</td>`).join('') + '</tr>';
      html += t + '</tbody></table>'; continue; }
    const buf = [ln]; i++;
    while(i < lines.length && !/^\s*$/.test(lines[i]) && !isStart(lines[i], lines[i + 1])) buf.push(lines[i++]);
    html += '<p>' + buf.map(inline).join('<br>') + '</p>';
  }
  return html;
}
const renderMd = src => blocks(src.replace(/\r/g, '').split('\n'), 0, true);

// ================= 状態
let cur = null, notes = [], tagFilter = '', hist = [], hpos = -1, dirty = false, saving = false, again = false, saveT, mode = lsGet('ln.mode') || 'split';
const ed = $('#ed'), pv = $('#pv'), titleIn = $('#title');

const TITLE_CASE = new Map();
async function refreshTitles(){ const t = await api('GET', '/api/titles'); TITLES = new Map(t.map(x => [x.title.toLowerCase(), x.id])); TITLE_CASE.clear(); t.forEach(x => TITLE_CASE.set(x.title.toLowerCase(), x.title)); }
async function refreshList(){
  const p = new URLSearchParams({q:$('#q').value.trim(), tag:tagFilter, sort:$('#sort').value});
  notes = await api('GET', '/api/notes?' + p); renderList();
  const tg = await api('GET', '/api/tags'); const box = $('#tags'); box.innerHTML = '';
  for(const t of tg.slice(0, 40)) box.append(h('span', {class:'tg' + (tagFilter === t.tag ? ' on' : ''), 'data-tag':t.tag, onclick:() => { tagFilter = tagFilter === t.tag ? '' : t.tag; refreshList(); }}, `#${t.tag} ${t.count}`));
}
function fmtDate(s){ const today = new Date().toISOString().slice(0,10); const d = s.slice(0,10); const loc = new Date(); const ld = `${loc.getFullYear()}-${String(loc.getMonth()+1).padStart(2,'0')}-${String(loc.getDate()).padStart(2,'0')}`; return d === ld ? s.slice(11,16) : s.slice(5,10).replace('-','/'); }
function renderList(){
  const L = $('#list'); L.innerHTML = '';
  for(const n of notes) L.append(h('div', {class:'item' + (cur && cur.id === n.id ? ' cur' : ''), 'data-id':n.id, onclick:() => go(n.id)},
    h('b', {}, (n.pinned ? '📌 ' : '') + n.title), h('small', {}, fmtDate(n.updated) + '  ' + (n.snippet || '（空）'))));
  if(!notes.length) L.append(h('div', {class:'sub', style:'padding:14px'}, '該当するメモがありません'));
}
function setMode(m){ mode = m; lsSet('ln.mode', m); const p = $('#panes'); p.className = m; for(const [k,id] of [['edit','mEdit'],['split','mSplit'],['splitmap','mSplitMap'],['view','mView'],['map','mMap']]) $('#' + id).classList.toggle('on', k === m); if(cur && m !== 'view' && m !== 'map' && m !== 'splitmap') setTimeout(() => ed.focus(), 0); if(isMapMode()) renderMap(true); }
function isMapMode(){ return mode === 'map' || mode === 'splitmap'; }
function showEditor(on){ $('#empty').hidden = on; ed.hidden = !on; pv.hidden = !on; titleIn.disabled = !on; $('#bPin').disabled = !on; $('#panes').className = on ? mode : 'split'; if(!on) { ed.hidden = true; pv.hidden = true; } }
function renderPreview(){ if(!cur) return; pv.innerHTML = renderMd(ed.value); if(isMapMode()){ clearTimeout(renderPreview.t); renderPreview.t = setTimeout(() => renderMap(false), 250); } }
function renderInfo(){
  const I = $('#info'); I.innerHTML = ''; if(!cur) return;
  const item = (n, extra) => h('div', {class:'ln' + (n.id == null ? ' miss' : ''), onclick:() => n.id != null ? go(n.id) : openTitle(n.title)}, h('b', {}, n.title), n.snippet ? h('small', {}, n.snippet) : null, extra);
  I.append(h('h4', {}, 'タグ'), cur.tags.length ? h('div', {}, cur.tags.map(t => h('a', {class:'tag', style:'cursor:pointer;margin-right:4px', onclick:() => { tagFilter = t; refreshList(); }}, '#' + t))) : h('div', {class:'sub'}, '#タグ を本文に書くとここに出ます'),
    h('h4', {}, `バックリンク (${cur.backlinks.length})`), cur.backlinks.length ? h('div', {}, cur.backlinks.map(b => item(b))) : h('div', {class:'sub'}, 'このメモへのリンクはまだありません'),
    h('h4', {}, `リンク先 (${cur.outlinks.length})`), cur.outlinks.length ? h('div', {}, cur.outlinks.map(o => item(o))) : h('div', {class:'sub'}, '[[ ]] でリンクを作れます'));
  if(cur.mentions.length) I.append(h('h4', {}, `未リンクの言及 (${cur.mentions.length})`), h('div', {}, cur.mentions.map(m => item(m, h('button', {class:'mini', onclick:async e => { e.stopPropagation(); const r = await api('POST', '/api/link_mention', {src:m.id, target:cur.id}); if(r.ok === false) return toast(r.error); toast('リンクにしました'); await reloadInfo(); } }, 'リンクにする')))));
  I.append(h('h4', {}, '情報'), h('div', {class:'sub'}, `作成 ${cur.created}`), h('div', {class:'sub', id:'upd'}, `更新 ${cur.updated}`), h('div', {class:'sub', id:'cnt'}, `${ed.value.length} 文字`));
}
async function reloadInfo(){ if(!cur) return; const d = await api('GET', '/api/notes/' + cur.id); if(d.id){ cur.backlinks = d.backlinks; cur.outlinks = d.outlinks; cur.mentions = d.mentions; cur.tags = d.tags; cur.updated = d.updated; renderInfo(); } }

// ---- 保存
function markDirty(){ dirty = true; $('#status').textContent = '編集中…'; clearTimeout(saveT); saveT = setTimeout(save, 700); }
async function save(){
  if(!cur || !dirty) return; clearTimeout(saveT);
  if(saving){ again = true; return; }
  saving = true; dirty = false; $('#status').textContent = '保存中…';
  const id = cur.id, r = await api('PUT', '/api/notes/' + id, {title:titleIn.value, body:ed.value, rev:cur.rev});
  saving = false;
  if(!cur || cur.id !== id) return;
  if(r.ok === false){ dirty = true; $('#status').textContent = '未保存'; toast(r.error); if(/同じ名前|使えません|100文字|タイトル/.test(r.error)) titleIn.value = cur.title; return; }
  cur.rev = r.rev; cur.updated = r.updated; const renamed = r.title !== cur.title; cur.title = r.title; titleIn.value = r.title;
  if(r.body !== ed.value && !dirty){ ed.value = r.body; renderPreview(); }
  $('#status').textContent = '保存済み'; if(r.renamed) toast(`他の${r.renamed}件のメモのリンクも更新しました`);
  if(again || dirty){ again = false; dirty = true; return save(); }
  await refreshTitles(); await refreshList(); await reloadInfo(); if(renamed || r.renamed) renderPreview();
}
async function flush(){ if(dirty) await save(); while(saving) await new Promise(r => setTimeout(r, 30)); }
window.addEventListener('beforeunload', () => { if(dirty && cur) fetch('/api/notes/' + cur.id, {method:'PUT', keepalive:true, headers:{'Content-Type':'application/json'}, body:JSON.stringify({title:titleIn.value, body:ed.value, rev:cur.rev})}); });

// ---- 移動
async function go(id, push = true, focusTitle = false){
  await flush(); const d = await api('GET', '/api/notes/' + id); if(d.ok === false) return toast(d.error);
  cur = d; if(push){ hist = hist.slice(0, hpos + 1); if(hist[hist.length - 1] !== id) hist.push(id); hpos = hist.length - 1; }
  showEditor(true); titleIn.value = d.title; ed.value = d.body; dirty = false; $('#status').textContent = ''; $('#bPin').classList.toggle('on', d.pinned);
  renderPreview(); renderInfo(); renderList(); setMode(mode); updNav(); $('#side').classList.remove('show'); $('#info').classList.remove('show');
  if(focusTitle){ titleIn.focus(); titleIn.select(); } else if(mode !== 'view' && !isMapMode()) ed.focus();
  lsSet('ln.last', String(id));
}
function updNav(){ $('#bBack').disabled = hpos <= 0; $('#bFwd').disabled = hpos >= hist.length - 1; }
async function openTitle(title){ const r = await api('POST', '/api/open', {title}); if(r.ok === false) return toast(r.error); if(r.created) toast(`「${title}」を作成しました`); await refreshTitles(); await refreshList(); go(r.id); }
async function newNote(){ await flush(); const r = await api('POST', '/api/notes', {title:'', body:''}); if(r.ok === false) return toast(r.error); await refreshTitles(); await refreshList(); go(r.id, true, true); }
$('#bNew').onclick = newNote;
$('#bDaily').onclick = async () => { const r = await api('POST', '/api/daily'); await refreshTitles(); await refreshList(); go(r.id); };
$('#bBack').onclick = () => { if(hpos > 0){ hpos--; go(hist[hpos], false); } };
$('#bFwd').onclick = () => { if(hpos < hist.length - 1){ hpos++; go(hist[hpos], false); } };
$('#bPin').onclick = async () => { if(!cur) return; await flush(); const r = await api('PUT', '/api/notes/' + cur.id, {pinned:!cur.pinned, rev:cur.rev}); if(r.ok === false) return toast(r.error); cur.pinned = !cur.pinned; cur.rev = r.rev; $('#bPin').classList.toggle('on', cur.pinned); refreshList(); };
titleIn.addEventListener('input', markDirty); titleIn.addEventListener('keydown', e => { if(e.key === 'Enter'){ e.preventDefault(); ed.focus(); } });
$('#mEdit').onclick = () => setMode('edit'); $('#mSplit').onclick = () => setMode('split'); $('#mView').onclick = () => setMode('view'); $('#mMap').onclick = () => setMode('map'); $('#mSplitMap').onclick = () => setMode('splitmap');
$('#q').addEventListener('input', () => { clearTimeout($('#q').t); $('#q').t = setTimeout(refreshList, 200); }); $('#sort').onchange = refreshList;
$('#bSide').onclick = () => $('#side').classList.toggle('show'); $('#bInfo').onclick = () => $('#info').classList.toggle('show');

// ---- メニュー
$('#bMenu').onclick = e => { e.stopPropagation(); const m = $('#menu'); m.style.display = m.style.display === 'block' ? 'none' : 'block'; };
document.addEventListener('click', () => { $('#menu').style.display = 'none'; });
$('#mExport').onclick = () => { location.href = '/api/export'; };
$('#mImport').onclick = () => $('#file').click();
$('#file').onchange = async e => { const files = []; for(const f of e.target.files) files.push({name:f.name, body:await f.text()}); e.target.value = ''; if(!files.length) return; const r = await api('POST', '/api/import', {files}); toast(`${r.done}件を読み込みました`); await refreshTitles(); refreshList(); };
$('#mDel').onclick = async () => { if(!cur || !confirm(`メモ「${cur.title}」を削除します。元に戻せません。よろしいですか？`)) return; dirty = false; const id = cur.id; await api('DELETE', '/api/notes/' + id); cur = null; hist = hist.filter(x => x !== id); hpos = hist.length - 1; showEditor(false); $('#info').innerHTML = ''; titleIn.value = ''; await refreshTitles(); await refreshList(); toast('削除しました'); updNav(); };

// ---- プレビューのクリック
pv.addEventListener('click', async e => {
  const a = e.target.closest('a.wl'); if(a){ e.preventDefault(); return openTitle(a.dataset.t); }
  const t = e.target.closest('a.tag'); if(t){ tagFilter = t.dataset.tag; return refreshList(); }
  if(e.target.matches('input[type=checkbox][data-line]')){
    const lines = ed.value.split('\n'), i = +e.target.dataset.line;
    if(lines[i] !== undefined){ lines[i] = lines[i].replace(/\[([ xX])\]/, (m, c) => c === ' ' ? '[x]' : '[ ]'); const sc = pv.scrollTop; ed.value = lines.join('\n'); renderPreview(); pv.scrollTop = sc; markDirty(); }
  }
});

// ---- エディタ操作
function wrap(l, r){ const s = ed.selectionStart, e = ed.selectionEnd, sel = ed.value.slice(s, e); ed.setRangeText(l + sel + r, s, e, 'end'); if(!sel) ed.setSelectionRange(s + l.length, s + l.length); ed.dispatchEvent(new Event('input')); }
function lineInfo(){ const v = ed.value, p = ed.selectionStart, s = v.lastIndexOf('\n', p - 1) + 1; let e = v.indexOf('\n', p); if(e < 0) e = v.length; return {s, e, line:v.slice(s, e), p}; }
ed.addEventListener('input', () => { renderPreview(); markDirty(); const c = $('#cnt'); if(c) c.textContent = ed.value.length + ' 文字'; checkAc(); });
ed.addEventListener('keydown', e => {
  if(ac.open){ if(e.key === 'ArrowDown' || e.key === 'ArrowUp'){ e.preventDefault(); acMove(e.key === 'ArrowDown' ? 1 : -1); return; } if(e.key === 'Enter' || e.key === 'Tab'){ e.preventDefault(); acPick(); return; } if(e.key === 'Escape'){ e.preventDefault(); acHide(); return; } }
  if((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'b'){ e.preventDefault(); return wrap('**', '**'); }
  if((e.ctrlKey || e.metaKey) && !e.shiftKey && e.key.toLowerCase() === 'i'){ e.preventDefault(); return wrap('*', '*'); }
  if(e.key === 'Tab'){ e.preventDefault(); const {s, line} = lineInfo(); if(e.shiftKey){ const m = /^( {1,2}|\t)/.exec(line); if(m){ ed.setRangeText('', s, s + m[0].length, 'preserve'); ed.dispatchEvent(new Event('input')); } } else if(/^\s*([-*+]|\d+[.)])\s/.test(line)){ const p = ed.selectionStart; ed.setRangeText('  ', s, s, 'preserve'); ed.setSelectionRange(p + 2, p + 2); ed.dispatchEvent(new Event('input')); } else { ed.setRangeText('  ', ed.selectionStart, ed.selectionEnd, 'end'); ed.dispatchEvent(new Event('input')); } return; }
  if(e.key === 'Enter' && !e.shiftKey && !e.isComposing && ed.selectionStart === ed.selectionEnd){
    const {s, line, p} = lineInfo(), m = /^(\s*)([-*+]|\d+[.)])(\s+)(\[[ xX]\]\s+)?(.*)$/.exec(line);
    if(m && p >= s + m[0].length - m[5].length){ e.preventDefault();
      if(!m[5].trim()){ ed.setRangeText('', s, s + line.length, 'end'); }
      else { let mk = m[2]; if(/\d/.test(mk)) mk = (parseInt(mk) + 1) + mk.slice(-1); ed.setRangeText('\n' + m[1] + mk + m[3] + (m[4] ? '[ ] ' : ''), p, p, 'end'); }
      ed.dispatchEvent(new Event('input')); }
  }
});
ed.addEventListener('click', e => { if(!(e.ctrlKey || e.metaKey)) return; const p = ed.selectionStart, v = ed.value; const re = /\[\[([^\[\]|\n]+?)(?:\|[^\[\]\n]*)?\]\]/g; let m; while((m = re.exec(v))){ if(p >= m.index && p <= m.index + m[0].length){ openTitle(m[1].trim()); return; } } });
ed.addEventListener('blur', () => { setTimeout(acHide, 150); flush(); }); ed.addEventListener('scroll', acHide);

// ---- [[ 候補
const ac = {open:false, items:[], sel:0, start:0, q:''};
function caretXY(pos){ const d = document.createElement('div'), cs = getComputedStyle(ed);
  for(const k of ['fontFamily','fontSize','lineHeight','padding','boxSizing','whiteSpace','wordWrap','letterSpacing','tabSize']) d.style[k] = cs[k];
  d.style.cssText += ';position:absolute;visibility:hidden;white-space:pre-wrap;word-wrap:break-word;width:' + ed.clientWidth + 'px;top:0;left:0'; d.style.padding = cs.padding;
  d.textContent = ed.value.slice(0, pos); const sp = document.createElement('span'); sp.textContent = '​'; d.append(sp); document.body.append(d);
  const r = ed.getBoundingClientRect(), x = r.left + sp.offsetLeft - ed.scrollLeft, y = r.top + sp.offsetTop - ed.scrollTop + parseFloat(cs.lineHeight || 24); d.remove(); return {x, y}; }
function checkAc(){
  if(document.activeElement !== ed) return acHide();
  const p = ed.selectionStart, m = /\[\[([^\[\]\n|]*)$/.exec(ed.value.slice(0, p)); if(!m){ return acHide(); }
  const q = m[1].toLowerCase(), all = [...TITLES.keys()].map(k => ({title:notesTitle(k), id:TITLES.get(k)})).filter(t => !cur || t.id !== cur.id);
  let items = all.filter(t => t.title.toLowerCase().includes(q)).sort((a, b) => (b.title.toLowerCase().startsWith(q)) - (a.title.toLowerCase().startsWith(q)) || a.title.localeCompare(b.title)).slice(0, 8);
  if(m[1].trim() && !all.some(t => t.title.toLowerCase() === q.trim()) && !(cur && cur.title.toLowerCase() === q.trim())) items.push({title:m[1].trim(), isNew:true});
  if(!items.length) return acHide();
  ac.open = true; ac.items = items; ac.sel = 0; ac.start = p - m[1].length; renderAc();
}
function notesTitle(k){ return TITLE_CASE.get(k) || k; }
function renderAc(){ const b = $('#ac'); b.innerHTML = ''; ac.items.forEach((it, i) => b.append(h('div', {class:i === ac.sel ? 'sel' : '', onmousedown:e => { e.preventDefault(); ac.sel = i; acPick(); }}, it.isNew ? h('span', {}, '＋ 新規リンク: ', h('b', {}, it.title)) : it.title)));
  const {x, y} = caretXY(ac.start); b.style.display = 'block'; b.style.left = Math.min(x, innerWidth - 350) + 'px'; b.style.top = Math.min(y, innerHeight - 270) + 'px'; }
function acMove(d){ ac.sel = (ac.sel + d + ac.items.length) % ac.items.length; renderAc(); }
function acHide(){ ac.open = false; $('#ac').style.display = 'none'; }
function acPick(){ const it = ac.items[ac.sel]; if(!it) return acHide(); const p = ed.selectionStart; let end = p; if(ed.value.slice(p, p + 2) === ']]') end = p + 2;
  ed.setRangeText(it.title + ']]', ac.start, end, 'end'); acHide(); ed.dispatchEvent(new Event('input')); }

// ---- クイック切替
const qs = $('#qs'), qsi = $('#qsi'); let qsItems = [], qsSel = 0;
function openQs(){ qsi.value = ''; qs.showModal(); drawQs(); qsi.focus(); }
function drawQs(){
  const q = qsi.value.trim().toLowerCase(), L = $('#qsl'); L.innerHTML = '';
  const all = notes.length && !$('#q').value && !tagFilter ? notes : []; const src = [...TITLES.keys()].map(k => ({id:TITLES.get(k), title:notesTitle(k)}));
  qsItems = src.filter(n => !q || n.title.toLowerCase().includes(q)).sort((a, b) => (b.title.toLowerCase().startsWith(q)) - (a.title.toLowerCase().startsWith(q))).slice(0, 12);
  if(q && !src.some(n => n.title.toLowerCase() === q)) qsItems.push({title:qsi.value.trim(), isNew:true});
  qsSel = Math.min(qsSel, qsItems.length - 1); if(qsSel < 0) qsSel = 0;
  qsItems.forEach((it, i) => L.append(h('div', {class:i === qsSel ? 'sel' : '', onclick:() => { qsSel = i; pickQs(); }}, it.isNew ? `＋ 「${it.title}」を新規作成` : it.title)));
}
function pickQs(){ const it = qsItems[qsSel]; if(!it) return; qs.close(); it.isNew ? openTitle(it.title) : go(it.id); }
qsi.addEventListener('input', () => { qsSel = 0; drawQs(); });
qsi.addEventListener('keydown', e => { if(e.key === 'ArrowDown'){ e.preventDefault(); qsSel = Math.min(qsSel + 1, qsItems.length - 1); drawQs(); } else if(e.key === 'ArrowUp'){ e.preventDefault(); qsSel = Math.max(qsSel - 1, 0); drawQs(); } else if(e.key === 'Enter' && !e.isComposing){ e.preventDefault(); pickQs(); } });
qs.addEventListener('click', e => { if(e.target === qs) qs.close(); });
$('#bQs').onclick = openQs;
document.addEventListener('keydown', e => {
  const mod = e.ctrlKey || e.metaKey, k = e.key.toLowerCase();
  if(mod && k === 'k'){ e.preventDefault(); openQs(); }
  else if(mod && k === 'n' && !e.shiftKey){ e.preventDefault(); newNote(); }
  else if(mod && k === 'e'){ e.preventDefault(); setMode(mode === 'view' ? 'split' : mode === 'split' ? 'edit' : 'view'); }
  else if(mod && k === 's'){ e.preventDefault(); flush(); }
});

// ---- グラフ
const gd = $('#gd'); $('#bGraph').onclick = async () => { await flush(); gd.showModal(); drawGraph(); }; $('#gClose').onclick = () => gd.close(); $('#gLocal').onchange = drawGraph;
async function drawGraph(){
  const g = await api('GET', '/api/graph'), svg = $('#gsvg'); svg.innerHTML = '';
  let nodes = g.nodes, edges = g.edges;
  if($('#gLocal').checked && cur){ const adj = new Map(); edges.forEach(([a, b]) => { (adj.get(a) || adj.set(a, new Set()).get(a)).add(b); (adj.get(b) || adj.set(b, new Set()).get(b)).add(a); });
    const keep = new Set([cur.id]); let fr = [cur.id]; for(let d = 0; d < 2; d++){ const nx = []; for(const x of fr) for(const y of adj.get(x) || []) if(!keep.has(y)){ keep.add(y); nx.push(y); } fr = nx; }
    nodes = nodes.filter(n => keep.has(n.id)); edges = edges.filter(([a, b]) => keep.has(a) && keep.has(b)); }
  const N = nodes.length; if(!N) return; const idx = new Map(nodes.map((n, i) => [n.id, i])), deg = new Array(N).fill(0);
  const E = edges.map(([a, b]) => [idx.get(a), idx.get(b)]); E.forEach(([a, b]) => { deg[a]++; deg[b]++; });
  const P = nodes.map((n, i) => ({x:Math.cos(i * 2.4) * (40 + i * 6 % 300), y:Math.sin(i * 2.4) * (40 + i * 6 % 300), vx:0, vy:0}));
  const iters = N > 300 ? 120 : 260;
  for(let it = 0; it < iters; it++){ const cool = 1 - it / iters;
    for(let i = 0; i < N; i++) for(let j = i + 1; j < N; j++){ let dx = P[i].x - P[j].x, dy = P[i].y - P[j].y, d2 = dx * dx + dy * dy + 0.01; if(d2 > 250000) continue; const f = 2600 / d2, d = Math.sqrt(d2); dx /= d; dy /= d; P[i].vx += dx * f; P[i].vy += dy * f; P[j].vx -= dx * f; P[j].vy -= dy * f; }
    for(const [a, b] of E){ const dx = P[b].x - P[a].x, dy = P[b].y - P[a].y, d = Math.sqrt(dx * dx + dy * dy) + 0.01, f = (d - 90) * 0.02; P[a].vx += dx / d * f; P[a].vy += dy / d * f; P[b].vx -= dx / d * f; P[b].vy -= dy / d * f; }
    for(const p of P){ p.vx -= p.x * 0.004; p.vy -= p.y * 0.004; p.x += Math.max(-30, Math.min(30, p.vx)) * cool; p.y += Math.max(-30, Math.min(30, p.vy)) * cool; p.vx *= 0.6; p.vy *= 0.6; } }
  const xs = P.map(p => p.x), ys = P.map(p => p.y), pad = 70, x0 = Math.min(...xs) - pad, y0 = Math.min(...ys) - pad, w = Math.max(...xs) - x0 + pad, hh = Math.max(...ys) - y0 + pad;
  const cw = svg.clientWidth || 900, ch = svg.clientHeight || 600, vw = Math.max(w, cw / 2), vh = Math.max(hh, ch / 2), sc = Math.max(vw / cw, vh / ch);
  svg.setAttribute('viewBox', `${x0 - (vw - w) / 2} ${y0 - (vh - hh) / 2} ${vw} ${vh}`);
  const NS = 'http://www.w3.org/2000/svg', mk = (t, a, txt) => { const e = document.createElementNS(NS, t); for(const k in a) e.setAttribute(k, a[k]); if(txt != null) e.textContent = txt; return e; };
  const css = getComputedStyle(document.body), ink = css.color, accent = getComputedStyle(document.documentElement).getPropertyValue('--accent'), sub = getComputedStyle(document.documentElement).getPropertyValue('--sub');
  const lines = E.map(([a, b]) => { const l = mk('line', {x1:P[a].x, y1:P[a].y, x2:P[b].x, y2:P[b].y, stroke:sub, 'stroke-opacity':.45, 'stroke-width':1.2 * sc}); svg.append(l); return l; });
  nodes.forEach((n, i) => { const r = (5 + Math.min(deg[i], 8) * 1.4) * sc, isCur = cur && n.id === cur.id;
    const gEl = mk('g', {class:'gn', 'data-id':n.id, style:n.ghost ? 'cursor:default' : 'cursor:pointer'});
    gEl.append(mk('circle', {cx:P[i].x, cy:P[i].y, r, fill:isCur ? accent : n.ghost ? 'none' : sub, stroke:n.ghost ? '#c0392b' : isCur ? accent : 'none', 'stroke-dasharray':n.ghost ? '3 2' : '', 'fill-opacity':isCur ? 1 : .75}));
    gEl.append(mk('text', {x:P[i].x, y:P[i].y + r + 13 * sc, 'text-anchor':'middle', 'font-size':12 * sc, fill:ink}, n.title.length > 16 ? n.title.slice(0, 15) + '…' : n.title));
    gEl.onmouseenter = () => lines.forEach((l, k) => { const [a, b] = E[k]; l.setAttribute('stroke', a === i || b === i ? accent : sub); l.setAttribute('stroke-opacity', a === i || b === i ? 1 : .2); });
    gEl.onmouseleave = () => lines.forEach(l => { l.setAttribute('stroke', sub); l.setAttribute('stroke-opacity', .45); });
    gEl.onclick = () => { if(n.ghost){ gd.close(); openTitle(n.title); } else { gd.close(); go(n.id); } };
    svg.append(gEl); });
}
gd.addEventListener('click', e => { if(e.target === gd) gd.close(); });


// ================= マインドマップ
const MM = {k:1, tx:0, ty:0, collapsed:{}, cache:new Map(), token:0, fitted:null, sel:'root', input:null, ct:null, nodes:[], root:null, bb:null,
  view:lsGet('ln.mmview') === 'notes' ? 'notes' : 'outline', depth:+(lsGet('ln.mmdepth') || 2), back:lsGet('ln.mmback') === '1', links:lsGet('ln.mmlinks') === '1'};
function stripMd(s){ return s.replace(/\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]*))?\]\]/g, (m, a, b) => b && b.trim() ? b : a).replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/(\*\*|~~|==|`)/g, '').replace(/(?<![\w*])\*([^*\n]+)\*/g, '$1').replace(/^\s*>\s?/, '').trim(); }
const WIKI_JS = /\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]*))?\]\]/g;
function jsLinks(body){ const t = body.replace(/^(`{3,}|~{3,}).*?^\1[ \t]*$/gms, ' ').replace(/`[^`\n]*`/g, ' '), seen = new Set(), out = [];
  for(const m of t.matchAll(WIKI_JS)){ const x = m[1].trim(), k = x.toLowerCase(); if(x && !seen.has(k)){ seen.add(k); out.push(x); } } return out; }

// ---- Markdown → 見出し/リスト構造
function parseOutline(title, body, noteId){
  const root = {kind:'root', text:title, level:0, kids:[], line:-1, last:-1, noteId}, stack = [root];
  let curH = 0, code = false, table = false; const lines = body.replace(/\r/g, '').split('\n');
  const touch = i => { for(const n of stack) n.last = i; };
  const add = (level, node) => { while(stack.length > 1 && stack[stack.length - 1].level >= level) stack.pop(); stack[stack.length - 1].kids.push(node); stack.push(node); touch(node.line); };
  const mk = (kind, level, raw, line, pre, pl, extra) => {
    const links = [...raw.matchAll(WIKI_JS)].map(m => ({title:m[1].trim(), label:(m[2] && m[2].trim()) || m[1].trim()}));
    const rest = stripMd(raw.replace(/\[\[[^\[\]\n]+?\]\]/g, ' ').replace(/\s+/g, ' ')).replace(/^[-*+]\s*|^\d+[.)]\s*/, '').trim();
    const node = Object.assign({kind, level, kids:[], line, noteId, pl, raw}, extra || {});
    if(links.length === 1 && !rest.replace(/[\s(){}\[\]:：、,。.「」]/g, '')){ node.kind = 'link'; node.link = links[0].title; node.text = pre + links[0].label; node.base = kind; }
    else { node.text = pre + stripMd(raw); links.forEach(l => node.kids.push({kind:'link', sub:true, level:level + 1, link:l.title, text:l.label, kids:[], line, noteId})); }
    return node;
  };
  lines.forEach((ln, i) => {
    if(/^(`{3,}|~{3,})/.test(ln)){ if(!code){ code = true; add(curH + 1, {kind:'code', level:curH + 1, text:'{ } コード', kids:[], line:i, noteId, pl:0, raw:ln}); } else { code = false; touch(i); } return; }
    if(code){ touch(i); return; }
    if(/^\s*\|/.test(ln)){ if(!table){ table = true; add(curH + 1, {kind:'table', level:curH + 1, text:'📊 ' + ln.trim().replace(/^\||\|$/g, '').split('|').map(c => stripMd(c)).join(' | '), kids:[], line:i, noteId, pl:0, raw:ln}); } else touch(i); return; }
    table = false;
    if(/^\s*$/.test(ln) || /^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(ln)) return;
    let m;
    if((m = /^(#{1,6}\s+)(.*)$/.exec(ln))){ const lv = m[1].trim().length, content = m[2].replace(/\s+#+\s*$/, ''), t = stripMd(content);
      if(lv === 1 && !root.kids.length && stack.length === 1 && t.toLowerCase() === title.toLowerCase()){ curH = 1; root.line = i; touch(i); return; }
      curH = lv; add(lv, mk('h', lv, content, i, '', m[1].length)); return; }
    if((m = /^(\s*)([-*+]|\d+[.)])(\s+)(\[[ xX]\]\s+)?(.*)$/.exec(ln))){ const w = m[1].replace(/\t/g, '    ').length, depth = Math.floor(w / 2), pre = m[4] ? (/\[ \]/.test(m[4]) ? '☐ ' : '☑ ') : '';
      add(curH + 1 + depth, mk('item', curH + 1 + depth, m[5], i, pre, m[1].length + m[2].length + m[3].length + (m[4] ? m[4].length : 0), {indent:m[1], marker:m[2]})); return; }
    const q = /^\s{0,3}>\s?/.exec(ln);
    if(q) add(curH + 1, mk('quote', curH + 1, ln.slice(q[0].length), i, '❝ ', q[0].length));
    else add(curH + 1, mk('text', curH + 1, ln.trim(), i, '', /^\s*/.exec(ln)[0].length));
  });
  return root;
}
function assignIds(n, id, parent){ n.id = id; n.parent = parent || null; n.kids.forEach((k, i) => assignIds(k, id + '/' + i, n)); }

// ---- メモ間のつながり → ツリー
async function buildNoteTree(){
  const g = await api('GET', '/api/graph'), byId = new Map(g.nodes.map(n => [n.id, n])), key = t => t.toLowerCase();
  const info = new Map(), out = new Map(), inn = new Map();
  g.nodes.forEach(n => info.set(key(n.title), {title:n.title, id:n.ghost ? null : n.id}));
  const edge = (a, b) => { if(a === b) return; if(!out.has(a)) out.set(a, new Set()); out.get(a).add(b); if(!inn.has(b)) inn.set(b, new Set()); inn.get(b).add(a); };
  const ck = key(cur.title);
  g.edges.forEach(([a, b]) => { const A = byId.get(a), B = byId.get(b); if(A && B && A.id !== cur.id) edge(key(A.title), key(B.title)); });
  info.set(ck, {title:cur.title, id:cur.id});
  for(const t of jsLinks(ed.value)){ const k = key(t); if(!info.has(k)) info.set(k, {title:t, id:TITLES.has(k) ? TITLES.get(k) : null}); edge(ck, k); }
  const root = {kind:'root', text:cur.title, link:cur.title, nid:cur.id, level:0, kids:[], noteId:cur.id}, seen = new Set([ck]);
  const q = [[root, ck, 0]];
  while(q.length){ const [node, k, d] = q.shift(); if(d >= MM.depth) continue;
    const kids = [...(out.get(k) || [])].map(x => [x, false]); if(MM.back) for(const x of inn.get(k) || []) if(!(out.get(k) || new Set()).has(x)) kids.push([x, true]);
    for(const [x, back] of kids.slice(0, 40)){ const inf = info.get(x), dup = seen.has(x);
      const child = {kind:'note', link:inf.title, nid:inf.id, back, dup, text:(dup ? '↺ ' : '') + (back ? '← ' : '') + inf.title, level:d + 1, kids:[], noteId:inf.id}; node.kids.push(child);
      if(!dup){ seen.add(x); q.push([child, x, d + 1]); } }
  }
  return root;
}
async function mmExpandLinks(root){
  const jobs = [];
  (function walk(n){ if(n.kind === 'link' && n.link){ const id = TITLES.get(n.link.toLowerCase()); if(id != null && id !== cur.id) jobs.push([n, id]); } n.kids.forEach(walk); })(root);
  await Promise.all(jobs.map(async ([n, id]) => { let d = MM.cache.get(id); if(!d){ d = await api('GET', '/api/notes/' + id); if(d.id) MM.cache.set(id, d); } if(!d || !d.id) return;
    const sub = parseOutline(d.title, d.body, id); n.kids = sub.kids.concat(n.kids); n.foreign = true; }));
}

// ---- 描画
const cw13 = ch => /[　-鿿＀-￯぀-ヿ]/.test(ch) ? 13.5 : 7.4;
const LH = 17, WRAPW = 300;
function wrapText(t, max = WRAPW){ const lines = []; let cur = '', w = 0, sp = -1;
  for(const ch of t){ if(ch === '\n'){ lines.push(cur); cur = ''; w = 0; sp = -1; continue; }
    const cw = cw13(ch);
    if(w + cw > max && cur){ if(sp > cur.length * .4 && ch !== ' '){ lines.push(cur.slice(0, sp)); cur = cur.slice(sp + 1); } else { lines.push(cur); cur = ''; } w = 0; for(const c of cur) w += cw13(c); sp = -1; }
    if(ch === ' ') sp = cur.length; cur += ch; w += cw; }
  lines.push(cur); let mw = 0; for(const l of lines){ let x = 0; for(const c of l) x += cw13(c); mw = Math.max(mw, x); } return {lines, w:mw}; }
function mmKey(n){ if(n.kind === 'root') return 'root'; if(MM.view === 'outline' && n.noteId === cur.id && !n.sub && n.line != null && n.line >= 0) return 'L' + n.line; return n.id; }
const mmCol = () => { const k = cur.id + ':' + MM.view; return MM.collapsed[k] || (MM.collapsed[k] = new Set()); };
async function renderMap(fit){
  if(!cur) return; const tok = ++MM.token, svg = $('#mmsvg');
  let root;
  if(MM.view === 'notes') root = await buildNoteTree();
  else { root = parseOutline(cur.title, ed.value, cur.id); if(MM.links) await mmExpandLinks(root); }
  if(tok !== MM.token) return;
  assignIds(root, 'r'); const col = mmCol();
  const css = getComputedStyle(document.documentElement), C = n => css.getPropertyValue(n).trim() || '#888', ink = getComputedStyle(document.body).color;
  const colors = {accent:C('--accent'), hi:C('--hi'), card:C('--card'), line:C('--line'), link:C('--link'), miss:C('--miss'), sub:C('--sub')};
  const ROW = 38, GAP = 54, colW = []; let y = 0;
  const vis = n => col.has(n.id) ? [] : n.kids;
  (function measure(n, d){ n.d = d; const f = wrapText(n.text || ''); n.lines = f.lines; n.h = Math.max(30, f.lines.length * LH + 13); n.w = Math.max(54, f.w + (n.kind === 'link' ? 40 : 26) + (n.kids.length ? 16 : 0)); colW[d] = Math.max(colW[d] || 0, n.w); vis(n).forEach(k => measure(k, d + 1)); })(root, 0);
  (function place(n){ const v = vis(n); if(!v.length){ n.y = y + n.h / 2; y += n.h + 8; } else { v.forEach(place); n.y = (v[0].y + v[v.length - 1].y) / 2; } })(root);
  const xs = [0]; for(let d = 1; d < colW.length; d++) xs[d] = xs[d - 1] + colW[d - 1] + GAP;
  const NS = 'http://www.w3.org/2000/svg', mkE = (t, a, txt) => { const e = document.createElementNS(NS, t); for(const k in a) e.setAttribute(k, a[k]); if(txt != null) e.textContent = txt; return e; };
  svg.innerHTML = ''; const g = mkE('g', {id:'mmg'}); svg.append(g);
  const nodes = []; (function all(n){ nodes.push(n); vis(n).forEach(all); })(root); nodes.forEach(n => { n.x = xs[n.d]; });
  const palette = [colors.accent, '#2f7fd1', '#2e9d6a', '#a557c9', '#d1982f', '#d1514a'], br = n => palette[(n.id.split('/').length > 1 ? +n.id.split('/')[1] : 0) % palette.length];
  for(const n of nodes) for(const k of vis(n)){ const x1 = n.x + n.w, y1 = n.y, x2 = k.x, y2 = k.y, mx = (x1 + x2) / 2;
    g.append(mkE('path', {d:`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, fill:'none', stroke:br(k), 'stroke-opacity':k.back ? .3 : .55, 'stroke-width':k.d <= 2 ? 2.2 : 1.5, 'stroke-dasharray':k.back ? '5 4' : ''})); }
  MM.byKey = new Map();
  for(const n of nodes){
    const key = mmKey(n); MM.byKey.set(key, n); const h2 = n.h / 2;
    const isLink = n.kind === 'link', isNote = n.kind === 'note', missing = (isLink && n.link && !TITLES.has(n.link.toLowerCase())) || (isNote && n.nid == null);
    const gr = mkE('g', {class:'mmn', 'data-key':key}), branch = br(n);
    const fill = n.kind === 'root' ? colors.accent : (n.kind === 'h' || isNote) ? colors.hi : colors.card;
    const stroke = n.kind === 'root' ? colors.accent : missing ? colors.miss : isLink ? colors.link : n.dup ? colors.sub : (n.kind === 'h' || isNote) ? branch : colors.line;
    gr.append(mkE('rect', {class:'selr', x:n.x - 4, y:n.y - h2 - 4, width:n.w + 8, height:h2 * 2 + 8, rx:12, fill:'none', stroke:colors.accent, 'stroke-width':3, opacity:0}));
    gr.append(mkE('rect', {class:'mb', x:n.x, y:n.y - h2, width:n.w, height:h2 * 2, rx:9, fill, stroke, 'stroke-width':n.kind === 'h' || isLink || isNote ? 1.8 : 1.2, 'stroke-dasharray':missing || n.dup ? '4 3' : ''}));
    const tcol = n.kind === 'root' ? '#fff' : missing ? colors.miss : isLink ? colors.link : ink;
    const txt = mkE('text', {x:n.x + 11 + (isLink ? 16 : 0), y:n.y - h2 + 17, 'font-size':13, fill:tcol, 'font-weight':n.kind === 'root' || n.kind === 'h' || isNote ? 700 : 400}, ''); for(let i = 0; i < n.lines.length; i++) txt.append(mkE('tspan', {x:n.x + 11 + (isLink ? 16 : 0), y:n.y - h2 + 13 + LH * i - 1 + 4}, n.lines[i] || ' '));
    if(isLink) gr.append(mkE('text', {x:n.x + 9, y:n.y + 4.5, 'font-size':12, fill:tcol}, '🔗'));
    gr.append(txt); if(n.foreign) gr.setAttribute('data-foreign', '1');
    gr.addEventListener('click', e => { e.stopPropagation(); if(MM.noClick) return; mmClickNode(n); });
    gr.addEventListener('dblclick', e => { e.stopPropagation(); clearTimeout(MM.ct); mmEdit(n); });
    g.append(gr);
    if(n.kids.length){ const cx = n.x + n.w + 1, isC = col.has(n.id), tg = mkE('g', {style:'cursor:pointer', 'data-tog':n.id});
      tg.append(mkE('circle', {cx, cy:n.y, r:8, fill:colors.card, stroke:branch, 'stroke-width':1.6}));
      tg.append(mkE('text', {x:cx, y:n.y + 4, 'text-anchor':'middle', 'font-size':12, fill:branch, 'font-weight':700}, isC ? '+' : '−'));
      tg.addEventListener('click', e => { e.stopPropagation(); isC ? col.delete(n.id) : col.add(n.id); renderMap(false); }); g.append(tg); }
  }
  MM.bb = {w:(xs[xs.length - 1] || 0) + (colW[colW.length - 1] || 0), h:Math.max(y - 8, ROW)}; MM.nodes = nodes; MM.root = root;
  mmMark();
  const fk = cur.id + ':' + MM.view;
  if(fit || MM.fitted !== fk){ MM.fitted = fk; mmFit(); } else mmApply();
}
function mmSelNode(){ return (MM.byKey && MM.byKey.get(MM.sel)) || MM.root; }
function mmMark(){ document.querySelectorAll('#mmsvg .mmn').forEach(g => { const r = g.querySelector('.selr'); if(r) r.setAttribute('opacity', g.dataset.key === MM.sel ? 1 : 0); }); }
function mmApply(){ const g = $('#mmg'); if(g) g.setAttribute('transform', `translate(${MM.tx},${MM.ty}) scale(${MM.k})`); const p = Math.round(MM.k * 100); $('#mmZv').textContent = p + '%'; $('#mmZ').value = p; }
function mmFit(){ const svg = $('#mmsvg'), W = svg.clientWidth || 800, H = svg.clientHeight || 500, bb = MM.bb; if(!bb) return;
  MM.k = Math.min(1.2, Math.max(.12, Math.min((W - 80) / (bb.w + 20), (H - 130) / (bb.h + 20)))); MM.tx = (W - bb.w * MM.k) / 2; MM.ty = 100 + (H - 100 - bb.h * MM.k) / 2; mmApply(); }
function mmZoom(f, cx, cy){ const svg = $('#mmsvg'); cx = cx ?? svg.clientWidth / 2; cy = cy ?? svg.clientHeight / 2; const nk = Math.min(3, Math.max(.1, MM.k * f)); MM.tx = cx - (cx - MM.tx) * nk / MM.k; MM.ty = cy - (cy - MM.ty) * nk / MM.k; MM.k = nk; mmApply(); }

// ---- クリック
function mmClickNode(n){
  MM.sel = mmKey(n); mmMark(); $('#mmsvg').focus(); clearTimeout(MM.ct);
  const nav = n.kind !== 'root' && (n.kind === 'link' || n.kind === 'note') && n.link;
  if(nav){ MM.ct = setTimeout(() => { const id = TITLES.get(n.link.toLowerCase()); id != null ? go(id) : openTitle(n.link); }, 260); return; }
  if(MM.view === 'outline' && n.noteId && n.noteId !== cur.id){ MM.ct = setTimeout(() => go(n.noteId), 260); return; }
  if(mode === 'splitmap' && MM.view === 'outline' && n.line != null && n.line >= 0){ const lines = ed.value.split('\n'); let off = 0; for(let i = 0; i < n.line; i++) off += lines[i].length + 1; ed.setSelectionRange(off, off + (lines[n.line] || '').length); ed.scrollTop = Math.max(0, (n.line - 4) * 24.5); }
}

// ---- 編集（インライン入力）
function mmInput(o){
  if(MM.input){ MM.input.blur(); }
  const inp = h('input', {class:'mmedit', value:o.value, placeholder:o.placeholder || ''}); inp.style.left = o.x + 'px'; inp.style.top = o.y + 'px'; inp.style.width = o.w + 'px';
  $('#mm').append(inp); MM.input = inp; inp.focus(); inp.select(); let done = false;
  const fin = (how, cancel) => { if(done) return; done = true; if(MM.input === inp) MM.input = null; const v = inp.value; inp.remove(); if(cancel){ o.onCancel && o.onCancel(); } else o.onCommit(v, how); if(how !== 'blur') $('#mmsvg').focus(); };
  inp.addEventListener('keydown', e => { e.stopPropagation(); if(e.isComposing || e.keyCode === 229) return;
    if(e.key === 'Enter'){ e.preventDefault(); fin('enter'); } else if(e.key === 'Tab'){ e.preventDefault(); fin('tab'); } else if(e.key === 'Escape'){ e.preventDefault(); fin('esc', true); } });
  inp.addEventListener('blur', () => fin('blur'));
}
function nodeBox(n, dx = 0){ return {x:(n.x + dx) * MM.k + MM.tx, y:(n.y - n.h / 2) * MM.k + MM.ty, w:Math.max(200, n.w * MM.k + 30)}; }
function applyLines(lines){ ed.value = lines.join('\n'); ed.dispatchEvent(new Event('input')); }
const editable = n => MM.view === 'outline' && n.noteId === cur.id && !n.sub && (n.kind === 'root' || (n.line != null && n.line >= 0));
function mmEdit(n, fresh){
  if(MM.view === 'notes') return mmEditNote(n);
  if(n.kind === 'root'){ const b = nodeBox(n); return mmInput({...b, value:cur.title, onCommit:(v, how) => { v = v.trim(); if(v && v !== cur.title){ titleIn.value = v; titleIn.dispatchEvent(new Event('input')); } if(how === 'enter' || how === 'tab') mmAdd(n, 'child'); }}); }
  if(n.sub){ return toast('この行の文章を編集してください（リンクは行の中に書かれています）'); }
  if(n.noteId !== cur.id){ return toast('他のメモの内容です。クリックでそのメモを開いて編集してください'); }
  if(n.kind === 'code' || n.kind === 'table'){ return toast('コード・表はテキスト側で編集してください'); }
  const b = nodeBox(n), line = n.line;
  mmInput({...b, value:n.raw, placeholder:'テキストを入力',
    onCommit:async (v, how) => { const lines = ed.value.split('\n'); const text = v.replace(/\n/g, ' ');
      if(!text.trim()){ if(fresh){ lines.splice(line, 1); applyLines(lines); MM.sel = n.parent ? mmKey(n.parent) : 'root'; await renderMap(false); } return; }
      if(lines[line] !== undefined && text !== n.raw){ lines[line] = lines[line].slice(0, n.pl) + text; applyLines(lines); }
      await renderMap(false); const nn = MM.byKey.get('L' + line); if(!nn) return;
      if(how === 'enter') mmAdd(nn, 'sibling'); else if(how === 'tab') mmAdd(nn, 'child'); },
    onCancel:async () => { if(fresh){ const lines = ed.value.split('\n'); lines.splice(line, 1); applyLines(lines); MM.sel = n.parent ? mmKey(n.parent) : 'root'; await renderMap(false); } }});
}
function ownLast(n){ for(let i = n.kids.length - 1; i >= 0; i--){ const k = n.kids[i]; if(!k.sub && k.noteId === cur.id && k.line >= 0) return k; } return null; }
function sibPlan(s){ if(s.kind === 'h') return {at:s.last + 1, text:'#'.repeat(s.level) + ' '};
  const base = s.kind === 'link' ? s.base : s.kind;
  if(base === 'h') return {at:s.last + 1, text:'#'.repeat(s.level) + ' '};
  if(base === 'item') return {at:s.last + 1, text:(s.indent || '') + (s.marker || '-') + ' '};
  return {at:s.last + 1, text:'- '}; }
function childPlan(n){
  const base = n.kind === 'link' ? n.base : n.kind;
  const lk = ownLast(n); if(lk && n.kind !== 'root'){ const p = sibPlan(lk); p.at = n.last + 1; return p; }
  if(n.kind === 'root'){ if(lk){ const p = sibPlan(lk); p.at = n.last + 1; return p; }
    return {at:n.last + 1, text:n.line >= 0 ? '## ' : '- '}; }
  if(base === 'h') return {at:n.last + 1, text:n.level <= 2 ? '#'.repeat(n.level + 1) + ' ' : '- '};
  if(base === 'item') return {at:n.last + 1, text:(n.indent || '') + '  ' + (/\d/.test(n.marker || '') ? '1.' : '-') + ' '};
  return {at:n.last + 1, text:'- '};
}
async function mmAdd(n, as){
  if(MM.view === 'notes') return mmAddNote(as === 'sibling' && n.kind !== 'root' ? (n.parent || n) : n);
  if(as === 'sibling' && n.kind === 'root') as = 'child';
  if(!(n.kind === 'root' || editable(n))) return toast('このノードには追加できません');
  const plan = as === 'child' ? childPlan(n) : sibPlan(n), lines = ed.value.split('\n');
  if(plan.at >= lines.length && lines.length === 1 && lines[0] === '') lines.pop();
  lines.splice(plan.at, 0, plan.text); applyLines(lines);
  await renderMap(false); const nn = MM.byKey.get('L' + plan.at); if(!nn) return;
  if(MM.collapsed[cur.id + ':outline']) { let p = nn.parent; while(p){ MM.collapsed[cur.id + ':outline'].delete(p.id); p = p.parent; } await renderMap(false); }
  const nn2 = MM.byKey.get('L' + plan.at) || nn; MM.sel = 'L' + plan.at; mmMark(); mmEdit(nn2, true);
}
async function mmDelete(n){
  if(MM.view === 'notes') return toast('メモそのものの削除は右上の「⋯」メニューから行えます');
  if(n.kind === 'root') return toast('メモの中心は削除できません');
  if(!editable(n)) return toast('このノードは削除できません');
  const cnt = n.last - n.line + 1; if(n.kids.length && !confirm(`「${(n.text || '').slice(0, 30)}」と、その下の項目（${cnt}行）を削除します。よろしいですか？`)) return;
  const lines = ed.value.split('\n'); lines.splice(n.line, cnt); applyLines(lines); MM.sel = n.parent ? mmKey(n.parent) : 'root'; await renderMap(false);
}
// ---- メモ間ビューでの編集
function mmEditNote(n){
  if(n.nid == null) return toast('まだ作成されていないメモです。クリックすると作成できます');
  const b = nodeBox(n);
  mmInput({...b, value:n.link, placeholder:'メモ名', onCommit:async (v, how) => { v = v.trim();
    if(v && v !== n.link){
      if(n.nid === cur.id){ titleIn.value = v; titleIn.dispatchEvent(new Event('input')); await flush(); }
      else { const d = await api('GET', '/api/notes/' + n.nid); const r = await api('PUT', '/api/notes/' + n.nid, {title:v, rev:d.rev}); if(r.ok === false) return toast(r.error); toast('名前を変更しました'); }
      MM.cache.clear(); await refreshTitles(); await refreshList(); if(cur) { const d2 = await api('GET', '/api/notes/' + cur.id); if(d2.id){ cur.title = d2.title; cur.rev = d2.rev; if(!dirty){ ed.value = d2.body; renderPreview(); } } }
      await renderMap(false); }
    if(how === 'enter' && n.kind !== 'root') mmAddNote(n.parent || n); else if(how === 'tab') mmAddNote(n); }});
}
async function mmAddNote(p){
  if(!p || p.nid == null) return toast('未作成のメモには追加できません。先にクリックして作成してください');
  const b = nodeBox(p, 40); b.x = (p.x + p.w + 30) * MM.k + MM.tx;
  mmInput({...b, value:'', placeholder:'新しいメモ名（既存のメモ名ならリンクだけ追加）', onCommit:async (v, how) => {
    const t = v.replace(/[\[\]|]/g, ' ').replace(/\s+/g, ' ').trim(); if(!t) return;
    const o = await api('POST', '/api/open', {title:t}); if(o.ok === false) return toast(o.error);
    let body, rev; if(p.nid === cur.id){ body = ed.value; } else { const d = await api('GET', '/api/notes/' + p.nid); body = d.body; rev = d.rev; }
    if(jsLinks(body).some(x => x.toLowerCase() === t.toLowerCase())) toast('すでにリンクされています');
    else { const nb = body + (body && !body.endsWith('\n') ? '\n' : '') + `- [[${t}]]\n`;
      if(p.nid === cur.id){ ed.value = nb; ed.dispatchEvent(new Event('input')); await flush(); }
      else { const r = await api('PUT', '/api/notes/' + p.nid, {body:nb, rev}); if(r.ok === false) return toast(r.error); } }
    MM.cache.clear(); await refreshTitles(); await refreshList(); await renderMap(false);
    if(how === 'enter') mmAddNote(p); else if(how === 'tab'){ const nn = MM.nodes.find(x => x.kind === 'note' && x.link.toLowerCase() === t.toLowerCase() && !x.dup); if(nn) mmAddNote(nn); } }});
}


// ---- ブランチのコピー&ペースト
function clipParse(text){
  const lines = text.replace(/\r\n?/g, '\n').replace(/\n+$/, '').split('\n'); let code = false, minL = 9, minI = 1e9, first = null, any = false;
  const kinds = lines.map(ln => { if(/^(`{3,}|~{3,})/.test(ln)){ code = !code; return 'c'; } if(code) return 'c';
    let m = /^(#{1,6})\s/.exec(ln); if(m){ minL = Math.min(minL, m[1].length); first = first || 'h'; any = true; return 'h'; }
    m = /^(\s*)(?:[-*+]|\d+[.)])\s/.exec(ln); if(m){ minI = Math.min(minI, indW(m[1])); first = first || 'item'; any = true; return 'i'; } return 't'; });
  return {lines, kinds, minL, minI, first: first || 'text', any};
}
function clipFit(text, ctx){
  const c = clipParse(text); if(!text.trim()) return {err:'クリップボードが空です'};
  let out;
  if(!c.any){ out = c.lines.filter(l => l.trim()).map(l => ' '.repeat(ctx.indent) + '- ' + l.trim()); }
  else if(c.first === 'h'){
    if(ctx.level == null) return {err:'見出しは、見出しか中心のノードの下にだけ貼り付けできます'};
    const d = ctx.level - c.minL; out = c.lines.map((l, i) => { if(c.kinds[i] !== 'h') return l; const m = /^(#{1,6})/.exec(l); return '#'.repeat(Math.min(6, Math.max(1, m[1].length + d))) + l.slice(m[1].length); }); }
  else { const ws = c.minI === 1e9 ? 0 : c.minI;
    out = c.lines.map((l, i) => { if(c.kinds[i] === 'c' || !l.trim()) return l; const ind = /^\s*/.exec(l)[0]; return ' '.repeat(Math.max(0, indW(ind) - ws + ctx.indent)) + l.slice(ind.length); }); }
  return {lines: out, head: c.first === 'h'};
}
function mmCopyText(n){
  if(MM.view === 'notes'){ if(n.kind === 'root' || !n.link) return {err:'コピーできるのはメモのノードです（メモへのリンクがコピーされます）'}; return {text:`- [[${n.link}]]`}; }
  if(n.sub) return {err:'この行の文章をテキスト側でコピーしてください'};
  if(n.kind !== 'root' && (n.noteId !== cur.id || n.line == null || n.line < 0)) return {err:'他のメモの内容です。そのメモを開いてコピーしてください'};
  const lines = ed.value.split('\n'), a = n.kind === 'root' ? 0 : n.line, t = lines.slice(a, n.last + 1).join('\n');
  return t.trim() ? {text:t} : {err:'コピーする内容がありません'};
}
async function mmCopy(n, cut){
  const r = mmCopyText(n); if(r.err) return toast(r.err);
  if(cut){ if(MM.view === 'notes') return toast('メモの移動はドラッグ&ドロップで行えます'); if(n.kind === 'root') return toast('メモの中心は切り取れません'); if(!editable(n)) return toast('このノードは切り取れません'); }
  MM.clip = r.text; try{ await navigator.clipboard.writeText(r.text); }catch(e){}
  if(cut){ const lines = ed.value.split('\n'); lines.splice(n.line, n.last - n.line + 1); applyLines(lines); MM.sel = n.parent ? mmKey(n.parent) : 'root'; await renderMap(false); toast('切り取りました（別の場所に貼り付けできます）'); }
  else toast(`コピーしました（${r.text.split('\n').length}行）。貼り付けたい場所のノードを選んで Ctrl+V`);
}
async function mmPaste(n, as, text){
  text = text != null ? text : MM.clip; if(!text) return toast('先にコピーまたは切り取りをしてください');
  if(MM.view === 'notes'){
    if(n.nid == null) return toast('未作成のメモには貼り付けできません');
    const f = clipFit(text, {indent:0, level:2}); if(f.err) return toast(f.err);
    const d = n.nid === cur.id ? {body:ed.value} : await api('GET', '/api/notes/' + n.nid); if(d.ok === false) return toast(d.error);
    const nb = d.body + (d.body && !d.body.endsWith('\n') ? '\n' : '') + f.lines.join('\n') + '\n';
    if(n.nid === cur.id){ ed.value = nb; ed.dispatchEvent(new Event('input')); await flush(); }
    else { const r = await api('PUT', '/api/notes/' + n.nid, {body:nb, rev:d.rev}); if(r.ok === false) return toast(r.error); }
    MM.cache.clear(); await refreshTitles(); await refreshList(); await renderMap(false); return toast(`「${n.link || cur.title}」の末尾に貼り付けました`);
  }
  if(as === 'sibling' && n.kind === 'root') as = 'child';
  const P = as === 'child' ? n : n.parent;
  if(!P || !(P.kind === 'root' || (P.noteId === cur.id && !P.sub && P.line >= 0)) || (n.kind !== 'root' && (n.noteId !== cur.id || n.sub || n.line == null || n.line < 0))) return toast('ここには貼り付けできません（他のメモの中身は、そのメモを開いて貼り付けてください）');
  const pb = P.kind === 'root' ? 'root' : baseOf(P); if(!['root', 'h', 'item'].includes(pb)) return toast('段落・表・コードの下には貼り付けできません');
  const nb = as === 'sibling' ? baseOf(n) : null, ctx = {};
  ctx.level = as === 'sibling' && nb === 'h' ? n.level : pb === 'root' ? rootHeadLevel(P, null) : pb === 'h' ? Math.min(6, P.level + 1) : null;
  ctx.indent = as === 'sibling' && nb === 'item' ? indW(n.indent) : as === 'child' && pb === 'item' ? indW(P.indent) + 2 : 0;
  const f = clipFit(text, ctx); if(f.err) return toast(f.err);
  const lines = ed.value.split('\n'); let at;
  if(as === 'sibling') at = n.last + 1;
  else if(f.head) at = P.last < 0 ? lines.length : P.last + 1;
  else { const fh = P.kids.find(k => !k.sub && k.noteId === cur.id && k.line >= 0 && baseOf(k) === 'h'); at = fh ? fh.line : (P.last < 0 ? lines.length : P.last + 1); }
  if(lines.length === 1 && lines[0] === ''){ lines.pop(); at = 0; }
  lines.splice(at, 0, ...f.lines); const col = mmCol(); let q = P; while(q){ col.delete(q.id); q = q.parent; }
  applyLines(lines); MM.sel = 'L' + at; await renderMap(false); toast(`貼り付けました（${f.lines.length}行）`);
}

// ---- ドラッグ&ドロップで付け替え
const baseOf = n => n.kind === 'link' ? (n.base || 'item') : n.kind;
const indW = s => (s || '').replace(/\t/g, '    ').length;
function inSubtree(a, b){ for(let x = b; x; x = x.parent) if(x === a) return true; return false; }
function resolveDrop(src, tgt, zone){
  if(!src || !tgt) return {err:'移動先がありません'};
  if(src.kind === 'root') return {err:'中心のノードは動かせません'};
  if(MM.view === 'notes'){
    if(src.back) return {err:'被リンク（←）のノードは、元のメモ側で付け替えてください'};
    if(src.nid == null || !src.parent) return {err:'未作成のメモは付け替えできません'};
    if(tgt.nid == null) return {err:'未作成のメモの下には移動できません'};
    if(tgt === src || tgt.nid === src.nid) return {err:'自分自身の下には移動できません'};
    if(tgt === src.parent) return {err:'すでにこの下にあります'};
    return {notes:true, src, from:src.parent, to:tgt};
  }
  if(src.noteId !== cur.id || src.sub || src.line == null || src.line < 0) return {err:'このノードは移動できません'};
  const isHead = baseOf(src) === 'h'; let P, ref = null, side = null;
  if(zone === 'child') P = tgt; else { P = tgt.parent; ref = tgt; side = zone; }
  if(!P) return {err:'移動先が正しくありません'};
  if(tgt.noteId !== cur.id || tgt.sub || (tgt.kind !== 'root' && (tgt.line == null || tgt.line < 0))) return {err:'他のメモの中には移動できません'};
  if(inSubtree(src, P)) return {err:'自分の下には移動できません'};
  const pb = P.kind === 'root' ? 'root' : baseOf(P);
  if(!['root', 'h', 'item'].includes(pb)) return {err:'段落・表・コードの下には移動できません'};
  if(isHead && pb === 'item') return {err:'見出しは、見出しか中心のノードの下にだけ移動できます'};
  if(ref && (baseOf(ref) === 'h') !== isHead) ref = null;
  return {src, P, ref, side, isHead, pb};
}
const rootHeadLevel = (P, src) => { const hs = P.kids.filter(k => k !== src && !k.sub && baseOf(k) === 'h' && k.noteId === cur.id).map(k => k.level); return hs.length ? Math.min(...hs) : (P.line >= 0 ? 2 : 1); };
async function mmMove(plan){
  const {src, P, ref, side, isHead, pb} = plan, lines = ed.value.split('\n'), s0 = src.line, s1 = src.last; let at;
  if(ref) at = side === 'before' ? ref.line : ref.last + 1;
  else if(isHead) at = P.kind === 'root' ? (P.last < 0 ? lines.length : P.last + 1) : P.last + 1;
  else { const fh = P.kids.find(k => k !== src && !k.sub && k.noteId === cur.id && k.line >= 0 && baseOf(k) === 'h'); at = fh ? fh.line : (P.last < 0 ? 0 : P.last + 1); }
  if(at >= s0 && at <= s1 + 1) return toast('位置は変わりません');
  const block = lines.slice(s0, s1 + 1), nodes = [];
  (function w(n){ if(n.line >= 0 && n.noteId === cur.id && !n.sub) nodes.push(n); n.kids.forEach(w); })(src);
  if(isHead){ const lvl = ref ? ref.level : (P.kind === 'root' ? rootHeadLevel(P, src) : P.level + 1), delta = lvl - src.level;
    for(const n of nodes) if(baseOf(n) === 'h'){ const i = n.line - s0, m = /^(#{1,6})/.exec(block[i]); if(m) block[i] = '#'.repeat(Math.min(6, Math.max(1, m[1].length + delta))) + block[i].slice(m[1].length); } }
  else if(baseOf(src) === 'item'){ const baseW = ref ? (baseOf(ref) === 'item' ? indW(ref.indent) : 0) : (pb === 'item' ? indW(P.indent) + 2 : 0), ws = indW(src.indent);
    for(const n of nodes) if(baseOf(n) === 'item'){ const i = n.line - s0, ind = n.indent || ''; block[i] = ' '.repeat(Math.max(0, indW(ind) - ws + baseW)) + block[i].slice(ind.length); } }
  lines.splice(s0, block.length); if(at > s1) at -= block.length; lines.splice(at, 0, ...block);
  const col = mmCol(); let p = P; while(p){ col.delete(p.id); p = p.parent; }
  applyLines(lines); MM.sel = 'L' + at; await renderMap(false); toast('付け替えました');
}
function unlinkIn(body, title){
  const esc2 = title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), only = new RegExp('^\\s*(?:[-*+]|\\d+[.)])\\s+(?:\\[[ xX]\\]\\s+)?\\[\\[\\s*' + esc2 + '\\s*(?:\\|[^\\]\\n]*)?\\]\\]\\s*$', 'i'), any = new RegExp('\\[\\[\\s*' + esc2 + '\\s*(?:\\|([^\\]\\n]*))?\\]\\]', 'gi');
  let code = false; const out = [];
  for(const ln of body.split('\n')){ if(/^(`{3,}|~{3,})/.test(ln)){ code = !code; out.push(ln); continue; } if(code){ out.push(ln); continue; }
    if(only.test(ln)) continue; out.push(ln.replace(any, (m, a) => (a && a.trim()) || title)); }
  return out.join('\n');
}
async function mmMoveNote(plan){
  const {src, from, to} = plan, title = src.link, key = title.toLowerCase();
  const load = async n => n.nid === cur.id ? {body:ed.value, rev:null} : await api('GET', '/api/notes/' + n.nid);
  const save = async (n, body, rev) => { if(n.nid === cur.id){ ed.value = body; ed.dispatchEvent(new Event('input')); await flush(); return {ok:true}; } return await api('PUT', '/api/notes/' + n.nid, {body, rev}); };
  const A = await load(from); if(A.ok === false) return toast(A.error);
  let r = await save(from, unlinkIn(A.body, title), A.rev); if(r.ok === false) return toast(r.error);
  const B = await load(to); if(B.ok === false) return toast(B.error);
  if(!jsLinks(B.body).some(x => x.toLowerCase() === key)){ r = await save(to, B.body + (B.body && !B.body.endsWith('\n') ? '\n' : '') + `- [[${title}]]\n`, B.rev); if(r.ok === false) return toast(r.error); }
  MM.cache.clear(); await refreshTitles(); await refreshList(); await renderMap(false); toast(`「${title}」を「${to.link}」の下へ付け替えました`);
}
function dropTarget(e){
  const svg = $('#mmsvg'), r = svg.getBoundingClientRect(), mx = (e.clientX - r.left - MM.tx) / MM.k, my = (e.clientY - r.top - MM.ty) / MM.k;
  for(const n of MM.nodes) if(mx >= n.x - 8 && mx <= n.x + n.w + 8 && my >= n.y - n.h / 2 - 2 && my <= n.y + n.h / 2 + 2){
    const dy = my - n.y; return {n, zone:MM.view === 'notes' ? 'child' : (dy < -6 ? 'before' : dy > 6 ? 'after' : 'child')}; }
  return null;
}
function dropMark(t, plan){
  const g = $('#mmg'); if(!g) return; let d = $('#mmdrop'); if(d) d.remove(); if(!t) return;
  const NS = 'http://www.w3.org/2000/svg', n = t.n, col = plan && plan.err ? '#c0392b' : getComputedStyle(document.documentElement).getPropertyValue('--accent').trim();
  d = document.createElementNS(NS, 'g'); d.setAttribute('id', 'mmdrop'); d.setAttribute('pointer-events', 'none');
  const mk = (tag, a) => { const e = document.createElementNS(NS, tag); for(const k in a) e.setAttribute(k, a[k]); d.append(e); };
  if(t.zone === 'child') mk('rect', {x:n.x - 4, y:n.y - n.h / 2 - 4, width:n.w + 8, height:n.h + 8, rx:12, fill:col, 'fill-opacity':.18, stroke:col, 'stroke-width':2.5, 'stroke-dasharray':'5 3'});
  else { const yy = t.zone === 'before' ? n.y - n.h / 2 - 4 : n.y + n.h / 2 + 4; mk('line', {x1:n.x - 6, y1:yy, x2:n.x + n.w + 6, y2:yy, stroke:col, 'stroke-width':4, 'stroke-linecap':'round'}); mk('circle', {cx:n.x - 6, cy:yy, r:5, fill:col}); }
  g.append(d);
}
const drag = {cand:null, on:false, ghost:null, t:null, plan:null};
function dragMove(e){
  const c = drag.cand; if(!c) return false;
  if(!drag.on){ if(Math.hypot(e.clientX - c.x, e.clientY - c.y) < 6) return false;
    drag.on = true; try{ $('#mmsvg').setPointerCapture(e.pointerId); }catch(x){} const gh = document.createElement('div'); gh.className = 'mmghost'; gh.textContent = c.node.text || '(空)'; $('#mm').append(gh); drag.ghost = gh;
    const el = document.querySelector(`#mmsvg .mmn[data-key="${CSS.escape(mmKey(c.node))}"]`); if(el) el.style.opacity = .35; drag.el = el; }
  const r = $('#mm').getBoundingClientRect(); drag.ghost.style.left = (e.clientX - r.left + 14) + 'px'; drag.ghost.style.top = (e.clientY - r.top + 10) + 'px';
  drag.t = dropTarget(e); drag.plan = drag.t ? resolveDrop(c.node, drag.t.n, drag.t.zone) : null; dropMark(drag.t, drag.plan); return true;
}
async function dragEnd(cancel){
  const c = drag.cand, was = drag.on, plan = drag.plan, t = drag.t;
  if(drag.ghost) drag.ghost.remove(); if(drag.el) drag.el.style.opacity = ''; dropMark(null);
  drag.cand = null; drag.on = false; drag.ghost = null; drag.t = null; drag.plan = null; drag.el = null;
  if(!was) return false; MM.noClick = true; setTimeout(() => { MM.noClick = false; }, 120);
  if(cancel || !t) return true;
  if(plan.err) { toast(plan.err); return true; }
  if(plan.notes) await mmMoveNote(plan); else await mmMove(plan); return true;
}
window.addEventListener('keydown', e => { if(e.key === 'Escape' && drag.on){ e.preventDefault(); dragEnd(true); } });

// ---- 初期化・操作
(function(){ const svg = $('#mmsvg'); let pan = null;
  svg.addEventListener('wheel', e => { e.preventDefault(); const r = svg.getBoundingClientRect(), dy = Math.max(-100, Math.min(100, e.deltaY)); mmZoom(Math.exp(-dy * (e.ctrlKey ? 0.006 : 0.0006)), e.clientX - r.left, e.clientY - r.top); }, {passive:false});
  svg.addEventListener('pointerdown', e => { svg.focus(); const ng = e.target.closest('.mmn'); if(ng){ if(!MM.input && e.button === 0){ const nn = MM.byKey && MM.byKey.get(ng.dataset.key); if(nn && nn.kind !== 'root') drag.cand = {node:nn, x:e.clientX, y:e.clientY}; } return; } if(e.target.closest('[data-tog]')) return; pan = {x:e.clientX, y:e.clientY, tx:MM.tx, ty:MM.ty}; svg.setPointerCapture(e.pointerId); svg.classList.add('drag'); });
  svg.addEventListener('pointermove', e => { if(drag.cand){ dragMove(e); return; } if(!pan) return; MM.tx = pan.tx + e.clientX - pan.x; MM.ty = pan.ty + e.clientY - pan.y; mmApply(); });
  const end = () => { pan = null; svg.classList.remove('drag'); if(drag.cand) dragEnd(false); }; svg.addEventListener('pointerup', end); svg.addEventListener('pointercancel', () => { pan = null; svg.classList.remove('drag'); if(drag.cand) dragEnd(true); });
  $('#mmIn').onclick = () => mmZoom(1.1); $('#mmOut').onclick = () => mmZoom(1 / 1.1); $('#mmFit').onclick = mmFit;
  $('#mmZ').oninput = e => mmZoom(+e.target.value / 100 / MM.k);
  $('#mmAll').onclick = () => { if(cur){ mmCol().clear(); renderMap(false); } };
  $('#mmLess').onclick = () => { if(!cur || !MM.root) return; const c = mmCol(); c.clear(); (function w(n){ if(n.kids.length && n.d >= 1) c.add(n.id); n.kids.forEach(w); })(MM.root); renderMap(true); };
  $('#mmView').value = MM.view; $('#mmDepth').value = String(MM.depth); $('#mmBack').checked = MM.back; $('#mmLink').checked = MM.links;
  const sync = () => { $('#mmOutOpts').hidden = MM.view === 'notes'; $('#mmNoteOpts').hidden = MM.view !== 'notes'; }; sync();
  $('#mmView').onchange = e => { MM.view = e.target.value; lsSet('ln.mmview', MM.view); MM.sel = 'root'; sync(); renderMap(true); };
  $('#mmDepth').onchange = e => { MM.depth = +e.target.value; lsSet('ln.mmdepth', MM.depth); renderMap(true); };
  $('#mmBack').onchange = e => { MM.back = e.target.checked; lsSet('ln.mmback', MM.back ? '1' : '0'); renderMap(true); };
  $('#mmLink').onchange = e => { MM.links = e.target.checked; lsSet('ln.mmlinks', MM.links ? '1' : '0'); renderMap(true); };
  $('#mmAddC').onclick = () => mmAdd(mmSelNode(), 'child'); $('#mmAddS').onclick = () => mmAdd(mmSelNode(), 'sibling');
  $('#mmHelp').onclick = () => toast('ダブルクリック=編集 / Tab=子を追加 / Enter=兄弟を追加 / Delete=削除 / Ctrl+C,X=枝をコピー・切取 / Ctrl+V=子として貼付 / Ctrl+Shift+V=兄弟として貼付（別のメモを開いて貼れば他のメモへ） / 矢印キー=移動 / Space=開閉 / ドラッグ&ドロップ=付け替え（ノードの上=子に / 上下の端=兄弟に） / ホイール=ズーム / 余白をドラッグ=移動');
  document.addEventListener('paste', e => { if(document.activeElement !== svg || MM.input) return; const n = mmSelNode(); if(!n) return;
    e.preventDefault(); const t = (e.clipboardData && e.clipboardData.getData('text/plain')) || MM.clip; mmPaste(n, MM.pasteAs || 'child', t); MM.pasteAs = null; });
  $('#mmCopy').onclick = () => mmCopy(mmSelNode(), false); $('#mmCut').onclick = () => mmCopy(mmSelNode(), true);
  $('#mmPaste').onclick = () => mmPaste(mmSelNode(), 'child'); $('#mmPasteS').onclick = () => mmPaste(mmSelNode(), 'sibling');
  $('#mmEd').onclick = () => mmEdit(mmSelNode()); $('#mmDel').onclick = () => mmDelete(mmSelNode());
  svg.addEventListener('keydown', e => { if(MM.input) return;
    if((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'c' || e.key === 'x' || e.key === 'C' || e.key === 'X')){ const n0 = mmSelNode(); if(n0){ e.preventDefault(); mmCopy(n0, e.key.toLowerCase() === 'x'); } return; }
    if((e.ctrlKey || e.metaKey) && !e.altKey && (e.key === 'v' || e.key === 'V')){ MM.pasteAs = e.shiftKey ? 'sibling' : 'child'; return; }
    if(e.ctrlKey || e.metaKey || e.altKey) return; const n = mmSelNode(); if(!n) return;
    const vis = n.kids.length && !mmCol().has(n.id);
    if(e.key === 'Tab'){ e.preventDefault(); mmAdd(n, 'child'); }
    else if(e.key === 'Enter'){ e.preventDefault(); mmAdd(n, 'sibling'); }
    else if(e.key === 'F2'){ e.preventDefault(); mmEdit(n); }
    else if(e.key === 'Delete' || e.key === 'Backspace'){ e.preventDefault(); mmDelete(n); }
    else if(e.key === ' '){ e.preventDefault(); if(n.kids.length){ const c = mmCol(); c.has(n.id) ? c.delete(n.id) : c.add(n.id); renderMap(false); } }
    else if(e.key === 'ArrowLeft'){ e.preventDefault(); if(n.parent){ MM.sel = mmKey(n.parent); mmMark(); } }
    else if(e.key === 'ArrowRight'){ e.preventDefault(); if(n.kids.length){ if(!vis){ mmCol().delete(n.id); renderMap(false).then(() => { MM.sel = mmKey(n.kids[0]); mmMark(); }); } else { MM.sel = mmKey(n.kids[0]); mmMark(); } } }
    else if(e.key === 'ArrowUp' || e.key === 'ArrowDown'){ e.preventDefault(); const row = MM.nodes.filter(x => x.d === n.d).sort((a, b) => a.y - b.y), i = row.indexOf(n), t = row[i + (e.key === 'ArrowDown' ? 1 : -1)]; if(t){ MM.sel = mmKey(t); mmMark(); } }
  });
  $('#mmSave').onclick = () => { const g = $('#mmg'); if(!g || !MM.bb) return; const clone = svg.cloneNode(true); clone.querySelector('#mmg').setAttribute('transform', 'translate(20,20)');
    clone.querySelectorAll('.selr').forEach(r => r.remove());
    clone.setAttribute('viewBox', `0 0 ${MM.bb.w + 60} ${MM.bb.h + 40}`); clone.setAttribute('width', MM.bb.w + 60); clone.setAttribute('height', MM.bb.h + 40); clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg'); clone.removeAttribute('class'); clone.removeAttribute('tabindex'); clone.setAttribute('style', 'background:#fff');
    const blob = new Blob([new XMLSerializer().serializeToString(clone)], {type:'image/svg+xml'}), a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = (cur ? cur.title : 'map') + '.svg'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); };
})();
window.addEventListener('resize', () => { if(isMapMode()) mmFit(); });

// ---- 起動
(async () => {
  await refreshTitles();
  await refreshList(); setMode(mode); showEditor(false);
  const last = +lsGet('ln.last'); const start = notes.find(n => n.id === last) || notes[0]; if(start) go(start.id); 
})();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    store = None

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

    def _json(self, fn):
        try:
            result = fn()
        except KeyError:
            return self._send(404, '{"ok":false,"error":"not found"}')
        except Conflict as e:
            return self._send(409, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        except NoteError as e:
            result = {"ok": False, "error": str(e)}
        except (ValueError, TypeError, AttributeError):
            result = {"ok": False, "error": "入力が正しくありません"}
        self._send(200, json.dumps(result, ensure_ascii=False))

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/export":
            name = "notes_" + dt.date.today().strftime("%Y%m%d") + ".zip"
            return self._send(200, self.store.export_zip(), "application/zip", {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
        if u.path.startswith("/api/"):
            return self._json(lambda: get_api(self.store, u.path, parse_qs(u.query)))
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def _write(self, method):
        if not self._guard(write=True):
            return
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_BODY * 3:
                return self._send(413, '{"ok":false,"error":"too large"}')
            body = json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            return self._send(400, '{"ok":false,"error":"invalid"}')
        self._json(lambda: post_api(self.store, method, urlparse(self.path).path, body))

    def do_POST(self):
        self._write("POST")

    def do_PUT(self):
        self._write("PUT")

    def do_DELETE(self):
        self._write("DELETE")

    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="メモ")
    ap.add_argument("--port", type=int, default=8409)
    a = ap.parse_args()
    Handler.store = Store(DB_PATH)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        raise SystemExit(f"ポート {a.port} を使えません（{e}）")
    print(f"メモを起動しました: http://127.0.0.1:{a.port}  (終了は Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
