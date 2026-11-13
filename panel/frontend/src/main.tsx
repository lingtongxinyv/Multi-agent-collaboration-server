import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, Navigate, NavLink, useNavigate, useLocation } from 'react-router-dom'
import { api, type Me } from './api'
import './styles.css'
import { Login } from './pages/Login'
import { Landing } from './pages/Landing'
import { HomePage } from './pages/Home'
import { QuestsPage } from './pages/Quests'
import { WorkstationPage } from './pages/Workstation'
import { DevicesPage } from './pages/Devices'
import { AgentsPage } from './pages/Agents'
import { LinkPage } from './pages/Link'
import { FilesPage } from './pages/Files'
import { DrivePage } from './pages/Drive'
import { SharedPage } from './pages/Shared'
import { SharedFile } from './pages/SharedFile'
import { ProfilePage } from './pages/Profile'
import { Dashboard } from './pages/admin/Dashboard'
import { Accounts } from './pages/admin/Accounts'
import { Workers } from './pages/admin/Workers'
import { AlertsPage } from './pages/admin/Alerts'
import { PersonaPage } from './pages/admin/Persona'
import { AuditPage } from './pages/admin/Audit'
import { ServicesPage } from './pages/admin/Services'
import { CodeCenter } from './pages/admin/CodeCenter'
import { AssistantChat } from './pages/admin/AssistantChat'
import { AdminDrive } from './pages/admin/AdminDrive'

interface AuthState {
  me: Me | null
  loading: boolean
  refresh: () => Promise<void>
  logout: () => void
}
const AuthCtx = createContext<AuthState>(null as unknown as AuthState)
export const useAuth = () => useContext(AuthCtx)

export function toast(msg: string, kind: 'ok' | 'err' = 'ok') {
  window.dispatchEvent(new CustomEvent('panel-toast', { detail: { msg, kind } }))
}

function Toasts() {
  const [items, setItems] = useState<{ id: number; msg: string; kind: string }[]>([])
  useEffect(() => {
    let n = 0
    const handler = (e: Event) => {
      const { msg, kind } = (e as CustomEvent).detail
      const id = ++n
      setItems(list => [...list, { id, msg, kind }])
      setTimeout(() => setItems(list => list.filter(t => t.id !== id)), 3500)
    }
    window.addEventListener('panel-toast', handler)
    return () => window.removeEventListener('panel-toast', handler)
  }, [])
  return <>{items.map(t => <div key={t.id} className={`toast ${t.kind}`}>{t.msg}</div>)}</>
}

/** 页面缓存：切换标签页时组件保持挂载（display:none 隐藏），表单/滚动/对话状态不丢失。 */
function KeepAlive({ pages }: { pages: Record<string, React.ReactNode> }) {
  const { pathname } = useLocation()
  const cache = React.useRef(new Map<string, React.ReactNode>())
  if (!cache.current.has(pathname) && pages[pathname]) {
    cache.current.set(pathname, pages[pathname])
  }
  return (
    <>
      {[...cache.current].map(([p, el]) => (
        <div key={p} style={p === pathname ? undefined : { display: 'none' }}>{el}</div>
      ))}
    </>
  )
}

/** 移动端顶部栏。桌面端由 CSS（.appbar{display:none}）隐藏，完全不占位。 */
function AppBar({ title, sub, onMenu, action }:
  { title: string; sub: string; onMenu: () => void; action?: React.ReactNode }) {
  return (
    <header className="appbar">
      <button className="appbar-btn" aria-label="打开导航菜单" onClick={onMenu}>☰</button>
      <span className="appbar-title">
        {title}
        <small>{sub}</small>
      </span>
      {action}
    </header>
  )
}

/** 抽屉开合状态：路由一变就自动收起，避免挡住新页面。 */
function useDrawer() {
  const { pathname } = useLocation()
  const [open, setOpen] = useState(false)
  useEffect(() => { setOpen(false) }, [pathname])
  return { open, toggle: () => setOpen(o => !o), close: () => setOpen(false) }
}

/** 顶部栏显示的当前页名（移动端内容区没有侧栏，用它提示“我在哪”） */
const USER_TITLES: Record<string, string> = {
  '/home': '主页', '/workstation': '工作站', '/drive': '我的网盘', '/shared': '共享空间',
  '/devices': '我的设备', '/agents': '我的智能体', '/link': '确认接入',
  '/files': '我的文件', '/quests': '星铃委托板', '/profile': '设置',
}
const ADMIN_TITLES: Record<string, string> = {
  '/admin': '监控仪表盘', '/admin/assistant': '站娘对话', '/admin/code': '代码中心',
  '/admin/drive': '网盘管理', '/admin/accounts': '账号管理', '/admin/workers': 'Worker 协作',
  '/admin/alerts': '告警中心', '/admin/persona': '站娘人格', '/admin/services': '服务管理',
  '/admin/audit': '审计日志',
}

