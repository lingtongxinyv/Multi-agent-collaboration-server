/**
 * 「我的智能体」页面的无头浏览器验收（Playwright）。
 *
 * 为什么单独放一个 .mjs 而不是并进 run_all.sh：
 *   1) 它依赖 Playwright（装在 /root/1012/visual/node_modules，root-only）；
 *   2) 它要真起浏览器，比其余套件慢且更"重"。
 * 所以它是**手动 / root 运行**的元验收，由 tests/meta_ui_check.py 驱动。
 *
 * 用法（由 meta_ui_check.py 调用）：
 *   node meta_ui_agents.mjs <baseUrl> <用户名> <密码> <队员名> <配对码> <派活文本>
 * 输出：最后一行 JSON {results: [{name, ok, detail}]}
 */
import { createRequire } from 'module'

const require = createRequire('/root/1012/visual/')
const { chromium } = require('playwright')

const [, , BASE, USER, PASS, MEMBER, CODE, PROMPT] = process.argv
const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok: !!ok, detail: String(detail).slice(0, 200) })
  console.log(`${ok ? '  ✅' : '  ❌'} ${name}${ok ? '' : '  → ' + detail}`)
}

const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1280, height: 900 },
  permissions: ['clipboard-read', 'clipboard-write'] })
const page = await context.newPage()
page.on('dialog', d => d.accept())        // 「撤销」会 window.confirm，一律同意
const lastToast = async () => {
  const t = await page.$$eval('.toast', els => els.map(e => e.textContent))
  return t.length ? t[t.length - 1] : ''
}
const errors = []
let loggedIn = false
const badResponses = []
page.on('pageerror', e => errors.push(String(e)))
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
page.on('response', r => {
  // 登录前 /api/auth/me 必然 401（还没会话），那是预期内的，不计入
  if (loggedIn && r.status() >= 400) badResponses.push(`${r.status()} ${r.url()}`)
})

try {
  // ── 登录 ──────────────────────────────────────────────
  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
  await page.fill('input[placeholder="用户名"]', USER)
  await page.fill('input[placeholder="密码"]', PASS)
  await page.click('button.primary')
  await page.waitForURL(u => !u.pathname.startsWith('/login'), { timeout: 15000 })
  check('新用户能登录并进入工作台', !page.url().includes('/login'), page.url())
  loggedIn = true

  // ── 我的智能体 ────────────────────────────────────────
  await page.goto(`${BASE}/agents`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(800)
  const body = await page.textContent('body')

  check('页面标题可见', body.includes('我的智能体'))
  check('向导是四步', body.includes('四步接入'))
  check('有"让它常驻——这一步别跳过"', body.includes('这一步别跳过'))
  check('run 命令可见', body.includes('bellhub-connector run'))
  check('autostart 命令可见', body.includes('bellhub-connector autostart'))
  check('doctor 提示可见', body.includes('bellhub-connector doctor'))
  check('没有残留的"三步接入"旧文案', !body.includes('三步接入'), '页面仍出现三步接入')

  const links = await page.$$eval('a[download]', as => as.map(a => a.getAttribute('href')))
  check('连接器下载链接存在', links.some(h => h && h.includes('bellhub_connector.py')),
    JSON.stringify(links))
  check('预编译产物下载链接存在', links.some(h => h && h.includes('connector-builds/')),
    JSON.stringify(links))

  const copyButtons = await page.$$eval('button', bs => bs.filter(b => b.textContent.includes('复制')).length)
  check('命令旁有复制按钮', copyButtons >= 2, String(copyButtons))

  // ── 确认接入（真码，由连接器生成）────────────────────
  await page.fill('input[placeholder="例如 K7M2QP4A"]', CODE)
  await page.click('button.primary:has-text("确认接入")')
  await page.waitForTimeout(2500)
  const afterConfirm = await page.textContent('body')
  check('确认后出现反馈/名册变化',
    afterConfirm.includes('已确认') || afterConfirm.includes(MEMBER),
    afterConfirm.slice(0, 120))

  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  const roster = await page.textContent('body')
  check('名册里出现这台机器', roster.includes(MEMBER), roster.slice(0, 200))
  check('名册显示在线/离线标记', roster.includes('在线') || roster.includes('离线'))

  // ── 派活表单 ─────────────────────────────────────────
  await page.fill('textarea', PROMPT)
  await page.click('button.primary:has-text("派活")')
  await page.waitForTimeout(2500)
  const afterDispatch = await page.textContent('body')
  check('派活后记录里出现该任务',
    afterDispatch.includes(PROMPT.slice(0, 8)) || afterDispatch.includes('待领取')
    || afterDispatch.includes('进行中'), afterDispatch.slice(0, 200))

  // ── 交互按钮：测试连接 / 复制 / 撤销 ───────────────
  await page.goto(`${BASE}/agents`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(900)
  await page.click('button:has-text("测试连接")')
  await page.waitForTimeout(1200)
  check('点「测试连接」有反馈', (await lastToast()).includes('可达'), await lastToast())

  await page.click('button:has-text("复制")')
  await page.waitForTimeout(800)
  const copyToast = await lastToast()
  check('点「复制」有反馈（成功或明确告知手动复制）',
    copyToast.includes('已复制') || copyToast.includes('手动'), copyToast)

  await page.click('button:has-text("撤销")')
  await page.waitForTimeout(1800)
  const afterRevoke = await page.textContent('body')
  check('点「撤销」后名册清空并回到空状态',
    afterRevoke.includes('还没有队员'), afterRevoke.slice(0, 160))

  // ── 错误状态：无效配对码要说清 ──────────────────────
  await page.fill('input[placeholder="例如 K7M2QP4A"]', 'ZZZZ9999')
  await page.waitForTimeout(1200)
  const badCode = await page.textContent('body')
  check('填无效配对码有明确提示',
    badCode.includes('无效') || badCode.includes('过期'), badCode.slice(0, 160))

  // ── 手机宽度下不应横向溢出（用户常在手机上看名册）──
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto(`${BASE}/agents`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(900)
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  check('手机宽度(390px)无横向溢出', overflow <= 1, `溢出 ${overflow}px`)
  const mobileBody = await page.textContent('body')
  check('手机宽度下向导仍可读', mobileBody.includes('四步接入'), mobileBody.slice(0, 100))

  // ── 控制台无 JS 报错 ─────────────────────────────────
  const realErrors = errors.filter(e => !/favicon|clipboard|NotAllowed|401 \(Unauthorized\)/i.test(e))
  check('登录后没有 JS 运行时错误', realErrors.length === 0, realErrors.slice(0, 2).join(' | '))
  check('登录后没有失败的网络请求（4xx/5xx）', badResponses.length === 0,
    badResponses.slice(0, 3).join(' | '))
} catch (err) {
  check('脚本执行未抛异常', false, err.message)
} finally {
  await browser.close()
  console.log(JSON.stringify({ results }))
}
