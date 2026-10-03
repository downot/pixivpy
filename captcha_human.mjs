// Human-in-the-loop reCAPTCHA solver for the pixiv migration.
//
// Opens a VISIBLE window with the reCAPTCHA (issued from a pixiv-trusted origin).
// Before you solve it, it captures the numbered grid + instruction. While you
// click, it records your selected cells. When you verify and a token appears,
// it saves a labeled example (grid + instruction + your selection + the LLM's
// own answer for comparison) and prints "TOKEN=<token>" to stdout.
//
//   node captcha_human.mjs [host] [round]
//     host  default www.pixiv.net
//     round default 1  (label suffix for the 10-round session)
import { spawn } from "node:child_process";
import http from "node:http";
import { writeSync, writeFileSync, mkdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const HOST = process.argv[2] || "www.pixiv.net";
const ROUND = process.argv[3] || "1";
const HTTP_PORT = 8799;
const DEBUG_PORT = 9373;
const S = 240;
const LLM_URL = "http://192.168.7.77:18000/v1/chat/completions";
const SITE_KEY = "6LejidcZAAAAAE0-BHUjuY_1yIR478OolN4akKyy";
const OUT_DIR = "/tmp/cdp/human_solves";
const SOLVE_TIMEOUT = 120000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => writeSync(2, a.join(" ") + "\n");
mkdirSync(OUT_DIR, { recursive: true });

const PAGE = `<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:20px">
<h3>Solve the CAPTCHA below</h3>
<div class="g-recaptcha" data-sitekey="${SITE_KEY}" data-callback="onCb" data-expired-callback="onExp"></div>
<script src="https://www.google.com/recaptcha/api.js?hl=ja" async defer></script>
<script>function onCb(t){window.__token=t;}function onExp(){document.title='EXPIRED';}</script>
</body></html>`;
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
  `--remote-debugging-port=${DEBUG_PORT}`, "--user-data-dir=" + path.join(tmpdir(), "chrome-captcha-human"),
  "--no-first-run", "--no-default-browser-check", "--window-size=640,540",
  `--host-resolver-rules=MAP ${HOST} 127.0.0.1`, "about:blank",
], { stdio: "ignore", detached: true });
let page;
for (let i = 0; i < 50; i++) { try { const l = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json(); page = l.find((t) => t.type === "page"); if (page) break; } catch {} await sleep(200); }
const activateChrome = () => spawn("/usr/bin/osascript", ["-e", 'tell application "Google Chrome" to activate'], { stdio: "ignore" });
const cdp = new CDP(new WebSocket(page.webSocketDebuggerUrl));
await cdp.open; await cdp.send("Page.enable");
await cdp.send("Page.navigate", { url: `http://${HOST}:${HTTP_PORT}/captcha.html` });
await sleep(7000);
activateChrome();
const anchor = await waitTarget("api2/anchor", 20);
const acdp = new CDP(new WebSocket(anchor.webSocketDebuggerUrl));
await acdp.open; await acdp.send("Runtime.enable"); await acdp.send("Input.enable");
const bframe = await waitTarget("api2/bframe", 30);
if (!bframe) { log("NO_BFRAME"); chrome.kill(); server.close(); process.exit(1); }
let bcdp = new CDP(new WebSocket(bframe.webSocketDebuggerUrl));
await bcdp.open; await bcdp.send("Runtime.enable"); await bcdp.send("Input.enable");

const WAIT_TILES = async (deadline) => {
  while (Date.now() < deadline) {
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
const SELECTED = `(() => [...document.querySelectorAll('td.rc-imageselect-tile')].filter(td => td.className.indexOf('selected') !== -1).map(td => Number(td.id)))`;

const tA = Date.now();
log("Waiting for you to click the checkbox and solve the CAPTCHA (visible window)...");
const tv = await WAIT_TILES(Date.now() + SOLVE_TIMEOUT);
if (!tv) { log("TIMEOUT_TILES"); chrome.kill(); server.close(); process.exit(1); }
const instr = tv.instruction;
const gridB64 = (await bcdp.send("Runtime.evaluate", { returnByValue: true, expression: COMPOSITE })).result?.result?.value;
if (!gridB64) { log("NO_GRID"); chrome.kill(); server.close(); process.exit(1); }
const tag = `r${ROUND}_${new Date().toISOString().replace(/[:.]/g, "-")}`;
writeFileSync(`${OUT_DIR}/${tag}.png`, Buffer.from(gridB64, "base64"));
log(`grid captured (${instr}) -> ${OUT_DIR}/${tag}.png`);

// Poll selection + token while the user works. The inner frame reloads on every
// wrong attempt (stale CDP context), so open a FRESH connection to it each poll
// and keep the last non-empty selection observed.
let userSel = [];
const tS = Date.now();
while (Date.now() - tS < SOLVE_TIMEOUT) {
  let l = null;
  try { l = await (await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`)).json(); } catch {}
  const cur = l?.find((x) => x.url?.includes("api2/bframe"));
  if (cur) {
    const tmp = new CDP(new WebSocket(cur.webSocketDebuggerUrl));
    try {
      await tmp.open;
      const s = (await tmp.send("Runtime.evaluate", { returnByValue: true, expression: SELECTED }, 2000)).result?.result?.value;
      if (Array.isArray(s) && s.length > 0) userSel = s;
    } catch {} finally { try { tmp.ws.close(); } catch {} }
  }
  const tr = await acdp.send("Runtime.evaluate", { expression: "window.__token" });
  if (tr.result?.result?.value) break;
  await sleep(400);
}
let token = (await acdp.send("Runtime.evaluate", { expression: "window.__token" })).result?.result?.value;
if (!token) {
  for (let i = 0; i < 20 && !token; i++) {  // catch a token that lands just after the window
    await sleep(1000);
    token = (await acdp.send("Runtime.evaluate", { expression: "window.__token" })).result?.result?.value;
  }
}
const elapsed = ((Date.now() - tA) / 1000).toFixed(1);
// (LLM comparison is done offline against the saved grid png — keep this round fast.)
const rec = { round: ROUND, tag, instruction: instr, n_cells: tv.n, user_selected: userSel, elapsed_s: +elapsed, ts: new Date().toISOString() };
writeFileSync(`${OUT_DIR}/${tag}.json`, JSON.stringify(rec, null, 2));
log(`user_selected=${JSON.stringify(userSel)} (${elapsed}s)`);
chrome.kill(); server.close();
if (token) { writeSync(1, `TOKEN=${token}\n`); process.exit(0); }
process.exit(1);
