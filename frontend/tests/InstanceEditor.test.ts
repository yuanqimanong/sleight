// @vitest-environment jsdom
import {mount,flushPromises} from '@vue/test-utils'
import {afterEach,expect,it,vi} from 'vitest'
import InstanceEditor from '../src/InstanceEditor.vue'
const wrappers:ReturnType<typeof mount>[]=[]
const configuration={ref:'profile:p',name:'Chrome',notes:'old',kind:'native',environment:'native',ephemeral:false,headless:false,no_sandbox:false,template_id:'',launch_template_id:'',extensions:'manual',extension_paths:[],fingerprint_seed:null,proxy_display:''}
function open(extra={}){const w=mount(InstanceEditor,{props:{configuration:{...configuration,...extra},templates:[{id:'proxy',name:'Proxy'}],launches:[],admin:true,platform:{os:'Windows'},busy:false,error:''}});wrappers.push(w);return w}
afterEach(()=>{wrappers.splice(0).forEach(w=>w.unmount());vi.unstubAllGlobals();vi.useRealTimers()})
it('submits only changed fields and leaves identity and existing configuration untouched',async()=>{
  vi.useFakeTimers()
  const w=open()
  await w.find('input[placeholder="给实例一个容易识别的名字"]').setValue('Renamed')
  await w.find('textarea').setValue('new note')
  await w.find('form').trigger('submit')
  expect(w.emitted('save')?.[0]).toEqual([{name:'Renamed',notes:'new note'}])
  expect(configuration.name).toBe('Chrome')
})
it('checks duplicate names excluding itself and blocks the save button',async()=>{
  vi.useFakeTimers()
  const fetcher=vi.fn(async(_url:string)=>({ok:true,json:async()=>({available:false})}));vi.stubGlobal('fetch',fetcher)
  const w=open()
  await w.find('input').setValue('Other')
  await vi.advanceTimersByTimeAsync(350);await flushPromises()
  expect(String(fetcher.mock.calls[0][0])).toContain('exclude=profile%3Ap')
  expect(w.find('button.primary').attributes('disabled')).toBeDefined()
})
it('saves explicit template and plugin changes but hides unsupported modern options',async()=>{
  const w=open({kind:'cloak',generation:'modern',extensions:'automatic',extension_paths:['/data/old']})
  expect(w.find('input[type=checkbox]').exists()).toBe(false)
  await w.findAll('select')[1].setValue('proxy')
  await w.find('textarea[placeholder="每行一个目录；清空后移除自动加载的插件"]').setValue('/data/new')
  await w.find('form').trigger('submit')
  expect(w.emitted('save')?.[0]).toEqual([{template_id:'proxy',extension_paths:['/data/new']}])
})
