import { useCallback, useEffect, useState } from 'react'
import { api, fmtSize, fmtTime, type DriveShare, type DriveListing } from '../api'
import { toast } from '../main'

/**
 * 共享空间：列出所有仍有效的分享。
 * 管理员明确的安全策略 —— 分享**仅登录用户可访问**（游客即使拿到链接也打不开），
 * 所以这里只做登录态校验，不做匿名入口。
 */
export function SharedPage() {
  const [mine, setMine] = useState<DriveShare[]>([])
  const [all, setAll] = useState<DriveShare[]>([])
  const [quota, setQuota] = useState<DriveListing | null>(null)

  const load = useCallback(() => {
    api.get<DriveShare[]>('/api/drive/share').then(setMine).catch(() => {})
    api.get<DriveShare[]>('/api/drive/shared').then(setAll).catch(err => toast(String(err.message), 'err'))
    api.get<DriveListing>('/api/drive/list').then(setQuota).catch(() => {})
  }, [])
  useEffect(() => { load() }, [load])

  const revoke = async (sh: DriveShare) => {
    if (!confirm(`撤销对「${sh.name}」的分享？`)) return
    try {
      await api.del(`/api/drive/share/${sh.id}`)
      toast('已撤销'); load()
    } catch (err) { toast(String(err.message), 'err') }
  }

  const copy = async (sh: DriveShare) => {
    const url = `${location.origin}/shared/${sh.id}`
    try { await navigator.clipboard.writeText(url); toast('链接已复制') }
    catch { toast(url) }
  }

  const others = all.filter(s => !s.owner_is_me)

  return (
    <>
      <h2>共享空间</h2>
      <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
        分享链接仅对已登录的 BellHub 用户有效；外网游客即使拿到链接也无法访问。
        我的网盘：已用 {quota?.used_mb ?? '-'} MB / {quota?.quota_mb ?? '-'} MB。
      </p>

      <div className="card">
        <h3>他人分享给我的（{others.length}）</h3>
        <div className="table-wrap">
        <table className="file-list table-cards">
          <thead><tr><th>文件名</th><th>分享者</th><th>备注</th><th>过期时间</th><th>操作</th></tr></thead>
          <tbody>
            {others.map(sh => (
              <tr key={sh.id}>
                <td className="name" data-label="文件名">📄 {sh.name}</td>
                <td className="muted" data-label="分享者">{sh.owner}</td>
                <td className="muted" data-label="备注">{sh.note || '-'}</td>
                <td className="muted" data-label="过期时间">{sh.expires_at ? fmtTime(sh.expires_at) : '永久'}</td>
                <td data-label="">
                  <span className="row" style={{ gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                    <a href={`/api/drive/shared/${sh.id}`}><button>下载</button></a>
                    <button onClick={() => copy(sh)}>复制链接</button>
                  </span>
                </td>
              </tr>
            ))}
            {others.length === 0 && <tr><td colSpan={5} className="muted" style={{ textAlign: 'center', padding: 22 }}>
              暂无他人分享给你的文件
            </td></tr>}
          </tbody>
        </table>
        </div>
      </div>

      <div className="card">
        <h3>我分享出去的（{mine.length}）</h3>
        <div className="table-wrap">
        <table className="file-list table-cards">
          <thead><tr><th>文件名</th><th>路径</th><th>备注</th><th>过期时间</th><th>操作</th></tr></thead>
          <tbody>
            {mine.map(sh => {
              const expired = sh.expires_at != null && sh.expires_at * 1000 < Date.now()
              return (
                <tr key={sh.id}>
                  <td className="name" data-label="文件名">{expired ? '⌛' : '📄'} {sh.name}</td>
                  <td className="muted mono" data-label="路径"
                    style={{ maxWidth: '46vw', display: 'inline-block' }}>{sh.relpath}</td>
                  <td className="muted" data-label="备注">{sh.note || '-'}</td>
                  <td className="muted" data-label="过期时间">{sh.expires_at ? fmtTime(sh.expires_at) : '永久'}{expired ? '（已过期）' : ''}</td>
                  <td data-label="">
                    <span className="row" style={{ gap: 8, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                      <button onClick={() => copy(sh)}>复制链接</button>
                      <button className="danger" onClick={() => revoke(sh)}>撤销</button>
                    </span>
                  </td>
                </tr>
              )
            })}
            {mine.length === 0 && <tr><td colSpan={5} className="muted" style={{ textAlign: 'center', padding: 22 }}>
              你还没有分享任何文件 —— 在「我的网盘」里对文件点「分享」
            </td></tr>}
          </tbody>
        </table>
        </div>
      </div>
    </>
  )
}
