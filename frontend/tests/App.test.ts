import {consoleData,response} from './fixtures'
// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import App from '../src/App.vue'
import ConfigPanel from '../src/ConfigPanel.vue'

let request: ReturnType<typeof vi.fn>
const wrappers: ReturnType<typeof mount>[] = []
beforeEach(() => {
  window.history.replaceState({}, '', '/sleight/')
  request = vi.fn(async (url: string) => {
    const path = new URL(url).pathname
    const data = consoleData(url) ?? (path.endsWith('/auth/me') ? {user:{id:'admin',name:'管理员',role:'admin'},scopes:['execute','manage','delegate']} : path.endsWith('/defaults') ? {version:'0.6.0',spec:{},profile_regions:[]} :
      path.endsWith('/platform') ? {os:'Windows',arch:'amd64',browsers:[{name:'Chrome',binary:'chrome.exe'}],deployments_dir:'D:/deploy'} : [])
    return {ok:true,json:async()=>data}
  })
  vi.stubGlobal('fetch', request)
})
afterEach(() => { wrappers.splice(0).forEach(w=>w.unmount()); vi.unstubAllGlobals() })
async function open() { const w=mount(App); wrappers.push(w); await flushPromises(); return w }
it('creates a custom executable without native select validation blocking it', async () => {
  const w=await open()
  await w.findAll('button').find(b=>b.text().includes('新建实例'))!.trigger('click');await flushPromises()

  await w.find('input[placeholder="完整可执行文件路径"]').setValue('D:/chromium/chrome.exe')
  await w.findAll('button').find(b=>b.text()==='检查内核')!.trigger('click');await flushPromises()
  await w.find('input[placeholder="给实例一个容易识别的名字"]').setValue('probe')
  expect(w.find('select').attributes('required')).toBeUndefined()
  await w.find('.dialog form').trigger('submit'); await flushPromises()
  const call=request.mock.calls.find(c=>String(c[0]).endsWith('/api/v1/fleet/create') && c[1]?.method==='POST')
  expect(call).toBeDefined()
  expect(JSON.parse(call![1].body)).toMatchObject({name:'probe',binary:'D:/chromium/chrome.exe'})
  expect(w.text()).toContain('实例已创建')
})
it('keeps deployment disabled until an environment check succeeds and supports Escape', async () => {
  const w=await open()
  await w.findAll('button').find(b=>b.text().includes('部署环境'))!.trigger('click')
  await w.findAll('button').find(b=>b.text().includes('部署 / 导入'))!.trigger('click')
  expect(w.findAll('.dialog button').find(b=>b.text()==='部署')!.attributes('disabled')).toBeDefined()
  expect(w.find('.dialog').text()).toContain('Docker 数据卷')
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'})); await flushPromises()
  expect(w.find('.dialog').exists()).toBe(false)
})
it('shows an actionable login error', async () => {
  request.mockResolvedValue({ok:false,status:401,json:async()=>({detail:'口令错误'})})
  const w=await open()
  await w.find('input[type=password]').setValue('wrong')
  await w.find('form').trigger('submit'); await flushPromises()
  expect(w.find('[role=alert]').text()).toBe('口令错误')
})

it('checks a new deployment before attempting to reach its not-yet-created Manager', async () => {
  const original = request.getMockImplementation()! as (url: string, options?: any) => Promise<any>
  request.mockImplementation(async (url: string, options?: any) => {
    if (url.includes('/preflight')) return {ok:true,json:async()=>({blocked:false,checks:[{name:'docker',level:'ok',detail:'ready'}]})}
    if (url.includes('/hosts/') && url.includes('/profiles')) return {ok:false,status:400,json:async()=>({detail:'no AUTH_TOKEN'})}
    return original(url, options)
  })
  const w=await open()
  await w.findAll('button').find(b=>b.text().includes('部署环境'))!.trigger('click')
  await w.findAll('button').find(b=>b.text().includes('部署 / 导入'))!.trigger('click')
  await w.find('.dialog form').trigger('submit'); await flushPromises()
  expect(w.find('.preflight').text()).toContain('ready')
  expect(w.findAll('.dialog button').find(b=>b.text()==='部署')!.attributes('disabled')).toBeUndefined()
  expect(request.mock.calls.some(c=>String(c[0]).includes('/hosts/') && String(c[0]).includes('/profiles'))).toBe(false)
})

it('edits a template in a dialog while preserving credentials and can create another template',async()=>{
  const original=request.getMockImplementation()! as (url:string,options?:any)=>Promise<any>
  request.mockImplementation(async(url:string,options?:any)=>url.includes('/config/templates')?response([{id:'saved',name:'Saved proxy',proxy_display:'socks5://***@proxy:1080',extension_paths:['/data/extensions/probe']}]):original(url,options))
  const w=await open()
  await w.findAll('button').find(b=>b.text().includes('插件与代理'))!.trigger('click');await flushPromises()
  expect(w.find('.dialog').exists()).toBe(false)
  await w.findAll('button').find(b=>b.text()==='编辑模板')!.trigger('click')
  expect(w.find('.dialog input[type=password]').attributes('disabled')).toBeDefined()
  await w.find('.dialog input[placeholder="例如：Reuters 采集"]').setValue('Renamed')
  await w.find('.dialog form').trigger('submit');await flushPromises()
  const call=request.mock.calls.find(c=>String(c[0]).includes('/config/templates')&&c[1]?.method==='POST')!
  expect(JSON.parse(call[1].body)).toMatchObject({id:'saved',name:'Renamed',extension_paths:['/data/extensions/probe']})
  expect(JSON.parse(call[1].body)).not.toHaveProperty('proxy')
  expect(w.find('.dialog').exists()).toBe(false)
  await w.findAll('button').find(b=>b.text()==='＋ 新建模板')!.trigger('click')
  expect((w.find('.dialog input[placeholder="例如：Reuters 采集"]').element as HTMLInputElement).value).toBe('')
  expect(w.find('.dialog input[type=password]').attributes('disabled')).toBeUndefined()
})

