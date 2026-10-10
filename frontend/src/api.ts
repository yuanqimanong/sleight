export class APIError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

// Relative URLs work for direct access, pyp /sleight/, and remote agent panels.
export function endpoint(path: string): string {
  return new URL(path.replace(/^\//, ''), new URL('./', window.location.href)).toString()
}

export async function api<T = any>(path: string, method = 'GET', body?: unknown, headers: Record<string,string> = {}): Promise<T> {
  const response = await fetch(endpoint(path), {
    method, credentials: 'same-origin',
    headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new APIError(response.status, typeof data.detail === 'object' ? data.detail.message : data.detail || '请求失败，请重试')
  return data as T
}

export const memory = (bytes: number) => bytes ? `${Math.round(bytes / 1024 / 1024)} MB` : '—'
export const stateLabel: Record<string, string> = {
  running: '运行中', stopped: '已停止', starting: '启动中', cleanup: '等待回收', cleaning: '回收中',
  ok: '已完成', error: '失败', interrupted: '已中断', idle: '空闲',
  creating: '创建中', cleanup_pending: '等待回收', released: '已释放',
}
