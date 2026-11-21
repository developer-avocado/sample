#!/usr/bin/env python3
"""ToDoリスト Webアプリ(Python標準ライブラリのみ)

起動:  python3 app.py            -> http://127.0.0.1:8000
       python3 app.py 8080       -> ポート指定
データは app.py と同じフォルダの todo.db (SQLite) に保存されます。
"""
import calendar
import html
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "todo.db")
WEEKDAYS = "月火水木金土日"
KINDS = ("once", "daily", "weekly", "monthly")


# ---------------------------------------------------------------- DB
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
            last_done  TEXT,                  -- 最後に完了した日
            created_at TEXT    NOT NULL
        )"""
    )
    # 既存DBへの列追加(コメント・リンク)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}
    if "comment" not in cols:
        con.execute("ALTER TABLE tasks ADD COLUMN comment TEXT NOT NULL DEFAULT ''")
    if "links" not in cols:
        con.execute("ALTER TABLE tasks ADD COLUMN links TEXT NOT NULL DEFAULT ''")
    con.commit()
    return con


# ---------------------------------------------------------------- 期限の計算
def monthly_on_or_after(d, md):
    """d 以降で最初の「毎月md日」。月末を超える指定(31日など)は末日に丸める。"""
    y, m = d.year, d.month
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


# ---------------------------------------------------------------- 操作
def add_task(title, kind, date_str, weekday_str, monthday_str):
    title = (title or "").strip()[:200]
    if not title or kind not in KINDS:
        return
    today = date.today()
    weekday = monthday = None
    if kind == "once":
        try:
            due = date.fromisoformat(date_str)
        except (TypeError, ValueError):
            due = today
    else:
        if kind == "weekly":
            weekday = min(max(int(weekday_str or today.weekday()), 0), 6)
        if kind == "monthly":
            monthday = min(max(int(monthday_str or today.day), 1), 31)
        due = occurrence_on_or_after(kind, today, weekday, monthday)
    con = db()
    with con:
        con.execute(
            "INSERT INTO tasks(title,kind,weekday,monthday,due_date,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (title, kind, weekday, monthday, due.isoformat(), today.isoformat()),
        )
    con.close()


def complete_task(tid):
    today = date.today()
    con = db()
    with con:
        r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            return
        if r["kind"] == "once":
            con.execute(
                "UPDATE tasks SET done=1,last_done=? WHERE id=?",
                (today.isoformat(), tid),
            )
        else:
            # 期限超過なら今日、前倒し完了なら元の期限を基準に「次回」を決める
            base = max(date.fromisoformat(r["due_date"]), today)
            nxt = occurrence_after(r["kind"], base, r["weekday"], r["monthday"])
            con.execute(
                "UPDATE tasks SET due_date=?,last_done=? WHERE id=?",
                (nxt.isoformat(), today.isoformat(), tid),
            )
    con.close()


def undo_task(tid):
    con = db()
    with con:
        con.execute("UPDATE tasks SET done=0 WHERE id=? AND kind='once'", (tid,))
    con.close()


def delete_task(tid):
    con = db()
    with con:
        con.execute("DELETE FROM tasks WHERE id=?", (tid,))
    con.close()


def clear_done():
    con = db()
    with con:
        con.execute("DELETE FROM tasks WHERE kind='once' AND done=1")
    con.close()


def save_detail(tid, title, comment, links):
    title = (title or "").strip()[:200]
    comment = (comment or "").replace("\r\n", "\n").strip()[:5000]
    links = (links or "").replace("\r\n", "\n").strip()[:2000]
    con = db()
    with con:
        if title:
            con.execute(
                "UPDATE tasks SET title=?,comment=?,links=? WHERE id=?",
                (title, comment, links, tid),
            )
        else:  # タイトルが空なら変更しない
            con.execute(
                "UPDATE tasks SET comment=?,links=? WHERE id=?", (comment, links, tid)
            )
    con.close()


# ---------------------------------------------------------------- コメント・リンク
URL_RE = re.compile(r"https?://[^\s<>\"']+")
STRICT_URL_RE = re.compile(r"https?://\S+")


def linkify(text):
    """コメント内のURLをリンク化(それ以外はエスケープ)。http/httpsのみ。"""
    out, pos = [], 0
    for m in URL_RE.finditer(text):
        out.append(html.escape(text[pos:m.start()]))
        url = m.group(0)
        out.append(
            f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(url)}</a>'
        )
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def parse_links(text):
    """1行1件。「URL」または「ラベル | URL」。戻り値: [(表示名, URLまたはNone)]"""
    items = []
    for line in text.splitlines():
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


def task_state(r, today):
    due = date.fromisoformat(r["due_date"])
    if r["kind"] == "once":
        finished = bool(r["done"])
    else:
        finished = r["last_done"] == today.isoformat()
    if finished:
        return "done"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    return "upcoming"


# ---------------------------------------------------------------- 表示
CSS = """
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;
  background:#f1f4f9;color:#1d2433;line-height:1.5}
