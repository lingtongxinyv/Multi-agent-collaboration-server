import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime, type AlertRow } from '../../api'
import { toast } from '../../main'

interface Rule {
  id: string; metric: string; op: string; threshold: number
  duration_sec: number; enabled: number; notify_email: number
}

export function AlertsPage() {
  const [alerts, setAlerts] = useState<AlertRow[]>([])
  const [rules, setRules] = useState<Rule[]>([])
  const [unackOnly, setUnackOnly] = useState(true)
  const [smtp, setSmtp] = useState({ smtp_host: '', smtp_port: '465', smtp_user: '', smtp_pass: '', smtp_to: '', configured: false })

  const load = useCallback(() => {
    api.get<AlertRow[]>(`/api/admin/alerts?unack=${unackOnly}`).then(setAlerts).catch(() => {})
    api.get<Rule[]>('/api/admin/alerts/rules').then(setRules).catch(() => {})
  }, [unackOnly])
  useEffect(() => { load(); const t = setInterval(load, 15_000); return () => clearInterval(t) }, [load])
  useEffect(() => { api.get<typeof smtp>('/api/admin/smtp').then(s => setSmtp(s as typeof smtp)).catch(() => {}) }, [])

  const ack = async (ids: number[]) => {
    try { await api.post('/api/admin/alerts/ack', { ids }); load() }
    catch (err) { toast(String(err.message), 'err') }
  }

  const saveSmtp = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      const r = await api.post<{ test_sent: boolean }>('/api/admin/smtp', {
        smtp_host: smtp.smtp_host, smtp_port: Number(smtp.smtp_port),
        smtp_user: smtp.smtp_user, smtp_pass: smtp.smtp_pass,
        smtp_from: smtp.smtp_user, smtp_to: smtp.smtp_to,
      })
      toast(r.test_sent ? '已保存，测试邮件已发送' : '已保存（测试邮件发送失败，检查配置）', r.test_sent ? 'ok' : 'err')
    } catch (err) { toast(String(err.message), 'err') }
  }

  return (
    <>
      <h2>告警中心</h2>
      <div className="card">
        <div className="row stack" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0, flex: 1 }}>告警列表</h3>
          <label className="row" style={{ gap: 6 }}>
            <input type="checkbox" checked={unackOnly} onChange={e => setUnackOnly(e.target.checked)} />
            仅未确认
          </label>
          <button onClick={() => ack(alerts.filter(a => !a.acknowledged).map(a => a.id))}>全部确认</button>
        </div>
        <div className="table-wrap">
          <table className="table-cards">
            <thead><tr><th>级别</th><th>时间</th><th>来源</th><th>标题</th><th>详情</th><th></th></tr></thead>
            <tbody>
              {alerts.map(a => (
                <tr key={a.id}>
                  <td data-label="级别"><span className={`badge ${a.level === 'critical' ? 'err' : a.level === 'warn' ? 'warn' : 'info'}`}>{a.level}</span></td>
                  <td className="muted" data-label="时间">{fmtTime(a.ts)}</td>
                  <td className="mono" data-label="来源">{a.source}</td>
                  <td data-label="标题">{a.title}</td>
                  <td className="muted" data-label="详情" style={{ maxWidth: 320, whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{a.detail}</td>
                  <td data-label="">{!a.acknowledged && <button onClick={() => ack([a.id])}>确认</button>}</td>
                </tr>
              ))}
              {alerts.length === 0 && <tr><td colSpan={6} className="muted" data-label="">没有告警</td></tr>}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3>告警规则</h3>
        <div className="table-wrap">
          <table className="table-cards">
            <thead><tr><th>指标</th><th>条件</th><th>阈值</th><th>持续</th><th>邮件</th><th>启用</th></tr></thead>
            <tbody>
              {rules.map(r => (
                <tr key={r.id}>
                  <td className="mono" data-label="指标">{r.metric}</td>
                  <td data-label="条件">{r.op}</td>
                  <td data-label="阈值"><input type="number" value={r.threshold} style={{ width: '100%', maxWidth: 120 }}
                    onChange={e => setRules(rules.map(x => x.id === r.id ? { ...x, threshold: Number(e.target.value) } : x))} /></td>
                  <td data-label="持续">{r.duration_sec}s</td>
                  <td data-label="邮件">
                    <input type="checkbox" checked={!!r.notify_email}
                      onChange={e => api.patch(`/api/admin/alerts/rules/${r.id}`, { notify_email: e.target.checked }).then(load)} />
                  </td>
                  <td className="row" style={{ gap: 6, flexWrap: 'nowrap' }} data-label="启用">
                    <input type="checkbox" checked={!!r.enabled}
                      onChange={e => api.patch(`/api/admin/alerts/rules/${r.id}`, { enabled: e.target.checked }).then(load)} />
                    <button style={{ minHeight: 40, padding: '8px 14px', whiteSpace: 'nowrap', flexShrink: 1 }}
                      onClick={() => api.patch(`/api/admin/alerts/rules/${r.id}`,
                        { threshold: r.threshold }).then(() => toast('阈值已保存')).catch(err => toast(String(err.message), 'err'))}
                    >保存阈值</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3>邮件推送（SMTP）{smtp.configured && <span className="badge ok">已配置</span>}</h3>
        <form onSubmit={saveSmtp} className="row stack">
          <input placeholder="SMTP 服务器" value={smtp.smtp_host} required
            onChange={e => setSmtp({ ...smtp, smtp_host: e.target.value })} />
          <input placeholder="端口" value={smtp.smtp_port} required
            onChange={e => setSmtp({ ...smtp, smtp_port: e.target.value })} />
          <input placeholder="账号" value={smtp.smtp_user} required
            onChange={e => setSmtp({ ...smtp, smtp_user: e.target.value })} />
          <input placeholder="密码/授权码" type="password" value={smtp.smtp_pass} required
            onChange={e => setSmtp({ ...smtp, smtp_pass: e.target.value })} />
          <input placeholder="收件邮箱" value={smtp.smtp_to} required
            onChange={e => setSmtp({ ...smtp, smtp_to: e.target.value })} />
          <button className="primary">保存并发送测试</button>
        </form>
      </div>
    </>
  )
}
