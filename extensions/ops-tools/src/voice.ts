import os from 'node:os'
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { readFile, writeFile, mkdir } from 'node:fs/promises'
import { extname, isAbsolute, join } from 'node:path'

// ─── 语音工具：接 ESP32-S3 网关已经固定的 /api/v1/voice/{asr,tts} ───
// 边界约定（与伙伴商定）：插件只调网关的公开 HTTP 接口，不直接碰 SiliconeFlow 密钥、
// 也不碰助手本体的敏感面。网关侧换供应商，插件不用改。
//
// 环境变量（都可不配，走默认值）：
//   DSH_VOICE_BASE       默认 http://127.0.0.1:8781/api/v1/voice
//   DSH_VOICE_TOKEN      服务令牌；不配则读 DSH_VOICE_TOKEN_FILE
//   DSH_VOICE_TOKEN_FILE 默认 $HOME/dsh-suite/data/voice_service.token
//   DSH_VOICE_OUT        默认 ~/deepseek-harness/.voice-out

const DEFAULT_BASE = 'http://127.0.0.1:8781/api/v1/voice'
const DEFAULT_TOKEN_FILE = os.homedir() + '/dsh-suite/data/voice_service.token'
const DEFAULT_OUT_DIR = os.homedir() + '/deepseek-harness/.voice-out'

async function serviceToken(): Promise<string> {
  const env = (process.env.DSH_VOICE_TOKEN || '').trim()
  if (env) return env
  const file = process.env.DSH_VOICE_TOKEN_FILE || DEFAULT_TOKEN_FILE
  try {
    const token = (await readFile(file, 'utf8')).trim()
    if (token) return token
  } catch {
    // 落到下面统一报错
  }
  throw new Error(`没有语音服务令牌：请设置 DSH_VOICE_TOKEN，或把令牌写入 ${file}`)
}

function baseUrl(): string {
  return (process.env.DSH_VOICE_BASE || DEFAULT_BASE).replace(/\/+$/, '')
}

async function describeError(resp: Response): Promise<string> {
  const body = await resp.text().catch(() => '')
  const hint: Record<number, string> = {
    401: '缺少服务令牌',
    403: '服务令牌无效',
    413: '音频超过网关上限',
    422: '参数不合法（文本为空或超长）',
    502: '上游语音服务失败',
    503: '网关未启用语音服务',
  }
  const why = hint[resp.status] ? `（${hint[resp.status]}）` : ''
  return `网关返回 ${resp.status}${why}：${body.slice(0, 300)}`
}

export const name = 'voice-tools'
export const inject = ['tools']

export function apply(ctx: Context) {
  // ─── voice_asr: 音频文件 → 文本 ───
  ctx.tools.register(defineTool({
    name: 'voice_asr',
    description: '把一段语音转写成文字（网关 ASR）。用于管理员发来语音消息时先听清内容。'
      + '支持 wav/mp3/pcm 文件，pcm 需为 16 kHz、16 bit、单声道小端裸流。',
    parameters: {
      path: { type: 'string', required: true, description: '音频文件绝对路径' },
      format: { type: 'string', enum: ['wav', 'mp3', 'pcm'], description: '音频格式，默认按扩展名推断' },
    },
    output: { schema: { type: 'string' }, render: (_a: any, v: string) => [{ type: 'text', text: v }] },
    async execute(args: any) {
      try {
        const file = String(args.path)
        if (!isAbsolute(file)) return `Error: 请给绝对路径，收到 ${file}`
        const format = args.format
          || ((extname(file).slice(1).toLowerCase() || 'wav') as string)
        const audio = await readFile(file)
        if (audio.length === 0) return 'Error: 音频文件是空的'

        const form = new FormData()
        form.append('file', new Blob([new Uint8Array(audio)]), file)
        const resp = await fetch(`${baseUrl()}/asr?format=${encodeURIComponent(format)}&sample_rate=16000`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${await serviceToken()}` },
          body: form,
          signal: AbortSignal.timeout(180_000),
        })
        if (!resp.ok) return `Error: ${await describeError(resp)}`
        const data = await resp.json() as { text?: string; chars?: number; audio_bytes?: number }
        const text = (data.text || '').trim()
        return text
          ? `转写结果：${text}\n（${data.chars ?? text.length} 字，音频 ${((data.audio_bytes ?? audio.length) / 32000).toFixed(1)} 秒）`
          : '转写结果为空（音频里可能没人声）'
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))

  // ─── voice_tts: 文本 → 语音文件 ───
  ctx.tools.register(defineTool({
    name: 'voice_tts',
    description: '把一段文字合成语音（网关 TTS），默认 16 kHz / 16 bit / 单声道，落盘后返回路径。'
      + '适合给管理员回一段语音、或喂给 ESP32-S3 设备播放。',
    parameters: {
      text: { type: 'string', required: true, description: '要合成的文字（≤500 字）' },
      format: { type: 'string', enum: ['wav', 'pcm'], description: '输出格式，wav（默认，带头）或 pcm（裸 s16le）' },
      out: { type: 'string', description: '输出文件绝对路径；不传则写到默认语音目录，文件名带时间戳' },
    },
    output: { schema: { type: 'string' }, render: (_a: any, v: string) => [{ type: 'text', text: v }] },
    async execute(args: any) {
      try {
        const text = String(args.text || '').trim()
        if (!text) return 'Error: text 不能为空'
        const format = (args.format === 'pcm' ? 'pcm' : 'wav') as 'wav' | 'pcm'

        const resp = await fetch(`${baseUrl()}/tts`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${await serviceToken()}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ text, format }),
          signal: AbortSignal.timeout(180_000),
        })
        if (!resp.ok) return `Error: ${await describeError(resp)}`
        const audio = Buffer.from(await resp.arrayBuffer())

        let target = String(args.out || '')
        if (!target) {
          const dir = process.env.DSH_VOICE_OUT || DEFAULT_OUT_DIR
          await mkdir(dir, { recursive: true })
          const stamp = new Date().toISOString().replace(/[:.]/g, '-')
          target = join(dir, `tts-${stamp}.${format}`)
        } else if (!isAbsolute(target)) {
          return `Error: out 需要绝对路径，收到 ${target}`
        }
        await writeFile(target, audio)
        const seconds = (audio.length / 32000).toFixed(1)
        return `已合成语音：${target}\n${format.toUpperCase()} · ${audio.length} 字节 · 约 ${seconds} 秒 · 16 kHz/16 bit/单声道`
      } catch (err: any) {
        return `Error: ${err.message || err}`
      }
    },
  }))
}