.wrap{max-width:760px;margin:0 auto;padding:16px 16px 60px}
h1{font-size:1.5rem;margin:8px 0 2px}
.date{color:#5b6477;margin-bottom:14px}
.banner{position:sticky;top:0;z-index:10;margin:0 -16px 16px;padding:12px 16px;text-align:center;
  font-weight:800;font-size:1.05rem;color:#fff;background:#d90429;animation:blink 1s infinite}
.summary{display:flex;gap:8px;margin-bottom:18px;flex-wrap:wrap}
.chip{padding:4px 12px;border-radius:999px;font-size:.85rem;font-weight:700;background:#e3e8f1;color:#394258}
.chip.over{background:#d90429;color:#fff}.chip.today{background:#ff9f1c;color:#3b2500}
form.add{background:#fff;border-radius:12px;padding:14px;box-shadow:0 1px 4px #0001;margin-bottom:22px;
  display:flex;flex-wrap:wrap;gap:8px;align-items:end}
form.add label{display:flex;flex-direction:column;font-size:.78rem;color:#5b6477;gap:2px}
form.add .grow{flex:1 1 220px}
input,select,button{font:inherit}
input[type=text],input[type=date],input[type=number],select{padding:8px 10px;border:1px solid #c9d0de;border-radius:8px;background:#fff}
input[type=number]{width:80px}
button{cursor:pointer;border:0;border-radius:8px;padding:8px 14px;font-weight:700}
.primary{background:#3a6df0;color:#fff}
h2{font-size:1.05rem;margin:26px 0 8px;display:flex;align-items:center;gap:8px}
.empty{color:#8b94a7;font-size:.9rem;padding:6px 2px}
.card{display:flex;align-items:center;gap:12px;background:#fff;border-radius:12px;padding:12px 14px;
  margin-bottom:8px;border-left:6px solid #b8c2d6;box-shadow:0 1px 3px #0001}
.card .main{flex:1;min-width:0}
.title{font-weight:600;word-break:break-word}
.meta{font-size:.8rem;color:#5b6477;margin-top:2px}
.badge{display:inline-block;font-size:.75rem;font-weight:800;padding:2px 9px;border-radius:999px;
  margin-right:6px;background:#e3e8f1;color:#394258}
.actions{display:flex;gap:6px}
.done-btn{background:#16a34a;color:#fff}
.sub{background:#eef1f7;color:#394258;padding:8px 10px}

/* 今日が期限 */
.card.today{background:#fff3c4;border-left:10px solid #ff9f1c;box-shadow:0 2px 8px #ff9f1c66}
.card.today .title{font-size:1.08rem;font-weight:800}
.card.today .badge{background:#ff9f1c;color:#3b2500}

/* 期限超過(最も目立たせる) */
.card.overdue{background:#ffd9d9;border-left:14px solid #d90429;animation:pulse 1.6s infinite}
.card.overdue .title{font-size:1.15rem;font-weight:900;color:#a4001d}
.card.overdue .badge{background:#d90429;color:#fff;font-size:.85rem;animation:blink 1s infinite}
.card.overdue .meta{color:#a4001d;font-weight:700}

.card.done{opacity:.6;border-left-color:#16a34a}
.card.done .title{text-decoration:line-through}

/* 詳細ページ */
.task-link{display:block;color:inherit;text-decoration:none}
.task-link:hover .title{text-decoration:underline}
.card:hover{box-shadow:0 3px 10px #0002}
.back{display:inline-block;margin:6px 0 10px;color:#3a6df0;text-decoration:none;font-weight:700}
.panel{background:#fff;border-radius:12px;padding:14px 16px;box-shadow:0 1px 4px #0001;margin-bottom:16px}
.panel h3{margin:0 0 8px;font-size:.95rem;color:#5b6477}
.panel a{color:#2457d6}
.comment{white-space:pre-wrap;word-break:break-word}
.links{margin:0;padding-left:20px}.links li{margin:4px 0;word-break:break-all}
.info{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:.9rem;margin:0}
.info dt{color:#5b6477}.info dd{margin:0}
.muted{color:#8b94a7}
.flash{background:#dcfce7;color:#14532d;padding:8px 12px;border-radius:8px;margin-bottom:12px;font-weight:700}
.detail-form label{display:flex;flex-direction:column;font-size:.8rem;color:#5b6477;gap:2px;margin-bottom:10px}
.detail-form input[type=text],.detail-form textarea{width:100%;padding:8px 10px;border:1px solid #c9d0de;
  border-radius:8px;font:inherit;color:#1d2433}
.big .title{font-size:1.25rem}

@keyframes blink{50%{opacity:.55}}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 #d9042977}50%{box-shadow:0 0 0 8px #d9042900,0 2px 14px #d9042999}}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""

JS = """
const kind=document.getElementById('kind');
function sync(){document.querySelectorAll('[data-for]').forEach(e=>{e.hidden=e.dataset.for!==kind.value})}
kind.addEventListener('change',sync);sync();
const d0=new Date().toDateString();
setInterval(()=>{if(new Date().toDateString()!==d0)location.reload()},60000);
"""


def fmt_date(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def repeat_label(r):
    if r["kind"] == "daily":
        return "毎日"
    if r["kind"] == "weekly":
        return f"毎週{WEEKDAYS[r['weekday']]}曜"
    if r["kind"] == "monthly":
        return f"毎月{r['monthday']}日"
    return "指定日"


def btn_form(action, label, cls, confirm=None):
    on = f' onsubmit="return confirm(\'{confirm}\')"' if confirm else ""
    return (
        f'<form method="post" action="{action}" style="margin:0"{on}>'
        f'<button class="{cls}">{label}</button></form>'
    )


def card(r, today, section):
    due = date.fromisoformat(r["due_date"])
    diff = (due - today).days
    title = html.escape(r["title"])
    rep = repeat_label(r)
    actions = ""
    if section == "overdue":
        badge = f'<span class="badge">⚠ {-diff}日超過</span>'
        meta = f"{rep} ・ 期限 {fmt_date(due)}"
        actions = btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn")
    elif section == "today":
        badge = '<span class="badge">TODAY 今日が期限</span>'
        meta = rep
        actions = btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn")
    elif section == "upcoming":
        word = "明日" if diff == 1 else f"あと{diff}日"
        badge = f'<span class="badge">{word}</span>'
        meta = f"{rep} ・ 期限 {fmt_date(due)}"
        actions = btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn")
    else:  # done
        badge = '<span class="badge">完了</span>'
        if r["kind"] == "once":
            meta = f"{rep} ・ 期限 {fmt_date(due)}"
            actions = btn_form(f"/undo/{r['id']}", "↩ 戻す", "sub")
        else:
            meta = f"{rep} ・ 次回 {fmt_date(due)}"
    actions += btn_form(f"/delete/{r['id']}", "🗑", "sub", "このタスクを削除しますか?")
    ind = ""
    if (r["comment"] or "").strip():
        ind += " ・ 💬"
    n_links = sum(1 for _, u in parse_links(r["links"] or "") if u)
    if n_links:
        ind += f" ・ 🔗{n_links}"
    return (
        f'<div class="card {section}"><div class="main">'
        f'<a class="task-link" href="/task/{r["id"]}" title="クリックで詳細を表示">'
        f'<div class="title">{badge}{title}</div>'
        f'<div class="meta">{html.escape(meta)}{ind}</div></a>'
        f'</div><div class="actions">{actions}</div></div>'
    )


def section_html(heading, key, items, today, empty, extra=""):
    body = "".join(card(r, today, key) for r in items) or f'<div class="empty">{empty}</div>'
    return f"<h2>{heading}{extra}</h2>{body}"


def render_page():
    today = date.today()
    con = db()
    rows = con.execute("SELECT * FROM tasks").fetchall()
    con.close()

    overdue, today_l, upcoming, completed = [], [], [], []
    for r in rows:
        due = date.fromisoformat(r["due_date"])
        if r["kind"] == "once":
            finished = bool(r["done"])
        else:
            finished = r["last_done"] == today.isoformat()
        if finished:
            completed.append(r)
        elif due < today:
            overdue.append(r)
        elif due == today:
            today_l.append(r)
        else:
            upcoming.append(r)
    overdue.sort(key=lambda r: (r["due_date"], r["id"]))
    today_l.sort(key=lambda r: r["id"])
    upcoming.sort(key=lambda r: (r["due_date"], r["id"]))
    completed.sort(key=lambda r: (r["last_done"] or "", r["id"]), reverse=True)

    n_over = len(overdue)
    banner = (
        f'<div class="banner">⚠ 期限を過ぎた未完了タスクが {n_over} 件あります!</div>'
        if n_over
        else ""
    )
    title_prefix = f"(⚠{n_over}) " if n_over else ""
    wd_opts = "".join(
        f'<option value="{i}"{" selected" if i == today.weekday() else ""}>{w}曜日</option>'
        for i, w in enumerate(WEEKDAYS)
    )
    clear_btn = (
        btn_form("/clear_done", "完了済み(指定日)を一括削除", "sub", "完了済みの指定日タスクを全て削除しますか?")
        if any(r["kind"] == "once" for r in completed)
        else ""
    )

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_prefix}ToDoリスト</title><style>{CSS}</style></head>
<body><div class="wrap">
{banner}
<h1>📝 ToDoリスト</h1>
<div class="date">{today.year}年{today.month}月{today.day}日({WEEKDAYS[today.weekday()]})</div>
<div class="summary">
  <span class="chip over">期限超過 {n_over}</span>
  <span class="chip today">今日 {len(today_l)}</span>
  <span class="chip">今後 {len(upcoming)}</span>
  <span class="chip">完了 {len(completed)}</span>
</div>

<form class="add" method="post" action="/add">
  <label class="grow">タスク名<input type="text" name="title" maxlength="200" required autofocus placeholder="例: 請求書を送る"></label>
  <label>繰り返し
    <select name="kind" id="kind">
      <option value="once">指定日</option><option value="daily">毎日</option>
      <option value="weekly">毎週</option><option value="monthly">毎月</option>
    </select></label>
  <label data-for="once">期限日<input type="date" name="date" value="{today.isoformat()}"></label>
  <label data-for="weekly">曜日<select name="weekday">{wd_opts}</select></label>
  <label data-for="monthly">日にち<input type="number" name="monthday" min="1" max="31" value="{today.day}"></label>
  <button class="primary">追加</button>
</form>

{section_html("🚨 期限超過", "overdue", overdue, today, "期限超過のタスクはありません 👍")}
{section_html("⭐ 今日のタスク", "today", today_l, today, "今日が期限のタスクはありません")}
{section_html("📅 今後のタスク", "upcoming", upcoming, today, "予定されているタスクはありません")}
{section_html("✅ 完了", "done", completed, today, "完了したタスクはまだありません", extra=" " + clear_btn)}
</div><script>{JS}</script></body></html>"""


def render_detail(tid, saved=False):
    today = date.today()
    con = db()
    r = con.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    con.close()
    if r is None:
        return None

    esc = html.escape
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

    full = lambda d: f"{d.year}年{d.month}月{d.day}日({WEEKDAYS[d.weekday()]})"
    due_label = "次回の期限" if (r["kind"] != "once" and state == "done") else "期限"
    info = [
        ("繰り返し", repeat_label(r)),
        (due_label, full(due)),
        ("作成日", full(date.fromisoformat(r["created_at"]))),
    ]
    if r["last_done"]:
        info.append(("最後に完了した日", full(date.fromisoformat(r["last_done"]))))
    info_html = "".join(f"<dt>{k}</dt><dd>{esc(v)}</dd>" for k, v in info)

    comment = (r["comment"] or "").strip()
    comment_html = (
        f'<div class="comment">{linkify(comment)}</div>'
        if comment
        else '<div class="muted">コメントはまだありません</div>'
    )

    items = parse_links(r["links"] or "")
    if items:
        lis = ""
        for label, url in items:
            if url:
                lis += (
                    f'<li><a href="{esc(url, quote=True)}" target="_blank" '
                    f'rel="noopener noreferrer">{esc(label)}</a></li>'
                )
            else:
                lis += f"<li>{esc(label)}</li>"
        links_html = f'<ul class="links">{lis}</ul>'
    else:
        links_html = '<div class="muted">リンクはまだありません</div>'

    actions = ""
    if state != "done":
        actions += btn_form(f"/done/{r['id']}", "✓ 完了", "done-btn")
    elif r["kind"] == "once":
        actions += btn_form(f"/undo/{r['id']}", "↩ 戻す", "sub")
    actions += btn_form(f"/delete/{r['id']}", "🗑", "sub", "このタスクを削除しますか?")

    flash = '<div class="flash">保存しました</div>' if saved else ""
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(r["title"])} - ToDoリスト</title><style>{CSS}</style></head>
<body><div class="wrap">
<a class="back" href="/">← 一覧に戻る</a>
{flash}
<div class="card {state} big"><div class="main">
  <div class="title"><span class="badge">{badge}</span>{esc(r["title"])}</div>
</div><div class="actions">{actions}</div></div>

<div class="panel"><h3>基本情報</h3><dl class="info">{info_html}</dl></div>
<div class="panel"><h3>💬 コメント</h3>{comment_html}</div>
<div class="panel"><h3>🔗 リンク</h3>{links_html}</div>

<form class="panel detail-form" method="post" action="/task/{r['id']}/save">
  <h3>✏️ 編集</h3>
  <label>タスク名<input type="text" name="title" maxlength="200" value="{esc(r["title"], quote=True)}"></label>
  <label>コメント(URLは自動でリンクになります)
    <textarea name="comment" rows="6" maxlength="5000">{esc(comment)}</textarea></label>
  <label>リンク(1行に1件。「ラベル | URL」または「URL」。http/httpsのみ)
    <textarea name="links" rows="4" maxlength="2000" placeholder="仕様書 | https://example.com/spec&#10;https://example.com/other">{esc(r["links"] or "")}</textarea></label>
  <button class="primary">保存</button>
</form>
</div></body></html>"""


# ---------------------------------------------------------------- サーバー
class Handler(BaseHTTPRequestHandler):
    def _redirect(self, location="/"):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        m = re.fullmatch(r"/task/(\d+)", path)
        if path == "/":
            body = render_page().encode("utf-8")
        elif m:
            page = render_detail(int(m.group(1)), "saved=1" in self.path)
            if page is None:
                self.send_error(404)
                return
            body = page.encode("utf-8")
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        get = lambda k: (form.get(k) or [""])[0]
        parts = self.path.strip("/").split("/")
        try:
            if parts[0] == "add":
                add_task(get("title"), get("kind"), get("date"), get("weekday"), get("monthday"))
            elif parts[0] in ("done", "undo", "delete") and len(parts) == 2:
                {"done": complete_task, "undo": undo_task, "delete": delete_task}[parts[0]](int(parts[1]))
            elif parts[0] == "task" and len(parts) == 3 and parts[2] == "save":
                tid = int(parts[1])
                save_detail(tid, get("title"), get("comment"), get("links"))
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
        self._redirect()


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"ToDoアプリ起動: http://127.0.0.1:{port}  (終了: Ctrl+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
