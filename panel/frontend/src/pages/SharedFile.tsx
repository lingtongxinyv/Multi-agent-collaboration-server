import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { api, fmtTime, type DriveShare } from '../api'

/**
 * 分享链接落地页：/shared/<sid>
 *
 * 安全策略（管理员 2026-10-13 确认）：**仅登录用户可访问**。
 * 未登录者由后端返回 401，这里给出明确提示并引导登录；
 * 后端 /api/drive/shared/{sid} 才是真正的鉴权关口，本页只负责展示。
 */
export function SharedFile() {
  const { sid = '' } = useParams()
  const [share, setShare] = useState<DriveShare | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.get<DriveShare[]>('/api/drive/shared')
      .then(list => {
        const hit = list.find(s => s.id === sid)
        if (hit) setShare(hit)
        else setErr('分享不存在或已过期')
      })
      .catch(e => setErr(e.status === 401 ? '该分享仅对已登录的 BellHub 用户开放，请先登录。' : String(e.message)))
      .finally(() => setLoading(false))
  }, [sid])

  if (loading) return <div className="card"><span className="muted">加载中…</span></div>
  if (err || !share) {
    return (
      <div className="card">
        <h2>无法打开分享</h2>
        <p className="muted">{err || '未知错误'}</p>
        <a href="/login"><button className="primary">去登录</button></a>
      </div>
    )
  }

  const expired = share.expires_at != null && share.expires_at * 1000 < Date.now()
  return (
    <div className="card">
      <h2>📄 {share.name}</h2>
      <div className="table-wrap">
      <table className="file-list table-cards">
        <tbody>
          <tr><td className="muted" data-label="项目">分享者</td><td data-label="内容">{share.owner}</td></tr>
          <tr><td className="muted" data-label="项目">路径</td><td className="mono" data-label="内容">{share.relpath}</td></tr>
          <tr><td className="muted" data-label="项目">备注</td><td data-label="内容">{share.note || '-'}</td></tr>
          <tr><td className="muted" data-label="项目">创建时间</td><td data-label="内容">{fmtTime(share.created_at)}</td></tr>
          <tr><td className="muted" data-label="项目">过期时间</td><td data-label="内容">{share.expires_at ? fmtTime(share.expires_at) : '永久'}{expired ? '（已过期）' : ''}</td></tr>
        </tbody>
      </table>
      </div>
      <div className="row stack" style={{ marginTop: 14 }}>
        <a href={`/api/drive/shared/${share.id}`}>
          <button className="primary" disabled={expired}>下载文件</button>
        </a>
        <a href="/drive"><button>返回我的网盘</button></a>
        <span className="muted" style={{ fontSize: 12, marginLeft: 12 }}>文件大小以下载时的响应头为准</span>
      </div>
    </div>
  )
}
