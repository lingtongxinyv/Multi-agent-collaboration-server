import { useCallback, useEffect, useState } from 'react'
import { api } from '../../api'
import { toast } from '../../main'

const SERVICES = ['dsh-admin', 'dsh-panel', 'dsh-hub', 'nginx', 'cpolar-dsh'] as const

const DESCRIPTIONS: Record<string, string> = {
  'dsh-admin': '站娘 DSH（管理员，:3080）',
  'dsh-panel': '面板后端（:8010）',
  'dsh-hub': 'MCP Hub + Worker 队列（:8765）',
  nginx: '反向代理（80/443）',
  'cpolar-dsh': '公网隧道（agent.example.com）',
}

interface StateMap { [name: string]: string }

export function ServicesPage() {
  const [states, setStates] = useState<StateMap>({})
  const [busy, setBusy] = useState('')
  const [selftest, setSelftest] = useState<{ ok: boolean; passed: number; failed: number; summary: string } | null>(null)
  const [testing, setTesting] = useState(false)

  const load = useCallback(async () => {
    const next: StateMap = {}
    await Promise.all(SERVICES.map(async name => {
      try {
        const r = await api.post<{ state: string }>(`/api/admin/services/${name}`, { action: 'status' })
        next[name] = r.state
      } catch { next[name] = 'unknown' }
    }))
    setStates(next)
  }, [])
  useEffect(() => { load(); const t = setInterval(load, 15_000); return () => clearInterval(t) }, [load])

  const restart = async (name: string) => {
    if (!confirm(`确认重启 ${name}？面板自身重启会短暂断开连接。`)) return
    setBusy(name)
    try {
      const r = await api.post<{ note: string }>(`/api/admin/services/${name}`, { action: 'restart' })
      toast(r.note)
      setTimeout(load, 2000)
    } catch (err) { toast(String(err.message), 'err') }
    finally { setBusy('') }
  }

  /** 一键跑「智能体链接」验收套件（tests/run_all.sh，隔离临时库）。 */
  const runSelftest = async () => {
    setTesting(true)
    try {
      const r = await api.post<{ ok: boolean; passed: number; failed: number; summary: string }>(
        '/api/admin/selftest')
      setSelftest(r)
      toast(r.ok ? `自检全绿（${r.passed} 套）` : `自检有失败（${r.failed} 套）`, r.ok ? 'ok' : 'err')
    } catch (err) { toast(String(err.message), 'err') }
    finally { setTesting(false) }
  }

  return (
    <>
      <h2>服务管理</h2>
      <div className="card">
        <div className="table-wrap">
          <table className="table-cards">
            <thead><tr><th>服务</th><th>说明</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>
              {SERVICES.map(name => (
                <tr key={name}>
                  <td className="mono" data-label="服务">{name}</td>
                  <td className="muted" data-label="说明">{DESCRIPTIONS[name]}</td>
                  <td data-label="状态">
                    <span className={`badge ${states[name] === 'active' ? 'ok' : states[name] === 'unknown' ? '' : 'err'}`}>
                      {states[name] || '…'}
                    </span>
                  </td>
                  <td data-label="">
                    <button style={{ width: '100%' }} onClick={() => restart(name)} disabled={busy === name}>
                      {busy === name ? '重启中…' : '重启'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted">仅白名单内服务可重启（sudoers 限定）。人格保存后重启 dsh-admin 生效。</p>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <h3>智能体链接 · 验收自检</h3>
        <p className="muted" style={{ margin: '6px 0' }}>
          一键跑 <code>tests/run_all.sh --bin</code>：配对、派活、越权边界、安卓 handler、自启与 doctor，
          外加打包二进制的端到端。全部使用<b>隔离临时库</b>，不会碰真实数据。约 15 秒。
        </p>
        <div className="row" style={{ gap: 8, alignItems: 'center' }}>
          <button className="primary" onClick={runSelftest} disabled={testing}>
            {testing ? '自检运行中…' : '运行自检'}
          </button>
          {selftest && (
            <span className={`badge ${selftest.ok ? 'ok' : 'err'}`}>
              {selftest.ok ? '全绿' : '有失败'} · 通过 {selftest.passed} 套 / 失败 {selftest.failed} 套
            </span>
          )}
        </div>
        {selftest && (
          <pre className="mono" style={{ marginTop: 10, maxHeight: 320, overflow: 'auto',
                                          fontSize: 11.5, whiteSpace: 'pre-wrap' }}>
            {selftest.summary}
          </pre>
        )}
      </div>
    </>
  )
}
