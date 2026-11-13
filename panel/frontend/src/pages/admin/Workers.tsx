import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime, type WorkerRow } from '../../api'
import { toast } from '../../main'

export function Workers() {
  const [rows, setRows] = useState<WorkerRow[]>([])
  const [newToken, setNewToken] = useState('')
  const [form, setForm] = useState({ name: '', capabilities: '', user_available: false })

  const load = useCallback(() => { api.get<WorkerRow[]>('/api/admin/workers').then(setRows).catch(() => {}) }, [])
  useEffect(load, [load])

  const add = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      const r = await api.post<{ token: string }>('/api/admin/workers', {
        name: form.name,
        capabilities: form.capabilities.split(/[,，\s]+/).filter(Boolean),
        user_available: form.user_available,
      })
      setNewToken(r.token)
      setForm({ name: '', capabilities: '', user_available: false })
      load()
    } catch (err) { toast(String(err.message), 'err') }
  }

  const patch = async (wid: string, body: Record<string, unknown>, msg: string) => {
    try { await api.patch(`/api/admin/workers/${wid}`, body); toast(msg); load() }
    catch (err) { toast(String(err.message), 'err') }
  }

  return (
    <>
      <h2>Worker 协作</h2>
      <div className="card">
        <h3>添加 worker（token 直接下发，自动批准）</h3>
        <form onSubmit={add} className="row stack">
          <input placeholder="worker 名（如 win-desk-01）" value={form.name} required minLength={2}
            onChange={e => setForm({ ...form, name: e.target.value })} />
          <input placeholder="能力标签（逗号分隔）" value={form.capabilities}
            onChange={e => setForm({ ...form, capabilities: e.target.value })} />
          <label className="row" style={{ gap: 8, minHeight: 40, display: 'flex', alignItems: 'center' }}>
            <input type="checkbox" checked={form.user_available}
              onChange={e => setForm({ ...form, user_available: e.target.checked })} />
            用户任务可派发
          </label>
          <button className="primary">添加</button>
        </form>
        {newToken && (
          <div className="row stack" style={{ marginTop: 10 }}>
            <span className="badge warn">token（仅显示一次）：</span>
            <code className="mono" style={{ minWidth: 0 }}>{newToken}</code>
          </div>
        )}
      </div>

      <div className="card">
        <h3>Worker 列表</h3>
        <div className="table-wrap">
          <table className="table-cards">
            <thead>
              <tr><th>名称</th><th>能力</th><th>状态</th><th>用户可用</th><th>审批</th><th>最近在线</th><th>操作</th></tr>
            </thead>
            <tbody>
              {rows.map(w => (
                <tr key={w.id}>
                  <td className="mono" data-label="名称">{w.name}</td>
                  <td className="muted" data-label="能力">{JSON.parse(w.capabilities || '[]').join(', ')}</td>
                  <td data-label="状态"><span className={`badge ${w.status === 'online' ? 'ok' : ''}`}>{w.status}</span></td>
                  <td data-label="用户可用"><span className={`badge ${w.user_available ? 'ok' : ''}`}>{w.user_available ? '是' : '否'}</span></td>
                  <td data-label="审批"><span className={`badge ${w.approved ? 'ok' : 'warn'}`}>{w.approved ? '已批准' : '待审批'}</span></td>
                  <td className="muted" data-label="最近在线">{fmtTime(w.last_seen)}</td>
                  <td data-label="">
                    <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                      {!w.approved && <button className="primary" onClick={() => patch(w.id, { approved: true }, '已批准')}>批准</button>}
                      <button onClick={() => patch(w.id, { user_available: !w.user_available },
                        w.user_available ? '已标记不可用' : '已标记用户可用')}>{w.user_available ? '取消可用' : '设为可用'}</button>
                      <button className="danger" onClick={() => {
                        if (confirm(`删除 worker ${w.name}？`)) api.del(`/api/admin/workers/${w.id}`).then(load)
                      }}>删除</button>
                    </div>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && <tr><td colSpan={7} className="muted">暂无 worker。外部设备可运行 workers/worker.py register 自助注册（注册后在此审批）。</td></tr>}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
