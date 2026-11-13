import { useCallback, useEffect, useState } from 'react'
import { api, type Account } from '../../api'
import { toast } from '../../main'

export function Accounts() {
  const [rows, setRows] = useState<Account[]>([])
  const [form, setForm] = useState({ username: '', password: '', role: 'user', display_name: '' })
  const [editing, setEditing] = useState<Account | null>(null)
  const [resetPw, setResetPw] = useState('')

  const load = useCallback(() => { api.get<Account[]>('/api/admin/accounts').then(setRows).catch(() => {}) }, [])
  useEffect(load, [load])

  const create = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      await api.post('/api/admin/accounts', form)
      toast(`账号 ${form.username} 已创建`)
      setForm({ username: '', password: '', role: 'user', display_name: '' })
      load()
    } catch (err) { toast(String(err.message), 'err') }
  }

  const patch = async (uid: string, body: Record<string, unknown>, msg: string) => {
    try { await api.patch(`/api/admin/accounts/${uid}`, body); toast(msg); load() }
    catch (err) { toast(String(err.message), 'err') }
  }

  const remove = async (acc: Account) => {
    if (!confirm(`删除账号 ${acc.username}？其沙箱文件将保留但无法再登录。`)) return
    try { await api.del(`/api/admin/accounts/${acc.id}`); load() }
    catch (err) { toast(String(err.message), 'err') }
  }

  return (
    <>
      <h2>账号管理</h2>
      <div className="card">
        <h3>新建账号</h3>
        <form onSubmit={create} className="row stack">
          <input placeholder="用户名" value={form.username} minLength={2} maxLength={32} required
            onChange={e => setForm({ ...form, username: e.target.value })} />
          <input placeholder="初始密码（≥8 位）" type="password" value={form.password} minLength={8} required
            onChange={e => setForm({ ...form, password: e.target.value })} />
          <input placeholder="显示名（可选）" value={form.display_name}
            onChange={e => setForm({ ...form, display_name: e.target.value })} />
          <select value={form.role} onChange={e => setForm({ ...form, role: e.target.value })}>
            <option value="user">普通用户</option>
            <option value="admin">管理员</option>
          </select>
          <button className="primary">创建</button>
        </form>
        <div className="muted" style={{ marginTop: 6 }}>默认配额：每日 10 万 token · 5 并发 · 1GB 磁盘（创建后可单独调整）</div>
      </div>

      <div className="card">
        <h3>账号列表</h3>
        <div className="table-wrap">
          <table className="table-cards">
            <thead>
              <tr>
                <th>用户名</th><th>角色</th><th>状态</th>
                <th>今日 token</th><th>沙箱 MB</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(a => (
                <tr key={a.id}>
                  <td data-label="用户名">
                    {a.username}
                    {a.display_name !== a.username && <span className="muted">（{a.display_name}）</span>}
                  </td>
                  <td data-label="角色"><span className={`badge ${a.role === 'admin' ? 'info' : ''}`}>{a.role}</span></td>
                  <td data-label="状态"><span className={`badge ${a.disabled ? 'err' : 'ok'}`}>{a.disabled ? '禁用' : '正常'}</span></td>
                  <td data-label="今日 token">{a.used_tokens} / {a.quota_tokens_daily}</td>
                  <td data-label="沙箱 MB">{a.used_disk_mb} / {a.quota_disk_mb}</td>
                  <td data-label="">
                    <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
                      <button onClick={() => setEditing(a)}>配额</button>
                      <button onClick={() => patch(a.id, { disabled: !a.disabled },
                        a.disabled ? '已启用' : '已禁用')}>{a.disabled ? '启用' : '禁用'}</button>
                      {a.role !== 'admin' && <button className="danger" onClick={() => remove(a)}>删除</button>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {editing && (
        <div className="card">
          <h3>调整配额：{editing.username}</h3>
          <div className="row stack">
            <label>token/日 <input type="number" value={editing.quota_tokens_daily}
              onChange={e => setEditing({ ...editing, quota_tokens_daily: Number(e.target.value) })} /></label>
            <label>并发 <input type="number" value={editing.quota_concurrent}
              onChange={e => setEditing({ ...editing, quota_concurrent: Number(e.target.value) })} /></label>
            <label>磁盘 MB <input type="number" value={editing.quota_disk_mb}
              onChange={e => setEditing({ ...editing, quota_disk_mb: Number(e.target.value) })} /></label>
            <label>重置密码 <input type="password" placeholder="留空不改" value={resetPw}
              onChange={e => setResetPw(e.target.value)} /></label>
          </div>
          <div className="row stack" style={{ marginTop: 10 }}>
            <button onClick={() => { setEditing(null); setResetPw('') }}>取消</button>
            <button className="primary" onClick={() => {
              const body: Record<string, unknown> = {
                quota_tokens_daily: editing.quota_tokens_daily,
                quota_concurrent: editing.quota_concurrent,
                quota_disk_mb: editing.quota_disk_mb,
              }
              if (resetPw) body.password = resetPw
              patch(editing.id, body, '配额已更新'); setEditing(null); setResetPw('')
            }}>保存</button>
          </div>
        </div>
      )}
    </>
  )
}