/** 用户端布局：主站只服务普通用户；admin 在主站也能用用户功能。 */
function UserShell() {
  const { me, logout } = useAuth()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const { open, toggle, close } = useDrawer()
  if (!me) return <Navigate to="/login" replace />
  const isAdmin = me.role === 'admin'
  return (
    <div className={`app${open ? ' nav-open' : ''}`}>
      <AppBar title={USER_TITLES[pathname] ?? '用户工作台'} sub="用户工作台" onMenu={toggle} />
      {/* 点任意导航项即收起抽屉（点当前页时 pathname 不变，所以不能只靠路由变化） */}
      <nav className="sidebar" onClick={e => { if ((e.target as HTMLElement).closest('a')) close() }}>
        <div className="brand">
          <img className="brand-ico" src="/favicon.svg" alt="" />
          <span>铃枢 BellHub<small>用户工作台</small></span>
          <button className="drawer-x" aria-label="收起菜单" onClick={close}>✕</button>
        </div>
        <NavLink to="/home">主页</NavLink>
        <NavLink to="/workstation">工作站</NavLink>
        <NavLink to="/drive">我的网盘</NavLink>
        <NavLink to="/shared">共享空间</NavLink>
        <NavLink to="/devices">我的设备</NavLink>
        <NavLink to="/agents">我的智能体</NavLink>
        <NavLink to="/files">我的文件</NavLink>
        <NavLink to="/quests">星铃委托板</NavLink>
        <div className="spacer" />
        <NavLink to="/profile">{me.display_name || me.username}（设置）</NavLink>
        <a onClick={() => { logout(); navigate('/login') }}>退出登录</a>
        {isAdmin && <a href="/admin" title="管理员专属入口">管理控制台 ↗</a>}
      </nav>
      <div className="drawer-mask" onClick={close} />
      <main className="main"><KeepAlive pages={USER_PAGES} /></main>
    </div>
  )
}

const USER_PAGES: Record<string, React.ReactNode> = {
  '/home': <HomePage />,
  '/workstation': <WorkstationPage />,
  '/drive': <DrivePage />,
  '/shared': <SharedPage />,
  '/devices': <DevicesPage />,
  '/agents': <AgentsPage />,
  '/link': <LinkPage />,
  '/files': <FilesPage />,
  '/quests': <QuestsPage />,
  '/profile': <ProfilePage />,
}

/** 管理端布局：仅 role=admin 可达，与主站界面完全独立。 */
function AdminShell() {
  const { me, logout } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const { open, toggle, close } = useDrawer()
  if (!me) return <Navigate to={`/admin/login?next=${encodeURIComponent(location.pathname)}`} replace />
  if (me.role !== 'admin') {
    return (
      <div className="login-wrap">
        <div className="login-box">
          <h1>403</h1>
          <div className="hint">该区域仅限管理员访问</div>
          <a className="back-home" href="/">← 返回用户工作台</a>
        </div>
      </div>
    )
  }
  return (
    <div className={`app admin${open ? ' nav-open' : ''}`}>
      <AppBar
        title={ADMIN_TITLES[location.pathname] ?? '管理控制台'}
        sub="管理控制台"
        onMenu={toggle}
        action={<a className="appbar-act" href="/" title="切换到用户工作台">主站 ↗</a>}
      />
      <nav className="sidebar" onClick={e => { if ((e.target as HTMLElement).closest('a')) close() }}>
        <div className="brand">
          <img className="brand-ico" src="/favicon.svg" alt="" />
          <span>铃枢 BellHub<small>管理控制台</small></span>
          <button className="drawer-x" aria-label="收起菜单" onClick={close}>✕</button>
        </div>
        <NavLink to="/admin" end>监控仪表盘</NavLink>
        <NavLink to="/admin/assistant">站娘对话</NavLink>
        <NavLink to="/admin/code">代码中心</NavLink>
        <NavLink to="/admin/drive">网盘管理</NavLink>
        <NavLink to="/admin/accounts">账号管理</NavLink>
        <NavLink to="/admin/workers">Worker 协作</NavLink>
        <NavLink to="/admin/alerts">告警中心</NavLink>
        <NavLink to="/admin/persona">站娘人格</NavLink>
        <NavLink to="/admin/services">服务管理</NavLink>
        <NavLink to="/admin/audit">审计日志</NavLink>
        <div className="spacer" />
        <NavLink to="/profile">{me.display_name || me.username}（设置）</NavLink>
        <a onClick={() => { logout(); navigate('/admin/login') }}>退出登录</a>
        <a href="/dsh-entry" target="_blank" rel="noreferrer">打开站娘 DSH ↗</a>
        <a href="/" title="切换到用户工作台">返回主站 ↗</a>
      </nav>
      <div className="drawer-mask" onClick={close} />
      <main className="main">
        {ADMIN_PAGES[location.pathname]
          ? <KeepAlive pages={ADMIN_PAGES} />
          : <Navigate to="/admin" replace />}
      </main>
    </div>
  )
}

