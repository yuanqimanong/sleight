// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Panel from '../src/ServicePanel.vue'
import FleetPanel from '../src/FleetPanel.vue'
import { consoleData, response } from './fixtures'

const wrappers: ReturnType<typeof mount>[] = []
const administrator = { user: { id: 'admin', name: '管理用户', role: 'admin', default_environment: 'native', limit: 3 }, scopes: ['execute', 'manage', 'delegate'] }
const userRows = [
  { ...administrator.user, active: true, environments: [], profile_names: [] },
  { id: 'worker', name: 'fin', role: 'operator', active: true, limit: 2, default_environment: 'native', default_template: 'saved-proxy', default_launch_template: 'saved-launch', environments: ['manager'], profile_names: ['fixed-one'] },
]
let fetcher: ReturnType<typeof vi.fn>
beforeEach(() => {
  vi.useFakeTimers()
  fetcher = vi.fn(async (url: string, options?: any) => {
    const path = new URL(url).pathname
    if (options?.method === 'POST' && path.endsWith('/tokens')) return response({ token: 'sl_show_once' })
    const data = consoleData(url) ?? (
      path.endsWith('/environments') ? [{ id: 'native', name: '本机浏览器' }, { id: 'manager', name: 'Linux Manager', kind: 'cloak' }] :
      path.endsWith('/users') ? userRows :
      path.endsWith('/config/templates') ? [{ id: 'saved-proxy', name: '采集代理' }] :
      path.endsWith('/profiles') ? [{ id: 'browser', name: '测试浏览器' }] :
      path.endsWith('/settings/alerts') ? { configured: false, cpu_percent: 85, memory_percent: 80, duration_s: 60, cooldown_s: 900, target: 'host' } :
      path.endsWith('/settings/storage') ? { current: { backend: 'sqlite', database: 'fixture.db' }, data_dir: 'fixture/data' } : [])
    return response(data)
  })
  vi.stubGlobal('fetch', fetcher)
})
afterEach(() => { wrappers.splice(0).forEach(w => w.unmount()); vi.unstubAllGlobals(); vi.useRealTimers() })
async function open(props: any = {}) { const w = mount(Panel, { props: { identity: administrator, ...props } }); wrappers.push(w); await flushPromises(); return w }
async function choose(w: ReturnType<typeof mount>, name: string) {
  await w.findAll('.service-menu button').find(b => b.find('span').text().startsWith(name))!.trigger('click'); await flushPromises()
}

it('operators can use sessions, tokens and audit without querying administrator resources', async () => {
  const w = await open({ initialTab: 'storage', identity: { user: { id: 'worker', name: 'fin', role: 'operator' }, scopes: ['execute'] } })
  expect(w.find('.service-menu .selected').text()).toContain('运行会话')
  expect(w.findComponent(FleetPanel).exists()).toBe(false)
  expect(w.text()).not.toContain('我的实例')
  expect(w.find('.service-menu').text()).not.toContain('用户与权限')
  expect(w.find('.service-menu').text()).not.toContain('系统设置')
  expect(fetcher.mock.calls.some(c => String(c[0]).endsWith('/api/v1/users'))).toBe(false)
  await choose(w, '操作记录')
  expect(w.find('.management-card-heading h2').text()).toBe('操作记录')
})

it('opens alert/template shortcuts directly and delegates instance management to the main page', async () => {
  const w = await open({ initialTab: 'alerts' })
  expect(w.find('.service-menu .selected').text()).toContain('阈值告警')
  await w.setProps({ initialTab: 'launch-templates' }); await flushPromises()
  expect(w.find('.service-menu .selected').text()).toContain('创建模板')
  await choose(w, '访问 token')
  expect(w.find('.management-card-heading h2').text()).toBe('访问 token')
  await w.find('.service-instance-link').trigger('click')
  expect(w.emitted('navigate')).toEqual([['instances']])
})

it('protects a newly generated token from accidental dismissal and refresh, then hides its plaintext', async () => {
  const w = await open({ initialTab: 'tokens' })
  await w.findAll('button').find(b => b.text() === '＋ 生成 token')!.trigger('click')
  await w.find('input[placeholder="例如 fin 采集、MCP 助手"]').setValue('fin API')
  await w.find('.dialog form').trigger('submit'); await flushPromises()
  const field = () => w.find('textarea[aria-label="新 token"]')
  expect((field().element as HTMLTextAreaElement).value).toBe('sl_show_once')
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); await flushPromises()
  await w.find('.overlay').trigger('click'); await flushPromises()
  expect(field().exists()).toBe(true)
  await w.find('.management-toolbar button').trigger('click'); await flushPromises()
  expect((field().element as HTMLTextAreaElement).value).toBe('sl_show_once')
  await w.findAll('button').find(b => b.text() === '已保存，隐藏 token')!.trigger('click')
  expect(field().exists()).toBe(false)
  expect(w.find('.dialog').exists()).toBe(false)
})

it('keeps user inputs after a failed save and preserves defaults and authorization in the request', async () => {
  const original = fetcher.getMockImplementation()! as (url: string, options?: any) => Promise<any>
  fetcher.mockImplementation(async (url: string, options?: any) => options?.method === 'POST' && url.endsWith('/users') ? { ok: false, status: 400, json: async () => ({ detail: '保存失败，请重试' }) } : original(url, options))
  const w = await open({ initialTab: 'users' })
  const row = w.findAll('tbody tr').find(r => r.text().includes('fin'))!
  await row.find('button').trigger('click')
  await w.find('input[placeholder="例如 fin 采集用户"]').setValue('fin updated')
  await w.find('textarea[placeholder="Manager 中已有的实例名称"]').setValue('fixed-one\nfixed-one\nfixed-two')
  await w.find('.dialog form').trigger('submit'); await flushPromises()
  expect(w.find('.dialog [role=alert]').text()).toContain('保存失败')
  expect((w.find('input[placeholder="例如 fin 采集用户"]').element as HTMLInputElement).value).toBe('fin updated')
  const call = fetcher.mock.calls.find(c => String(c[0]).endsWith('/users') && c[1]?.method === 'POST')!
  expect(JSON.parse(call[1].body)).toMatchObject({ id: 'worker', name: 'fin updated', environments: ['manager'], profile_names: ['fixed-one', 'fixed-two'], default_template: 'saved-proxy', default_launch_template: 'saved-launch', limit: 2 })
})

