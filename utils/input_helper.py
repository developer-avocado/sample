#!/usr/bin/env python3
"""入力支援ツール - 日付・定型文・テキスト整形をワンクリックでコピーできるWebアプリ

標準ライブラリのみで動作します（追加インストール不要）。

    python3 input_helper.py                # http://127.0.0.1:8100
    python3 input_helper.py --port 9000

定型文は同じフォルダの snippets.db (SQLite) に保存されます。
環境変数 SNIPPET_DB で保存先を変更できます。
"""
import argparse
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get(
    "SNIPPET_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "snippets.db"),
)

DEFAULT_SNIPPETS = [
    ("日報の書き出し", "{日付}（{曜日}）の業務報告です。"),
    ("打ち合わせ依頼", "お世話になっております。\n{日付}（{曜日}）に30分ほどお打ち合わせのお時間をいただけますでしょうか。"),
    ("納期のご連絡", "納期は{月末}（{月末曜日}）を予定しております。"),
    ("お礼", "本日はお忙しい中お時間をいただき、誠にありがとうございました。"),
]


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS snippets ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL)"
        )
        if con.execute("SELECT COUNT(*) FROM snippets").fetchone()[0] == 0:
            con.executemany("INSERT INTO snippets (title, body) VALUES (?,?)", DEFAULT_SNIPPETS)


