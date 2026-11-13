/** 我的智能体：即装即用接入向导 + 队员名册 + IDE 反向接入。
 *
 * 助手 2026-10-14：把"接入"从「找管理员要密钥 + 手写配置文件」变成
 * 「下载连接器 → 浏览器点确认 → 绿灯」，并且每个用户各自一份凭据。
 */
import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../api'
import { toast } from '../main'

interface LinkAgent {
  id: string
  name: string
  agent_type: string
  platform: string
  host: string
  capabilities: { type?: string; label?: string; app?: string }[]
  online: boolean
  last_seen: number
  last_ping_at: number
  registered_at: number
}

interface AgentFile {
  name: string
  size: number
  version: string | null
  sha256: string
  mtime: number
}

interface ConnectorBuild {
  platform: string
  file: string
  label: string
  size: number
  sha256: string
  mtime: number
}

interface LinkTask {
  id: string
  parent_task_id: string
  step: number
  title: string
  assignee: string
  capability: string
  status: string
  result: string
  created_at: number
  finished_at: number | null
}

const TASK_LABEL: Record<string, string> = {
  pending: '待领取', running: '进行中', done: '已完成', failed: '失败', skipped: '已跳过',
}

const APP_HINT: Record<string, string> = {
  trae: 'Trae CN：用户级 %APPDATA%\\Trae CN\\User\\mcp.json（别放进项目里的 .trae/mcp.json）',
  claude: 'Claude Code：项目 .mcp.json 或用户级配置（作用域见官方文档）',
  cursor: 'Cursor：~/.cursor/mcp.json',
  vscode: 'VS Code：设置里的 MCP 服务器段（或用 vscode:mcp/install 深链）',
  generic: '通用 MCP 客户端：把 mcpServers 段并入它的配置文件',
}