it('issues operator credentials only for their own user and allowed scopes with the selected lifetime', async () => {
  const w = await open({ initialTab: 'tokens', identity: { user: { id: 'worker', name: 'fin', role: 'operator' }, scopes: ['execute'] } })
  await w.findAll('button').find(b => b.text() === '＋ 生成 token')!.trigger('click')
  expect(w.find('input[value="manage"]').attributes('disabled')).toBeDefined()
  expect(w.find('input[value="delegate"]').attributes('disabled')).toBeDefined()
  await w.find('input[placeholder="例如 fin 采集、MCP 助手"]').setValue('MCP')
  await w.find('.dialog select').setValue('604800')
  await w.find('.dialog form').trigger('submit'); await flushPromises()
  const call = fetcher.mock.calls.find(c => String(c[0]).endsWith('/tokens') && c[1]?.method === 'POST')!
  expect(JSON.parse(call[1].body)).toMatchObject({ user_id: 'worker', name: 'MCP', scopes: ['execute'], ttl: 604800 })
})

it('filters audit records before pagination and resets the page when filters change', async () => {
  const original = fetcher.getMockImplementation()! as (url: string, options?: any) => Promise<any>
  const audit = Array.from({ length: 25 }, (_, i) => ({ id: `record-${i}`, time: 1700000000 + i, user: 'worker', method: 'GET', path: `/job-${i}`, status: i < 22 ? 200 : 500, duration_ms: 12 }))
  fetcher.mockImplementation(async (url: string, options?: any) => url.endsWith('/audit') ? response(audit) : original(url, options))
  const w = await open({ initialTab: 'audit' })
  expect(w.findAll('tbody tr')).toHaveLength(20)
  await w.find('button[aria-label="下一页"]').trigger('click')
  expect(w.findAll('tbody tr')).toHaveLength(5)
  await w.find('.management-toolbar select').setValue('errors')
  expect(w.findAll('tbody tr')).toHaveLength(3)
  await w.find('input[type="search"]').setValue('job-24')
  expect(w.findAll('tbody tr')).toHaveLength(1)
  expect(w.find('tbody').text()).toContain('/job-24')
  expect(w.find('.management-pagination').text()).toContain('1 / 1')
})

it('discards a late response when moving to another management function', async () => {
  const original = fetcher.getMockImplementation()! as (url: string, options?: any) => Promise<any>
  let finish: (value: any) => void = () => {}, calls = 0
  fetcher.mockImplementation(async (url: string, options?: any) => {
    if (url.endsWith('/users') && ++calls === 1) return new Promise(resolve => { finish = resolve })
    if (url.endsWith('/tokens')) return response([{ id: 'fin-token', user_id: 'worker', name: 'fin API', scopes: ['execute'], expires: 0, revoked: false }])
    return original(url, options)
  })
  const w = await open({ initialTab: 'users' })
  await choose(w, '访问 token')
  finish(response([{ id: 'worker', name: '旧请求覆盖用户' }])); await flushPromises()
  expect(w.find('tbody').text()).toContain('fin')
  expect(w.text()).not.toContain('旧请求覆盖用户')
})

it('invalidates a pending administrator response after management permission is removed', async () => {
  const original = fetcher.getMockImplementation()! as (url: string, options?: any) => Promise<any>
  let finish: (value: any) => void = () => {}
  fetcher.mockImplementation(async (url: string, options?: any) => url.endsWith('/users') ? new Promise(resolve => { finish = resolve }) : original(url, options))
  const w = await open({ initialTab: 'users' })
  await w.setProps({ identity: { user: { id: 'worker', name: 'fin', role: 'operator' }, scopes: ['execute'] } }); await flushPromises()
  finish(response([{ id: 'secret', name: '管理员资源' }])); await flushPromises()
  expect(w.find('.service-menu .selected').text()).toContain('运行会话')
  expect(w.text()).not.toContain('管理员资源')
  expect(w.find('.service-menu').text()).not.toContain('用户与权限')
})

it('requires a new connection test if the database target changes during a pending check', async () => {
  const original = fetcher.getMockImplementation()! as (url: string, options?: any) => Promise<any>
  let finish: (value: any) => void = () => {}
  fetcher.mockImplementation(async (url: string, options?: any) => url.endsWith('/storage/test') ? new Promise(resolve => { finish = resolve }) : original(url, options))
  const w = await open({ initialTab: 'storage' })
  await w.find('input[placeholder="内网数据库地址"]').setValue('first-host')
  await w.find('.management-settings-card form').trigger('submit'); await flushPromises()
  await w.find('input[placeholder="内网数据库地址"]').setValue('second-host')
  finish(response({ message: '连接可用' })); await flushPromises()
  expect(w.text()).toContain('重新测试当前配置')
  expect(w.find('input[placeholder="迁移"]').attributes('disabled')).toBeDefined()
  expect(fetcher.mock.calls.some(c => String(c[0]).endsWith('/storage/migrate'))).toBe(false)
})