PAGE = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>入力支援ツール</title>
<style>
:root{--bg:#f4f6f9;--card:#fff;--ink:#1f2933;--sub:#6b7785;--line:#e1e6ec;--accent:#0f7b6c}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic",Meiryo,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--accent);color:#fff;padding:14px 24px;font-size:18px;font-weight:600}
main{max-width:1000px;margin:20px auto;padding:0 16px;display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}
h2{font-size:15px;margin:0 0 12px}
h3{font-size:12px;color:var(--sub);font-weight:500;margin:14px 0 6px}
input,select,button,textarea{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--ink)}
textarea{width:100%;resize:vertical}
button{cursor:pointer}
button.primary{background:var(--accent);color:#fff;border:none}
button.chip{background:#eef5f3;border-color:#cfe3de;color:#0b5a4f}
button.chip.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.formats{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px}
.fmt{text-align:left;display:grid;gap:2px;padding:9px 12px}
.fmt small{color:var(--sub);font-size:11px}
.fmt:hover,.snip:hover{border-color:var(--accent);background:#f3faf8}
.snips{display:grid;gap:8px}
.snip{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;border:1px solid var(--line);border-radius:8px;padding:10px 12px;cursor:pointer}
.snip b{font-size:14px}
.snip pre{margin:4px 0 0;white-space:pre-wrap;font:inherit;font-size:13px;color:var(--sub)}
.snip button{color:#c0392b;border-color:#e8c4bf}
.hint{font-size:12px;color:var(--sub)}
.grid2{display:grid;grid-template-columns:1fr 2fr;gap:8px;margin-bottom:8px}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#1f2933;color:#fff;padding:9px 16px;border-radius:99px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}
#toast.show{opacity:.95}
@media(max-width:600px){.grid2{grid-template-columns:1fr}}
</style></head><body>
<header>入力支援ツール</header>
<main>

<section class="card"><h2>日付を入力する</h2>
<div class="row">
  <label>基準日 <input type="date" id="base"></label>
  <button class="chip" data-shift="today">今日</button>
  <button class="chip" data-shift="1">明日</button>
  <button class="chip" data-shift="2">明後日</button>
  <button class="chip" data-shift="nextmon">来週月曜</button>
  <button class="chip" data-shift="eom">月末</button>
  <button class="chip" data-shift="nextfirst">翌月1日</button>
  <label>営業日後 <input type="number" id="bizn" value="3" min="1" max="365" style="width:70px"></label>
  <button class="chip" data-shift="biz">移動</button>
</div>
<div class="hint" style="margin-top:6px">営業日は土日のみ除外します（祝日は考慮しません）。書式をクリックするとコピーされます。</div>
<h3>書式（クリックでコピー）</h3>
<div class="formats" id="formats"></div>
</section>

<section class="card"><h2>定型文</h2>
<div class="hint">クリックで、基準日を反映してコピーします。差し込み: {日付} {和暦} {曜日} {年} {月} {日} {月末} {月末曜日} {翌営業日}</div>
<div class="snips" id="snips" style="margin-top:10px"></div>
<h3>定型文を追加</h3>
<div class="grid2"><input id="newTitle" placeholder="タイトル" maxlength="40"><div></div></div>
<textarea id="newBody" rows="3" placeholder="本文（{日付} などを使えます）"></textarea>
<div class="row" style="margin-top:8px"><button class="primary" id="add">追加</button></div>
</section>

<section class="card"><h2>テキスト整形</h2>
<textarea id="scratch" rows="5" placeholder="ここに貼り付けて整形します"></textarea>
<div class="row" style="margin-top:8px">
  <button class="chip" data-fix="half">全角英数・記号 → 半角</button>
  <button class="chip" data-fix="full">半角カナ → 全角</button>
  <button class="chip" data-fix="trim">前後・行末の空白を除去</button>
  <button class="chip" data-fix="blank">空行を詰める</button>
  <button class="chip" data-fix="join">改行を除去</button>
  <button class="primary" id="copyScratch">コピー</button>
</div></section>
</main><div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const W = ['日','月','火','水','木','金','土'];
const pad = n => String(n).padStart(2,'0');
const toISO = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
const fromISO = s => { const [y,m,d] = s.split('-').map(Number); return new Date(y,m-1,d); };
const addDays = (d,n) => new Date(d.getFullYear(), d.getMonth(), d.getDate()+n);
const eom = d => new Date(d.getFullYear(), d.getMonth()+1, 0);
const addBiz = (d,n) => { let c = d; while(n>0){ c = addDays(c,1); if(c.getDay()%6) n--; } return c; };
function wareki(d, short){
  const t = d.getTime();
  const eras = [['令和',new Date(2019,4,1),2018,'R'],['平成',new Date(1989,0,8),1988,'H'],['昭和',new Date(1926,11,25),1925,'S'],['大正',new Date(1912,6,30),1911,'T'],['明治',new Date(1868,0,25),1867,'M']];
  for(const [n,s,off,a] of eras) if(t>=s.getTime()){
    const y = d.getFullYear()-off;
    return short ? `${a}${y}` : `${n}${y===1?'元':y}年`;
  }
  return `${d.getFullYear()}年`;
}
function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(toast.t); toast.t=setTimeout(()=>t.classList.remove('show'),1400); }
async function copy(text){
  try{ await navigator.clipboard.writeText(text); }
  catch(e){ const a=document.createElement('textarea'); a.value=text; document.body.appendChild(a); a.select(); document.execCommand('copy'); a.remove(); }
  toast('コピーしました: ' + (text.length>30 ? text.slice(0,30)+'…' : text).replace(/\n/g,' '));
}
const base = () => fromISO($('#base').value || toISO(new Date()));

function formatsOf(d){
  const y=d.getFullYear(), m=d.getMonth()+1, dd=d.getDate(), w=W[d.getDay()];
  return [
    ['スラッシュ', `${y}/${m}/${dd}`],
    ['スラッシュ（ゼロ埋め）', `${y}/${pad(m)}/${pad(dd)}`],
    ['ハイフン (ISO)', `${y}-${pad(m)}-${pad(dd)}`],
    ['年月日', `${y}年${m}月${dd}日`],
    ['年月日＋曜日', `${y}年${m}月${dd}日（${w}）`],
    ['月日＋曜日', `${m}月${dd}日（${w}）`],
    ['月/日(曜日)', `${m}/${dd}(${w})`],
    ['数字8桁', `${y}${pad(m)}${pad(dd)}`],
    ['和暦', `${wareki(d)}${m}月${dd}日`],
    ['和暦（略称）', `${wareki(d,true)}.${m}.${dd}`],
    ['年月のみ', `${y}年${m}月`],
    ['ファイル名向け', `${y}${pad(m)}${pad(dd)}_`],
  ];
}
function renderFormats(){
  const box = $('#formats'); box.innerHTML='';
  for(const [label,val] of formatsOf(base())){
    const b = document.createElement('button'); b.className='fmt';
    const s = document.createElement('span'); s.textContent = val;
    const l = document.createElement('small'); l.textContent = label;
    b.append(s,l); b.onclick = () => copy(val); box.append(b);
  }
  renderSnips();
}
function expand(text){
  const d=base(), e=eom(d), nb=addBiz(d,1);
  const map = {'日付':`${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`,'和暦':`${wareki(d)}${d.getMonth()+1}月${d.getDate()}日`,
    '曜日':W[d.getDay()],'年':d.getFullYear(),'月':d.getMonth()+1,'日':d.getDate(),
    '月末':`${e.getMonth()+1}月${e.getDate()}日`,'月末曜日':W[e.getDay()],
    '翌営業日':`${nb.getMonth()+1}月${nb.getDate()}日（${W[nb.getDay()]}）`};
  return text.replace(/\{([^{}]+)\}/g,(m,k)=> k in map ? map[k] : m);
}

document.querySelectorAll('[data-shift]').forEach(b => b.onclick = () => {
  const k=b.dataset.shift, d=base(); let r;
  if(k==='today') r=new Date();
  else if(k==='eom') r=eom(d);
  else if(k==='nextfirst') r=new Date(d.getFullYear(),d.getMonth()+1,1);
  else if(k==='nextmon') r=addDays(d, (8-d.getDay())%7 || 7);
  else if(k==='biz') r=addBiz(d, Math.max(1, parseInt($('#bizn').value)||1));
  else r=addDays(new Date(), Number(k));
  $('#base').value = toISO(r); renderFormats();
});
$('#base').onchange = renderFormats;

let snippets = [];
async function loadSnips(){ snippets = await (await fetch('/api/snippets')).json(); renderSnips(); }
function renderSnips(){
  const box=$('#snips'); box.innerHTML='';
  for(const s of snippets){
    const el=document.createElement('div'); el.className='snip';
    const left=document.createElement('div');
    const t=document.createElement('b'); t.textContent=s.title;
    const p=document.createElement('pre'); p.textContent=expand(s.body);
    left.append(t,p);
    const del=document.createElement('button'); del.textContent='削除';
    del.onclick = async ev => { ev.stopPropagation(); if(!confirm(`「${s.title}」を削除しますか？`)) return;
      await fetch('/api/snippets/'+s.id,{method:'DELETE'}); loadSnips(); };
    el.onclick = () => copy(expand(s.body));
    el.append(left,del); box.append(el);
  }
  if(!snippets.length) box.textContent='定型文はまだありません。';
}
$('#add').onclick = async () => {
  const title=$('#newTitle').value.trim(), body=$('#newBody').value.trim();
  if(!title||!body){ toast('タイトルと本文を入力してください'); return; }
  await fetch('/api/snippets',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,body})});
  $('#newTitle').value=''; $('#newBody').value=''; loadSnips();
};