function fmtSize(n: number) {
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n > 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${n} B`
}

/** 能力按「应用」归并。
 *
 * 连接器的 capabilities 是**按能力类型**逐条上报的（同一个应用会贡献多条：
 * Claude Code 一次报 code/general/write/dsh_headless 四条），直接铺开会出现
 * 「四个一模一样的 Claude Code」并把后面的 DSH / Ollama 挤出视野（2026-10-14 修）。
 */
type Cap = { type?: string; label?: string; app?: string }

function capGroups(caps: Cap[]) {
  const m = new Map<string, { label: string; types: string[] }>()
  for (const c of caps) {
    const label = c.label || c.type || '未知能力'
    const g = m.get(label) ?? { label, types: [] }
    if (c.type && !g.types.includes(c.type)) g.types.push(c.type)
    m.set(label, g)
  }
  return [...m.values()]
}

function capText(caps: Cap[]) {
  return capGroups(caps).map(g => `${g.label}(${g.types.join('/')})`).join('、')
}


async function copyText(text: string, okMsg = '已复制') {
  try {
    await navigator.clipboard.writeText(text)
    toast(okMsg)
  } catch {
    toast('当前浏览器不允许自动复制，请手动长按/选中复制', 'err')
  }
}

export function AgentsPage() {
  const [rows, setRows] = useState<LinkAgent[]>([])
  const [tasks, setTasks] = useState<LinkTask[]>([])
  const [file, setFile] = useState<AgentFile | null>(null)
  const [bins, setBins] = useState<ConnectorBuild[]>([])
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [client, setClient] = useState('trae')
  const [prompt, setPrompt] = useState('')
  const [cap, setCap] = useState('')
  const [target, setTarget] = useState('')
  const [sending, setSending] = useState(false)

  const load = useCallback(() => {
    api.get<LinkAgent[]>('/api/link/agents').then(setRows).catch(() => {})
    api.get<LinkTask[]>('/api/link/tasks?limit=10').then(setTasks).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    api.get<AgentFile[]>('/api/devices/agent-files')
      .then(list => setFile(list.find(f => f.name === 'bellhub_connector.py') ?? null))
      .catch(() => {})
    api.get<ConnectorBuild[]>('/api/link/connector-builds').then(setBins).catch(() => {})
    const timer = window.setInterval(load, 15000)   // 在线状态自动刷新
    return () => window.clearInterval(timer)
  }, [load])

  const confirmCode = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    try {
      await api.post('/api/link/approve', { user_code: code.trim().toUpperCase() })
      toast('已确认，这台设备马上会上线')
      setCode('')
      setTimeout(load, 1500)
    } catch (err) {
      toast(String((err as Error).message), 'err')
    } finally { setBusy(false) }
  }

  /** 面板直接派活：不依赖站娘会话，所有用户当场可用。 */
  const dispatch = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!prompt.trim()) return
    setSending(true)
    try {
      const r = await api.post<{ assignee: string; online: boolean; note: string }>(
        '/api/link/dispatch',
        { prompt: prompt.trim(), capability: cap.trim(), agent: target })
      toast(r.note || `已派给「${r.assignee}」，结果会出现在下面的记录里`)
      setPrompt('')
      setTimeout(load, 1500)
    } catch (err) {
      toast(String((err as Error).message), 'err')
    } finally { setSending(false) }
  }

  const ping = async (a: LinkAgent) => {
    try {
      const r = await api.post<{ online: boolean; note: string }>(`/api/link/agents/${a.id}/ping`)
      if (r.online) toast(`「${a.name}」可达（最近有心跳）`)
      else toast(`「${a.name}」未响应：${r.note || '最近 90 秒没有心跳'}`, 'err')
      load()
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const revoke = async (a: LinkAgent) => {
    if (!window.confirm(`撤销「${a.name}」？它立刻失去接入权限，需要重新配对。`)) return
    try {
      await api.del(`/api/link/agents/${a.id}`)
      toast('已撤销')
      load()
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const dl = '/api/devices/agent-files/bellhub_connector.py'

  return (
    <>
      <h2>我的智能体</h2>
      <p className="muted" style={{ marginTop: -6 }}>
        把你这台电脑上的 AI 应用编入站娘的队伍：装一次连接器，站娘就能把活派给它，
        结果自动回到会话里。<b>连接器不会代跑你的命令</b> —— 动手的是本机 AI，涉及写文件/跑命令的活
        还会先在本机弹一次确认。
      </p>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>四步接入（约 2 分钟）</h3>
        <ol style={{ lineHeight: 2, paddingLeft: 20, margin: '8px 0 0' }}>
          <li>
            <b>下载连接器</b>　
            {bins.map(b => (
              <span key={b.platform} style={{ marginRight: 8 }}>
                <a className="l-btn ghost small" href={`/api/link/connector-builds/${b.platform}`} download>
                  ⬇ {b.file}
                </a>
                <span className="muted" style={{ fontSize: 11.5, marginLeft: 6 }}>
                  {b.label} · {fmtSize(b.size)} · sha256 {b.sha256.slice(0, 10)}…
                </span>
              </span>
            ))}
            <a className="l-btn ghost small" href={dl} download>⬇ bellhub_connector.py（源码）</a>
            {file && (
              <span className="muted" style={{ fontSize: 11.5, marginLeft: 8 }}>
                {file.version ? `版本 ${file.version} · ` : ''}{fmtSize(file.size)} ·
                更新于 {fmtTime(file.mtime)} · sha256 {file.sha256.slice(0, 10)}…
              </span>
            )}
            <div className="muted" style={{ fontSize: 12.5 }}>
              {bins.length > 0
                ? '优先下打包版（.exe / 二进制）：双击即用，不需要装 Python。'
                : '打包版（.exe / Linux 二进制）双击即用，不需要装 Python；当前站点还没放预编译产物，'
                  + '可先用源码版（需要 Python 3.9+），或让管理员在对应系统上运行 workers/build_connector 脚本。'}
            </div>
          </li>
          <li>
            <b>运行一次配对</b>
            <div className="row stack" style={{ marginTop: 6 }}>
              <code className="mono" style={{ padding: '6px 8px', background: 'var(--bg2,rgba(0,0,0,.15))', borderRadius: 6 }}>
                bellhub-connector pair
              </code>
              <button onClick={() => copyText('bellhub-connector pair', '命令已复制')}>复制命令</button>
            </div>
            <div className="muted" style={{ fontSize: 12.5 }}>
              源码版用 <code>python bellhub_connector.py pair</code>。它会检测本机的 AI 应用
              （Claude Code / Codex / DSH / Ollama），并自动打开浏览器。
            </div>
          </li>
          <li>
            <b>在浏览器里点「确认」</b>
            <div className="muted" style={{ fontSize: 12.5 }}>
              确认页会把配对请求绑到<b>你这个账号</b>，凭据只存在你这台电脑上。
            </div>
          </li>
          <li>
            <b>让它常驻 —— 这一步别跳过</b>
            <div className="muted" style={{ fontSize: 12.5, marginBottom: 4 }}>
              配对只是拿到凭据；<b>真正上线靠 run</b>。二选一：
            </div>
            <div className="row stack" style={{ marginBottom: 4 }}>
              <code className="mono" style={{ padding: '6px 8px', background: 'var(--bg2,rgba(0,0,0,.15))', borderRadius: 6 }}>
                bellhub-connector run
              </code>
              <button onClick={() => copyText('bellhub-connector run', '命令已复制')}>复制</button>
              <span className="muted" style={{ fontSize: 12 }}>现在就上线，窗口别关</span>
            </div>
            <div className="row stack">
              <code className="mono" style={{ padding: '6px 8px', background: 'var(--bg2,rgba(0,0,0,.15))', borderRadius: 6 }}>
                bellhub-connector autostart
              </code>
              <button onClick={() => copyText('bellhub-connector autostart', '命令已复制')}>复制</button>
              <span className="muted" style={{ fontSize: 12 }}>
                开机自动在线（Linux 与 Windows 会立即启动一次；安卓要等重启，或先另开窗口 run）
              </span>
            </div>
            <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
              拿不准就先跑 <code>bellhub-connector doctor</code>：它逐项检查并给结论。
            </div>
          </li>
        </ol>

        <form onSubmit={confirmCode} className="row stack" style={{ marginTop: 12 }}>
          <span className="muted" style={{ fontSize: 12.5 }}>
            浏览器没自动打开？把连接器显示的 8 位配对码填在这里：
          </span>
          <input className="mono" placeholder="例如 K7M2QP4A" value={code}
            maxLength={8} onChange={e => setCode(e.target.value.toUpperCase())} />
          <button className="primary" disabled={busy || code.trim().length < 4}>确认接入</button>
        </form>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>名册（{rows.length}）</h3>
        {rows.length === 0 ? (
          <p className="muted">还没有队员。按上面四步接入第一台电脑后，这里会出现它。</p>
        ) : (
          <div className="table-wrap">
            <table className="table-cards">
              <thead>
                <tr><th>队员</th><th>类型</th><th>平台</th><th>能力</th><th>状态</th><th>操作</th></tr>
              </thead>
              <tbody>
                {rows.map(a => (
                  <tr key={a.id}>
                    <td data-label="队员"><b>{a.name}</b></td>
                    <td data-label="类型">{a.agent_type === 'connector' ? '连接器' : a.agent_type}</td>
                    <td data-label="平台">{a.platform || '—'}</td>
                    <td data-label="能力">
                      {capGroups(a.capabilities).map(g => (
                        <span key={g.label} className="badge" style={{ marginRight: 4 }}
                          title={g.types.join(' / ')}>
                          {g.label}{g.types.length > 1 ? ` ×${g.types.length}` : ''}
                        </span>
                      ))}
                      {a.capabilities.length === 0 && <span className="muted">—</span>}
                    </td>
                    <td data-label="状态">
                      <span className={`badge ${a.online ? '' : 'warn'}`}>
                        {a.online ? '在线' : '离线'}
                      </span>
                      <div className="muted" style={{ fontSize: 11.5 }}>
                        {a.last_seen ? fmtTime(a.last_seen) : '从未心跳'}
                        {a.last_ping_at ? ` · 测过 ${fmtTime(a.last_ping_at)}` : ''}
                      </div>
                    </td>
                    <td data-label="操作">
                      <div className="row" style={{ gap: 6 }}>
                        <button onClick={() => ping(a)}>测试连接</button>
                        <button onClick={() => revoke(a)}>撤销</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>队伍视图</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          站娘是队长，下面每个队员都是你这台机器上的一个 AI 应用。她按能力标签挑人，
          失败的活可以换人重派。
        </p>
        <div style={{ marginTop: 10, paddingLeft: 4 }}>
          <div className="row" style={{ gap: 8, alignItems: 'center', marginBottom: 6 }}>
            <span className="badge">队长</span>
            <b>站娘（助手）</b>
            <span className="muted" style={{ fontSize: 12 }}>拆解任务 · 按能力派活 · 收结果</span>
          </div>
          {rows.length === 0 ? (
            <div className="muted" style={{ paddingLeft: 20 }}>└─ （暂无队员）</div>
          ) : rows.map((a, i) => {
            const last = tasks.find(t => t.assignee === a.name)
            return (
              <div key={a.id} style={{ paddingLeft: 20, marginTop: 4 }}>
                {i === rows.length - 1 ? '└─' : '├─'}{' '}
                <b>{a.name}</b>{' '}
                <span className={`badge ${a.online ? '' : 'warn'}`}>{a.online ? '在线' : '离线'}</span>
                <span className="muted" style={{ fontSize: 12, marginLeft: 6 }}>
                  {capText(a.capabilities) || '（未上报能力）'}
                </span>
                {last && (
                  <span className="muted" style={{ fontSize: 12, marginLeft: 6 }}>
                    · 最近任务「{last.title}」{TASK_LABEL[last.status] ?? last.status}
                  </span>
                )}
              </div>
            )
          })}
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>派活（不用等站娘开口）</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          直接给队员布置一件事：<b>留空队员</b>=按能力标签自动挑一台在线的；
          点名一台离线的也行，任务会排队，等它上线自动执行。结果与失败原因都出现在下面的记录里。
        </p>
        <form onSubmit={dispatch} className="row stack" style={{ marginTop: 8 }}>
          <textarea rows={3} value={prompt} onChange={e => setPrompt(e.target.value)}
            placeholder="要它做什么？例如：统计 D:\logs 里今天的错误行数，并把结果写进 err.txt" />
          <div className="row stack">
            <input value={cap} onChange={e => setCap(e.target.value)}
              placeholder="能力标签（可选，如 code / general / chat）" />
            <select value={target} onChange={e => setTarget(e.target.value)}>
              <option value="">自动挑在线队员</option>
              {rows.map(a => (
                <option key={a.id} value={a.name}>
                  {a.name}{a.online ? '' : '（离线，排队）'}
                  {a.capabilities.length ? ` · ${capText(a.capabilities)}` : ''}
                </option>
              ))}
            </select>
            <button className="primary" disabled={sending || !prompt.trim()}>
              {sending ? '派出中…' : '派活'}
            </button>
          </div>
        </form>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>最近的派活记录（{tasks.length}）</h3>
        {tasks.length === 0 ? (
          <p className="muted">还没有派给你的队员的活。上面派一个，或让站娘在会话里接任务，都会在这里留痕。</p>
        ) : (
          <div className="table-wrap">
            <table className="table-cards">
              <thead>
                <tr><th>任务</th><th>队员</th><th>能力</th><th>状态</th><th>结果</th></tr>
              </thead>
              <tbody>
                {tasks.map(t => (
                  <tr key={t.id}>
                    <td data-label="任务">
                      <b>{t.title}</b>
                      {t.title !== t.result && t.result && (
                        <div className="muted ellipsis" style={{ fontSize: 12, maxWidth: 320 }}>{t.result}</div>
                      )}
                    </td>
                    <td data-label="队员">{t.assignee || '（待领取）'}</td>
                    <td data-label="能力">{t.capability || 'general'}</td>
                    <td data-label="状态">
                      <span className={`badge ${t.status === 'failed' ? 'warn' : ''}`}>
                        {TASK_LABEL[t.status] ?? t.status}
                      </span>
                      <div className="muted" style={{ fontSize: 11.5 }}>
                        {t.created_at ? fmtTime(t.created_at) : ''}
                      </div>
                    </td>
                    <td data-label="结果">
                      <span className="muted" style={{ fontSize: 12 }}>
                        {(t.result || '—').slice(0, 160)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>反向接入：让 IDE 里的 AI 也能调用铃枢</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          Trae / Cursor / VS Code / Claude Code 这类客户端<b>没有"被派活"的接口</b>，
          但它们可以主动调用铃枢。在已配对的电脑上跑一条命令，它会打印一段带好凭据的配置：
        </p>
        <div className="row stack">
          <select value={client} onChange={e => setClient(e.target.value)}>
            <option value="trae">Trae CN</option>
            <option value="cursor">Cursor</option>
            <option value="vscode">VS Code</option>
            <option value="claude">Claude Code</option>
            <option value="generic">通用 MCP 客户端</option>
          </select>
          <code className="mono" style={{ padding: '6px 8px', background: 'var(--bg2,rgba(0,0,0,.15))', borderRadius: 6 }}>
            bellhub-connector print-config {client}
          </code>
          <button onClick={() => copyText(`bellhub-connector print-config ${client}`, '命令已复制')}>
            复制命令
          </button>
          <code className="mono" style={{ padding: '6px 8px', background: 'var(--bg2,rgba(0,0,0,.15))', borderRadius: 6 }}>
            bellhub-connector print-config {client} --write
          </code>
          <button onClick={() => copyText(`bellhub-connector print-config ${client} --write`, '命令已复制')}>
            复制「一键写入」
          </button>
        </div>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
          加 <code>--write</code> 就不用自己找配置路径：连接器会只改 <code>mcpServers.bellhub</code>
          这一项、先备份原文件、重复执行也不会重复改。写入后重启客户端即可。
        </div>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>{APP_HINT[client]}</div>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
          安全边界：这段配置里的令牌等价于「在这台电脑上派活」的权限，别提交进代码仓库；
          怀疑泄露就在这里撤销队员，旧凭据立刻失效。
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>常见问题</h3>
        <div className="l-faq" style={{ marginTop: 8 }}>
          <details>
            <summary>配对码在哪看？过期了怎么办？</summary>
            <p>
              连接器运行 <code>pair</code> 时会在终端打印 8 位配对码，同时尝试自动打开浏览器确认页。
              配对码 15 分钟内有效；过期或被拒就重新跑一次 <code>pair</code>（同一台机器重复配对
              只会轮换凭据，不会产生重复队员）。
            </p>
          </details>
          <details>
            <summary>队员显示「离线」？</summary>
            <p>
              连接器需要常驻（<code>bellhub-connector run</code>）。离线通常是因为电脑休眠、终端窗口被关、
              或没设开机自启。名册每 15 秒自动刷新，点「测试连接」可立刻探测一次。
            </p>
          </details>
          <details>
            <summary>接不通 / 不确定哪里有问题？</summary>
            <p>
              在电脑上跑 <code>bellhub-connector doctor</code>：它一条命令逐项检查
              <b>配对状态、站点可达、领活通道、本机队员、开机自启</b>，每项给出 ✅/⚠/✗ 和下一步动作，
              最后给一句结论。比反复试错快得多，也是我们远程排障时让你跑的第一条命令。
            </p>
          </details>
          <details>
            <summary>站娘会不会直接在我电脑上跑命令？</summary>
            <p>
              <b>不会。</b>连接器只把任务交给本机 AI 应用执行，自己不解命令。
              命中"写文件/跑命令/安装"这类高危动作时，默认会在本机弹一次确认；
              无人值守（没有终端）时直接拒绝并回传原因，不会静默执行。
            </p>
          </details>
          <details>
            <summary>本机什么都没装，能用吗？</summary>
            <p>
              连上可以，但没有可被派活的 AI 应用时就没人干活。连接器会自动探测本机已装的：
              Claude Code、Codex CLI、DSH、Gemini CLI、Aider、OpenCode、Ollama（本地模型
              <code>qwen3:1.7b</code> 约 1.4GB，4GB 显存够用）。
              装好后重跑 <code>bellhub-connector detect</code> 即可。
            </p>
            <p>
              其它命令行 AI 也能接：在 <code>~/.bellhub/connector.json</code> 里加一条
              <code>custom_agents</code>（给一个 <code>cmd</code> 且含 <code>{'{prompt}'}</code> 占位符），
              不必等官方适配。
            </p>
          </details>
          <details>
            <summary>怎么知道连接器是不是最新版？</summary>
            <p>
              在已配对的电脑上跑 <code>bellhub-connector version --check</code>：
              它会向站点查询最新版本并对比本机版本；不一致时直接列出去哪儿下载、
              以及对应的 sha256 供核对。它<b>不会</b>擅自替换正在运行的文件。
            </p>
          </details>
          <details>
            <summary>多台电脑 / 多人共用？</summary>
            <p>
              每台电脑单独配对一次；每个用户的凭据互相隔离，站娘只会看到该会话所属用户名下的队员。
              同一账号可以在多台机器上各有队员。
            </p>
          </details>
        </div>
      </div>
    </>
  )
}
