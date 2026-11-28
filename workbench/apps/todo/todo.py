#!/usr/bin/env python3
"""ToDo — 標準ライブラリだけで動くToDo Webアプリ

起動:   python3 todo_plus.py            -> http://127.0.0.1:8000
        python3 todo_plus.py 8080       -> ポート指定
環境変数: TODO_DB=/path/to/todo.db  データベースの場所(既定: このファイルと同じフォルダの todo.db)
          TODO_HOST=0.0.0.0          同じネットワークのスマホ等からも開く場合(既定は自分のPCのみ)

以前の app.py と同じ todo.db をそのまま引き継げます(列は自動で追加されます)。
"""
import calendar
import html
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TODO_DB") or os.path.join(HERE, "todo.db")
WEEKDAYS = "月火水木金土日"
KINDS = ("once", "daily", "weekly", "monthly")
PRIO_MAP = {"高": 0, "中": 1, "低": 2}
MAX_BODY = 5_000_000

esc = html.escape


# ================================================================ DB
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(
        """CREATE TABLE IF NOT EXISTS tasks(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT    NOT NULL,
            kind       TEXT    NOT NULL,      -- once / daily / weekly / monthly
            weekday    INTEGER,               -- weekly: 0=月 ... 6=日
            monthday   INTEGER,               -- monthly: 1-31
            due_date   TEXT    NOT NULL,      -- 現在の期限 (繰り返しは次回の期限)
            done       INTEGER NOT NULL DEFAULT 0,  -- 指定日タスクの完了フラグ
            last_done  TEXT,
            created_at TEXT    NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS logs(
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id        INTEGER,
            title          TEXT NOT NULL,
            done_on        TEXT NOT NULL,
            prev_due       TEXT,
            prev_last_done TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_logs_done ON logs(done_on)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_logs_task ON logs(task_id)")
    cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}
    extra = {
        "comment": "TEXT NOT NULL DEFAULT ''",
        "links": "TEXT NOT NULL DEFAULT ''",
        "priority": "INTEGER NOT NULL DEFAULT 1",  # 0=高 1=中 2=低
        "category": "TEXT NOT NULL DEFAULT ''",
    }
    for name, decl in extra.items():
        if name not in cols:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
    con.commit()
    return con


# ================================================================ 日付の計算
def monthly_on_or_after(d, md):
    """d 以降で最初の「毎月md日」。月末を超える指定(31日など)は末日に丸める。"""
    y, m = d.year, d.month
    cand = d
    for _ in range(2):
        last = calendar.monthrange(y, m)[1]
        cand = date(y, m, min(md, last))
        if cand >= d:
            return cand
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return cand


def occurrence_on_or_after(kind, d, weekday=None, monthday=None):
    if kind == "daily":
        return d
    if kind == "weekly":
        return d + timedelta(days=(weekday - d.weekday()) % 7)
    if kind == "monthly":
        return monthly_on_or_after(d, monthday)
    return d


def occurrence_after(kind, d, weekday=None, monthday=None):
    return occurrence_on_or_after(kind, d + timedelta(days=1), weekday, monthday)


def task_state(r, today):
    """done / overdue / today / upcoming"""
    due = date.fromisoformat(r["due_date"])
    if r["kind"] == "once":
        if r["done"]:
            return "done"
    elif due > today and r["last_done"] == today.isoformat():
        # 繰り返し: 次の回がまだ先で、今日完了済みなら「完了」。
        # 期限が今日以前なら、その回はまだ未完了(期限超過の回を完了した直後も含む)。
        return "done"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    return "upcoming"


def parse_date(s, default):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return default


def to_int(s, default, lo, hi):
    try:
        v = int(s)
    except (TypeError, ValueError):
        return default
    return min(max(v, lo), hi)


# ================================================================ 入力の解釈
CAT_RE = re.compile(r"(?:^|\s)#(\S+)")
PRIO_RE = re.compile(r"(?:^|\s)!(高|中|低)")


def parse_quick(title):
    """タイトル中の「#カテゴリ」「!高/!中/!低」を取り出す。"""
    found = {}

    def take_cat(m):
        found.setdefault("cat", m.group(1))
        return " "

    def take_prio(m):
        found.setdefault("prio", PRIO_MAP[m.group(1)])
        return " "

    t = CAT_RE.sub(take_cat, title)
    t = PRIO_RE.sub(take_prio, t)
    return " ".join(t.split()), found.get("cat"), found.get("prio")


URL_RE = re.compile(r"https?://[^\s<>\"']+")
STRICT_URL_RE = re.compile(r"https?://\S+")


def linkify(text):
    """コメント内のURLをリンク化(それ以外はエスケープ)。http/httpsのみ。"""
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(esc(text[pos:m.start()]))
        url = m.group(0)
        out.append(
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(url)}</a>'
        )
        pos = m.end()
    out.append(esc(text[pos:]))
    return "".join(out)


def parse_links(text):
    """1行1件。「URL」または「ラベル | URL」。戻り値: [(表示名, URLまたはNone)]"""
    items = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        label, url = "", line
        if "|" in line:
            label, url = (s.strip() for s in line.split("|", 1))
        if STRICT_URL_RE.fullmatch(url):  # http/https以外(javascript:等)は除外
            items.append((label or url, url))
        else:
            items.append((line, None))
    return items


def safe_next(n):
    if not n or not n.startswith("/") or n.startswith("//"):
        return "/"
    if any(ord(c) < 32 for c in n):
        return "/"
    return quote(n, safe="/?=&#%:+,-_.~")


# ================================================================ 操作
def add_task(title, kind, date_str, weekday_str, monthday_str, prio_str, cat):
    title, qcat, qprio = parse_quick(title or "")
    title = title[:200]
    if not title or kind not in KINDS:
        return
    today = date.today()
    category = (qcat if qcat is not None else (cat or "")).strip().lstrip("#")[:30]
    priority = qprio if qprio is not None else to_int(prio_str, 1, 0, 2)
    weekday = monthday = None
    if kind == "once":
        due = parse_date(date_str, today)
    else:
        if kind == "weekly":
            weekday = to_int(weekday_str, today.weekday(), 0, 6)
        if kind == "monthly":
            monthday = to_int(monthday_str, today.day, 1, 31)
        due = occurrence_on_or_after(kind, today, weekday, monthday)
    with closing(db()) as con, con:
        con.execute(
            "INSERT INTO tasks(title,kind,weekday,monthday,due_date,priority,category,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (title, kind, weekday, monthday, due.isoformat(), priority, category, today.isoformat()),
        )


