/** 面板 API 封装：统一 fetch + 错误处理。 */

export interface Me {
  id: string
  username: string
  role: 'admin' | 'user'
  display_name: string
  must_change_pw: boolean
  quota?: { tokens_daily: number; concurrent: number; disk_mb: number; used_tokens: number; used_disk_mb: number }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (resp.status === 401) {
    window.dispatchEvent(new CustomEvent('panel-logout'))
    throw new ApiError(401, '未登录或会话已过期')
  }
  const text = await resp.text()
  // 有的接口返回纯文本（如文件内容），JSON 解析失败时按原文返回
  let data: unknown = {}
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }
  if (!resp.ok) {
    const detail = typeof data === 'object' && data !== null
      ? ((data as Record<string, unknown>).detail ?? (data as Record<string, unknown>).error)
      : undefined
    throw new ApiError(resp.status, String(detail ?? text.slice(0, 200) ?? `HTTP ${resp.status}`))
  }
  return data as T
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) }),
  put: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  del: <T,>(path: string) => request<T>(path, { method: 'DELETE' }),
}

export interface Account {
  id: string; username: string; role: 'admin' | 'user'; display_name: string
  disabled: number; quota_tokens_daily: number; quota_concurrent: number
  quota_disk_mb: number; used_tokens: number; used_disk_mb: number
}

export interface FileItem {
  name: string; type: 'dir' | 'file'; size: number | null; mtime: number; relpath: string
}

export interface Session {
  id: string; user_id: string; title: string; pinned: number
  status: 'idle' | 'running' | 'done' | 'failed' | 'cancelled'
  dsh_session_id: string; tokens_used: number
  created_at: number; updated_at: number
}

export interface SessionDetail extends Session {
  messages: { role: 'user' | 'assistant'; text: string; ts: number }[]
}

export interface WorkerRow {
  id: string; name: string; capabilities: string; user_available: number
  approved: number; status: string; last_seen: number; host: string
}

export interface AlertRow {
  id: number; ts: number; level: 'info' | 'warn' | 'critical'; source: string
  title: string; detail: string; acknowledged: number
}

export interface PersonaRow {
  card: Record<string, unknown>; prefix: string; suffix: string; updated_at: number
}

export const fmtTime = (ts: number | null) =>
  ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '-'

// ── 个人网盘（助手 2026-10-13 阶段二/三）──────────────────────────

export interface DriveItem {
  name: string; type: 'dir' | 'file'; size: number | null; mtime: number; relpath: string
}

export interface DriveListing {
  path: string; parent: string; items: DriveItem[]
  used_mb: number; quota_mb: number; max_file_mb: number
}

export interface DriveShare {
  id: string; owner: string; relpath: string; name: string; note: string
  created_at: number; expires_at: number | null; owner_is_me?: boolean
}

export interface AdminDriveRow {
  username: string; role: string; used_mb: number; quota_mb: number; shares: number
}

/**
 * 带进度回调的文件上传。
 * 用 XMLHttpRequest 而非 fetch：只有 XHR 能报告 **上传** 进度（fetch 的
 * ReadableStream 只能读响应体进度），大文件必须让用户看得见进度条。
 */
export function uploadDriveFile(
  path: string,
  file: File,
  onProgress?: (loaded: number, total: number) => void,
): Promise<{ relpath: string; size: number; used_mb: number }> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('path', path)
    form.append('file', file, file.name)
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/drive/upload')
    xhr.withCredentials = true
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total)
    }
    xhr.onload = () => {
      let data: unknown = {}
      try { data = JSON.parse(xhr.responseText) } catch { data = xhr.responseText }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data as { relpath: string; size: number; used_mb: number })
      } else {
        const detail = typeof data === 'object' && data !== null
          ? (data as Record<string, unknown>).detail : undefined
        reject(new ApiError(xhr.status, String(detail ?? `HTTP ${xhr.status}`)))
      }
    }
    xhr.onerror = () => reject(new ApiError(0, '网络错误（上传中断）'))
    xhr.send(form)
  })
}

/** 通过浏览器原生下载（带 cookie，支持中文名）。 */
export function downloadDriveFile(path: string, filename?: string): void {
  const a = document.createElement('a')
  a.href = `/api/drive/download?path=${encodeURIComponent(path)}`
  if (filename) a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}

