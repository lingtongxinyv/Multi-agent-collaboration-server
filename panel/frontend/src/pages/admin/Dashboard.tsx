import { useCallback, useEffect, useState } from 'react'
import { api, fmtTime } from '../../api'
import { toast } from '../../main'

interface Metrics {
  cpu: number; mem: number; disk: number; load1: number
  net_rx: number; net_tx: number; ts: number
  top_processes?: { pid: number; name: string; cpu_percent: number; memory_percent: number }[]
  services?: { service: string; active: boolean; state: string }[]
  sandbox_usage_mb?: Record<string, number>
  usage?: {
    error?: string
    file?: string
    cny_today?: number
    tokens_today?: number
    balance?: number
    history?: { day: string; cny: number }[]
    events?: { at: number; tokens: number; cny: number }[]
  }
}
interface Sample { ts: number; cpu: number; mem: number; disk: number; load1: number }

function Spark({ data, metric }: { data: Sample[]; metric: 'cpu' | 'mem' | 'disk' }) {
  if (data.length < 2) return <div className="muted">采样中…</div>
  const w = 560, h = 120, max = 100
  const step = w / (data.length - 1)
  const pts = data.map((s, i) => `${(i * step).toFixed(1)},${(h - (s[metric] / max) * h).toFixed(1)}`)
  return (
    // 手机上让折线横向铺满卡片宽度（viewBox 560×120 + preserveAspectRatio=none 会压缩，
    // 但 24 小时全貌一眼可见）；若改成固定 min-width 横向滚动，最新的采样点会被推到屏幕外，
    // 看趋势反而更费劲，所以这里刻意不加 min-width。
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none">
      {[25, 50, 75].map(y => (
        <line key={y} x1={0} x2={w} y1={h * y / 100} y2={h * y / 100}
          stroke="var(--border)" strokeWidth={1} strokeDasharray="4 4" />
      ))}
      <polygon className="chart-area" points={`0,${h} ${pts.join(' ')} ${w},${h}`} />
      <polyline className="chart-line" points={pts.join(' ')} />
    </svg>
  )
}

function level(v: number): string { return v >= 90 ? 'err' : v >= 75 ? 'warn' : 'ok' }

export function Dashboard() {
  const [cur, setCur] = useState<Metrics | null>(null)
  const [hist, setHist] = useState<Sample[]>([])
  const [view, setView] = useState<'cpu' | 'mem' | 'disk'>('cpu')

  const load = useCallback(() => {
    api.get<Metrics>('/api/admin/monitor/current').then(setCur).catch(() => {})
    api.get<Sample[]>('/api/admin/monitor/history?hours=24').then(setHist).catch(() => {})
  }, [])
  useEffect(() => { load(); const t = setInterval(load, 10_000); return () => clearInterval(t) }, [load])

  if (!cur) return <h2>监控仪表盘</h2>
  return (
    <>
      <h2>监控仪表盘</h2>
      <div className="grid cols4" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="label">CPU</div>
          <div className={`value ${level(cur.cpu)}`}>{cur.cpu}%</div>
        </div>
        <div className="stat">
          <div className="label">内存</div>
          <div className={`value ${level(cur.mem)}`}>{cur.mem}%</div>
        </div>
        <div className="stat">
          <div className="label">根磁盘</div>
          <div className={`value ${level(cur.disk)}`}>{cur.disk}%</div>
        </div>
        <div className="stat">
          <div className="label">负载（1min）</div>
          <div className={`value ${cur.load1 > 4 ? 'err' : cur.load1 > 2 ? 'warn' : 'ok'}`}>{cur.load1}</div>
        </div>
      </div>
      <div className="card">
        <div className="row stack" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0, flex: 1 }}>24 小时趋势</h3>
          {/* 三个切换键放进独立子行共占一行；否则 .row.stack 会让每个按钮各占一整行 */}
          <div className="row" style={{ gap: 8, flex: '1 1 100%' }}>
            {(['cpu', 'mem', 'disk'] as const).map(m => (
              <button key={m} style={{ flex: 1, minWidth: 0 }}
                className={view === m ? 'primary' : ''} onClick={() => setView(m)}>{m.toUpperCase()}</button>
            ))}
          </div>
        </div>
        <Spark data={hist} metric={view} />
        <div className="muted">采样点 {hist.length} 个 · 更新于 {fmtTime(cur.ts)}</div>
      </div>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>今日成本</h3>
        {cur.usage?.error ? (
          <div className="muted">{cur.usage.error}</div>
        ) : (
          <>
            <div className="grid cols3">
              <div className="stat">
                <div className="label">今日花费</div>
                <div className={`value ${(cur.usage?.cny_today ?? 0) > 5 ? 'err' : (cur.usage?.cny_today ?? 0) > 2 ? 'warn' : 'ok'}`}>
                  ¥{cur.usage?.cny_today?.toFixed(2)}
                </div>
              </div>
              <div className="stat">
                <div className="label">今日 token</div>
                <div className="value">{(cur.usage?.tokens_today ?? 0).toLocaleString()}</div>
              </div>
              <div className="stat">
                <div className="label">余额</div>
                <div className="value">¥{cur.usage?.balance?.toFixed(2)}</div>
              </div>
            </div>
            <div className="muted" style={{ marginTop: 8 }}>
              近 7 天：{(cur.usage?.history ?? []).map(h => `${h.day.slice(5)} ¥${h.cny}`).join(' · ')}
            </div>
            <div className="muted">
              今日最贵的几次：{(cur.usage?.events ?? []).slice(-4).reverse()
                .map(e => `${fmtTime(e.at)} ¥${e.cny}`).join(' · ')}
            </div>
            <div className="muted">来源 {cur.usage?.file}（DSH 自己的账本；面板不再估算）</div>
          </>
        )}
      </div>
      <div className="grid cols2">
        <div className="card">
          <h3>托管服务</h3>
          <div className="table-wrap">
            <table className="table-cards">
              <tbody>
                {(cur.services ?? []).map(s => (
                  <tr key={s.service}>
                    <td className="mono" data-label="服务">{s.service}</td>
                    <td data-label="状态"><span className={`badge ${s.active ? 'ok' : 'err'}`}>{s.state}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
        <div className="card">
          <h3>资源占用 Top 进程</h3>
          <div className="table-wrap">
            <table className="table-cards">
              <thead><tr><th>PID</th><th>名称</th><th>CPU%</th><th>MEM%</th></tr></thead>
              <tbody>
                {(cur.top_processes ?? []).map(p => (
                  <tr key={p.pid}>
                    <td className="mono" data-label="PID">{p.pid}</td>
                    <td className="mono" data-label="名称">{p.name}</td>
                    <td data-label="CPU%">{p.cpu_percent?.toFixed(1)}</td>
                    <td data-label="MEM%">{p.memory_percent?.toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      {cur.sandbox_usage_mb && Object.keys(cur.sandbox_usage_mb).length > 0 && (
        <div className="card">
          <h3>用户沙箱占用</h3>
          <div className="row">
            {Object.entries(cur.sandbox_usage_mb).map(([user, mb]) => (
              <span key={user} className="badge">{user}: {mb} MB</span>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