def complete_task(tid):
    today = date.today()
    with closing(db()) as con, con:
        r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None or task_state(r, today) == "done":
            return
        if r["kind"] == "once":
            con.execute("UPDATE tasks SET done=1,last_done=? WHERE id=?", (today.isoformat(), tid))
        else:
            # 1回の完了 = 期限日の分を1回消化。次回は「期限日」の次の予定日にする。
            # (今日を基準にすると、期限超過の回を完了した日に、今日の分まで飛ばしてしまう)
            nxt = occurrence_after(
                r["kind"], date.fromisoformat(r["due_date"]), r["weekday"], r["monthday"]
            )
            con.execute(
                "UPDATE tasks SET due_date=?,last_done=? WHERE id=?",
                (nxt.isoformat(), today.isoformat(), tid),
            )
        con.execute(
            "INSERT INTO logs(task_id,title,done_on,prev_due,prev_last_done) VALUES(?,?,?,?,?)",
            (tid, r["title"], today.isoformat(), r["due_date"], r["last_done"]),
        )


def undo_task(tid):
    """完了を取り消す(繰り返しタスクも元の期限に戻る)。"""
    today = date.today()
    with closing(db()) as con, con:
        r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            return
        log = con.execute(
            "SELECT * FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        if log is not None and log["done_on"] != today.isoformat():
            log = None  # 取り消せるのは「今日完了した記録」だけ
        if log is None and task_state(r, today) != "done":
            return  # 取り消す記録がない(古いデータで今日完了済みの場合だけ下の補正で戻す)
        if r["kind"] == "once":
            con.execute(
                "UPDATE tasks SET done=0,last_done=? WHERE id=?",
                (log["prev_last_done"] if log else None, tid),
            )
        else:
            if log and log["prev_due"]:
                due, last = log["prev_due"], log["prev_last_done"]
            else:  # 古いデータ(履歴なし): 今日以降の最初の予定日に戻す
                due = occurrence_on_or_after(r["kind"], today, r["weekday"], r["monthday"]).isoformat()
                last = None
            con.execute("UPDATE tasks SET due_date=?,last_done=? WHERE id=?", (due, last, tid))
        if log:
            con.execute("DELETE FROM logs WHERE id=?", (log["id"],))


def bulk_complete(ids):
    """選択した複数タスクをまとめて完了する(各タスク1回分)。
    重複・存在しないID・すでに完了済みのものは無視される。"""
    seen = set()
    for tid in ids:
        if tid not in seen:
            seen.add(tid)
            complete_task(tid)


def snooze_task(tid, mode):
    delta = {"today": 0, "tomorrow": 1, "week": 7}.get(mode)
    if delta is None:
        return
    today = date.today()
    with closing(db()) as con, con:
        r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None or task_state(r, today) == "done":
            return
        con.execute(
            "UPDATE tasks SET due_date=? WHERE id=?",
            ((today + timedelta(days=delta)).isoformat(), tid),
        )


def update_task(tid, get):
    today = date.today()
    with closing(db()) as con, con:
        r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            return
        title, qcat, qprio = parse_quick(get("title"))
        title = title[:200] or r["title"]
        kind = get("kind") if get("kind") in KINDS else r["kind"]
        old_due = date.fromisoformat(r["due_date"])
        weekday = monthday = None
        if kind == "weekly":
            weekday = to_int(
                get("weekday"), r["weekday"] if r["weekday"] is not None else old_due.weekday(), 0, 6
            )
        if kind == "monthly":
            monthday = to_int(get("monthday"), r["monthday"] or old_due.day, 1, 31)
        if kind == "once":
            due = parse_date(get("date"), old_due)
        elif kind == r["kind"] and weekday == r["weekday"] and monthday == r["monthday"]:
            due = old_due  # 設定が変わらなければ現在の期限を維持
        else:
            due = occurrence_on_or_after(kind, today, weekday, monthday)
        done = r["done"] if kind == r["kind"] else 0
        priority = qprio if qprio is not None else to_int(get("priority"), r["priority"], 0, 2)
        category = (qcat if qcat is not None else get("category")).strip().lstrip("#")[:30]
        comment = get("comment").replace("\r\n", "\n").strip()[:5000]
        links = get("links").replace("\r\n", "\n").strip()[:2000]
        con.execute(
            "UPDATE tasks SET title=?,kind=?,weekday=?,monthday=?,due_date=?,done=?,"
            "priority=?,category=?,comment=?,links=? WHERE id=?",
            (title, kind, weekday, monthday, due.isoformat(), done, priority, category, comment, links, tid),
        )


def delete_task(tid):
    with closing(db()) as con, con:
        con.execute("DELETE FROM tasks WHERE id=?", (tid,))


def clear_done():
    with closing(db()) as con, con:
        con.execute("DELETE FROM tasks WHERE kind='once' AND done=1")


# ================================================================ 統計
def stats(con, today):
    by = {r["done_on"]: r["c"] for r in con.execute("SELECT done_on, COUNT(*) c FROM logs GROUP BY done_on")}
    t = by.get(today.isoformat(), 0)
    monday = (today - timedelta(days=today.weekday())).isoformat()
    week = sum(c for d, c in by.items() if d >= monday)
    d = today if today.isoformat() in by else today - timedelta(days=1)
    streak = 0
    while d.isoformat() in by:
        streak += 1
        d -= timedelta(days=1)
    return t, week, streak


# ================================================================ バックアップ
def export_data():
    with closing(db()) as con:
        tasks = [dict(r) for r in con.execute("SELECT * FROM tasks ORDER BY id")]
        logs = [dict(r) for r in con.execute("SELECT * FROM logs ORDER BY id")]
    return json.dumps(
        {"app": "todo_plus", "version": 2, "exported_at": datetime.now().isoformat(timespec="seconds"),
         "tasks": tasks, "logs": logs},
        ensure_ascii=False, indent=1,
    )


def _iso_or_none(v):
    try:
        return date.fromisoformat(str(v)).isoformat()
    except (TypeError, ValueError):
        return None


def import_data(data):
    """JSONバックアップを読み込む(既存のタスクは消さず、追加する)。"""
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError("バックアップの形式が正しくありません")
    added = 0
    idmap = {}
    with closing(db()) as con, con:
        for t in data["tasks"]:
            if not isinstance(t, dict):
                continue
            title = str(t.get("title") or "").strip()[:200]
            kind = t.get("kind")
            due = _iso_or_none(t.get("due_date"))
            if not title or kind not in KINDS or due is None:
                continue
            dd = date.fromisoformat(due)
            weekday = monthday = None
            if kind == "weekly":
                weekday = to_int(t.get("weekday"), dd.weekday(), 0, 6)
            if kind == "monthly":
                monthday = to_int(t.get("monthday"), dd.day, 1, 31)
            cur = con.execute(
                "INSERT INTO tasks(title,kind,weekday,monthday,due_date,done,last_done,created_at,"
                "comment,links,priority,category) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    title, kind, weekday, monthday, due,
                    1 if (kind == "once" and t.get("done")) else 0,
                    _iso_or_none(t.get("last_done")),
                    _iso_or_none(t.get("created_at")) or date.today().isoformat(),
                    str(t.get("comment") or "")[:5000],
                    str(t.get("links") or "")[:2000],
                    to_int(t.get("priority"), 1, 0, 2),
                    str(t.get("category") or "").strip()[:30],
                ),
            )
            idmap[t.get("id")] = cur.lastrowid
            added += 1
        logs = data.get("logs")
        if isinstance(logs, list):
            for l in logs:
                if not isinstance(l, dict):
                    continue
                done_on = _iso_or_none(l.get("done_on"))
                title = str(l.get("title") or "").strip()[:200]
                if done_on is None or not title:
                    continue
                con.execute(
                    "INSERT INTO logs(task_id,title,done_on,prev_due,prev_last_done) VALUES(?,?,?,?,?)",
                    (idmap.get(l.get("task_id")), title, done_on,
                     _iso_or_none(l.get("prev_due")), _iso_or_none(l.get("prev_last_done"))),
                )
    return added


