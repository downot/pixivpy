// Self-contained reCAPTCHA solver for the pixiv migration.
//
// Issues a reCAPTCHA token from a pixiv-trusted origin (www.pixiv.net) by:
//   1. starting a local HTTP server that serves a minimal reCAPTCHA page,
//   2. mapping <host> -> 127.0.0.1 in Chrome via --host-resolver-rules,
//   3. driving the widget with CDP: LLM (per-cell) answer + human-like clicks,
//   4. reading the token from the page callback.
//
// Emits "TOKEN=<token>" on stdout on success; all diagnostics go to stderr.
//   node captcha_solver.mjs [host]     (default host = www.pixiv.net)
import { spawn } from "node:child_process";
import http from "node:http";
import { writeSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const HOST = process.argv[2] || "www.pixiv.net";
const HTTP_PORT = 8799;
const DEBUG_PORT = 9372;
const S = 240;
const MAX_ATTEMPTS = 6;
const LLM_URL = "http://192.168.7.77:18000/v1/chat/completions";
const SITE_KEY = "6LejidcZAAAAAE0-BHUjuY_1yIR478OolN4akKyy";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const rnd = (a, b) => a + Math.random() * (b - a);
const log = (...a) => writeSync(2, a.join(" ") + "\n");

const PAGE = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div class="g-recaptcha" data-sitekey="${SITE_KEY}" data-callback="onCb" data-expired-callback="onExp"></div>
<script src="https://www.google.com/recaptcha/api.js?hl=ja" async defer></script>
<script>function onCb(t){window.__token=t;}function onExp(){document.title='EXPIRED';}</script>
</body></html>`;

// Serve the page locally so the reCAPTCHA is issued from the mapped origin.
const server = http.createServer((req, res) => { res.writeHead(200, { "Content-Type": "text/html" }); res.end(PAGE); });
await new Promise((r) => server.listen(HTTP_PORT, "127.0.0.1", r));

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pend = new Map();
    ws.onmessage = (ev) => { const m = JSON.parse(ev.data);
      if (m.id && this.pend.has(m.id)) { const f = this.pend.get(m.id); this.pend.delete(m.id); f(m); } }; }
  send(method, params = {}, t = 8000) { return new Promise((res) => { const i = ++this.id;
    const to = setTimeout(() => { this.pend.delete(i); res({ error: { message: "timeout" } }); }, t);
    this.pend.set(i, (m) => { clearTimeout(to); res(m); });
    this.ws.send(JSON.stringify({ id: i, method, params })); }); }
  get open() { return new Promise((r) => { if (this.ws.readyState === 1) r(); else this.ws.onopen = r; }); }
  async humanClick(cx, cy) {
    const sx = rnd(40, 560), sy = rnd(40, 300);
    const steps = Math.floor(rnd(14, 22));
    for (let s = 1; s <= steps; s++) {
      const t = s / steps, e = 1 - Math.pow(1 - t, 2);
      const bow = Math.sin(t * Math.PI) * rnd(-30, 30);
      await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: sx + (cx - sx) * e + bow, y: sy + (cy - sy) * e });
      await sleep(rnd(6, 16));
    }
    await sleep(rnd(90, 260));
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x: cx, y: cy, button: "left", clickCount: 1 });
    await sleep(rnd(40, 90));
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: cx, y: cy, button: "left", clickCount: 1 });
  }
  async center(selExpr) {
    const r = await this.send("Runtime.evaluate", { returnByValue: true, expression: selExpr });
    return r.result?.result?.value || null;
  }
}
const LLM = {
  async ask(b64, text) {
    for (let tryn = 0; tryn < 2; tryn++) {
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), 20000);
      try {
        const r = await fetch(LLM_URL, { method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
          body: JSON.stringify({ model: "qwen3.8-27b", chat_template_kwargs: { enable_thinking: false },
            messages: [{ role: "user", content: [{ type: "text", text }, { type: "image_url", image_url: { url: `data:image/png;base64,${b64}` } }] }],
            max_tokens: 256, temperature: 0 }) });
        const d = await r.json();
        return d.choices?.[0]?.message?.content?.trim() || "";
      } catch { await sleep(1500); } finally { clearTimeout(to); }
    }
    return null;
  },
};
async function waitTarget(part, tries = 25) {
  for (let i = 0; i < tries; i++) { const l = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json(); const t = l.find((x) => x.url?.includes(part)); if (t) return t; await sleep(800); }
  return null;
}
const chrome = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
  `--remote-debugging-port=${DEBUG_PORT}`, "--user-data-dir=" + path.join(tmpdir(), "chrome-captcha-solver"),
  "--no-first-run", "--no-default-browser-check", "--window-size=600,500",
  `--host-resolver-rules=MAP ${HOST} 127.0.0.1`, "about:blank",
], { stdio: "ignore", detached: true });
let page;
for (let i = 0; i < 50; i++) { try { const l = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json(); page = l.find((t) => t.type === "page"); if (page) break; } catch {} await sleep(200); }
const cdp = new CDP(new WebSocket(page.webSocketDebuggerUrl));
await cdp.open; await cdp.send("Page.enable");
await cdp.send("Page.navigate", { url: `http://${HOST}:${HTTP_PORT}/captcha.html` });
await sleep(8000);
const anchor = await waitTarget("api2/anchor", 15);
const acdp = new CDP(new WebSocket(anchor.webSocketDebuggerUrl));
await acdp.open; await acdp.send("Runtime.enable"); await acdp.send("Input.enable");
const ab = await acdp.center(`(() => { const el = document.getElementById('recaptcha-anchor'); const r = el.getBoundingClientRect(); return { x: r.x + r.width/2, y: r.y + r.height/2 }; })()`);
await acdp.humanClick(ab.x, ab.y);
const bframe = await waitTarget("api2/bframe", 20);
if (!bframe) { log("NO_BFRAME"); chrome.kill(); server.close(); process.exit(1); }
const bcdp = new CDP(new WebSocket(bframe.webSocketDebuggerUrl));
await bcdp.open; await bcdp.send("Runtime.enable"); await bcdp.send("Input.enable");

const WAIT_TILES = async () => {
  for (let i = 0; i < 40; i++) {
    const r = await bcdp.send("Runtime.evaluate", { returnByValue: true, expression: `(() => {
      const cells=[...document.querySelectorAll('td.rc-imageselect-tile')];
      return { n: cells.length, rendered: cells.filter(td => (td.querySelector('img')?.naturalWidth||0) > 0).length, instruction: (document.querySelector('.rc-imageselect-instructions')?.textContent||'').trim() };
    })()` });
    const v = r.result?.result?.value;
    if (v && v.n >= 9 && v.rendered === v.n && v.instruction) return v;
    await sleep(500);
  }
  return null;
};
// Build a numbered grid image entirely in the browser (no external compositor).
const COMPOSITE = `(() => {
  const cells=[...document.querySelectorAll('td.rc-imageselect-tile')]; const n=cells.length;
  const cols = n===16?4:3, rows=Math.ceil(n/cols), S=${S};
  const grid=document.createElement('canvas'); grid.width=cols*S; grid.height=rows*S;
  const g=grid.getContext('2d'); g.imageSmoothingEnabled=true; g.imageSmoothingQuality='high';
  cells.forEach((td,i)=>{ const img=td.querySelector('img'); const wrap=td.querySelector('.rc-image-tile-wrapper')||td;
    const wr=wrap.getBoundingClientRect(); const ir=img.getBoundingClientRect();
    const x=Math.max(wr.x,ir.x),y=Math.max(wr.y,ir.y),x2=Math.min(wr.right,ir.right),y2=Math.min(wr.bottom,ir.bottom);
    g.drawImage(img,(x-ir.x)/ir.width*img.naturalWidth,(y-ir.y)/ir.height*img.naturalHeight,(x2-x)/ir.width*img.naturalWidth,(y2-y)/ir.height*img.naturalHeight,(i%cols)*S,Math.floor(i/cols)*S,S,S); });
  const bw=36;
  cells.forEach((td,i)=>{ const x0=(i%cols)*S, y0=Math.floor(i/cols)*S;
    g.fillStyle='rgba(255,0,0,0.85)'; g.fillRect(x0,y0,bw,bw);
    g.fillStyle='#ffff00'; g.font='bold 26px sans-serif'; g.fillText(String(i),x0+8,y0+26); });
  return grid.toDataURL('image/png').split(',')[1];
})()`;
const composite = async () => (await bcdp.send("Runtime.evaluate", { returnByValue: true, expression: COMPOSITE })).result?.result?.value;
// opacity-immune per-tile identity to detect a real re-confirmation image swap
const IDENTITY = `(() => [...document.querySelectorAll('td.rc-imageselect-tile')].map(td => { const img=td.querySelector('img'); const cs=getComputedStyle(img); return (img?.src||'')+'|'+cs.top+','+cs.left+'|'+(img?.naturalWidth||0); })())`;
const identity = async () => (await bcdp.send("Runtime.evaluate", { returnByValue: true, expression: IDENTITY })).result?.result?.value;
const centerOf = (id) => `(() => { const el=document.querySelector('td.rc-imageselect-tile[id="${id}"]'); if(!el) return null; const r=el.getBoundingClientRect(); return { x:r.x+r.width/2, y:r.y+r.height/2 }; })()`;

let token = null;
for (let attempt = 1; !token && attempt <= MAX_ATTEMPTS; attempt++) {
  const tA = Date.now();
  const tv = await WAIT_TILES();
  if (!tv) { log(`attempt ${attempt}: tiles not rendered`); await sleep(1500); continue; }
  const cols = tv.n === 16 ? 4 : 3;
  const grid = await composite();
  if (!grid) { log(`attempt ${attempt}: composite failed`); await sleep(1000); continue; }
  const text = `A ${cols}x${cols} grid is a SINGLE continuous image divided into ${tv.n} cells, numbered 0..${tv.n - 1} row-major (top-left=0). Task: ${tv.instruction}\n`
    + `Target objects may span several cells. For EACH cell 0..${tv.n - 1} output one token: '<n>:Y' if that cell contains ANY part of a target object, else '<n>:N'. `
    + `Be strict: a similar-but-different object is N. Output ONLY one line of ${tv.n} tokens (e.g. 0:N 1:Y 2:Y ...), nothing else.`;
  const answer = await LLM.ask(grid, text);
  if (answer === null) { log(`attempt ${attempt}: LLM timeout, re-solving`); await sleep(500); continue; }
  const pairs = [...answer.matchAll(/(\d+)\s*:\s*([YNyn])/g)];
  let cells = [...new Set(pairs.filter((m) => m[2].toUpperCase() === "Y").map((m) => Number(m[1])))].filter((x) => Number.isInteger(x) && x >= 0 && x < tv.n);
  if (pairs.length < tv.n) cells = [...new Set(answer.split(/[\s,、]+/).map((s) => Number(s)).filter((x) => Number.isInteger(x) && x >= 0 && x < tv.n))];
  log(`attempt ${attempt}: instr="${tv.instruction.slice(0, 22)}" cells=${JSON.stringify(cells)} t+${((Date.now() - tA) / 1000).toFixed(1)}s`);
  const idRef = await identity();
  for (const id of cells) { const c = await bcdp.center(centerOf(id)); if (!c) continue; await bcdp.humanClick(c.x, c.y); await sleep(rnd(320, 900)); }
  const watchEnd = Date.now() + rnd(4000, 8000);
  let refreshed = false;
  while (Date.now() < watchEnd) {
    await sleep(1000);
    const now = await identity();
    if (now && now.length === idRef?.length) for (const id of cells) if (id < now.length && now[id] !== idRef[id]) { refreshed = true; break; }
    if (refreshed) break;
    await bcdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: rnd(60, 540), y: rnd(60, 300) });
  }
  if (refreshed) { log(`  -> selected cell refreshed (re-confirmation), re-solving`); await sleep(600); continue; }
  if (cells.length === 0) {
    const p = await bcdp.center(`(() => { const el=[...document.querySelectorAll('button,[role="button"]')].find(b => /スキップ|skip/i.test(b.textContent||'')); if(!el) return null; const r=el.getBoundingClientRect(); return { x:r.x+r.width/2, y:r.y+r.height/2 }; })()`);
    if (p) await bcdp.humanClick(p.x, p.y);
    log("  -> empty, skip");
  } else {
    const p = await bcdp.center(`(() => { const el=document.getElementById('recaptcha-verify-button'); const r=el.getBoundingClientRect(); return { x:r.x+r.width/2, y:r.y+r.height/2 }; })()`);
    if (p) await bcdp.humanClick(p.x, p.y);
    log(`  -> verify at t+${((Date.now() - tA) / 1000).toFixed(1)}s`);
  }
  for (let i = 0; i < 20; i++) {
    await sleep(1000);
    const tok = await cdp.send("Runtime.evaluate", { returnByValue: true, expression: "window.__token" });
    if (tok.result?.result?.value) { token = tok.result.result.value; break; }
  }
  if (!token) log("  -> no token, next attempt");
}
if (token) process.stdout.write("TOKEN=" + token + "\n");
chrome.kill(); server.close();
process.exit(token ? 0 : 1);
