// @vitest-environment jsdom
import {mount,flushPromises} from '@vue/test-utils'
import {afterEach,beforeEach,expect,it,vi} from 'vitest'
import FleetPanel from '../src/FleetPanel.vue'
import {consoleData,response} from './fixtures'
type Row=Record<string,any>
const instance={ref:'profile:p',profile_id:'p',id:'source',name:'Local Chrome',kind:'native',kernel:'Chrome',version:'155',status:'running',ephemeral:false,user_name:'管理员',environment:'native',connection:{profile_id:'p'}}
const pages=[{targetId:'a',title:'A',url:'https://a.example'},{targetId:'b',title:'B',url:'https://b.example'}]
const wrappers:ReturnType<typeof mount>[]=[]
let request:ReturnType<typeof vi.fn>
beforeEach(()=>{
  vi.useFakeTimers()
  request=vi.fn(async(url:string,options?:Row)=>{
    const path=new URL(url).pathname
    if(path.endsWith('/fleet'))return response({instances:[instance],counts:{all:1,running:1,persistent:1,temporary:0},warnings:[]})
    if(path.endsWith('/fleet/inspect'))return response({pages,target:new URL(url).searchParams.get('target')||'a',image:'data:image/jpeg;base64,AA'})
    if(path.endsWith('/fleet/action'))return response({session_id:'session-fixture'})
    return response(consoleData(url)??[])
  });vi.stubGlobal('fetch',request)
})
afterEach(()=>{wrappers.splice(0).forEach(w=>w.unmount());vi.unstubAllGlobals();vi.useRealTimers()})
async function open(){const w=mount(FleetPanel,{props:{identity:{user:{id:'admin',role:'admin'},scopes:['execute','manage']},platform:{os:'Windows'}}});wrappers.push(w);await flushPromises();return w}
it('shows six live statistics and routes threshold settings to the alert tab',async()=>{
  const w=await open();expect(w.findAll('.fleet-stats .stat')).toHaveLength(6)
  expect(w.find('.fleet-stats').text()).toContain('12.0%')
  await w.findAll('button').find(b=>b.text()==='设置阈值 ↗')!.trigger('click')
  expect(w.emitted('navigate')?.[0]).toEqual(['service','alerts'])
})
it('starts a browser without navigating and gives the app its session heartbeat',async()=>{
  const w=await open();await w.findAll('.browser button').find(b=>b.text()==='重启')!.trigger('click');await flushPromises()
  expect(w.emitted('session')?.[0]).toEqual(['session-fixture'])
  expect(w.emitted('navigate')).toBeUndefined()
})
it('opens instance logs as a table and refreshes them from the top button',async()=>{
  let message='实例已启动'
  const original=request.getMockImplementation()! as (url:string,options?:Row)=>Promise<any>
  request.mockImplementation(async(url:string,options?:Row)=>url.includes('/fleet/logs')?response({entries:[{time:1700000000,action:'start',message}]}):original(url,options))
  const w=await open();await w.findAll('.browser button').find(b=>b.text()==='日志')!.trigger('click');await flushPromises()
  expect(w.find('table').text()).toContain('实例已启动')
  expect((w.find('select[aria-label="每页日志条数"]').element as HTMLSelectElement).value).toBe('20')
  message='刷新后的日志'
  await w.find('.instance-log-controls button').trigger('click');await flushPromises()
  expect(w.find('table').text()).toContain('刷新后的日志')
  expect(request.mock.calls.filter(c=>String(c[0]).includes('/fleet/logs'))).toHaveLength(2)
})
it('edits a stopped instance in place and prevents editing a running one',async()=>{
  const original=request.getMockImplementation()! as (url:string,options?:Row)=>Promise<any>
  const running=await open();expect(running.findAll('button').find(b=>b.text()==='编辑实例')!.attributes('disabled')).toBeDefined()
  request.mockImplementation(async(url:string,options?:Row)=>new URL(url).pathname.endsWith('/fleet')?response({instances:[{...instance,status:'stopped'}],counts:{},warnings:[]}):url.includes('/fleet/configuration')?response({ref:instance.ref,name:instance.name,notes:'',kind:'native',environment:'native',ephemeral:false,headless:false,no_sandbox:false,template_id:'',launch_template_id:'',extensions:'manual'}):original(url,options))
  const w=await open();await w.findAll('button').find(b=>b.text()==='编辑实例')!.trigger('click');await flushPromises()
  await w.find('.fleet-dialog textarea').setValue('New note')
  await w.find('.fleet-dialog form').trigger('submit');await flushPromises()
  const call=request.mock.calls.find(c=>String(c[0]).includes('/fleet/edit'))!
  expect(JSON.parse(call[1].body)).toEqual({ref:instance.ref,changes:{notes:'New note'}})
  expect(request.mock.calls.some(c=>String(c[0]).includes('/fleet/create'))).toBe(false)
})
it('keeps the instance preview open when a capture fails and can retry',async()=>{
  const w=await open();await w.findAll('button').find(b=>b.text()==='查看实例')!.trigger('click');await flushPromises()
  const original=request.getMockImplementation()! as (url:string,options?:Row)=>Promise<any>
  request.mockImplementation(async(url:string,options?:Row)=>url.includes('/fleet/inspect')?{ok:false,status:409,json:async()=>({detail:'预览忙'})}:original(url,options))
  await w.findAll('.preview-tabs button')[1].trigger('click');await flushPromises()
  expect(w.find('[role=dialog]').exists()).toBe(true);expect(w.find('.fleet-dialog').text()).toContain('预览忙')
  request.mockImplementation(original)
  await w.findAll('button').find(b=>b.text()==='重试')!.trigger('click');await flushPromises()
  expect(w.find('.fleet-dialog img').exists()).toBe(true)
})
it('ignores older preview responses after switching tabs',async()=>{
  const w=await open();await w.findAll('button').find(b=>b.text()==='查看实例')!.trigger('click');await flushPromises()
  let finish:(value:any)=>void=()=>{}
  const original=request.getMockImplementation()! as (url:string,options?:Row)=>Promise<any>
  request.mockImplementation(async(url:string,options?:Row)=>url.includes('/fleet/inspect')&&new URL(url).searchParams.get('target')==='a'?await new Promise(resolve=>{finish=resolve}):original(url,options))
  await w.findAll('.preview-tabs button')[0].trigger('click')
  await w.findAll('.preview-tabs button')[1].trigger('click');await flushPromises()
  finish(response({pages,target:'a',image:'old-frame'}));await flushPromises()
  expect(w.findAll('.preview-tabs button')[1].attributes('aria-selected')).toBe('true')
  expect(w.find('.fleet-dialog img').attributes('src')).not.toBe('old-frame')
})
it('preserves typed navigation text during automatic refresh and supports new windows',async()=>{
  const w=await open();await w.findAll('button').find(b=>b.text()==='查看实例')!.trigger('click');await flushPromises()
  await w.find('input[aria-label="网址"]').setValue('https://typed.example')
  await vi.advanceTimersByTimeAsync(2500);await flushPromises()
  expect((w.find('input[aria-label="网址"]').element as HTMLInputElement).value).toBe('https://typed.example')
  await w.find('select[aria-label="打开方式"]').setValue('window')
  await w.find('.navigate-form').trigger('submit');await flushPromises()
  const call=request.mock.calls.find(c=>String(c[0]).includes('/fleet/navigate'))!
  expect(JSON.parse(call[1].body)).toMatchObject({url:'https://typed.example',mode:'window'})
})
it('hides seed and extension override for ordinary Chrome, blocks missing fingerprint environments',async()=>{
  const w=await open();await w.findAll('button').find(b=>b.text().includes('新建实例'))!.trigger('click');await flushPromises()
  expect(w.find('.fleet-dialog').text()).not.toContain('指纹种子')
  expect(w.find('.fleet-dialog').text()).toContain('chrome://extensions')
  expect(w.find('textarea[placeholder="每行一个目录，留空使用模板"]').exists()).toBe(false)
  await w.find('.fleet-dialog select').setValue('fingerprint');await flushPromises()
  expect(w.find('.fleet-dialog').text()).toContain('先准备运行环境')
  expect(w.find('input[placeholder="给实例一个容易识别的名字"]').exists()).toBe(false)
  await w.findAll('button').find(b=>b.text()==='去部署环境 →')!.trigger('click')
  expect(w.emitted('navigate')?.[0]).toEqual(['environments'])
})
it('uses actual Manager capabilities and hides unsupported headless controls',async()=>{
  const original=request.getMockImplementation()! as (url:string,options?:Row)=>Promise<any>
  request.mockImplementation(async(url:string,options?:Row)=>new URL(url).pathname.endsWith('/environments')?response([{id:'local/manager',name:'Manager',kind:'cloak',ready:true}]):url.includes('/fleet/capabilities')?response({generation:'modern'}):original(url,options))
  const w=await open();await w.findAll('button').find(b=>b.text().includes('新建实例'))!.trigger('click');await flushPromises()
  await w.find('.fleet-dialog select').setValue('cloak');await flushPromises()
  expect(w.find('.fleet-dialog').text()).toContain('不支持每个实例设置无头模式')
  expect(w.find('.fleet-dialog input[type=checkbox]').exists()).toBe(false)
})