# ================================================================ 画面: 共通
CSS = """
:root{--bg:#f1f4f9;--fg:#1d2433;--sub:#5b6477;--mute:#8b94a7;--card:#fff;--line:#c9d0de;
  --chip:#e3e8f1;--chip-fg:#394258;--accent:#3a6df0;--link:#2457d6;--ok:#16a34a;--shadow:#0001;
  --today-bg:#fff3c4;--today-bd:#ff9f1c;--today-fg:#3b2500;
  --over-bg:#ffd9d9;--over-bd:#d90429;--over-fg:#a4001d}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e8ecf4;--sub:#a4adc2;--mute:#7d869a;
  --card:#1c212c;--line:#363d4e;--chip:#2a3040;--chip-fg:#c9d1e3;--accent:#5b8cff;--link:#8fb0ff;
  --shadow:#0006;--today-bg:#3d3000;--today-bd:#ffb02e;--today-fg:#ffe7a8;
  --over-bg:#4a0f18;--over-bd:#ff3b57;--over-fg:#ffb3be}}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;
  background:var(--bg);color:var(--fg);line-height:1.5}
.wrap{max-width:820px;margin:0 auto;padding:16px 16px 60px}
.wrap.wide{max-width:1000px}
a{color:var(--link)}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{font-size:1.5rem;margin:8px 0 2px}
.tabs{display:flex;gap:4px;background:var(--chip);padding:4px;border-radius:10px}
.tabs a{padding:6px 14px;border-radius:8px;text-decoration:none;color:var(--chip-fg);font-weight:700;font-size:.9rem}
.tabs a.on{background:var(--card);color:var(--fg);box-shadow:0 1px 3px var(--shadow)}
.date{color:var(--sub);margin-bottom:12px}
.banner{position:sticky;top:0;z-index:10;margin:0 -16px 16px;padding:12px 16px;text-align:center;
  font-weight:800;font-size:1.05rem;color:#fff;background:#d90429;animation:blink 1s infinite}
.stats{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.chip{padding:4px 12px;border-radius:999px;font-size:.85rem;font-weight:700;background:var(--chip);color:var(--chip-fg)}
.chip.over{background:var(--over-bd);color:#fff}.chip.today{background:var(--today-bd);color:#3b2500}
.chip.ok{background:var(--ok);color:#fff}
.progress{height:8px;background:var(--chip);border-radius:999px;overflow:hidden}
.progress>i{display:block;height:100%;background:var(--ok);transition:width .3s}
.ptext{font-size:.8rem;color:var(--sub);margin:4px 0 16px}
.filters{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.filters form{display:flex;gap:6px;flex:1 1 240px}
.cats{display:flex;gap:6px;flex-wrap:wrap}
.cats a{padding:3px 11px;border-radius:999px;background:var(--chip);color:var(--chip-fg);
  text-decoration:none;font-size:.82rem;font-weight:700}
.cats a.on{background:var(--accent);color:#fff}
form.add{background:var(--card);border-radius:12px;padding:14px;box-shadow:0 1px 4px var(--shadow);
  margin-bottom:6px;display:flex;flex-wrap:wrap;gap:8px;align-items:end}
form.add label,.detail-form label{display:flex;flex-direction:column;font-size:.78rem;color:var(--sub);gap:2px}
form.add .grow{flex:1 1 220px}
.hint{font-size:.78rem;color:var(--mute);margin:0 2px 8px}
input,select,textarea,button{font:inherit}
input[type=text],input[type=date],input[type=number],input[type=search],select,textarea{
  padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
input[type=search]{flex:1;min-width:0}
input[type=number]{width:80px}
button{cursor:pointer;border:0;border-radius:8px;padding:8px 14px;font-weight:700;background:var(--chip);color:var(--chip-fg)}
.primary{background:var(--accent);color:#fff}
.done-btn{background:var(--ok);color:#fff}
.sub{padding:6px 10px;font-size:.82rem}
h2{font-size:1.05rem;margin:24px 0 8px;display:flex;align-items:center;gap:8px}
h2 .n{font-size:.8rem;font-weight:700;color:var(--sub)}
details.sec>summary{list-style:none;cursor:pointer}
details.sec>summary::-webkit-details-marker{display:none}
details.sec>summary h2::before{content:"▸"}
details.sec[open]>summary h2::before{content:"▾"}
.empty{color:var(--mute);font-size:.9rem;padding:6px 2px}
.card{display:flex;align-items:center;gap:12px;background:var(--card);border-radius:12px;padding:12px 14px;
  margin-bottom:8px;border-left:6px solid var(--line);box-shadow:0 1px 3px var(--shadow)}
.card:hover{box-shadow:0 3px 10px var(--shadow)}
.card .main{flex:1;min-width:0}
.task-link{display:block;color:inherit;text-decoration:none}
.task-link:hover .title{text-decoration:underline}
.title{font-weight:600;word-break:break-word}
.meta{font-size:.8rem;color:var(--sub);margin-top:2px}
.badge{display:inline-block;font-size:.75rem;font-weight:800;padding:2px 9px;border-radius:999px;
  margin-right:6px;background:var(--chip);color:var(--chip-fg)}
.pri{display:inline-block;font-size:.72rem;font-weight:800;padding:1px 8px;border-radius:999px;margin-right:6px}
.pri.p0{background:#d90429;color:#fff}.pri.p2{background:var(--chip);color:var(--sub)}
.cat{display:inline-block;font-size:.72rem;font-weight:700;padding:1px 8px;border-radius:999px;
  margin-right:4px;background:var(--chip);color:var(--chip-fg)}
.actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}

/* 今日が期限 */
.card.today{background:var(--today-bg);border-left:10px solid var(--today-bd);box-shadow:0 2px 8px var(--today-bd)}
.card.today .title{font-size:1.08rem;font-weight:800}
.card.today .badge{background:var(--today-bd);color:#3b2500}

/* 期限超過(最も目立たせる) */
.card.overdue{background:var(--over-bg);border-left:14px solid var(--over-bd);animation:pulse 1.6s infinite}
.card.overdue .title{font-size:1.15rem;font-weight:900;color:var(--over-fg)}
.card.overdue .badge{background:var(--over-bd);color:#fff;font-size:.85rem;animation:blink 1s infinite}
.card.overdue .meta{color:var(--over-fg);font-weight:700}

.card.done{opacity:.6;border-left-color:var(--ok)}
.card.done .title{text-decoration:line-through}
.big .title{font-size:1.25rem}

/* 詳細ページ */
.back{display:inline-block;margin:6px 0 10px;text-decoration:none;font-weight:700}
.panel{background:var(--card);border-radius:12px;padding:14px 16px;box-shadow:0 1px 4px var(--shadow);margin-bottom:16px}
.panel h3{margin:0 0 8px;font-size:.95rem;color:var(--sub)}
.comment{white-space:pre-wrap;word-break:break-word}
.links{margin:0;padding-left:20px}.links li{margin:4px 0;word-break:break-all}
.info{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:.9rem;margin:0}
.info dt{color:var(--sub)}.info dd{margin:0}
.muted{color:var(--mute)}
.flash{background:#dcfce7;color:#14532d;padding:8px 12px;border-radius:8px;margin-bottom:12px;font-weight:700}
.detail-form label{margin-bottom:10px}
.detail-form input[type=text],.detail-form textarea{width:100%}
.row{display:flex;gap:10px;flex-wrap:wrap}
.snooze{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:16px;font-size:.85rem;color:var(--sub)}

/* カレンダー */
.cal-nav{display:flex;align-items:center;gap:10px;margin:8px 0 12px}
.cal-nav h2{margin:0;font-size:1.2rem;flex:1;justify-content:center}
.cal-nav a{padding:6px 12px;border-radius:8px;background:var(--chip);color:var(--chip-fg);text-decoration:none;font-weight:700}
.cal{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}
.cal .dow{text-align:center;font-weight:700;color:var(--sub);font-size:.8rem;padding:4px 0}
.cal .day{background:var(--card);border-radius:8px;min-height:104px;padding:4px 5px;border:2px solid transparent;overflow:hidden}
.cal .day.out{opacity:.45}
.cal .day.today{border-color:var(--today-bd);background:var(--today-bg)}
.cal .dn{font-size:.8rem;font-weight:700;color:var(--sub);text-decoration:none;display:block}
.cal .ev{display:block;font-size:.72rem;padding:1px 4px;margin-top:2px;border-radius:4px;background:var(--chip);
  color:var(--chip-fg);text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cal .ev.over{background:var(--over-bd);color:#fff;font-weight:700}
.cal .ev.tdy{background:var(--today-bd);color:#3b2500;font-weight:700}
.cal .ev.fin{opacity:.6;text-decoration:line-through}
.cal .more{font-size:.7rem;color:var(--sub)}
.legend{font-size:.8rem;color:var(--sub);margin-top:10px}
.foot{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);font-size:.82rem;color:var(--sub);
  display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.foot a,.foot button{font-size:.82rem}
@media (max-width:600px){.cal .day{min-height:68px}.cal .ev{font-size:.62rem}.card{flex-wrap:wrap}}

/* まとめて完了 */
.sel{width:20px;height:20px;flex:none;cursor:pointer;accent-color:var(--accent)}
h2 [data-selall]{margin-left:auto}
.bulkbar{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:30;display:flex;gap:10px;
  align-items:center;background:var(--card);color:var(--fg);padding:10px 16px;border-radius:999px;
  border:1px solid var(--line);box-shadow:0 4px 18px #0005;white-space:nowrap}

@keyframes blink{50%{opacity:.55}}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 #d9042977}50%{box-shadow:0 0 0 8px #d9042900,0 2px 14px #d9042999}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""

JS = """
document.querySelectorAll('form').forEach(f=>{
  const k=f.querySelector('select[name=kind]'); if(!k) return;
  const s=()=>f.querySelectorAll('[data-for]').forEach(e=>{e.hidden=e.dataset.for!==k.value});
  k.addEventListener('change',s); s();
});
document.addEventListener('submit',e=>{
  const f=e.target;
  if(e.defaultPrevented||!f.dataset||!f.dataset.ajax) return;
  e.preventDefault();
  fetch(f.action,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams(new FormData(f))}).then(()=>location.reload()).catch(()=>f.submit());
});
document.addEventListener('keydown',e=>{
  if(['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)||e.ctrlKey||e.metaKey||e.altKey) return;
  if(e.key==='n'){const el=document.getElementById('new-title'); if(el){e.preventDefault(); el.focus();}}
  if(e.key==='/'){const el=document.getElementById('search'); if(el){e.preventDefault(); el.focus();}}
});
const bform=document.getElementById('bulk-form');
if(bform){
  const boxes=()=>[...document.querySelectorAll('input.sel')];
  const upd=()=>{const n=boxes().filter(b=>b.checked).length;
    bform.hidden=n===0;document.getElementById('bulk-n').textContent=n;};
  document.addEventListener('change',e=>{if(e.target.classList&&e.target.classList.contains('sel'))upd();});
  document.querySelectorAll('[data-selall]').forEach(b=>b.addEventListener('click',()=>{
    const bs=boxes().filter(x=>x.dataset.sec===b.dataset.selall);
    const all=bs.every(x=>x.checked);
    bs.forEach(x=>{x.checked=!all});upd();}));
  document.getElementById('bulk-clear').addEventListener('click',()=>{boxes().forEach(x=>{x.checked=false});upd();});
  bform.addEventListener('submit',e=>{
    const n=boxes().filter(b=>b.checked).length;
    if(!n||!confirm(n+'件を完了にしますか?'))e.preventDefault();});
  window.addEventListener('pageshow',upd);upd();
}
const d0=new Date().toDateString();
setInterval(()=>{if(new Date().toDateString()!==d0) location.reload()},60000);
const imp=document.getElementById('import-file');
if(imp) imp.addEventListener('change',async()=>{
  const f=imp.files[0]; if(!f) return;
  try{
    const res=await fetch('/import',{method:'POST',headers:{'Content-Type':'application/json'},body:await f.text()});
    const j=await res.json();
    if(j.ok){alert(j.added+'件のタスクを読み込みました'); location.reload();}
    else alert('読み込みに失敗しました: '+(j.error||''));
  }catch(err){alert('読み込みに失敗しました');}
  imp.value='';
});
"""


def fmt_date(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def full_date(d):
    return f"{d.year}年{d.month}月{d.day}日({WEEKDAYS[d.weekday()]})"


def repeat_label(r):
    if r["kind"] == "daily":
        return "毎日"
    if r["kind"] == "weekly":
        return f"毎週{WEEKDAYS[r['weekday']]}曜"
    if r["kind"] == "monthly":
        return f"毎月{r['monthday']}日"
    return "指定日"


def btn_form(action, label, cls="", confirm=None, ajax=False, nxt=None):
    attrs = ""
    if confirm:
        attrs += f' onsubmit="return confirm(\'{confirm}\')"'
    if ajax:
        attrs += ' data-ajax="1"'
    hidden = f'<input type="hidden" name="next" value="{esc(nxt)}">' if nxt else ""
    return (
        f'<form method="post" action="{action}" style="margin:0"{attrs}>'
        f'{hidden}<button class="{cls}">{label}</button></form>'
    )


def shell(title, body, n_over, active, today, wide=False):
    banner = (
        f'<div class="banner">⚠ 期限を過ぎた未完了タスクが {n_over} 件あります!</div>' if n_over else ""
    )
    prefix = f"(⚠{n_over}) " if n_over else ""
    tabs = (
        f'<nav class="tabs"><a class="{"on" if active == "list" else ""}" href="/">一覧</a>'
        f'<a class="{"on" if active == "cal" else ""}" href="/calendar">カレンダー</a></nav>'
    )
    wrap = "wrap wide" if wide else "wrap"
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{prefix}{esc(title)}</title><style>{CSS}</style></head>
<body><div class="{wrap}">
{banner}
<div class="top"><h1>📝 ToDo</h1>{tabs}</div>
<div class="date">{full_date(today)}</div>
{body}
</div><script>{JS}</script></body></html>"""


def kind_fields(kind, due, weekday, monthday, today):
    """追加・編集フォーム共通の「繰り返し」入力欄。"""
    opts = [("once", "指定日"), ("daily", "毎日"), ("weekly", "毎週"), ("monthly", "毎月")]
    kind_html = "".join(
        f'<option value="{k}"{" selected" if k == kind else ""}>{label}</option>' for k, label in opts
    )
    wd = today.weekday() if weekday is None else weekday
    wd_html = "".join(
        f'<option value="{i}"{" selected" if i == wd else ""}>{w}曜日</option>'
        for i, w in enumerate(WEEKDAYS)
    )
    md = today.day if monthday is None else monthday
    return (
        f'<label>繰り返し<select name="kind">{kind_html}</select></label>'
        f'<label data-for="once">期限日<input type="date" name="date" value="{due}"></label>'
        f'<label data-for="weekly">曜日<select name="weekday">{wd_html}</select></label>'
        f'<label data-for="monthly">日にち<input type="number" name="monthday" min="1" max="31" value="{md}"></label>'
    )


def prio_select(cur):
    return "".join(
        f'<option value="{v}"{" selected" if v == cur else ""}>{label}</option>'
        for v, label in ((0, "高"), (1, "中"), (2, "低"))
    )


# ================================================================ 画面: 一覧
def card(r, today, section, redo=False):
    due = date.fromisoformat(r["due_date"])
    diff = (due - today).days
    rep = repeat_label(r)
    actions = ""
    if section == "overdue":
        badge = f'<span class="badge">⚠ {-diff}日超過</span>'
        meta = f"{rep} ・ 期限 {fmt_date(due)}"
        actions += btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn", ajax=True)
        actions += btn_form(f"/snooze/{r['id']}/today", "今日へ", "sub", ajax=True)
        actions += btn_form(f"/snooze/{r['id']}/tomorrow", "明日へ", "sub", ajax=True)
    elif section == "today":
        badge = '<span class="badge">TODAY 今日が期限</span>'
        meta = rep
        actions += btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn", ajax=True)
        actions += btn_form(f"/snooze/{r['id']}/tomorrow", "明日へ", "sub", ajax=True)
    elif section == "upcoming":
        word = "明日" if diff == 1 else f"あと{diff}日"
        badge = f'<span class="badge">{word}</span>'
        meta = f"{rep} ・ 期限 {fmt_date(due)}"
        actions += btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn", ajax=True)
    else:
        badge = '<span class="badge">完了</span>'
        meta = f"{rep} ・ 期限 {fmt_date(due)}" if r["kind"] == "once" else f"{rep} ・ 次回 {fmt_date(due)}"
        actions += btn_form(f"/undo/{r['id']}", "↩ 取り消し", "sub", ajax=True)
    if redo and section in ("overdue", "today"):
        actions += btn_form(f"/undo/{r['id']}", "↩ 前回の完了を取り消し", "sub", ajax=True)
        meta += " ・ 前の回は今日完了済み"
    actions += btn_form(f"/delete/{r['id']}", "🗑", "sub", "このタスクを削除しますか?", ajax=True)

    pills = ""
    if r["priority"] == 0:
        pills += '<span class="pri p0">高</span>'
    elif r["priority"] == 2:
        pills += '<span class="pri p2">低</span>'
    ind = ""
    if r["category"]:
        ind += f' ・ <span class="cat">#{esc(r["category"])}</span>'
    if (r["comment"] or "").strip():
        ind += " ・ 💬"
    n_links = sum(1 for _, u in parse_links(r["links"]) if u)
    if n_links:
        ind += f" ・ 🔗{n_links}"
    box = ""
    if section != "done":  # まとめて完了の対象に選ぶチェックボックス
        box = (
            f'<input type="checkbox" class="sel" name="ids" value="{r["id"]}" form="bulk-form" '
            f'data-sec="{section}" aria-label="まとめて完了の対象に選ぶ">'
        )
    return (
        f'<div class="card {section}">{box}<div class="main">'
        f'<a class="task-link" href="/task/{r["id"]}" title="クリックで詳細を表示">'
        f'<div class="title">{badge}{pills}{esc(r["title"])}</div>'
        f'<div class="meta">{esc(meta)}{ind}</div></a>'
        f'</div><div class="actions">{actions}</div></div>'
    )


def section(heading, key, items, today, empty, details=False, opened=True, extra="", redo=frozenset()):
    n = f'<span class="n">{len(items)}件</span>'
    body = extra + (
        "".join(card(r, today, key, r["id"] in redo) for r in items)
        or f'<div class="empty">{empty}</div>'
    )
    if details:
        o = " open" if opened else ""
        return f'<details class="sec"{o}><summary><h2>{heading}{n}</h2></summary>{body}</details>'
    selall = ""
    if key in ("overdue", "today") and items:
        selall = f'<button type="button" class="sub" data-selall="{key}">全選択</button>'
    return f"<h2>{heading}{n}{selall}</h2>{body}"


def bulk_bar(nxt):
    """チェックが1件以上あるときだけ画面下に出る「まとめて完了」バー。"""
    return (
        '<form id="bulk-form" class="bulkbar" method="post" action="/bulk" hidden>'
        f'<input type="hidden" name="next" value="{esc(nxt)}">'
        '<span><b id="bulk-n">0</b>件選択中</span>'
        '<button class="done-btn">✓ まとめて完了</button>'
        '<button type="button" class="sub" id="bulk-clear">選択を解除</button></form>'
    )


def matches_filter(r, q, cat):
    if cat and r["category"] != cat:
        return False
    if q:
        hay = " ".join([r["title"], r["category"], r["comment"] or ""]).lower()
        if any(w not in hay for w in q.lower().split()):
            return False
    return True


def render_page(params):
    today = date.today()
    q = (params.get("q") or [""])[0].strip()
    cat = (params.get("cat") or [""])[0].strip()
    pre = parse_date((params.get("date") or [""])[0], None)
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM tasks").fetchall()
        t_done, week, streak = stats(con, today)
        # 今日すでに1回以上完了した記録があるタスク(期限超過の回を片付けた直後など)
        redo_ids = {
            x["task_id"]
            for x in con.execute("SELECT DISTINCT task_id FROM logs WHERE done_on=?", (today.isoformat(),))
        }

    states = {r["id"]: task_state(r, today) for r in rows}
    n_over = sum(1 for s in states.values() if s == "overdue")
    n_today = sum(1 for s in states.values() if s == "today")
    cats = {}
    for r in rows:
        if r["category"]:
            cats[r["category"]] = cats.get(r["category"], 0) + 1

    buckets = {"overdue": [], "today": [], "upcoming": [], "done": []}
    for r in rows:
        if matches_filter(r, q, cat):
            buckets[states[r["id"]]].append(r)
    buckets["overdue"].sort(key=lambda r: (r["due_date"], r["priority"], r["id"]))
    buckets["today"].sort(key=lambda r: (r["priority"], r["id"]))
    buckets["upcoming"].sort(key=lambda r: (r["due_date"], r["priority"], r["id"]))
    buckets["done"].sort(key=lambda r: (r["last_done"] or "", r["id"]), reverse=True)
    done_shown = buckets["done"][:30]

    # --- 進捗
    denom = t_done + n_today + n_over
    pct = int(100 * t_done / denom) if denom else 0
    if denom == 0:
        ptext = "今日やることはありません"
    elif n_today + n_over == 0:
        ptext = f"今日の分は全部完了です 🎉({t_done}件)"
    else:
        ptext = f"今日の進捗 {t_done} / {denom}"
    stats_html = (
        f'<div class="stats">'
        f'<span class="chip {"over" if n_over else ""}">期限超過 {n_over}</span>'
        f'<span class="chip {"today" if n_today else ""}">今日 {n_today}</span>'
        f'<span class="chip ok">今日の完了 {t_done}</span>'
        f'<span class="chip">今週の完了 {week}</span>'
        f'<span class="chip">🔥 連続 {streak}日</span></div>'
        f'<div class="progress"><i style="width:{pct}%"></i></div><div class="ptext">{ptext}</div>'
    )

    # --- 検索・カテゴリ
    def href(**kw):
        qs = {k: v for k, v in kw.items() if v}
        return "/" + ("?" + urlencode(qs) if qs else "")

    chips = ""
    if cats:
        chips = f'<a class="{"" if cat else "on"}" href="{esc(href(q=q))}">すべて</a>' + "".join(
            f'<a class="{"on" if c == cat else ""}" href="{esc(href(q=q, cat=c))}">#{esc(c)} {n}</a>'
            for c, n in sorted(cats.items())
        )
    clear = f' <a href="{esc(href(cat=cat))}">クリア</a>' if q else ""
    filters = (
        f'<div class="filters"><form method="get" action="/">'
        f'<input type="search" id="search" name="q" value="{esc(q)}" placeholder="🔍 検索(タイトル・コメント・カテゴリ)  /">'
        f'<input type="hidden" name="cat" value="{esc(cat)}"><button>検索</button></form>{clear}'
        f'<div class="cats">{chips}</div></div>'
    )

    # --- 追加フォーム
    cat_opts = "".join(f'<option value="{esc(c)}">' for c in sorted(cats))
    default_date = (pre or today).isoformat()
    form = f"""<form class="add" id="add" method="post" action="/add">
  <label class="grow">タスク名<input type="text" id="new-title" name="title" maxlength="200" required
    autofocus placeholder="例: 請求書を送る #仕事 !高"></label>
  {kind_fields("once", default_date, None, None, today)}
  <label>優先度<select name="priority">{prio_select(1)}</select></label>
  <label>カテゴリ<input type="text" name="category" maxlength="30" list="cats" style="width:120px"></label>
  <datalist id="cats">{cat_opts}</datalist>
  <button class="primary">追加</button>
</form>
<div class="hint">ヒント: タスク名に「#カテゴリ」「!高」を書くと一緒に設定されます。キー操作: n=新規入力 / /=検索</div>"""

    # --- セクション
    clear_btn = ""
    if any(r["kind"] == "once" for r in buckets["done"]):
        clear_btn = '<div style="margin-bottom:8px">' + btn_form(
            "/clear_done", "完了済み(指定日)を一括削除", "sub", "完了済みの指定日タスクを全て削除しますか?", ajax=True
        ) + "</div>"
    note = ""
    if q or cat:
        note = f'<div class="hint">絞り込み中: 表示 {sum(len(v) for v in buckets.values())}件</div>'
    body = (
        stats_html + filters + form + note
        + section("🚨 期限超過", "overdue", buckets["overdue"], today, "期限超過のタスクはありません 👍",
                  redo=redo_ids)
        + section("⭐ 今日のタスク", "today", buckets["today"], today, "今日が期限のタスクはありません",
                  redo=redo_ids)
        + section("📅 今後のタスク", "upcoming", buckets["upcoming"], today, "予定されているタスクはありません",
                  details=True, opened=True)
        + section("✅ 完了", "done", done_shown, today, "完了したタスクはまだありません",
                  details=True, opened=False, extra=clear_btn)
        + """<div class="foot"><span>バックアップ:</span>
  <a href="/export">📤 書き出し(JSON)</a>
  <button class="sub" type="button" onclick="document.getElementById('import-file').click()">📥 読み込み…</button>
  <input type="file" id="import-file" accept="application/json,.json" hidden></div>"""
        + bulk_bar(href(q=q, cat=cat))
    )
    return shell("ToDo", body, n_over, "list", today)


# ================================================================ 画面: カレンダー
def occurs_on(r, d, today, state):
    """タスクrがカレンダーの日dに表示されるか。"""
    if state == "done":
        # 完了済み: 繰り返しは次回以降の予定のみ(指定日は表示しない)
        if r["kind"] == "once":
            return False
    due = date.fromisoformat(r["due_date"])
    unfinished = state != "done"
    if d < today:
        return unfinished and d == due  # 期限超過のタスクは本来の期限日に赤で表示
    if d == today and unfinished and due < today:
        return True  # 期限超過は今日にも持ち越して表示
    if r["kind"] == "once":
        return unfinished and d == due
    if d < due:
        return False
    if r["kind"] == "daily":
        return True
    if r["kind"] == "weekly":
        return d.weekday() == r["weekday"]
    return d.day == min(r["monthday"], calendar.monthrange(d.year, d.month)[1])


def render_calendar(params):
    today = date.today()
    ym = (params.get("ym") or [""])[0]
    try:
        y, m = (int(x) for x in ym.split("-"))
        if not (1 <= m <= 12 and 1970 <= y <= 2100):
            raise ValueError
    except ValueError:
        y, m = today.year, today.month
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(y, m)
    first, last = weeks[0][0], weeks[-1][-1]
    with closing(db()) as con:
        rows = con.execute("SELECT * FROM tasks").fetchall()
        logs = con.execute(
            "SELECT title, done_on FROM logs WHERE done_on BETWEEN ? AND ? ORDER BY id",
            (first.isoformat(), last.isoformat()),
        ).fetchall()
    states = {r["id"]: task_state(r, today) for r in rows}
    n_over = sum(1 for s in states.values() if s == "overdue")
    logs_by = {}
    for l in logs:
        logs_by.setdefault(l["done_on"], []).append(l["title"])
    rows = sorted(rows, key=lambda r: (r["priority"], r["id"]))

    py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
    ny, nm = (y, m + 1) if m < 12 else (y + 1, 1)
    nav = (
        f'<div class="cal-nav"><a href="/calendar?ym={py}-{pm:02d}">◀ 前月</a>'
        f'<h2>{y}年{m}月</h2><a href="/calendar">今月</a><a href="/calendar?ym={ny}-{nm:02d}">翌月 ▶</a></div>'
    )
    cells = "".join(f'<div class="dow">{w}</div>' for w in "日月火水木金土")
    for week in weeks:
        for d in week:
            ents = []
            for r in rows:
                st = states[r["id"]]
                if not occurs_on(r, d, today, st):
                    continue
                due = date.fromisoformat(r["due_date"])
                if d < today or (d == today and st != "done" and due < today):
                    cls = "over"
                elif d == today:
                    cls = "tdy"
                else:
                    cls = ""
                mark = "🔺" if r["priority"] == 0 else ""
                ents.append((cls, mark + r["title"], f"/task/{r['id']}"))
            for t in logs_by.get(d.isoformat(), []):
                ents.append(("fin", "✓ " + t, None))
            shown = ""
            for cls, text, link in ents[:4]:
                if link:
                    shown += f'<a class="ev {cls}" href="{link}" title="{esc(text)}">{esc(text)}</a>'
                else:
                    shown += f'<span class="ev {cls}" title="{esc(text)}">{esc(text)}</span>'
            if len(ents) > 4:
                shown += f'<div class="more">ほか {len(ents) - 4}件</div>'
            dcls = "day" + (" today" if d == today else "") + (" out" if d.month != m else "")
            cells += (
                f'<div class="{dcls}"><a class="dn" href="/?date={d.isoformat()}#add" '
                f'title="この日のタスクを追加">{d.day}</a>{shown}</div>'
            )
    legend = (
        '<div class="legend">赤 = 期限超過 / オレンジ = 今日 / 取り消し線 ✓ = 完了した日 / '
        "日付をクリックすると、その日を期限にしたタスクを追加できます(繰り返しの予定は先の月にも表示されます)</div>"
    )
    return shell("カレンダー", f'{nav}<div class="cal">{cells}</div>{legend}', n_over, "cal", today, wide=True)


# ================================================================ 画面: 詳細
def render_detail(tid, saved=False):
    today = date.today()
    with closing(db()) as con:
        r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            return None
        n_over = sum(1 for x in con.execute("SELECT * FROM tasks") if task_state(x, today) == "overdue")
        n_done = con.execute("SELECT COUNT(*) FROM logs WHERE task_id=?", (tid,)).fetchone()[0]
        done_today = con.execute(
            "SELECT 1 FROM logs WHERE task_id=? AND done_on=? LIMIT 1", (tid, today.isoformat())
        ).fetchone() is not None
        recent = [
            x["done_on"]
            for x in con.execute(
                "SELECT done_on FROM logs WHERE task_id=? ORDER BY id DESC LIMIT 5", (tid,)
            )
        ]
        cats = sorted({x["category"] for x in con.execute("SELECT category FROM tasks") if x["category"]})

    state = task_state(r, today)
    due = date.fromisoformat(r["due_date"])
    diff = (due - today).days
    if state == "overdue":
        badge = f"⚠ {-diff}日超過"
    elif state == "today":
        badge = "TODAY 今日が期限"
    elif state == "upcoming":
        badge = "明日" if diff == 1 else f"あと{diff}日"
    else:
        badge = "完了"
    pills = ""
    if r["priority"] == 0:
        pills += '<span class="pri p0">高</span>'
    elif r["priority"] == 2:
        pills += '<span class="pri p2">低</span>'

    nxt = f"/task/{r['id']}"
    actions = ""
    if state != "done":
        actions += btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn", nxt=nxt)
        if done_today:
            actions += btn_form(f"/undo/{r['id']}", "↩ 前回の完了を取り消し", "sub", nxt=nxt)
    else:
        actions += btn_form(f"/undo/{r['id']}", "↩ 取り消し", "sub", nxt=nxt)
    actions += btn_form(f"/delete/{r['id']}", "🗑", "sub", "このタスクを削除しますか?")

    snooze = ""
    if state != "done":
        snooze = (
            '<div class="snooze">期限を先送り:'
            + btn_form(f"/snooze/{r['id']}/today", "今日へ", "sub", nxt=nxt)
            + btn_form(f"/snooze/{r['id']}/tomorrow", "明日へ", "sub", nxt=nxt)
            + btn_form(f"/snooze/{r['id']}/week", "1週間後へ", "sub", nxt=nxt)
            + "</div>"
        )

    due_label = "次回の期限" if (r["kind"] != "once" and state == "done") else "期限"
    info = [("繰り返し", repeat_label(r)), (due_label, full_date(due)),
            ("作成日", full_date(date.fromisoformat(r["created_at"]))),
            ("優先度", {0: "高", 1: "中", 2: "低"}[r["priority"]]),
            ("カテゴリ", r["category"] or "なし"),
            ("累計完了回数", f"{n_done}回")]
    if recent:
        info.append(("最近の完了日", " / ".join(fmt_date(date.fromisoformat(d)) for d in recent)))
    info_html = "".join(f"<dt>{k}</dt><dd>{esc(v)}</dd>" for k, v in info)

    comment = (r["comment"] or "").strip()
    comment_html = (
        f'<div class="comment">{linkify(comment)}</div>' if comment
        else '<div class="muted">コメントはまだありません</div>'
    )
    items = parse_links(r["links"])
    if items:
        lis = ""
        for label, url in items:
            if url:
                lis += f'<li><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(label)}</a></li>'
            else:
                lis += f"<li>{esc(label)}</li>"
        links_html = f'<ul class="links">{lis}</ul>'
    else:
        links_html = '<div class="muted">リンクはまだありません</div>'

    flash = '<div class="flash">保存しました</div>' if saved else ""
    cat_opts = "".join(f'<option value="{esc(c)}">' for c in cats)
    form = f"""<form class="panel detail-form" method="post" action="/task/{r['id']}/save">
  <h3>✏️ 編集</h3>
  <label>タスク名<input type="text" name="title" maxlength="200" value="{esc(r["title"])}"></label>
  <div class="row">
    {kind_fields(r["kind"], r["due_date"], r["weekday"], r["monthday"], today)}
    <label>優先度<select name="priority">{prio_select(r["priority"])}</select></label>
    <label>カテゴリ<input type="text" name="category" maxlength="30" list="cats" value="{esc(r["category"])}"
      style="width:130px"></label>
    <datalist id="cats">{cat_opts}</datalist>
  </div>
  <label>コメント(URLは自動でリンクになります)
    <textarea name="comment" rows="6" maxlength="5000">{esc(comment)}</textarea></label>
  <label>リンク(1行に1件。「ラベル | URL」または「URL」。http/httpsのみ)
    <textarea name="links" rows="4" maxlength="2000"
      placeholder="仕様書 | https://example.com/spec&#10;https://example.com/other">{esc(r["links"])}</textarea></label>
  <button class="primary">保存</button>
</form>"""
    body = f"""<a class="back" href="/">← 一覧に戻る</a>
{flash}
<div class="card {state} big"><div class="main">
  <div class="title"><span class="badge">{badge}</span>{pills}{esc(r["title"])}</div>
</div><div class="actions">{actions}</div></div>
{snooze}
<div class="panel"><h3>基本情報</h3><dl class="info">{info_html}</dl></div>
<div class="panel"><h3>💬 コメント</h3>{comment_html}</div>
<div class="panel"><h3>🔗 リンク</h3>{links_html}</div>
{form}"""
    return shell(r["title"], body, n_over, "list", today)


# ================================================================ サーバー
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location="/"):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        params = parse_qs(u.query)
        m = re.fullmatch(r"/task/(\d+)", u.path)
        if u.path == "/":
            self._send(200, render_page(params))
        elif u.path == "/calendar":
            self._send(200, render_calendar(params))
        elif m:
            page = render_detail(int(m.group(1)), "saved=1" in u.query)
            if page is None:
                self.send_error(404)
            else:
                self._send(200, page)
        elif u.path == "/export":
            name = f"todo-backup-{date.today().strftime('%Y%m%d')}.json"
            self._send(200, export_data(), "application/json; charset=utf-8",
                       {"Content-Disposition": f'attachment; filename="{name}"'})
        else:
            self.send_error(404)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send_error(403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.send_error(413)
            return
        raw = self.rfile.read(length)
        path = urlparse(self.path).path

        if path == "/import":
            try:
                added = import_data(json.loads(raw.decode("utf-8")))
                out = {"ok": True, "added": added}
            except (ValueError, UnicodeDecodeError) as e:
                out = {"ok": False, "error": str(e)}
            self._send(200, json.dumps(out, ensure_ascii=False), "application/json; charset=utf-8")
            return

        form = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        get = lambda k: (form.get(k) or [""])[0]
        parts = path.strip("/").split("/")
        target = safe_next(get("next"))
        try:
            if parts[0] == "add":
                add_task(get("title"), get("kind"), get("date"), get("weekday"), get("monthday"),
                         get("priority"), get("category"))
            elif parts[0] in ("done", "undo", "delete") and len(parts) == 2:
                {"done": complete_task, "undo": undo_task, "delete": delete_task}[parts[0]](int(parts[1]))
            elif parts[0] == "bulk" and len(parts) == 1:
                ids = [int(x) for x in form.get("ids", [])[:500] if x.isascii() and x.isdigit()]
                bulk_complete(ids)
            elif parts[0] == "snooze" and len(parts) == 3:
                snooze_task(int(parts[1]), parts[2])
            elif parts[0] == "task" and len(parts) == 3 and parts[2] == "save":
                tid = int(parts[1])
                update_task(tid, get)
                self._redirect(f"/task/{tid}?saved=1")
                return
            elif parts[0] == "clear_done":
                clear_done()
            else:
                self.send_error(404)
                return
        except (ValueError, sqlite3.Error):
            self.send_error(400)
            return
        self._redirect(target)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    host = os.environ.get("TODO_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"ToDo 起動: http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}  (終了: Ctrl+C)")
    print(f"データベース: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