const fixes = {
  half: t => t.replace(/[！-～]/g,c=>String.fromCharCode(c.charCodeAt(0)-0xFEE0)).replace(/　/g,' ').replace(/[－ー―‐]/g,m=>m==='－'?'-':m),
  full: t => t.normalize('NFKC') === t ? t : t.replace(/[ｦ-ﾟ]+/g,m=>m.normalize('NFKC')),
  trim: t => t.split('\n').map(l=>l.trim()).join('\n').trim(),
  blank: t => t.split('\n').filter(l=>l.trim()!=='').join('\n'),
  join: t => t.replace(/\r?\n/g,''),
};
document.querySelectorAll('[data-fix]').forEach(b => b.onclick = () => { const a=$('#scratch'); a.value = fixes[b.dataset.fix](a.value); });
$('#copyScratch').onclick = () => copy($('#scratch').value);

$('#base').value = toISO(new Date());
renderFormats(); loadSnips();
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

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/snippets":
            with db() as con:
                rows = [dict(r) for r in con.execute("SELECT id, title, body FROM snippets ORDER BY id")]
            return self._send(200, json.dumps(rows, ensure_ascii=False))
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/snippets":
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        try:
            size = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(size).decode("utf-8"))
            title, body = str(data["title"]).strip()[:40], str(data["body"]).strip()[:2000]
            if not title or not body:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self._send(400, '{"error":"invalid"}')
        with db() as con:
            cur = con.execute("INSERT INTO snippets (title, body) VALUES (?,?)", (title, body))
        self._send(201, json.dumps({"id": cur.lastrowid}))

    def do_DELETE(self):
        last = self.path.rsplit("/", 1)[-1]
        if self.path.startswith("/api/snippets/") and last.isdigit():
            with db() as con:
                con.execute("DELETE FROM snippets WHERE id=?", (int(last),))
            return self._send(200, '{"ok":true}')
        self._send(404, "Not Found", "text/plain; charset=utf-8")

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser(description="入力支援ツール")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8100)
    a = p.parse_args()
    init_db()
    print(f"起動しました: http://{a.host}:{a.port}  (終了は Ctrl+C)")
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n終了します")


if __name__ == "__main__":
    main()