const ADMIN_PAGES: Record<string, React.ReactNode> = {
  '/admin': <Dashboard />,
  '/admin/assistant': <AssistantChat />,
  '/admin/code': <CodeCenter />,
  '/admin/drive': <AdminDrive />,
  '/admin/accounts': <Accounts />,
  '/admin/workers': <Workers />,
  '/admin/alerts': <AlertsPage />,
  '/admin/persona': <PersonaPage />,
  '/admin/services': <ServicesPage />,
  '/admin/audit': <AuditPage />,
}

/** 主站区域：已知用户页进工作台，其余回首页。 */
function UserArea() {
  const { pathname } = useLocation()
  if (USER_PAGES[pathname]) return <UserShell />
  return <Navigate to="/" replace />
}

function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)
  const lastAt = useRef(Date.now())
  const refresh = useCallback(async () => {
    lastAt.current = Date.now()
    try { setMe(await api.get<Me>('/api/auth/me')) }
    catch { setMe(null) }
    finally { setLoading(false) }
  }, [])
  const logout = useCallback(() => {
    api.post('/api/auth/logout').catch(() => {})
    setMe(null)
  }, [])
  useEffect(() => {
    refresh()
    const onKick = () => setMe(null)
    window.addEventListener('panel-logout', onKick)
    return () => window.removeEventListener('panel-logout', onKick)
  }, [refresh])

  // 配额按「本地日」切分（后端 db.today()）：面板挂着过夜时若不重拉，
  // 首页/设置页仍会显示昨天的「今日用量」。跨零点后 60 秒内自动刷新；
  // 另外从后台切回前台时，若已跨天或距上次刷新超过 10 分钟也重拉一次
  // （顺带让管理员调整过的配额、被禁用的账号及时反映到界面）。
  useEffect(() => {
    if (!me) return
    const localDay = () => {
      const d = new Date()
      return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
    }
    let day = localDay()
    const timer = window.setInterval(() => {
      if (localDay() !== day) { day = localDay(); refresh() }
    }, 60_000)
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      if (localDay() !== day || Date.now() - lastAt.current > 10 * 60_000) {
        day = localDay()
        refresh()
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('focus', onVisible)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('focus', onVisible)
    }
  }, [me, refresh])

  if (loading) return <div className="login-wrap"><div className="hint">加载中…</div></div>
  return (
    <AuthCtx.Provider value={{ me, loading, refresh, logout }}>
      <BrowserRouter>
        <Toasts />
        <Routes>
          {/* 主站：落地页 / 用户登录 / 用户工作台（admin 亦可用） */}
          <Route path="/login" element={me ? <Navigate to="/home" replace /> : <Login onSuccess={refresh} />} />
          <Route path="/" element={!me ? <Landing /> : <Navigate to="/home" replace />} />

          {/* 管理端：独立入口 /admin，独立登录页，仅限管理员 */}
          <Route path="/admin/login" element={me && me.role === 'admin' ? <Navigate to="/admin" replace /> : <Login admin onSuccess={refresh} />} />
          <Route path="/admin/*" element={<AdminShell />} />

          {/* 分享链接落地页：未登录也能打开该页，但后端会返回 401 并引导登录
              （管理员确认的安全策略：分享仅登录用户可取） */}
          <Route path="/shared/:sid" element={<div className="main" style={{ padding: 24 }}><SharedFile /></div>} />

          {/* 旧路径兼容：书签/旧链接重定向 */}
          <Route path="/chat" element={<Navigate to="/workstation" replace />} />
          <Route path="/tasks" element={<Navigate to="/workstation" replace />} />
          <Route path="/dashboard" element={<Navigate to="/admin" replace />} />
          <Route path="/accounts" element={<Navigate to="/admin/accounts" replace />} />
          <Route path="/workers" element={<Navigate to="/admin/workers" replace />} />
          <Route path="/alerts" element={<Navigate to="/admin/alerts" replace />} />
          <Route path="/persona" element={<Navigate to="/admin/persona" replace />} />
          <Route path="/services" element={<Navigate to="/admin/services" replace />} />
          <Route path="/audit" element={<Navigate to="/admin/audit" replace />} />

          {/* 用户工作台（keep-alive 保持页面状态） */}
          <Route path="/*" element={<UserArea />} />
        </Routes>
      </BrowserRouter>
    </AuthCtx.Provider>
  )
}

createRoot(document.getElementById('root')!).render(<App />)
