/**
 * 星铃委托板（Star Bell Commission Board）—— 用户任务板，游戏任务列表风。
 *
 * 设计口径：
 *  · 四类委托：主线（长期）/ 支线（随手）/ 每日（零点刷新）/ 悬赏（额外奖励）
 *  · 状态机：待接取 → 进行中 → 已完成（后端只允许这两个动作，防跳态）
 *  · 每日委托里「与站娘交谈」「整理云端文件」由后端按真实记录核验，不靠自报，
 *    所以面板上显示的是「已完成」就一定发生过；打卡那条才是手动。
 *  · 文案统一用委托/委托人/星铃，不用「待办/项目管理」这类企业词。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { toast } from '../main'

export interface Quest {
  id: string
  title: string
  detail: string
  category: 'main' | 'side' | 'daily' | 'bounty'
  rarity: 'common' | 'rare' | 'epic' | 'legend'
  status: 'open' | 'active' | 'done'
  progress: number
  target: number
  reward: string
  day: string
  /** 委托人：'self' = 管理员自己挂的；其余为站娘/值守/服务名 */
  origin: string
  /** 外部幂等键（站娘挂账用，重复发布会更新而非新挂） */
  ref: string
}

interface Stats {
  total: number
  done: number
  active: number
  open: number
  stars: number
  daily_total: number
  daily_done: number
  staff_open: number
}

const CATS: { key: Quest['category'] | 'all'; name: string; glyph: string; hint: string }[] = [
  { key: 'all', name: '全部委托', glyph: '✦', hint: '所有悬在心头的与正在做的' },
  { key: 'main', name: '主线委托', glyph: '◈', hint: '长期目标，安安静静往前走' },
  { key: 'side', name: '支线委托', glyph: '◇', hint: '顺手就能收掉的小事' },
  { key: 'daily', name: '每日委托', glyph: '☀', hint: '每天零点刷新，做与不做都算数' },
  { key: 'bounty', name: '悬赏委托', glyph: '❖', hint: '有点难，但星铃给得多' },
]

const RARITY: Record<Quest['rarity'], { name: string; cls: string }> = {
  common: { name: '普通', cls: 'r-common' },
  rare: { name: '稀有', cls: 'r-rare' },
  epic: { name: '史诗', cls: 'r-epic' },
  legend: { name: '传说', cls: 'r-legend' },
}

const STATUS_LABEL: Record<Quest['status'], string> = {
  open: '待接取', active: '进行中', done: '已完成',
}

