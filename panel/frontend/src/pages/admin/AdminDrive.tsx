import { useCallback, useEffect, useState } from 'react'
import { api, fmtSize, fmtTime, type Account, type AdminDriveRow } from '../../api'
import { toast } from '../../main'

interface AdminDriveResp {
  users: AdminDriveRow[]
  max_file_mb: number
  shares_total: number
}

interface PendingUpload {
  sid: string; owner: string; dest_path: string; name: string
  size: number; received: number; total_chunks: number
  created_at: number; updated_at: number
}

interface PendingResp {
  uploads: PendingUpload[]
  keep_days: number
  total_mb: number
}

/** 管理端：所有用户的网盘占用与配额调整 + 未完成上传管理。 */
export function AdminDrive() {
  const [data, setData] = useState<AdminDriveResp | null>(null)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [pending, setPending] = useState<PendingResp | null>(null)

  const load = useCallback(() => {
    api.get<AdminDriveResp>('/api/admin/drive').then(setData).catch(err => toast(String(err.message), 'err'))
    api.get<Account[]>('/api/admin/accounts').then(setAccounts).catch(() => {})
    api.get<PendingResp>('/api/admin/drive/uploads').then(setPending).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const cleanupAll = async () => {
    if (!confirm('立即清理所有未完成上传（含未过期的半成品）？用户将无法续传这些文件。')) return
    try {
      const r = await api.post<{ removed: number; orphans: number; freed_mb: number }>(
        '/api/admin/drive/uploads/cleanup?days=0')
      toast(`已清理 ${r.removed} 个会话、${r.orphans} 个孤儿目录，释放 ${r.freed_mb}MB`)
      load()
    } catch (err) { toast(String(err.message), 'err') }
  }

  const uidOf = (username: string) => accounts.find(a => a.username === username)?.id

  const setQuota = async (row: AdminDriveRow) => {
    const uid = uidOf(row.username)
    if (!uid) { toast('找不到账号 id，请刷新', 'err'); return }
    const raw = prompt(`设置 ${row.username} 的网盘配额（MB，当前 ${row.quota_mb}MB）`, String(row.quota_mb))
    if (raw == null) return
    const mb = Number(raw)
    if (!Number.isFinite(mb) || mb < 1) { toast('请输入 ≥1 的整数', 'err'); return }
    try {
      await api.patch(`/api/admin/drive/${uid}`, { quota_drive_mb: Math.floor(mb) })
      toast(`${row.username} 配额已设为 ${Math.floor(mb)}MB`); load()
    } catch (err) { toast(String(err.message), 'err') }
  }

  const totalUsed = (data?.users ?? []).reduce((s, u) => s + u.used_mb, 0)
  const totalQuota = (data?.users ?? []).reduce((s, u) => s + u.quota_mb, 0)

  return (
    <>
      <h2>网盘管理</h2>
      <div className="card">
        <div className="row stack" style={{ gap: 24, flexWrap: 'wrap' }}>
          <div><span className="muted">用户数：</span><strong>{(data?.users ?? []).length}</strong></div>
          <div><span className="muted">总占用：</span><strong>{totalUsed.toFixed(1)} MB</strong></div>
          <div><span className="muted">总配额：</span><strong>{totalQuota.toFixed(0)} MB</strong></div>
          <div><span className="muted">单文件上限：</span><strong>{data?.max_file_mb ?? '-'} MB</strong></div>
          <div><span className="muted">分享总数：</span><strong>{data?.shares_total ?? 0}</strong></div>
          <button onClick={load}>刷新</button>
        </div>
      </div>

      <div className="card">
        <div className="table-wrap">
          <table className="file-list table-cards">
            <thead>
              <tr><th>用户</th><th>角色</th><th>已用</th><th>配额</th><th>使用率</th><th>分享数</th><th>操作</th></tr>
            </thead>
            <tbody>
              {(data?.users ?? []).map(u => {
                const pct = u.quota_mb > 0 ? Math.min(100, (u.used_mb / u.quota_mb) * 100) : 0
                return (
                  <tr key={u.username}>
                    <td className="name" data-label="用户">{u.username}</td>
                    <td className="muted" data-label="角色">{u.role === 'admin' ? '管理员' : '用户'}</td>
                    <td data-label="已用">{u.used_mb.toFixed(2)} MB</td>
                    <td data-label="配额">{u.quota_mb} MB</td>
                    <td data-label="使用率">
                      <div style={{ minWidth: 0, flex: 1 }}>
                        <div style={{ height: 8, background: 'var(--line, #e5eaf2)', borderRadius: 4, overflow: 'hidden' }}>
                          <div style={{
                            width: `${pct}%`, height: '100%',
                            background: pct > 90 ? '#e5484d' : (pct > 70 ? '#f5a524' : '#4a8cff'),
                          }} />
                        </div>
                        <span className="muted" style={{ fontSize: 12.5 }}>{pct.toFixed(1)}%</span>
                      </div>
                    </td>
                    <td className="muted" data-label="分享数">{u.shares}</td>
                    <td data-label=""><button style={{ width: '100%' }} onClick={() => setQuota(u)}>改配额</button></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
          提示：网盘配额（quota_drive_mb）与代码沙箱配额（quota_disk_mb）分开记账，互不占用。
          单文件上限可通过 settings 表的 drive_max_file_mb 调整（当前 {data?.max_file_mb ?? '-'} MB）。
          总占用 {fmtSize(Math.round(totalUsed * 1048576))}。
        </p>
      </div>

      <div className="card">
        <div className="row stack" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>未完成上传（分片续传会话）</h3>
          <span className="muted" style={{ fontSize: 12.5 }}>
            共 {pending?.uploads.length ?? 0} 个｜半成品占用 {pending?.total_mb ?? 0} MB｜保留 {pending?.keep_days ?? 7} 天
          </span>
          <button className="danger" onClick={cleanupAll} disabled={!pending?.uploads.length}>
            立即清理全部
          </button>
        </div>
        <div className="table-wrap">
          <table className="file-list table-cards">
            <thead>
              <tr><th>用户</th><th>目标文件</th><th>大小</th><th>进度</th><th>最后更新</th></tr>
            </thead>
            <tbody>
              {(pending?.uploads ?? []).map(u => {
                const pct = u.total_chunks ? (u.received / u.total_chunks) * 100 : 0
                return (
                  <tr key={u.sid}>
                    <td className="name" data-label="用户">{u.owner}</td>
                    <td className="mono" data-label="目标文件">{u.dest_path}</td>
                    <td data-label="大小">{fmtSize(u.size)}</td>
                    <td data-label="进度">
                      {u.received}/{u.total_chunks} 片（{pct.toFixed(0)}%）
                      <div style={{ height: 6, background: 'var(--line, #e5eaf2)', borderRadius: 3, marginTop: 4 }}>
                        <div style={{ width: `${pct}%`, height: '100%', background: '#4a8cff', borderRadius: 3 }} />
                      </div>
                    </td>
                    <td className="muted" data-label="最后更新">{fmtTime(u.updated_at)}</td>
                  </tr>
                )
              })}
              {!(pending?.uploads ?? []).length && (
                <tr><td colSpan={5} className="muted" style={{ textAlign: 'center', padding: 22 }}>
                  没有未完成的上传 —— 用户中途断线的文件会出现在这里，保留期内可续传
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
        <p className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
          半成品存在 <span className="mono">~/dsh-users/&lt;用户&gt;/drive/.upload/</span>，<b>不计入用户配额</b>；
          超过保留期会自动清理。「立即清理全部」会删除所有半成品（含未过期的），用户需重新上传。
        </p>
      </div>
    </>
  )
}
