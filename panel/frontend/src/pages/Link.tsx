/** 确认接入页（/link?code=XXXXXXXX）。
 *
 * 连接器跑 pair 时会自动把浏览器开到这里：先把"这台设备是谁、要接什么"摊开给用户看，
 * 再让他点一次确认 —— 这是"浏览器授权"的那一下，也是凭据唯一的授权入口。
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, fmtTime } from '../api'
import { toast } from '../main'

interface PairInfo {
  user_code: string
  agent_name: string
  host: string
  platform: string
  capabilities: { type?: string; label?: string; app?: string }[]
  expires_at: number
  status: string
}

export function LinkPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [code, setCode] = useState((params.get('code') || '').toUpperCase())
  const [info, setInfo] = useState<PairInfo | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)

  const lookup = useCallback((value: string) => {
    if (value.length < 8) { setInfo(null); setErr(''); return }
    api.get<PairInfo>(`/api/link/pair-code?code=${encodeURIComponent(value)}`)
      .then(i => { setInfo(i); setErr('') })
      .catch(e => { setInfo(null); setErr(String((e as Error).message || '配对码无效')) })
  }, [])

  useEffect(() => { lookup(code) }, [code, lookup])

  const decide = async (approve: boolean) => {
    setBusy(true)
    try {
      await api.post(approve ? '/api/link/approve' : '/api/link/deny', { user_code: code })
      if (approve) { setDone(true); toast('已确认，设备马上上线') }
      else { toast('已拒绝这次接入'); navigate('/agents') }
    } catch (e) {
      toast(String((e as Error).message), 'err')
    } finally { setBusy(false) }
  }

  if (done) {
    return (
      <div className="card" style={{ maxWidth: 620, margin: '40px auto' }}>
        <h2>✅ 接入已确认</h2>
        <p className="muted">
          这台电脑现在属于你的账号了。回到电脑上的连接器窗口，它会自动领到凭据并启动；
          也可以运行 <code>bellhub-connector run</code> 让它常驻。
        </p>
        <div className="row" style={{ gap: 8 }}>
          <button className="primary" onClick={() => navigate('/agents')}>去看我的智能体</button>
        </div>
      </div>
    )
  }

  return (
    <div className="card" style={{ maxWidth: 620, margin: '40px auto' }}>
      <h2>确认接入这台设备</h2>
      <p className="muted" style={{ marginTop: -6 }}>
        连接器请求把一台电脑接入你的铃枢账号。它会以你的名义被站娘派活
        （交给本机 AI 执行，高危动作仍会在本机二次确认）。
      </p>

      <div className="row stack" style={{ marginTop: 12 }}>
        <input className="mono" placeholder="8 位配对码" maxLength={8} value={code}
          onChange={e => setCode(e.target.value.toUpperCase())} />
      </div>

      {err && <div className="hint" style={{ color: 'var(--danger,#e66)' }}>{err}</div>}

      {info && (
        <div style={{ marginTop: 12 }}>
          <table className="table"><tbody>
            <tr><th style={{ textAlign: 'left' }}>设备</th><td>{info.agent_name || '未命名'}</td></tr>
            <tr><th style={{ textAlign: 'left' }}>主机</th><td>{info.host || '—'}</td></tr>
            <tr><th style={{ textAlign: 'left' }}>平台</th><td>{info.platform || '—'}</td></tr>
            <tr><th style={{ textAlign: 'left' }}>能力</th>
              <td>{info.capabilities.length
                ? [...new Set(info.capabilities.map(c => c.label || c.type))].join('、')
                : '（未上报，稍后由连接器补）'}</td></tr>
            <tr><th style={{ textAlign: 'left' }}>有效期至</th><td>{fmtTime(info.expires_at)}</td></tr>
          </tbody></table>
          <div className="row" style={{ gap: 8, marginTop: 12 }}>
            <button className="primary" disabled={busy || info.status !== 'pending'}
              onClick={() => decide(true)}>确认接入</button>
            <button disabled={busy} onClick={() => decide(false)}>拒绝</button>
          </div>
          {info.status !== 'pending' && (
            <div className="muted" style={{ fontSize: 12.5 }}>
              这条请求已被处理过（状态：{info.status}）。重新跑一次连接器的 pair 可再发起。
            </div>
          )}
        </div>
      )}
    </div>
  )
}
