#!/usr/bin/env python3
"""チャット定型文 - Slack/Teams/メール返信などの定型文をワンクリックでコピーできるWebアプリ

標準ライブラリのみで動作します（追加インストール不要）。

    python3 chat_templates.py                # http://127.0.0.1:8200
    python3 chat_templates.py --port 9000
    python3 chat_templates.py --host 0.0.0.0 # 社内LANで共有する場合

定型文は同じフォルダの templates.db (SQLite) に保存されます（環境変数 TEMPLATE_DB で変更可）。
本文中の {お客様名} のような {項目} は、使うときに入力欄として表示されます。
自動で入る項目: {今日} {曜日} {挨拶}（時間帯で変化）
"""
import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get(
    "TEMPLATE_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.db"),
)

DEFAULTS = [
    ("お客様対応", "お問い合わせへの初回返信",
     "{お客様名}様\n\nお問い合わせいただきありがとうございます。{担当者}と申します。\n内容を確認し、{回答期限}までにご連絡いたします。\n今しばらくお待ちください。"),
    ("お客様対応", "お詫び（不具合）",
     "{お客様名}様\n\nこの度はご不便をおかけし、誠に申し訳ございません。\n{不具合内容}について原因を調査しております。\n状況が分かり次第、改めてご連絡いたします。"),
    ("お客様対応", "対応完了のご連絡",
     "{お客様名}様\n\nお問い合わせの件、{今日}付けで対応が完了いたしました。\nご不明な点がございましたら、お気軽にお知らせください。"),
    ("社内連絡", "日程調整の依頼",
     "{挨拶}。{件名}の打ち合わせについて、{候補日1}または{候補日2}でご都合いかがでしょうか。\nご確認をお願いします。"),
    ("社内連絡", "休暇のお知らせ",
     "{挨拶}。{休暇日}は休暇をいただきます。\n緊急の際はチャットでご連絡ください。ご迷惑をおかけしますが、よろしくお願いします。"),
    ("社内連絡", "確認・承知しました",
     "{挨拶}。承知しました。{今日}（{曜日}）中に確認してご返信します。"),
    ("営業", "商談のお礼",
     "{お客様名}様\n\n本日はお時間をいただき、ありがとうございました。\n{次回アクション}について、{回答期限}までにご連絡いたします。"),
]


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS templates ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, title TEXT NOT NULL,"
            "body TEXT NOT NULL, uses INTEGER NOT NULL DEFAULT 0)"
        )
        if con.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            con.executemany("INSERT INTO templates (category, title, body) VALUES (?,?,?)", DEFAULTS)


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>チャット定型文</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#1f2933;--sub:#6b7785;--line:#e1e6ec;--accent:#5b4fc4}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--accent);color:#fff;padding:14px 24px;font-size:18px;font-weight:600;display:flex;justify-content:space-between;align-items:center}
main{max-width:1100px;margin:20px auto;padding:0 16px;display:grid;gap:14px}
input,select,button,textarea{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}
textarea{width:100%;resize:vertical}
button{cursor:pointer}
button.primary{background:var(--accent);color:#fff;border:none}
button.chip{border-radius:99px;padding:5px 14px;font-size:13px}
button.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger{color:#c0392b;border-color:#e8c4bf}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.bar input[type=search]{flex:1;min-width:200px}
.list{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}
.t{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;cursor:pointer;display:grid;gap:6px;align-content:start}
.t:hover{border-color:var(--accent);box-shadow:0 2px 8px rgba(91,79,196,.12)}
.t small{color:var(--sub);font-size:11px}
.t b{font-size:15px}
.t pre{margin:0;white-space:pre-wrap;font:inherit;font-size:13px;color:var(--sub);max-height:5.6em;overflow:hidden}
.vars{display:flex;gap:4px;flex-wrap:wrap}
.vars span{background:#eeecfa;color:#42399a;border-radius:4px;padding:1px 6px;font-size:11px}
dialog{border:none;border-radius:12px;padding:0;width:min(640px,94vw);box-shadow:0 10px 40px rgba(0,0,0,.25)}
dialog::backdrop{background:rgba(20,25,35,.45)}
.dlg{padding:18px;display:grid;gap:10px}
.dlg h2{margin:0;font-size:16px}
label{font-size:12px;color:var(--sub);display:grid;gap:4px}
.fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
.preview{white-space:pre-wrap;background:#f7f7fb;border:1px solid var(--line);border-radius:8px;padding:12px;font-size:14px;min-height:5em}
.preview mark{background:#ffe9a8;border-radius:3px}
.actions{display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap}
.empty{color:var(--sub);text-align:center;padding:40px 0}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#1f2933;color:#fff;padding:9px 16px;border-radius:99px;font-size:13px;opacity:0;transition:.2s;pointer-events:none;z-index:9}
#toast.show{opacity:.95}
</style></head><body>
<header><span>チャット定型文</span><button class="primary" id="new" style="background:#fff;color:var(--accent)">＋ 新規作成</button></header>
<main>
  <div class="bar"><input type="search" id="q" placeholder="キーワードで検索（タイトル・本文）"><span id="cats" class="bar"></span></div>
  <div class="list" id="list"></div>
</main>

<dialog id="useDlg"><div class="dlg">
  <h2 id="useTitle"></h2>
  <div class="fields" id="fields"></div>
  <div class="preview" id="preview"></div>
  <div class="actions">
    <button id="useEdit">編集</button>
    <button id="useClose">閉じる</button>
    <button class="primary" id="useCopy">コピーして閉じる</button>
  </div>
</div></dialog>

<dialog id="editDlg"><div class="dlg">
  <h2 id="editHead">定型文を編集</h2>
  <label>カテゴリ<input id="eCat" list="catlist" maxlength="20"><datalist id="catlist"></datalist></label>
  <label>タイトル<input id="eTitle" maxlength="60"></label>
  <label>本文（{項目名} と書くと使用時に入力欄になります）<textarea id="eBody" rows="8" maxlength="3000"></textarea></label>
  <div class="actions">
    <button class="danger" id="eDel">削除</button><span style="flex:1"></span>
    <button id="eCancel">キャンセル</button><button class="primary" id="eSave">保存</button>
  </div>
</div></dialog>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const W = ['日','月','火','水','木','金','土'];
let items = [], cat = '全部', current = null, editing = null;

const store = {
  get(){ try{ return JSON.parse(localStorage.getItem('vars')||'{}'); }catch(e){ return {}; } },
  set(o){ try{ localStorage.setItem('vars', JSON.stringify(o)); }catch(e){} }
};
function builtins(){
  const d = new Date(), h = d.getHours();
  return {'今日': `${d.getMonth()+1}月${d.getDate()}日`, '曜日': W[d.getDay()],
          '挨拶': h < 11 ? 'おはようございます' : h < 18 ? 'お疲れ様です' : 'お疲れ様です（夜分に失礼します）'};
}
const varsOf = body => [...new Set([...body.matchAll(/\{([^{}]+)\}/g)].map(m => m[1]))];
const manualVars = body => varsOf(body).filter(v => !(v in builtins()));
function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(toast.t); toast.t=setTimeout(()=>t.classList.remove('show'),1500); }
async function api(method, url, data){
  const r = await fetch(url, {method, headers:{'Content-Type':'application/json'}, body: data ? JSON.stringify(data) : undefined});
  return r.json();
}
async function load(){ items = await api('GET','/api/templates'); render(); }

function render(){
  const cats = ['全部', ...new Set(items.map(i=>i.category))];
  if(!cats.includes(cat)) cat = '全部';
  const cb = $('#cats'); cb.innerHTML = '';
  for(const c of cats){ const b=document.createElement('button'); b.className='chip'+(c===cat?' on':''); b.textContent=c; b.onclick=()=>{cat=c;render();}; cb.append(b); }
  $('#catlist').innerHTML = ''; for(const c of cats.slice(1)){ const o=document.createElement('option'); o.value=c; $('#catlist').append(o); }

  const q = $('#q').value.trim().toLowerCase();
  const shown = items.filter(i => (cat==='全部'||i.category===cat) && (!q || (i.title+i.body).toLowerCase().includes(q)));
  const box = $('#list'); box.innerHTML = '';
  for(const i of shown){
    const el = document.createElement('div'); el.className='t';
    const s = document.createElement('small'); s.textContent = `${i.category} ・ 使用 ${i.uses}回`;
    const t = document.createElement('b'); t.textContent = i.title;
    const p = document.createElement('pre'); p.textContent = i.body;
    const v = document.createElement('div'); v.className='vars';
    for(const n of manualVars(i.body)){ const sp=document.createElement('span'); sp.textContent=n; v.append(sp); }
    el.append(s,t,p,v); el.onclick = () => openUse(i); box.append(el);
  }
  if(!shown.length){ box.innerHTML = '<div class="empty">該当する定型文がありません</div>'; }
}
$('#q').oninput = render;

function fill(body, vals){ const b = builtins(); return body.replace(/\{([^{}]+)\}/g, (m,k) => k in b ? b[k] : (vals[k] ? vals[k] : m)); }
function openUse(i){
  current = i; $('#useTitle').textContent = i.title;
  const saved = store.get(), box = $('#fields'); box.innerHTML = '';
  for(const n of manualVars(i.body)){
    const l = document.createElement('label'); l.textContent = n;
    const inp = document.createElement('input'); inp.dataset.k = n; inp.value = saved[n] || '';
    inp.oninput = updatePreview; l.append(inp); box.append(l);
  }
  updatePreview(); $('#useDlg').showModal();
  const first = box.querySelector('input'); if(first) first.focus();
}
function values(){ const v = {}; document.querySelectorAll('#fields input').forEach(i => v[i.dataset.k] = i.value.trim()); return v; }
function updatePreview(){
  const vals = values(), b = builtins(), pv = $('#preview'); pv.innerHTML = '';
  // 未入力の項目は目立たせて表示（textContentで安全に組み立てる）
  for(const part of current.body.split(/(\{[^{}]+\})/)){
    const m = part.match(/^\{([^{}]+)\}$/);
    if(m && !(m[1] in b) && !vals[m[1]]){ const mk=document.createElement('mark'); mk.textContent=part; pv.append(mk); }
    else pv.append(document.createTextNode(m ? fill(part, vals) : part));
  }
}
async function copy(text){
  try{ await navigator.clipboard.writeText(text); }
  catch(e){ const a=document.createElement('textarea'); a.value=text; document.body.appendChild(a); a.select(); document.execCommand('copy'); a.remove(); }
}
$('#useCopy').onclick = async () => {
  const vals = values(); const missing = Object.keys(vals).filter(k => !vals[k]);
  if(missing.length && !confirm(`未入力の項目があります（${missing.join('、')}）。このままコピーしますか？`)) return;
  await copy(fill(current.body, vals));
  store.set({...store.get(), ...Object.fromEntries(Object.entries(vals).filter(([,v])=>v))});
  await api('POST', `/api/templates/${current.id}/use`);
  $('#useDlg').close(); toast('コピーしました'); load();
};
$('#useClose').onclick = () => $('#useDlg').close();
$('#useEdit').onclick = () => { $('#useDlg').close(); openEdit(current); };

function openEdit(i){
  editing = i || null;
  $('#editHead').textContent = i ? '定型文を編集' : '定型文を新規作成';
  $('#eCat').value = i ? i.category : (cat==='全部' ? '' : cat);
  $('#eTitle').value = i ? i.title : ''; $('#eBody').value = i ? i.body : '';
  $('#eDel').style.display = i ? '' : 'none';
  $('#editDlg').showModal();
}
$('#new').onclick = () => openEdit(null);
$('#eCancel').onclick = () => $('#editDlg').close();
$('#eSave').onclick = async () => {
  const data = {category: $('#eCat').value.trim() || '未分類', title: $('#eTitle').value.trim(), body: $('#eBody').value.trim()};
  if(!data.title || !data.body){ toast('タイトルと本文を入力してください'); return; }
  if(editing) await api('PUT', `/api/templates/${editing.id}`, data); else await api('POST', '/api/templates', data);
  $('#editDlg').close(); toast('保存しました'); load();
};
$('#eDel').onclick = async () => {
  if(!confirm(`「${editing.title}」を削除しますか？`)) return;
  await api('DELETE', `/api/templates/${editing.id}`); $('#editDlg').close(); load();
};
load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self):
        size = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
        category = str(data["category"]).strip()[:20] or "未分類"
        title = str(data["title"]).strip()[:60]
        body = str(data["body"]).strip()[:3000]
        if not title or not body:
            raise ValueError
        return category, title, body

    def _id(self, tail=None):
        parts = self.path.strip("/").split("/")  # api/templates/<id>[/use]
        if len(parts) >= 3 and parts[:2] == ["api", "templates"] and parts[2].isdigit():
            if tail is None and len(parts) == 3 or tail is not None and parts[3:] == [tail]:
                return int(parts[2])
        return None

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/templates":
            with db() as con:
                rows = [dict(r) for r in con.execute(
                    "SELECT id, category, title, body, uses FROM templates ORDER BY uses DESC, id")]
            return self._send(200, json.dumps(rows, ensure_ascii=False))
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path == "/api/templates":
            try:
                vals = self._json_body()
            except (ValueError, KeyError, TypeError):
                return self._send(400, '{"error":"invalid"}')
            with db() as con:
                cur = con.execute("INSERT INTO templates (category, title, body) VALUES (?,?,?)", vals)
            return self._send(201, json.dumps({"id": cur.lastrowid}))
        tid = self._id("use")
        if tid is not None:
            with db() as con:
                con.execute("UPDATE templates SET uses = uses + 1 WHERE id=?", (tid,))
            return self._send(200, '{"ok":true}')
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_PUT(self):
        tid = self._id()
        if tid is None:
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        try:
            vals = self._json_body()
        except (ValueError, KeyError, TypeError):
            return self._send(400, '{"error":"invalid"}')
        with db() as con:
            con.execute("UPDATE templates SET category=?, title=?, body=? WHERE id=?", (*vals, tid))
        self._send(200, '{"ok":true}')

    def do_DELETE(self):
        tid = self._id()
        if tid is None:
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        with db() as con:
            con.execute("DELETE FROM templates WHERE id=?", (tid,))
        self._send(200, '{"ok":true}')

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser(description="チャット定型文")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    a = p.parse_args()
    init_db()
    print(f"起動しました: http://{a.host}:{a.port}  (終了は Ctrl+C)")
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
