/** 我的设备：接入自有设备 agent（token 发放、接入指引、指令下发与历史）。 */

import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../api'
import { toast } from '../main'

interface Device {
  id: string; name: string; desc: string
  status: string; last_seen: number; host: string; created_at: number
  online: boolean
}

interface DeviceCmd {
  id: string; text: string; status: 'pending' | 'sent' | 'done' | 'failed'
  result: string; source: string; created_at: number
}

const cmdBadge: Record<string, string> = {
  pending: 'warn', sent: 'info', done: 'ok', failed: 'err',
}
const cmdText: Record<string, string> = {
  pending: '待取走', sent: '执行中', done: '已完成', failed: '失败',
}

const AGENT_FILES = [
  { name: 'device_agent.py', must: true, note: '客户端主程序（必下）' },
  { name: 'device_handler.py', must: true, note: '默认处理器：只回显指令，不执行任何操作（必下）' },
  { name: 'shell_handler.py', must: false, note: '可选处理器：把指令当命令执行，需自行评估风险后启用' },
  { name: 'termux_handler.py', must: false, note: '安卓(Termux)专用：白名单模式操作手机，不接受任意命令' },
]

/** agent 内置自检指令（由 agent 自己回答，不经过本地 handler，全部只读） */
const BUILTINS: { cmd: string; use: string; what: string }[] = [
  { cmd: '!help', use: '查看全部内置指令', what: '列出 agent 自己能回答的指令，以及当前生效的 handler 是哪一个。' },
  { cmd: '!ping', use: '存活探测', what: '返回本机时间、agent 进程号与版本——用来确认这道指令通道是通的。' },
  { cmd: '!version', use: '只看版本号', what: '确认设备上跑的 agent 是不是最新版（与上方下载区标注的版本比对）。' },
  { cmd: '!info', use: '环境快照（最有用）', what: '版本、进程号与运行时长、主机名、系统、Python 路径、工作目录、hub 地址、设备名、token 掩码、心跳间隔、handler 文件是否存在。' },
  { cmd: '!ps', use: '查重复实例', what: '列出本机 Python 进程。最隐蔽的坑：设备上悄悄跑着两个 agent，指令被另一个实例抢走，结果迟迟不回。' },
]

/** 设备侧常见错误对照：现象 / 语义 / 处置（来自 2026-10-13 实战） */
const DEV_TROUBLE: { sym: string; why: string; fix: string }[] = [
  {
    sym: '设备卡片一直「离线」',
    why: 'agent 没在运行、config.json 的 name/token 与本页不一致、设备访问不到站点，或设备正在休眠。',
    fix: '逐项检查：进程是否在跑（终端窗口不能关，建议后台常驻）→ 配置三项是否完全一致 → 用设备浏览器打开 https://agent.example.com 是否可达。休眠唤醒后会自动恢复。',
  },
  {
    sym: '指令一直停在「待取走」',
    why: '这条指令还在队列里，设备没有心跳来取它。',
    fix: '确认设备在线；在线的话等一个心跳周期（约 15 秒）再看。',
  },
  {
    sym: '指令停在「执行中」很久',
    why: '设备已取走指令，handler 还在跑或已经异常退出；单条 handler 最长执行 30 分钟。',
    fix: '发 !ps 看进程情况，必要时重启设备上的 agent；超过上限的那条会以失败收尾。',
  },
  {
    sym: '回传「handler 异常: TypeError: expected str…」',
    why: '设备上跑的是旧版 agent：指令编号类型未转换是已知缺陷，曾导致该设备所有指令必然失败。',
    fix: '重新下载最新版 device_agent.py 覆盖即可，版本号与 sha256 在上方下载区可核对。',
  },
  {
    sym: '回传 UnicodeEncodeError，或中文显示成问号',
    why: 'Windows 中文系统的默认代码页是 cp936，handler 打印 emoji 或生僻字时失败——指令明明发出去了，却回传失败。',
    fix: '新版 agent 已强制两端统一 UTF-8；若仍报错，检查你的 handler 是否自己改写了输出编码。',
  },
  {
    sym: '结果里出现「agent 版本 …」「pong｜…」这类文本',
    why: '你发的是以 ! 开头的内置指令，由 agent 自己回答，不会经过本地 handler。',
    fix: '正常行为，不是故障；发 !help 可以看到完整的内置指令列表。',
  },
]