it('keeps a failed template save open with an actionable error',async()=>{
  const original=request.getMockImplementation()! as (url:string,options?:any)=>Promise<any>
  request.mockImplementation(async(url:string,options?:any)=>url.includes('/config/templates')&&options?.method==='POST'?{ok:false,status:400,json:async()=>({detail:'代理格式无效'})}:original(url,options))
  const w=await open()
  await w.findAll('button').find(b=>b.text().includes('插件与代理'))!.trigger('click');await flushPromises()
  await w.findAll('button').find(b=>b.text()==='＋ 新建模板')!.trigger('click')
  await w.find('.dialog input[placeholder="例如：Reuters 采集"]').setValue('Probe')
  await w.find('.dialog form').trigger('submit');await flushPromises()
  expect(w.find('.dialog [role=alert]').text()).toBe('代理格式无效')
  expect((w.find('.dialog input[placeholder="例如：Reuters 采集"]').element as HTMLInputElement).value).toBe('Probe')
})

it('discards a stale plugin inventory when the target environment changes',async()=>{
  const original=request.getMockImplementation()! as (url:string,options?:any)=>Promise<any>
  let finish:(value:any)=>void=()=>{}
  request.mockImplementation(async(url:string,options?:any)=>{
    const path=new URL(url).pathname
    if(path.endsWith('/hosts'))return response([{name:'local',deployments:[{name:'manager',spec:{}}]}])
    if(path.endsWith('/extensions'))return new Promise(resolve=>{finish=resolve})
    return original(url,options)
  })
  const w=await open()
  await w.findAll('button').find(b=>b.text().includes('插件与代理'))!.trigger('click');await flushPromises()
  await w.findAll('button').find(b=>b.text()==='Manager 插件管理')!.trigger('click')
  await w.find('.plugin-environment select').setValue('local/manager');await flushPromises()
  expect(w.find('.plugin-environment select').attributes('disabled')).toBeDefined()
  w.findComponent(ConfigPanel).vm.$emit('select-environment','native')
  finish(response([{dirname:'stale',name:'Wrong environment',container_path:'/data/stale'}]));await flushPromises()
  expect(w.text()).not.toContain('Wrong environment')
  expect(w.find('.plugin-management-grid').exists()).toBe(false)
})

it('installs and applies plugins to the selected Manager with the explicit selection',async()=>{
  const original=request.getMockImplementation()! as (url:string,options?:any)=>Promise<any>
  request.mockImplementation(async(url:string,options?:any)=>{
    const path=new URL(url).pathname
    if(path.endsWith('/hosts'))return response([{name:'local',deployments:[{name:'manager',spec:{}}]}])
    if(path.endsWith('/extensions'))return response([{dirname:'keep',name:'Keep',container_path:'/data/keep'},{dirname:'other',name:'Other',container_path:'/data/other'}])
    if(path.endsWith('/extensions/push')||path.endsWith('/extensions/apply'))return response({job:'plugin-job'})
    if(path.endsWith('/jobs/plugin-job'))return response({id:'plugin-job',status:'success',lines:[]})
    return original(url,options)
  })
  const w=await open()
  await w.findAll('button').find(b=>b.text().includes('插件与代理'))!.trigger('click');await flushPromises()
  await w.findAll('button').find(b=>b.text()==='Manager 插件管理')!.trigger('click')
  await w.find('.plugin-environment select').setValue('local/manager');await flushPromises()
  await w.find('.plugin-install input').setValue('D:/fixture/plugin')
  await w.find('.plugin-install form').trigger('submit');await flushPromises()
  const install=request.mock.calls.find(c=>String(c[0]).includes('/extensions/push'))!
  expect(new URL(String(install[0])).searchParams.get('deployment')).toBe('manager')
  expect(JSON.parse(install[1].body)).toEqual({path:'D:/fixture/plugin'})
  expect((w.find('.plugin-install input').element as HTMLInputElement).value).toBe('')
  await w.findAll('button').find(b=>b.text()==='选择插件并下发')!.trigger('click')
  expect(w.find('.dialog').text()).toContain('全部实例')
  await w.find('.dialog input[value="other"]').setValue(false)
  await w.find('.dialog form').trigger('submit');await flushPromises()
  const apply=request.mock.calls.find(c=>String(c[0]).includes('/extensions/apply'))!
  expect(JSON.parse(apply[1].body)).toEqual({only:['keep'],restart:true})
})
