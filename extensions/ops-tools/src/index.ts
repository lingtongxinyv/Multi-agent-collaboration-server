import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { readFile } from 'node:fs/promises'
import { DatabaseSync } from 'node:sqlite'
import os from 'node:os'

const execFileAsync = promisify(execFile)

export const name = 'ops-tools'
export const inject = ['tools']

export function apply(ctx: Context) {
  // ─── firewall_manage: UFW 防火墙管理 ───
  ctx.tools.register(defineTool({
    name: 'firewall_manage',
    description: 'Manage UFW firewall rules. Supports status, allow, deny, delete, enable, disable. Requires root.',
    parameters: {
      action: { type: 'string', required: true, description: 'One of: status, allow, deny, delete, enable, disable' },
      rule: { type: 'string', description: 'Rule string e.g. "80/tcp", "22", "443/tcp". Required for allow/deny/delete.' },
      direction: { type: 'string', description: 'Direction: in or out. Optional for allow/deny.' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const { action, rule, direction } = args
      const cmdArgs: string[] = []

      switch (action) {
        case 'status':
          cmdArgs.push('status', 'verbose')
          break
        case 'enable':
          cmdArgs.push('--force', 'enable')
          break
        case 'disable':
          cmdArgs.push('disable')
          break
        case 'allow':
        case 'deny':
        case 'delete':
          if (!rule) return `Error: rule is required for ${action}`
          if (action === 'delete') cmdArgs.push('delete')
          if (direction) cmdArgs.push(direction)
          cmdArgs.push(action === 'delete' ? (direction || 'allow') : action, rule)
          break
        default:
          return `Error: unknown action "${action}". Use: status, allow, deny, delete, enable, disable`
      }

      try {
        const { stdout, stderr } = await execFileAsync('ufw', cmdArgs, { timeout: 15000 })
        return (stdout || stderr || 'Done').trim()
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))

  // ─── log_inspect: 日志监控与入侵检测 ───
  ctx.tools.register(defineTool({
    name: 'log_inspect',
    description: 'Inspect system logs for security and diagnostics. Sources: journalctl (systemd journal), auth.log (authentication), syslog. Can search for failed SSH logins, sudo activity, etc.',
    parameters: {
      source: { type: 'string', required: true, description: 'Log source: journal, auth, syslog' },
      action: { type: 'string', required: true, description: 'Action: recent (last N lines), search (grep pattern), failed_ssh (failed login attempts), sudo (sudo usage)' },
      pattern: { type: 'string', description: 'Search pattern (regex). Required for "search" action.' },
      lines: { type: 'number', description: 'Number of lines to show (default 50)' },
      since: { type: 'string', description: 'Time filter e.g. "1 hour ago", "today", "2026-01-01"' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const { source, action, pattern, lines = 50, since } = args

      try {
        if (source === 'journal') {
          const cmdArgs: string[] = []
          if (since) cmdArgs.push('--since', since)
          cmdArgs.push('-n', String(lines), '--no-pager')
          if (action === 'search' && pattern) {
            cmdArgs.push('-g', pattern)
          } else if (action === 'failed_ssh') {
            cmdArgs.push('-u', 'ssh', '-g', 'Failed|fail|invalid')
          } else if (action === 'sudo') {
            cmdArgs.push('-g', 'sudo')
          }
          const { stdout } = await execFileAsync('journalctl', cmdArgs, { timeout: 15000, maxBuffer: 1024 * 1024 })
          return stdout || 'No logs found'
        } else if (source === 'auth') {
          const logPath = '/var/log/auth.log'
          let content: string
          try {
            content = await readFile(logPath, 'utf8')
          } catch {
            return 'Error: cannot read /var/log/auth.log (try journalctl instead)'
          }
          const logLines = content.split('\n').filter(Boolean)
          let filtered = logLines
          if (action === 'failed_ssh') {
            filtered = logLines.filter(l => /Failed|invalid|fail/i.test(l))
          } else if (action === 'sudo') {
            filtered = logLines.filter(l => /sudo/i.test(l))
          } else if (action === 'search' && pattern) {
            const re = new RegExp(pattern, 'i')
            filtered = logLines.filter(l => re.test(l))
          }
          return filtered.slice(-lines).join('\n') || 'No matching entries'
        } else if (source === 'syslog') {
          const { stdout } = await execFileAsync('journalctl', ['-n', String(lines), '--no-pager', '-t', 'syslog'], { timeout: 15000 })
          return stdout || 'No syslog entries'
        }
        return `Error: unknown source "${source}"`
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))

  // ─── system_update: 系统更新与漏洞扫描 ───
  ctx.tools.register(defineTool({
    name: 'system_update',
    description: 'Manage system updates and security patches. Actions: list (available updates), upgrade (install updates), security (security-only updates), autoremove (clean old packages). Requires root for upgrade/autoremove.',
    parameters: {
      action: { type: 'string', required: true, description: 'Action: list, upgrade, security, autoremove' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const { action } = args

      try {
        switch (action) {
          case 'list': {
            const { stdout } = await execFileAsync('apt', ['list', '--upgradable', '-qq'], { timeout: 30000 })
            return stdout || 'No updates available'
          }
          case 'upgrade': {
            const { stdout, stderr } = await execFileAsync('apt', ['update', '-qq'], { timeout: 60000 })
            const { stdout: upStdout, stderr: upStderr } = await execFileAsync('apt', ['upgrade', '-y', '-qq'], { timeout: 300000 })
            return [stdout, stderr, upStdout, upStderr].filter(Boolean).join('\n')
          }
          case 'security': {
            const { stdout } = await execFileAsync('apt', ['list', '--upgradable', '-qq'], { timeout: 30000 })
            const security = stdout.split('\n').filter(l => /security|security/i.test(l))
            return security.join('\n') || 'No security updates available'
          }
          case 'autoremove': {
            const { stdout, stderr } = await execFileAsync('apt', ['autoremove', '-y', '-qq'], { timeout: 120000 })
            return [stdout, stderr].filter(Boolean).join('\n') || 'Nothing to remove'
          }
          default:
            return `Error: unknown action "${action}". Use: list, upgrade, security, autoremove`
        }
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))

  // ─── resource_monitor: 资源监控与告警 ───
  ctx.tools.register(defineTool({
    name: 'resource_monitor',
    description: 'Monitor system resources: CPU usage, memory usage, disk usage, network stats, and running processes. Actions: cpu, memory, disk, network, processes, summary.',
    parameters: {
      action: { type: 'string', required: true, description: 'Action: cpu, memory, disk, network, processes, summary' },
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args) {
      const { action } = args

      try {
        switch (action) {
          case 'cpu': {
            const cpus = os.cpus()
            const loadAvg = os.loadavg()
            const uptime = os.uptime()
            return `CPU Cores: ${cpus.length}\nLoad Average (1/5/15min): ${loadAvg.join(', ')}\nUptime: ${Math.floor(uptime / 3600)}h ${Math.floor((uptime % 3600) / 60)}m\n\nCPU Models:\n${cpus.map(c => c.model).filter((v, i, a) => a.indexOf(v) === i).join('\n')}`
          }
          case 'memory': {
            const total = os.totalmem()
            const free = os.freemem()
            const used = total - free
            return `Memory Total: ${(total / 1024 / 1024 / 1024).toFixed(2)} GB\nMemory Used: ${(used / 1024 / 1024 / 1024).toFixed(2)} GB\nMemory Free: ${(free / 1024 / 1024 / 1024).toFixed(2)} GB\nUsage: ${((used / total) * 100).toFixed(1)}%`
          }
          case 'disk': {
            const { stdout } = await execFileAsync('df', ['-h', '--type=ext4', '--type=xfs', '--type=btrfs', '--type=ntfs'], { timeout: 10000 })
            return stdout
          }
          case 'network': {
            const { stdout } = await execFileAsync('ip', ['-s', 'addr'], { timeout: 10000 })
            return stdout
          }
          case 'processes': {
            const { stdout } = await execFileAsync('ps', ['aux', '--sort=-pcpu'], { timeout: 10000 })
            const lines = stdout.split('\n')
            return lines.slice(0, 21).join('\n') // header + top 20
          }
          case 'summary': {
            const total = os.totalmem()
            const free = os.freemem()
            const used = total - free
            const loadAvg = os.loadavg()
            const { stdout: diskOut } = await execFileAsync('df', ['-h', '/'], { timeout: 10000 })
            const diskLine = diskOut.split('\n')[1] || ''
            return `=== System Summary ===\nHostname: ${os.hostname()}\nUptime: ${Math.floor(os.uptime() / 3600)}h\nCPU Cores: ${os.cpus().length}\nLoad (1/5/15): ${loadAvg.join(', ')}\nMemory: ${((used / total) * 100).toFixed(1)}% (${(used / 1024 / 1024 / 1024).toFixed(1)}/${(total / 1024 / 1024 / 1024).toFixed(1)} GB)\nRoot FS: ${diskLine}`
          }
          default:
            return `Error: unknown action "${action}". Use: cpu, memory, disk, network, processes, summary`
        }
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))

  // ─── 面板共享库工具：监控历史 / 告警 / 配额（node:sqlite 读 panel.db） ───
  const panelDbPath = os.homedir() + '/dsh-suite/data/panel.db'

  ctx.tools.register(defineTool({
    name: 'metrics_history',
    description: 'Read server monitor history (CPU/memory/disk/network/load samples) collected by the panel. Use for trend analysis and health reports.',
    parameters: {
      hours: { type: 'number', description: 'Hours of history to read (default 24, max 168)' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute(args) {
      const hours = Math.min(Math.max(Number(args.hours ?? 24) || 24, 1), 168)
      const db = new DatabaseSync(panelDbPath, { readOnly: true })
      try {
        const since = Math.floor(Date.now() / 1000) - hours * 3600
        const rows = db.prepare(
          'SELECT ts, cpu, mem, disk, net_rx, net_tx, load1 FROM monitor_samples WHERE ts >= ? ORDER BY ts',
        ).all(since) as { ts: number; cpu: number; mem: number; disk: number; net_rx: number; net_tx: number; load1: number }[]
        if (rows.length === 0) return 'No samples recorded yet'
        const avg = (f: (r: any) => number) => (rows.reduce((s, r) => s + f(r), 0) / rows.length)
        const peak = (f: (r: any) => number) => Math.max(...rows.map(f))
        const t = (ts: number) => new Date(ts * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
        return [
          `时间范围: ${t(rows[0].ts)} ~ ${t(rows[rows.length - 1].ts)}（${rows.length} 个采样点）`,
          `CPU   均值 ${avg(r => r.cpu).toFixed(1)}%  峰值 ${peak(r => r.cpu)}%`,
          `内存  均值 ${avg(r => r.mem).toFixed(1)}%  峰值 ${peak(r => r.mem)}%`,
          `磁盘  当前 ${rows[rows.length - 1].disk}%`,
          `负载1 均值 ${avg(r => r.load1).toFixed(2)}  峰值 ${peak(r => r.load1)}`,
          `网络  最近接收 ${(rows[rows.length - 1].net_rx / 1024).toFixed(1)} MB/s 级别`,
        ].join('\n')
      } catch (err: any) {
        return `Error: ${err.message || err}`
      } finally {
        db.close()
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'alert_list',
    description: 'List server alerts raised by the panel monitor (threshold breaches, service failures). Filter: all or unacknowledged. 助手 should review and handle unacknowledged alerts.',
    parameters: {
      unack: { type: 'boolean', description: 'Only unacknowledged alerts (default true)' },
      limit: { type: 'number', description: 'Max rows (default 30)' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute(args) {
      const unack = args.unack !== false
      const limit = Math.min(Number(args.limit ?? 30) || 30, 200)
      const db = new DatabaseSync(panelDbPath, { readOnly: true })
      try {
        const rows = db.prepare(
          `SELECT id, ts, level, source, title, detail, acknowledged FROM alerts
           ${unack ? 'WHERE acknowledged = 0' : ''} ORDER BY id DESC LIMIT ?`,
        ).all(limit) as any[]
        if (rows.length === 0) return unack ? '没有未确认的告警' : '没有告警记录'
        return rows.map(r => [
          `#${r.id} [${r.level}] ${new Date(r.ts * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })} (${r.source})${r.acknowledged ? ' [已确认]' : ''}`,
          `  ${r.title}`,
          r.detail ? `  ${String(r.detail).slice(0, 300)}` : '',
        ].filter(Boolean).join('\n')).join('\n\n')
      } catch (err: any) {
        return `Error: ${err.message || err}`
      } finally {
        db.close()
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'alert_ack',
    description: 'Acknowledge one or more alerts by id (comma separated), after you have handled or reviewed them. Requires root-owned db write via panel db path.',
    parameters: {
      ids: { type: 'string', required: true, description: 'Alert ids, e.g. "12" or "12,13,14"' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute(args) {
      const ids = String(args.ids).split(',').map(s => Number(s.trim())).filter(Number.isSafeInteger)
      if (ids.length === 0) return 'Error: no valid ids'
      try {
        const db = new DatabaseSync(panelDbPath) // 可写
        const stmt = db.prepare('UPDATE alerts SET acknowledged = 1 WHERE id = ?')
        let n = 0
        for (const id of ids) n += Number(stmt.run(id).changes)
        db.close()
        return `OK: ${n}/${ids.length} 条告警已确认`
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'quota_overview',
    description: 'Read user account quota usage from the panel (tokens today, concurrent tasks, sandbox disk). Use when the admin asks about user resource consumption.',
    parameters: {},
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute() {
      const db = new DatabaseSync(panelDbPath, { readOnly: true })
      try {
        const rows = db.prepare(
          `SELECT username, role, disabled, quota_tokens_daily, used_tokens, quota_concurrent,
                  quota_disk_mb, used_disk_mb, used_date FROM accounts ORDER BY username`,
        ).all() as any[]
        const today = new Date().toLocaleDateString('sv-SE', { timeZone: 'Asia/Shanghai' })
        return rows.map(a => {
          const usedToday = a.used_date === today ? a.used_tokens : 0
          return `${a.username} (${a.role}${a.disabled ? ', 禁用' : ''}): `
            + `token ${usedToday}/${a.quota_tokens_daily}，`
            + `沙箱 ${a.used_disk_mb}/${a.quota_disk_mb} MB，`
            + `并发上限 ${a.quota_concurrent}`
        }).join('\n')
      } catch (err: any) {
        return `Error: ${err.message || err}`
      } finally {
        db.close()
      }
    },
  }))

  // ─── 星铃委托板：站娘给管理员挂账 ───
  // 管理员随时能在面板「星铃委托板」里看到服务器上需要他处理的事，而不是散在聊天里。
  // 走面板 HTTP 接口（X-Quest-Token），把校验/幂等/审计留在后端一处。
  const QUEST_TOKEN_PATH = os.homedir() + '/dsh-suite/data/quest-publish.token'
  const QUEST_BASE = process.env.DSH_QUEST_BASE || 'http://127.0.0.1:8010/api/quests'

  async function questFetch(path: string, init?: RequestInit) {
    const token = (await readFile(QUEST_TOKEN_PATH, 'utf8')).trim()
    if (!token) throw new Error(`发布凭据为空：${QUEST_TOKEN_PATH}`)
    const resp = await fetch(QUEST_BASE + path, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        'X-Quest-Token': token,
        ...(init?.headers ?? {}),
      },
    })
    const text = await resp.text()
    if (!resp.ok) throw new Error(`面板 ${resp.status}：${text.slice(0, 300)}`)
    return JSON.parse(text || '{}')
  }

  ctx.tools.register(defineTool({
    name: 'quest_publish',
    description: '把一件「需要管理员处理」的事挂到管理员的星铃委托板上（面板 /quests 页面）。'
      + '服务器侧要管理员点头/择时/拍板的事，用它挂账，不要只在聊天里说。'
      + '同一 user+ref 重复发布只更新内容、不重复挂单（可以每轮重发）；ref 用稳定键，如 open-items:j3-runtime。'
      + '不传 ref 则每次新挂一张。',
    parameters: {
      title: { type: 'string', required: true, description: '委托标题（≤80 字，一句话说清要管理员做什么）' },
      detail: { type: 'string', description: '补充说明：现状、为什么需要管理员、可选方案（≤300 字）' },
      category: { type: 'string', enum: ['main', 'side', 'bounty'], description: '主线=长期/需择时；支线=顺手；悬赏=有点难但值得' },
      rarity: { type: 'string', enum: ['common', 'rare', 'epic', 'legend'], description: '稀有度（影响卡片配色）' },
      target: { type: 'number', description: '目标步数，默认 1' },
      reward: { type: 'string', description: '给管理员的回报/收益，短句（≤24 字）' },
      ref: { type: 'string', description: '幂等键；同一 ref 再发=更新而不是新挂' },
      user: { type: 'string', description: '收件人用户名，默认 admin' },
      origin: { type: 'string', description: '委托人署名，默认「助手」' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute(args) {
      try {
        const out = await questFetch('/publish', {
          method: 'POST',
          body: JSON.stringify({
            user: args.user || 'admin',
            title: args.title,
            detail: args.detail || '',
            category: args.category || 'side',
            rarity: args.rarity || 'common',
            target: Number(args.target ?? 1) || 1,
            reward: args.reward || '',
            ref: args.ref || '',
            origin: args.origin || '助手',
          }),
        })
        const q = out.quest || {}
        return (out.created ? '已挂上委托' : '已更新委托（同 ref 幂等，未重复挂单）')
          + ` #${q.id}「${q.title}」· 委托人 ${q.origin} · ${q.status}`
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))

  ctx.tools.register(defineTool({
    name: 'quest_board',
    description: '读取管理员星铃委托板上的委托（含 origin=委托人、status）。挂账前先看一眼，别重复挂。',
    parameters: {
      user: { type: 'string', description: '用户名，默认 admin' },
      openOnly: { type: 'boolean', description: '只看未完成（默认 false）' },
    },
    output: { schema: { type: 'string' }, render: (_a, v) => [{ type: 'text', text: v }] },
    async execute(args) {
      try {
        const user = args.user || 'admin'
        const out = await questFetch(`/board?user=${encodeURIComponent(user)}`)
        const rows: any[] = (out.quests || []).filter((q: any) =>
          args.openOnly !== true || q.status !== 'done')
        const s = out.stats || {}
        if (rows.length === 0) return `${user} 的委托板是空的`
        const cat: Record<string, string> = { main: '主线', side: '支线', bounty: '悬赏', daily: '每日' }
        const st: Record<string, string> = { open: '待接取', active: '进行中', done: '已完成' }
        return `委托板（${user}）：待接取 ${s.open ?? 0} / 进行中 ${s.active ?? 0}`
          + ` / 已完成 ${s.done ?? 0} · 站娘挂账 ${s.staff_open ?? 0}\n`
          + rows.map(q => `· [${st[q.status] || q.status}]「${q.title}」`
            + `（${cat[q.category] || q.category}，委托人 ${q.origin || 'self'}，ref=${q.ref || '-'}）`).join('\n')
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))
}