interface AgentFileMeta {
  name: string; size: number; mtime: number; version: string | null; sha256: string
}

/** 人类可读的字节数 */
function fmtSize(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`
}

export function DevicesPage() {
  const [devices, setDevices] = useState<Device[]>([])
  const [activeId, setActiveId] = useState<string | null>(null)
  const [cmds, setCmds] = useState<DeviceCmd[]>([])
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [creating, setCreating] = useState(false)
  const [newToken, setNewToken] = useState<{ name: string; token: string } | null>(null)
  const [cmdText_, setCmdText_] = useState('')
  const [agentFiles, setAgentFiles] = useState<AgentFileMeta[]>([])

  // 下载文件的版本/大小/哈希：让用户一眼确认「拿到的是不是最新版」
  useEffect(() => {
    api.get<AgentFileMeta[]>('/api/devices/agent-files').then(setAgentFiles).catch(() => {})
  }, [])

  // MCP 桥脚本的版本/哈希：与设备 agent 走同一个下载接口（白名单内）
  const mcpMeta = agentFiles.find(x => x.name === 'bellhub_mcp_bridge.py')

  const load = useCallback(() => {
    api.get<Device[]>('/api/devices').then(setDevices).catch(() => {})
  }, [])
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [load])

  const loadCmds = useCallback((did: string) => {
    api.get<DeviceCmd[]>(`/api/devices/${did}/commands`).then(setCmds).catch(() => {})
  }, [])
  useEffect(() => {
    if (!activeId) { setCmds([]); return }
    loadCmds(activeId)
    const t = setInterval(() => loadCmds(activeId), 8000)
    return () => clearInterval(t)
  }, [activeId, loadCmds])

  const create = async () => {
    const n = name.trim()
    if (!n || creating) return
    setCreating(true)
    try {
      const d = await api.post<Device & { token: string }>('/api/devices', { name: n, desc: desc.trim() })
      setNewToken({ name: d.name, token: d.token })
      setName(''); setDesc('')
      load()
      setActiveId(d.id)
    } catch (err) { toast(String((err as Error).message), 'err') }
    finally { setCreating(false) }
  }

  /** 复制 token：微信/WebView 里 clipboard 常被禁用，失败时明确告诉用户手动长按复制。 */
  const copyText = async (text: string) => {
    try {
      if (!navigator.clipboard) throw new Error('clipboard unavailable')
      await navigator.clipboard.writeText(text)
      toast('已复制')
    } catch {
      toast('当前浏览器不允许自动复制，请长按 token 选中后手动复制', 'err')
    }
  }

  const remove = async (d: Device) => {
    if (!confirm(`删除设备「${d.name}」？其指令历史将一并清除，设备上的 agent 将无法再连接。`)) return
    try {
      await api.del(`/api/devices/${d.id}`)
      if (activeId === d.id) setActiveId(null)
      load()
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const send = async () => {
    const t = cmdText_.trim()
    if (!t || !activeId) return
    try {
      await api.post(`/api/devices/${activeId}/command`, { text: t })
      setCmdText_('')
      loadCmds(activeId)
      toast('指令已入队，设备将在下次心跳时取走')
    } catch (err) { toast(String((err as Error).message), 'err') }
  }

  const active = devices.find(d => d.id === activeId) ?? null

  return (
    <div className="page">
      <div className="page-head">
        <h2>我的设备</h2>
        <div className="muted" style={{ fontSize: 13 }}>
          把你的电脑、手机接入铃枢：设备上的 agent 与站娘互通，站娘可以远程给你的设备派活。
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <h3>添加设备</h3>
        <div className="row stack" style={{ flexWrap: 'wrap' }}>
          <input placeholder="设备名（字母/数字/-/_，如 my-laptop）" value={name} maxLength={32}
            onChange={e => setName(e.target.value)} />
          <input placeholder="备注（可选，如 我的 Windows 笔记本）" value={desc} maxLength={200}
            onChange={e => setDesc(e.target.value)} />
          <button className="primary" disabled={!/^[A-Za-z0-9_-]{2,32}$/.test(name) || creating} onClick={create}>
            {creating ? '创建中…' : '＋ 添加并生成 token'}
          </button>
        </div>
      </div>

      {newToken && (
        <div className="card" style={{ marginBottom: 14, borderColor: 'var(--accent)' }}>
          <h3>设备「{newToken.name}」已创建</h3>
          <div className="hint" style={{ marginBottom: 6 }}>
            token 仅显示这一次，请立即复制保存（泄露后可删除设备重建）。
          </div>
          {/* 用只读输入框而不是 <code>：手机上可长按选中，
              且微信/WebView 里 clipboard API 常被禁用，自动复制会失败。 */}
          <div className="row stack">
            <input className="mono" readOnly value={newToken.token}
              onFocus={e => e.currentTarget.select()}
              style={{ flex: 1, minWidth: 0, fontSize: 13 }} />
            <button onClick={() => copyText(newToken.token)}>复制</button>
            <button className="primary" onClick={() => setNewToken(null)}>我已保存</button>
          </div>
        </div>
      )}

      {devices.length === 0 ? (
        <div className="card muted" style={{ textAlign: 'center', padding: 32 }}>
          还没有设备。添加设备后，按接入指引在设备上运行 agent 即可上线。
        </div>
      ) : (
        <div className="dev-grid">
          {devices.map(d => (
            <div key={d.id} className={`card dev-card ${activeId === d.id ? 'active' : ''}`}
              onClick={() => setActiveId(d.id)}>
              <div className="dev-head">
                <span className={`dev-dot ${d.online ? 'on' : ''}`} />
                <strong>{d.name}</strong>
                <span className={`badge ${d.online ? 'ok' : ''}`}>{d.online ? '在线' : '离线'}</span>
              </div>
              {d.desc && <div className="muted" style={{ fontSize: 12.5 }}>{d.desc}</div>}
              <div className="muted" style={{ fontSize: 12.5 }}>
                最近心跳：{d.last_seen ? fmtTime(d.last_seen) : '从未'} · {d.host || '未知来源'}
              </div>
              <div className="row" style={{ marginTop: 8 }}>
                <button className="ghost" onClick={e => { e.stopPropagation(); remove(d) }}>删除</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {active && (
        <div className="card" style={{ marginTop: 14 }}>
          <h3>设备「{active.name}」</h3>

          <div className="row stack" style={{ alignItems: 'flex-start', margin: '10px 0' }}>
            <textarea placeholder="给设备下发指令（文本，由设备端 handler 解释执行）" value={cmdText_}
              style={{ flex: 1, minHeight: 54 }}
              onChange={e => setCmdText_(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send()
              }} />
            <button className="primary" disabled={!cmdText_.trim()} onClick={send}>下发指令</button>
          </div>

          <h3 style={{ fontSize: 13.5 }}>指令历史</h3>
          {cmds.length === 0 && <div className="muted" style={{ fontSize: 13 }}>暂无指令。</div>}
          {cmds.map(c => (
            <div key={c.id} className="dev-cmd">
              <div className="dev-cmd-head">
                <span className={`badge ${cmdBadge[c.status] ?? ''}`}>{cmdText[c.status] ?? c.status}</span>
                <span className="muted" style={{ fontSize: 12 }}>
                  {c.source === 'assistant' ? '站娘下发' : '我下发'} · {fmtTime(c.created_at)}
                </span>
              </div>
              <div className="dev-cmd-text">{c.text}</div>
              {c.result && <pre className="result" style={{ maxHeight: 160, margin: '4px 0 0' }}>{c.result}</pre>}
            </div>
          ))}
        </div>
      )}

      {/* ── 使用教程 ── */}
      <div className="card" style={{ marginTop: 14 }}>
        <h3>接入教程：把你的设备连上铃枢</h3>
        <div className="l-steps" style={{ marginTop: 8 }}>
          <div className="l-step"><span className="no">1</span><div>
            <b>在本页添加设备</b>
            <p className="muted" style={{ margin: '2px 0 0' }}>
              填一个设备名（如 <code>my-laptop</code>），点「添加并生成 token」。<b>token 只显示一次</b>，
              请立即复制保存；弄丢了就删除设备重新添加。
            </p>
          </div></div>
          <div className="l-step"><span className="no">2</span><div>
            <b>下载 agent 文件到设备同一目录</b>
            <div style={{ marginTop: 4 }}>
              {AGENT_FILES.map(f => {
                const meta = agentFiles.find(x => x.name === f.name)
                return (
                  <div key={f.name} className="row" style={{ margin: '3px 0', gap: 8, flexWrap: 'wrap' }}>
                    <a className="l-btn ghost small" href={`/api/devices/agent-files/${f.name}`} download>
                      ⬇ {f.name}
                    </a>
                    <span className="muted" style={{ fontSize: 12.5 }}>{f.note}</span>
                    {meta && (
                      <span className="muted" style={{ fontSize: 11.5, opacity: 0.85 }}>
                        {meta.version ? `版本 ${meta.version} · ` : ''}
                        {fmtSize(meta.size)} · 更新于 {fmtTime(meta.mtime)} · sha256 {meta.sha256.slice(0, 10)}…
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
            <p className="muted" style={{ margin: '8px 0 0' }}>
              文件会随版本更新，所以每个文件旁边都标了<b>版本号、大小、更新时间和 sha256 短哈希</b>——
              这是你核对的依据。下完确认哈希对得上，才算真的拿到新版，
              「以为更新了、其实还是旧文件」是远程排障里最常见的假象。
            </p>
          </div></div>
          <div className="l-step"><span className="no">3</span><div>
            <b>在设备上运行并填写配置</b>
            <p className="muted" style={{ margin: '2px 0 0' }}>
              需要 Python 3.8+（Windows / Linux / macOS / Termux 均可，无第三方依赖）。
              首次运行 <code>python device_agent.py</code> 会自动生成 <code>config.json</code>，
              在里面填入 <code>name</code>（本页的设备名）和 <code>token</code>（第 1 步保存的）。
            </p>
            <p className="muted" style={{ margin: '6px 0 0', fontSize: 12.5 }}>
              <b>Windows 用户注意三个坑</b>（都踩过）：① 记事本另存可能把文件名变成
              <code>config.json.txt</code>，看起来像 json、其实不是；② 编码可能是 UTF-16 或带 BOM，
              请另存为 UTF-8；③ 引号必须是英文半角，末尾别多逗号。
              配置写坏时程序会直接报出语法错误与行列号，不会闷声不响。
            </p>
          </div></div>
          <div className="l-step"><span className="no">4</span><div>
            <b>再次运行，确认上线</b>
            <p className="muted" style={{ margin: '2px 0 0' }}>
              启动横幅会打印<b>版本、设备名、hub 地址、心跳间隔与当前 handler</b>——先看这一行。
              看到「已启动，心跳 15s」后回到本页刷新，设备卡片变为<b>「在线」</b>即接入成功。
            </p>
            <p className="muted" style={{ margin: '6px 0 0', fontSize: 12.5 }}>
              想让它开机就在线，跑一条 <code>python device_agent.py autostart</code>：
              Windows 走任务计划（登录时启动，用 <code>pythonw</code> 不弹黑框）、
              Linux 写 systemd --user 单元、安卓写 Termux:Boot 开机脚本；
              取消用 <code>autostart --remove</code>。
            </p>
          </div></div>
          <div className="l-step"><span className="no">5</span><div>
            <b>开始沟通</b>
            <p className="muted" style={{ margin: '2px 0 0' }}>
              在本页设备详情里「下发指令」试跑；也可以到<a href="/workstation">工作站</a>让站娘远程派活
              （见下方「让站娘给你的设备派活」）。
            </p>
          </div></div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 14, background: 'linear-gradient(120deg, #ffffff 30%, #e8f5ff)' }}>
        <h3>让站娘给你的设备派活</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          站娘已掌握三个设备工具，在工作站对话里直接说人话即可，无需记任何命令：
        </p>
        <ul style={{ margin: 0, paddingLeft: 20, lineHeight: 2 }}>
          <li><b>看有哪些设备</b>——「看看我的设备都在线吗？」</li>
          <li><b>下发指令</b>——「帮我在 my-laptop 上看一下剩余磁盘空间」</li>
          <li><b>取回结果</b>——「刚才那条指令执行得怎么样了？」</li>
        </ul>
        <p className="muted" style={{ margin: '6px 0 0', fontSize: 12.5 }}>
          说明：指令默认由设备上的 handler 解释执行（默认只回显）。设备每 15 秒左右取一次指令，
          站娘下发后可能需要等待一次心跳周期才能拿到结果。
        </p>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>安卓手机接入（Termux）· 白名单模式</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          手机上没有可被派活的 AI 应用，所以它走的是<b>设备通道</b>。为了安全，
          安卓用专门的 <code>termux_handler.py</code>：它<b>只认固定动词</b>，不接受任意命令，
          用户文本也只会作为单个参数传入（全程无 shell）。
        </p>
        <ol style={{ lineHeight: 2, paddingLeft: 20, margin: '8px 0 0' }}>
          <li>装 <b>Termux</b>（F-Droid 版）和 <b>Termux:API</b> 两个应用；</li>
          <li>Termux 里执行：<code>pkg install python termux-api</code>；</li>
          <li>
            照上面第 1 步在本页添加一个设备（如 <code>my-phone</code>）拿到 token，
            把 <code>device_agent.py</code> 与 <code>termux_handler.py</code> 放到同一目录；
          </li>
          <li>
            首次运行 <code>python device_agent.py</code> 生成 <code>config.json</code>，
            填好 <code>name</code>/<code>token</code>，并把 <code>handler</code> 改成
            <code>termux_handler.py</code>；
          </li>
          <li>
            再运行 <code>python device_agent.py</code>；想开机自启就跑一条
            <code>python device_agent.py autostart</code> —— 它会写好
            <code>~/.termux/boot/</code> 开机脚本（并先拿 wake-lock 防止被系统杀），
            剩下只需装 Termux:Boot 并在系统设置里关掉 Termux 的电池优化。
            取消：<code>python device_agent.py autostart --remove</code>。
          </li>
        </ol>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
          <b>能做的事</b>（发指令时直接写动词）：<code>battery</code>（电量/温度）、
          <code>wifi</code>、<code>location</code>、<code>clipboard</code>／<code>clipboard set 文本</code>、
          <code>toast 文本</code>、<code>notify 标题 | 内容</code>、<code>vibrate 500</code>、
          <code>torch on|off</code>、<code>volume music 7</code>、<code>photo back</code>；
          发 <code>help</code> 可随时查看完整清单。
        </div>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
          <b>为什么不用 shell_handler</b>：手机几乎不离身，一旦 shell 化，token 泄露等于把手机交出去。
          白名单 handler 认不出的指令一律拒绝并回帮助，不做任何猜测。
          定位/拍照首次使用需要在手机上点一次允许。
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>设备体检 · 用内置指令远程自检</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          以 <code>!</code> 开头的指令由 agent 自己回答，<b>不经过</b>本地 handler，全部只读、无副作用。
          设备看起来不对劲时，先发这五条，比远程猜要快得多（结果会出现在下方指令历史里）：
        </p>
        <div style={{ display: 'grid', gap: 10 }}>
          {BUILTINS.map(b => (
            <div key={b.cmd} style={{ borderLeft: '3px solid var(--accent2)', paddingLeft: 12 }}>
              <code className="mono"><b>{b.cmd}</b></code>
              <span className="muted" style={{ fontSize: 12.5, marginLeft: 8 }}>{b.use}</span>
              <div className="muted" style={{ fontSize: 12.5, marginTop: 3, lineHeight: 1.75 }}>{b.what}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>进阶：把铃枢接进你的 AI 客户端（MCP）</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          除了面板，铃枢的 MCP Hub 也能被支持 MCP 的客户端（如 Trae）直接调用，
          让本地 IDE 里同样能列出你的设备、下发指令、取回结果。
        </p>
        <p style={{ margin: '6px 0' }}>
          <b>推荐路线（免手抄密钥）：</b>去「<a href="/agents">我的智能体</a>」下载连接器，
          浏览器里点一次确认就拿到<b>只属于你账号</b>的凭据，再跑
          <code>bellhub-connector print-config trae</code> 即可生成带好凭据的配置。
        </p>
        <p className="muted" style={{ margin: '6px 0' }}>
          下面这个 stdio 桥是<b>兼容 / 离线路线</b>（服务器同机、或想绕过隧道时用），需要一个全站共享密钥：
        </p>
        <div className="row" style={{ gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <a className="l-btn ghost small" href="/api/devices/agent-files/bellhub_mcp_bridge.py" download>
            ⬇ bellhub_mcp_bridge.py
          </a>
          {mcpMeta && (
            <span className="muted" style={{ fontSize: 11.5, opacity: 0.85 }}>
              {mcpMeta.version ? `版本 ${mcpMeta.version} · ` : ''}
              {fmtSize(mcpMeta.size)} · 更新于 {fmtTime(mcpMeta.mtime)} · sha256 {mcpMeta.sha256.slice(0, 10)}…
            </span>
          )}
        </div>
        <ul style={{ margin: '10px 0 0', paddingLeft: 20, lineHeight: 2 }}>
          <li><b>密钥怎么来</b>——全站共享密钥由服务器侧生成（找管理员获取或轮换）。桥按「环境变量 →
              <code>config.json</code> 的 <code>mcp_token</code> → 同目录的 <code>mcp_token.txt</code>」三个来源依次查找，
              你只要用其中一种填上即可。日常接入请优先用上面的「我的智能体」路线。</li>
          <li><b>接不通先看启动横幅</b>——桥启动时会打印版本、程序目录、候选端点、配置路径、密钥来源与长度
              （不打印密钥本身）。这一行就是自检报告，比反复试错快。</li>
          <li><b>配置写坏会当场报错</b>——JSON 语法错误会给出行列号，编码不对会指出编码问题；
              桥不会因为读不到配置就静默失败。</li>
          <li><b>Windows 三个坑同上</b>——<code>config.json.txt</code> 扩展名、UTF-16/BOM 编码、全角引号。</li>
          <li><b>安全边界</b>——这串密钥等价于「在你的设备上执行指令」的权限：只放在自己的客户端配置里，
              不要提交进代码仓库或贴到公开场合；怀疑泄露请立刻找管理员轮换，旧密钥会立即失效。</li>
        </ul>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>常见错误对照 · 现象 → 它是什么意思 → 怎么办</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          这些现象都在真实排障中出现过。先确认语义，再决定要不要动手。
        </p>
        <p style={{ margin: '6px 0' }}>
          <b>动手之前先跑一条：</b><code>python device_agent.py doctor</code> ——
          它逐项检查配置、handler、Termux 依赖（安卓）与 hub 连通性，每项给结论和下一步，
          最后告诉你「是否可以直接启动」。
        </p>
        <div style={{ display: 'grid', gap: 14, marginTop: 12 }}>
          {DEV_TROUBLE.map(t => (
            <div key={t.sym} style={{ borderLeft: '3px solid var(--accent2)', paddingLeft: 12 }}>
              <b>{t.sym}</b>
              <div className="muted" style={{ fontSize: 13, marginTop: 3, lineHeight: 1.8 }}>
                <span style={{ opacity: 0.75 }}>为什么：</span>{t.why}
              </div>
              <div style={{ fontSize: 13, marginTop: 3, lineHeight: 1.8 }}>
                <span className="muted" style={{ opacity: 0.75 }}>怎么办：</span>{t.fix}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>常见问题</h3>
        <div className="l-faq" style={{ marginTop: 8 }}>
          <details>
            <summary>设备一直是「离线」？</summary>
            <p>
              检查三件事：① 设备上 agent 进程是否在跑（终端窗口不能关，建议后台常驻）；
              ② config.json 的 <code>name</code> 与本页设备名、<code>token</code> 是否完全一致；
              ③ 设备能否访问 <code>https://agent.example.com</code>。设备休眠也会掉线，唤醒后自动恢复。
            </p>
          </details>
          <details>
            <summary>token 忘记保存 / 怀疑泄露了？</summary>
            <p>在本页删除该设备重新添加即可（旧 token 立即失效，新 token 仅显示一次）。</p>
          </details>
          <details>
            <summary>默认 handler 只回显，怎么让它真干活？</summary>
            <p>
              两种方式：① 把 <code>device_handler.py</code> 里的回显逻辑改成你自己的业务（控制家电、跑本地脚本等）；
              ② 下载 <code>shell_handler.py</code>，在 <code>config.json</code> 里把 <code>handler</code> 改为
              <code>shell_handler.py</code>——指令会作为 shell 命令执行。
              <b>注意：shell 模式下任何拿到 token 的人都能在你设备上执行任意命令</b>，只在自有信任设备上启用。
            </p>
          </details>
          <details>
            <summary>支持哪些系统？要装依赖吗？</summary>
            <p>Windows / Linux / macOS / 安卓 Termux 均可，只需 Python 3.8+，无任何第三方依赖。</p>
          </details>
          <details>
            <summary>config.json 改完好像没生效，或者直接报错？</summary>
            <p>
              先看报错内容——新版 agent 对配置问题是<b>大声报错</b>的：JSON 写坏会给出行列号，
              文件不存在会打印它期望的完整路径。常见的三个坑都在 Windows 上：
              记事本另存成了 <code>config.json.txt</code>、编码被存成 UTF-16 或带 BOM、
              引号被输入法换成了中文全角。改完可以发一条 <code>!info</code> 复核实际生效的配置摘要。
            </p>
          </details>
          <details>
            <summary>怎么确认设备上只跑着一个 agent？跑的是不是最新版？</summary>
            <p>
              发 <code>!ps</code> 看本机 Python 进程：如果发现两个 device_agent 同时在跑，
              大概率是旧的没关掉——指令会被其中一个抢走，另一个看起来「不工作」，非常迷惑。
              发 <code>!version</code> 核对版本号，和本页下载区标注的版本、sha256 一致才算最新。
            </p>
          </details>
          <details>
            <summary>指令发出去了，但结果回不来？</summary>
            <p>
              先看指令状态：停在「待取走」是设备没心跳；停在「执行中」是 handler 还在跑（最长 30 分钟）或已挂。
              再看向回传的结果文本：出现 <code>handler 异常: …</code> 说明是设备侧 handler 的问题，
              异常类型会一并带回，照着上面的「常见错误对照」处理即可。
            </p>
          </details>
          <details>
            <summary>站娘能直接操作我的设备吗，安全吗？</summary>
            <p>
              站娘只能给「你自己的设备」下发文本指令，且指令必须经你设备上的 handler 执行——
              默认 handler 不执行任何操作。真正的权限边界在你手里：你不改 handler、不启用 shell 模式，
              设备就什么都不会做。
            </p>
          </details>
        </div>
      </div>
    </div>
  )
}
