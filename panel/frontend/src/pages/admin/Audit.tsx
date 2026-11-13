import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../../api'

interface AuditRow {
  id: number; ts: number; actor: string; actor_role: string
  action: string; detail: string
}

export function AuditPage() {
  const [rows, setRows] = useState<AuditRow[]>([])
  const [limit, setLimit] = useState(200)

  const load = useCallback(() => {
    api.get<AuditRow[]>(`/api/admin/audit?limit=${limit}`).then(setRows).catch(() => {})
  }, [limit])
  useEffect(() => { load(); const t = setInterval(load, 15_000); return () => clearInterval(t) }, [load])

  return (
    <>
      <h2>审计日志</h2>
      <div className="card">
        <div className="row stack" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0, flex: 1 }}>最近 {rows.length} 条</h3>
          <select value={limit} onChange={e => setLimit(Number(e.target.value))}>
            <option value={100}>100 条</option>
            <option value={200}>200 条</option>
            <option value={500}>500 条</option>
            <option value={1000}>1000 条</option>
          </select>
          <button onClick={load}>刷新</button>
        </div>
        <div className="table-wrap">
          <table className="table-cards">
            <thead><tr><th>时间</th><th>操作者</th><th>角色</th><th>动作</th><th>详情</th></tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.id}>
                  <td className="muted" data-label="时间">{fmtTime(r.ts)}</td>
                  <td data-label="操作者">{r.actor}</td>
                  <td data-label="角色"><span className={`badge ${r.actor_role === 'admin' ? 'info' : ''}`}>{r.actor_role}</span></td>
                  <td className="mono" data-label="动作">{r.action}</td>
                  <td className="muted" data-label="详情" style={{ maxWidth: '60vw' }}>{r.detail}</td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td colSpan={5} className="muted">暂无记录</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