export const fmtSize = (bytes: number | null): string => {
  if (bytes == null) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`
  return `${(bytes / 1073741824).toFixed(2)} GB`
}

// ── 分片上传与跨会话续传（助手 2026-10-13）────────────────────────

export interface UploadSession {
  sid: string; owner: string; dest_path: string; name: string
  size: number; sha256: string; chunk_size: number; total_chunks: number
  uploaded: number[]; received: number; created_at: number; updated_at: number
  resumed?: boolean; direct?: boolean
}

/** 计算文件 SHA-256（分块读取，大文件不占内存）；不支持 WebCrypto 时返回空串。 */
export async function computeFileSha256(
  file: File,
  onProgress?: (done: number, total: number) => void,
): Promise<string> {
  if (!window.crypto?.subtle) return ''
  const CH = 8 * 1024 * 1024
  const total = file.size
  const hex = (d: ArrayBuffer) =>
    [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, '0')).join('')
  try {
    if (total <= CH) {
      const d = await crypto.subtle.digest('SHA-256', await file.slice(0, total).arrayBuffer())
      onProgress?.(total, total)
      return hex(d)
    }
    // 大文件：分块读入后一次性喂给同一个 hasher（准确且省内存）
    const parts: BlobPart[] = []
    let off = 0
    while (off < total) {
      const end = Math.min(off + CH, total)
      parts.push(await file.slice(off, end).arrayBuffer())
      off = end
      onProgress?.(off, total)
    }
    const d = await crypto.subtle.digest('SHA-256', await new Blob(parts).arrayBuffer())
    return hex(d)
  } catch {
    return ''      // 哈希失败不阻断上传（自动退化为「路径+大小」匹配）
  }
}

/** 创建或复用上传会话。 */
export async function initChunkedUpload(
  path: string, size: number, sha256 = '', chunkSize?: number,
): Promise<UploadSession> {
  return api.post<UploadSession>('/api/drive/upload/init',
    { path, size, sha256, chunk_size: chunkSize })
}

/** 上传单个分片（幂等：重传覆盖）。 */
export function uploadChunk(
  sid: string, index: number, blob: Blob,
  onProgress?: (loaded: number, total: number) => void,
): Promise<{ index: number; received: number; total_chunks: number }> {
  return new Promise((resolve, reject) => {
    const form = new FormData()
    form.append('sid', sid)
    form.append('index', String(index))
    form.append('data', blob, `${index}.part`)
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/drive/upload/chunk')
    xhr.withCredentials = true
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded, e.total)
    }
    xhr.onload = () => {
      let data: unknown = {}
      try { data = JSON.parse(xhr.responseText) } catch { data = xhr.responseText }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data as { index: number; received: number; total_chunks: number })
      } else {
        const detail = typeof data === 'object' && data !== null
          ? (data as Record<string, unknown>).detail : undefined
        reject(new ApiError(xhr.status, String(detail ?? `HTTP ${xhr.status}`)))
      }
    }
    xhr.onerror = () => reject(new ApiError(0, '网络错误（分片上传中断）'))
    xhr.send(form)
  })
}

export async function completeChunkedUpload(
  sid: string,
): Promise<{ relpath: string; size: number; used_mb: number }> {
  return api.post('/api/drive/upload/complete', { sid })
}

/** 查询会话进度（刷新页面后可恢复显示）。 */
export async function chunkedUploadStatus(sid: string): Promise<UploadSession> {
  return api.get<UploadSession>(`/api/drive/upload/status?sid=${encodeURIComponent(sid)}`)
}

/** 放弃上传并清理服务端分片。 */
export async function cancelChunkedUpload(sid: string): Promise<void> {
  await api.post('/api/drive/upload/cancel', { sid })
}

/**
 * 完整的分片流程：init → 逐片串行上传（失败退避重试）→ complete。
 * 断线后重新调用本函数即可命中同一会话，只补传缺失分片。
 */
export async function chunkedUpload(
  file: File, destPath: string, sha256: string,
  hooks: {
    onProgress?: (loaded: number, total: number, phase: string) => void
    onSession?: (s: UploadSession) => void
    shouldStop?: () => boolean
    maxRetry?: number
  } = {},
): Promise<{ relpath: string; size: number; used_mb: number }> {
  const maxRetry = hooks.maxRetry ?? 3
  const session = await initChunkedUpload(destPath, file.size, sha256)
  hooks.onSession?.(session)
  const got = new Set(session.uploaded || [])
  const CH = session.chunk_size
  let doneBytes = got.size * CH
  const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

  for (let i = 0; i < session.total_chunks; i++) {
    if (hooks.shouldStop?.()) throw new ApiError(0, '已取消上传')
    if (got.has(i)) continue
    const start = i * CH
    const blob = file.slice(start, Math.min(start + CH, file.size))
    let attempt = 0
    for (;;) {
      try {
        await uploadChunk(session.sid, i, blob, (loaded) => {
          hooks.onProgress?.(doneBytes + loaded, file.size, 'uploading')
        })
        doneBytes += blob.size
        hooks.onProgress?.(doneBytes, file.size, 'uploading')
        break
      } catch (err) {
        attempt += 1
        if (attempt >= maxRetry) throw err
        hooks.onProgress?.(doneBytes, file.size, `第 ${i} 片重试 ${attempt}/${maxRetry - 1}`)
        await sleep(1000 * attempt)       // 退避：1s、2s…
      }
    }
  }
  hooks.onProgress?.(file.size, file.size, 'assembling')
  return completeChunkedUpload(session.sid)
}