export function QuestsPage() {
  const [quests, setQuests] = useState<Quest[]>([])
  const [stats, setStats] = useState<Stats | null>(null)
  const [cat, setCat] = useState<Quest['category'] | 'all'>('all')
  const [onlyStaff, setOnlyStaff] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ title: '', detail: '', category: 'side', rarity: 'common', target: 1, reward: '' })

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ quests: Quest[]; stats: Stats }>('/api/quests')
      setQuests(data.quests)
      setStats(data.stats)
    } catch (e) {
      toast(`委托板读取失败：${(e as Error).message}`, 'err')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const act = async (q: Quest, kind: 'claim' | 'complete') => {
    setBusy(q.id)
    try {
      await api.post(`/api/quests/${encodeURIComponent(q.id)}/${kind}`)
      toast(kind === 'claim' ? `已接取「${q.title}」` : `「${q.title}」已完成，星铃入袋 ✦`)
      await load()
    } catch (e) {
      toast((e as Error).message, 'err')
    } finally {
      setBusy('')
    }
  }

  const create = async () => {
    if (!form.title.trim()) return
    setBusy('new')
    try {
      await api.post('/api/quests', { ...form, title: form.title.trim(), target: Math.max(1, Number(form.target) || 1) })
      setForm({ title: '', detail: '', category: 'side', rarity: 'common', target: 1, reward: '' })
      setCreating(false)
      toast('委托已挂上板，慢慢来～')
      await load()
    } catch (e) {
      toast((e as Error).message, 'err')
    } finally {
      setBusy('')
    }
  }

  const shown = useMemo(
    () => quests.filter(q =>
      (cat === 'all' || q.category === cat)
      && (!onlyStaff || (q.origin || 'self') !== 'self')),
    [quests, cat, onlyStaff])

  const grouped = useMemo(() => {
    const out: { key: string; items: Quest[] }[] = []
    for (const c of CATS) {
      if (c.key === 'all') continue
      const items = shown.filter(q => q.category === c.key)
      if (items.length) out.push({ key: c.key, items })
    }
    return out
  }, [shown])

  const pct = stats && stats.total ? Math.round((stats.done / stats.total) * 100) : 0

  return (
    <div className="qwrap">
      <section className="q-hero">
        <div className="q-hero-main">
          <div className="q-eyebrow">铃枢 · 委托者名册</div>
          <h1 className="q-title-lg">星铃委托板</h1>
          <p className="q-sub">
            把想做的事挂上板子，接取、推进、收尾。每完成一单，星铃会记在你这本册子上；
            站娘发现服务器上有需要你点头的事，也会直接挂到这里来。
            每日委托到零点自动翻页，做与不做都不催你。
          </p>
          <div className="q-chips">
            <span className="chip">待接取 {stats?.open ?? 0}</span>
            <span className="chip">进行中 {stats?.active ?? 0}</span>
            <span className="chip">已完成 {stats?.done ?? 0}</span>
            <span className="chip q-star">星铃 {stats?.stars ?? 0} ✦</span>
          </div>
        </div>
        <div className="q-hero-side">
          <div className="q-ring" style={{ ['--p' as string]: `${pct}%` }}>
            <div className="q-ring-in">
              <b>{pct}%</b>
              <small>本册完成度</small>
            </div>
          </div>
          <div className="q-today">
            今日委托 {stats?.daily_done ?? 0}/{stats?.daily_total ?? 0}
          </div>
        </div>
      </section>

      <div className="q-bar-row">
        <div className="q-tabs">
          {CATS.map(c => (
            <button key={c.key} className={cat === c.key ? 'on' : ''} onClick={() => setCat(c.key)} title={c.hint}>
              <span className="q-glyph">{c.glyph}</span>{c.name}
            </button>
          ))}
        </div>
        <div className="q-actions">
          <button className={`q-staff${onlyStaff ? ' on' : ''}`} onClick={() => setOnlyStaff(v => !v)}
                  title="只看站娘/值守替你挂的账：服务器上需要你处理的事">
            ✦ 站娘挂账 {stats?.staff_open ?? 0}
          </button>
          <button className="q-new" onClick={() => setCreating(v => !v)}>{creating ? '收起' : '＋ 挂一张新委托'}</button>
        </div>
      </div>

      {creating && (
        <div className="card q-form">
          <div className="q-form-row">
            <input placeholder="委托标题（想做什么）" value={form.title} maxLength={80}
                   onChange={e => setForm({ ...form, title: e.target.value })} />
            <select value={form.category} onChange={e => setForm({ ...form, category: e.target.value })}>
              <option value="main">主线</option><option value="side">支线</option>
              <option value="bounty">悬赏</option>
            </select>
            <select value={form.rarity} onChange={e => setForm({ ...form, rarity: e.target.value })}>
              <option value="common">普通</option><option value="rare">稀有</option>
              <option value="epic">史诗</option><option value="legend">传说</option>
            </select>
            <input type="number" min={1} max={99} value={form.target}
                   onChange={e => setForm({ ...form, target: Number(e.target.value) })} title="目标步数" />
            <input placeholder="奖励（可空）" value={form.reward} maxLength={24}
                   onChange={e => setForm({ ...form, reward: e.target.value })} />
          </div>
          <textarea rows={2} placeholder="补充说明（可空）" value={form.detail} maxLength={300}
                    onChange={e => setForm({ ...form, detail: e.target.value })} />
          <div className="q-form-actions">
            <button className="primary" disabled={!form.title.trim() || busy === 'new'} onClick={create}>挂上板</button>
          </div>
        </div>
      )}

      {loading && <div className="card muted" style={{ textAlign: 'center', padding: 28 }}>翻册中…</div>}

      {!loading && shown.length === 0 && (
        <div className="card muted q-empty">
          <div className="q-empty-glyph">☾</div>
          <div>{onlyStaff
            ? '站娘这会儿没给你挂账。服务器没在等你点头的事，安心做自己的就好。'
            : '这一栏还空着。挂一张新委托，或者等每日委托明天翻页。'}</div>
        </div>
      )}

      {grouped.map(g => {
        const meta = CATS.find(c => c.key === g.key)!
        return (
          <section key={g.key} className="q-section">
            <div className="q-sec-head">
              <span className="q-glyph">{meta.glyph}</span>
              <h2>{meta.name}</h2>
              <small>{meta.hint}</small>
            </div>
            <div className="q-grid">
              {g.items.map(q => {
                const rar = RARITY[q.rarity] ?? RARITY.common
                const done = q.status === 'done'
                const p = q.target ? Math.min(100, Math.round((q.progress / q.target) * 100)) : 0
                return (
                  <article key={q.id} className={`q-card ${rar.cls}${done ? ' is-done' : ''}`}>
                    <div className="q-card-top">
                      <div className="q-tags">
                        <span className={`q-rar ${rar.cls}`}>{rar.name}</span>
                        {(q.origin || 'self') !== 'self' && (
                          <span className="q-origin" title={`委托人：${q.origin}`}>✦ {q.origin}</span>
                        )}
                      </div>
                      <span className={`q-status s-${q.status}`}>{STATUS_LABEL[q.status]}</span>
                    </div>
                    <h3 className="q-card-title">{q.title}</h3>
                    {q.detail && <p className="q-card-detail">{q.detail}</p>}
                    <div className="q-prog">
                      <div className="q-bar"><i style={{ width: `${done ? 100 : p}%` }} /></div>
                      <span className="q-prog-num">{done ? q.target : q.progress}/{q.target}</span>
                    </div>
                    <div className="q-card-foot">
                      <span className="q-reward">{q.reward || '—'}</span>
                      {!done && (
                        q.status === 'open'
                          ? <button disabled={busy === q.id} onClick={() => act(q, 'claim')}>接取</button>
                          : <button className="primary" disabled={busy === q.id} onClick={() => act(q, 'complete')}>完成</button>
                      )}
                      {done && <span className="q-stamp">已归档</span>}
                    </div>
                  </article>
                )
              })}
            </div>
          </section>
        )
      })}
    </div>
  )
}
