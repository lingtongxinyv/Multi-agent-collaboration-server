// 构建后把 dist/index.html 里挂件资源的 ?v= 换成**部署件真实哈希**。
// 为什么必须有：nginx 对带 ?v= 的 /resident-avatar/ 资源发 `immutable`（一年），
// 一旦 HTML 里的版本号没跟着文件变，浏览器会拿着旧文件缓存一年（2026-11-07 实测踩过）。
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const dist = new URL('./dist/', import.meta.url).pathname;
const htmlPath = join(dist, 'index.html');
if (!existsSync(htmlPath)) { console.log('[version-assets] 没有 dist/index.html，跳过'); process.exit(0); }

const hash = (rel) => createHash('sha256').update(readFileSync(join(dist, rel))).digest('hex').slice(0, 8);
let html = readFileSync(htmlPath, 'utf8');

// 1) /resident-avatar/avatar.js?v=…
const avRel = 'resident-avatar/avatar.js';
if (existsSync(join(dist, avRel))) {
  html = html.replace(new RegExp(`/resident-avatar/avatar\\.js\\?v=[0-9a-f]{6,}`, 'g'),
    `/resident-avatar/avatar.js?v=${hash(avRel)}`);
}

// 2) 挂件立绘：沿用 HTML 里已经写好的那张，只刷新它的版本号
const m = html.match(/\/resident-avatar\/assets\/(resident-[0-9]+\.webp)\?v=[0-9a-f]{6,}/);
if (m) {
  const rel = `resident-avatar/assets/${m[1]}`;
  if (existsSync(join(dist, rel))) {
    html = html.replace(m[0], `/resident-avatar/assets/${m[1]}?v=${hash(rel)}`);
  }
}

// 3) 站点自身 css/js 也顺带对一遍（Vite 的 assets/* 已带内容哈希，这里只是兜底）
for (const rel of ['assets/style.css', 'assets/app.js']) {
  if (existsSync(join(dist, rel))) {
    html = html.replace(new RegExp(`${rel}\\?v=[0-9a-f]{6,}`, 'g'), `${rel}?v=${hash(rel)}`);
  }
}

writeFileSync(htmlPath, html);
console.log('[version-assets] 已按 dist 真实哈希重写 index.html 的 ?v=');
