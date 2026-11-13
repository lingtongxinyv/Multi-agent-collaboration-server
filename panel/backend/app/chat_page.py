"""对话接口的极简网页（/chat）—— 不依赖前端构建，单文件返回。

为什么不做成 React 页面：面板前端是 `npm run build` 的产物，以 root 构建会留下
root 属主的 dist（2026-10-15 N2 事故同源）。对话页只需要「列表 + 消息 + 输入框」，
用一份自带样式的 HTML 反而更稳、更好改。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

page_router = APIRouter()

HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>助手 · 对话接口</title>
<style>
  :root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e9ef;--dim:#8b94a7;--accent:#7aa2f7}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.6 system-ui,-apple-system,"Noto Sans SC",sans-serif;background:var(--bg);color:var(--fg);height:100vh;display:flex;flex-direction:column}
  header{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
  header b{font-size:15px}
  #dot{width:8px;height:8px;border-radius:50%;background:#444}
  #dot.on{background:#39d98a;box-shadow:0 0 8px #39d98a}
  #meta{margin-left:auto;color:var(--dim);font-size:12px}
  main{flex:1;display:flex;min-height:0}
  aside{width:250px;border-right:1px solid var(--line);display:flex;flex-direction:column;background:var(--panel)}
  aside .bar{padding:8px;border-bottom:1px solid var(--line);display:flex;gap:6px}
  button{background:#20263a;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 10px;cursor:pointer}
  button:hover{border-color:var(--accent)}
  #threads{overflow:auto;flex:1}
  .thread{padding:8px 10px;border-bottom:1px solid #1d2129;cursor:pointer;font-size:13px}
  .thread:hover{background:#1c2029}
  .thread.sel{background:#1e2637}
  .thread small{color:var(--dim);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  section{flex:1;display:flex;flex-direction:column;min-width:0}
  #posts{flex:1;overflow:auto;padding:14px 18px}
  .post{margin:0 0 14px;max-width:78%}
  .post .who{font-size:12px;color:var(--dim);margin-bottom:3px}
  .post .bubble{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 12px;white-space:pre-wrap;word-break:break-word}
  .post.me{margin-left:auto;text-align:right}
  .post.me .bubble{background:#1d2a45;border-color:#2b3d63;text-align:left}
  .post.sys .bubble{background:transparent;border-style:dashed;color:var(--dim);font-size:12px}
  footer{border-top:1px solid var(--line);padding:10px;display:flex;gap:8px;background:var(--panel)}
  textarea{flex:1;resize:none;height:46px;background:#12151c;color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font:inherit}
  .hint{color:var(--dim);font-size:12px;padding:0 16px 8px}
  #login{position:fixed;inset:0;background:#0f1115ee;display:none;align-items:center;justify-content:center}
  #login div{background:var(--panel);border:1px solid var(--line);padding:20px;border-radius:10px;width:340px}
  #login input{width:100%;margin:8px 0;background:#12151c;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:8px}
</style></head><body>
<header><span id="dot"></span><b>助手 · 对话接口</b><span id="meta">连接中…</span></header>
<main>
  <aside>
    <div class="bar"><button id="new">＋ 新线程</button><button id="refresh">刷新</button></div>
    <div id="threads"></div>
  </aside>
  <section>
    <div id="posts"><div class="hint">选一个线程，或者新建一个。</div></div>
    <div class="hint" id="quota"></div>
    <footer><textarea id="input" placeholder="和助手说点什么…（Enter 发送，Shift+Enter 换行）"></textarea><button id="send">发送</button></footer>
  </section>
</main>
<div id="login"><div><b>需要对话凭据</b><p class="hint">已登录面板可直接用；否则粘贴 peer token。</p>
<input id="tok" placeholder="Bearer token"><button id="save">保存并连接</button></div></div>
<script>
const $ = s => document.querySelector(s);
let token = localStorage.getItem('chat_token') || '';
let tid = localStorage.getItem('chat_tid') || '';
let cursor = 0, polling = false;

function headers(){ const h = {'Content-Type':'application/json'}; if(token) h.Authorization = 'Bearer ' + token; return h; }
async function api(path, opt={}){
  const r = await fetch(path, {credentials:'include', headers:headers(), ...opt});
  if(r.status === 401){ $('#login').style.display='flex'; throw new Error('401'); }
  const d = await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(d.detail || ('HTTP '+r.status));
  return d;
}
async function health(){
  try{
    const d = await api('/api/chat/health');
    $('#dot').classList.add('on');
    $('#meta').textContent = d.as + ' · ' + d.model + ' · 今日 ' + d.tokens_today + '/' + d.token_cap + ' tok';
  }catch(e){ $('#dot').classList.remove('on'); $('#meta').textContent='未连接：'+e.message; }
}
async function loadThreads(){
  const d = await api('/api/chat/threads');
  const box = $('#threads'); box.innerHTML='';
  for(const t of d.threads){
    const el = document.createElement('div');
    el.className = 'thread' + (t.id===tid?' sel':'');
    el.innerHTML = '<div>'+t.title+'</div><small>'+(t.last? t.last.from+': '+t.last.text : '（空）')+'</small>';
    el.onclick = ()=>{ tid=t.id; localStorage.setItem('chat_tid',tid); cursor=0; $('#posts').innerHTML=''; loadThreads(); poll(true); };
    box.appendChild(el);
  }
}
function render(p){
  const d = document.createElement('div');
  d.className = 'post ' + (p.role==='assistant'?'':'me') + (p.role==='system'?' sys':'');
  d.innerHTML = '<div class="who"></div><div class="bubble"></div>';
  d.querySelector('.who').textContent = p.at + ' · ' + (p.role==='assistant'?'助手':p.from);
  d.querySelector('.bubble').textContent = p.text;
  $('#posts').appendChild(d);
  $('#posts').scrollTop = 1e9;
}
async function poll(once=false){
  if(!tid || polling) return;
  polling = true;
  try{
    do{
      const d = await api('/api/chat/threads/'+tid+'?after='+cursor+'&wait='+(once?0:20));
      for(const p of d.posts) render(p);
      cursor = d.cursor;
      if(once) break;
    }while(true);
  }catch(e){ /* 401 已提示；其它错误下一轮再试 */ }
  finally{ polling = false; }
}
let sending = false;
function addBubble(who){
  const d = document.createElement('div');
  d.className = 'post';
  d.innerHTML = '<div class="who"></div><div class="bubble"></div>';
  d.querySelector('.who').textContent = who;
  $('#posts').appendChild(d); $('#posts').scrollTop = 1e9;
  return d;
}
// SSE 解析：fetch 流 → 按空行切帧 → event/data
async function sse(url, opts, onEvent){
  const r = await fetch(url, {credentials:'include', headers:headers(), ...opts});
  if(r.status === 401){ $('#login').style.display='flex'; throw new Error('401'); }
  if(!r.ok || !r.body){ const t = await r.text().catch(()=> ''); throw new Error('HTTP '+r.status+' '+t.slice(0,120)); }
  const reader = r.body.getReader(); const dec = new TextDecoder();
  let buf = '';
  while(true){
    const {done, value} = await reader.read();
    if(done) break;
    buf += dec.decode(value, {stream:true});
    let i;
    while((i = buf.indexOf('\n\n')) >= 0){
      const frame = buf.slice(0, i); buf = buf.slice(i + 2);
      const ev = (frame.match(/^event: (.*)$/m) || [])[1];
      const dl = (frame.match(/^data: (.*)$/m) || [])[1];
      if(ev && dl){ try{ onEvent(ev, JSON.parse(dl)); }catch(_){ } }
    }
  }
}
async function send(){
  const text = $('#input').value.trim(); if(!text || !tid || sending) return;
  $('#input').value=''; sending = true;
  let node = null, acc = '';
  try{
    await sse('/api/chat/threads/'+tid+'/messages/stream',
      {method:'POST', body:JSON.stringify({text})},
      (ev, d)=>{
        if(ev === 'meta'){ render(d.post); if(d.cursor > cursor) cursor = d.cursor; }
        else if(ev === 'delta'){ if(!node) node = addBubble('助手 · 正在输入…'); acc += d.text; node.querySelector('.bubble').textContent = acc; $('#posts').scrollTop = 1e9; }
        else if(ev === 'done'){
          if(node){ node.querySelector('.who').textContent = d.post.at + ' · 助手'; node.querySelector('.bubble').textContent = d.post.text; node = null; }
          else render(d.post);
          if(d.cursor > cursor) cursor = d.cursor;
        }
        else if(ev === 'error' && node){ node.querySelector('.bubble').textContent = acc + '\n[出错] ' + d.message; }
        else if(ev === 'notice'){ render({at:new Date().toLocaleString(), from:'系统', role:'system', text:d.text}); }
      });
  }catch(e){ if(node) node.querySelector('.bubble').textContent = acc + '\n[断开] ' + e.message; }
  finally{ sending = false; poll(false); }
}
$('#new').onclick = async ()=>{
  const title = prompt('线程标题', '和 伙伴-A 的对话'); if(title===null) return;
  const d = await api('/api/chat/threads', {method:'POST', body:JSON.stringify({title, participants:['管理员','伙伴-A','助手']})});
  tid = d.thread.id; localStorage.setItem('chat_tid', tid); cursor=0; $('#posts').innerHTML='';
  loadThreads(); poll(true);
};
$('#refresh').onclick = ()=>{ loadThreads(); };
$('#send').onclick = send;
$('#input').addEventListener('keydown', e=>{ if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send(); }});
$('#save').onclick = ()=>{ token = $('#tok').value.trim(); localStorage.setItem('chat_token', token); $('#login').style.display='none'; init(); };
async function init(){
  await health(); await loadThreads();
  if(!tid){ const d = await api('/api/chat/threads'); if(d.threads[0]){ tid=d.threads[0].id; localStorage.setItem('chat_tid',tid); } }
  if(tid) poll(true);
}
init();
setInterval(health, 30000);
</script></body></html>"""


@page_router.get("/chat", response_class=HTMLResponse)
def chat_page() -> HTMLResponse:
    return HTMLResponse(HTML)
