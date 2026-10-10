import {consoleData,response} from './fixtures'
// @vitest-environment jsdom
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import ServicePanel from '../src/ServicePanel.vue'

afterEach(()=>vi.unstubAllGlobals())
it('edits environment permissions and fixed profiles without mutating the listed user', async()=>{
  const user={id:'fin',name:'fin',role:'operator',active:true,limit:1,default_environment:'native',environments:[],profile_names:[]}
  const request=vi.fn(async(url:string, _options?: any)=>({ok:true,json:async()=>consoleData(url)??(url.endsWith('/environments')?[{id:'native',name:'本机'},{id:'local/manager',name:'Manager'}]:url.endsWith('/users')?[user]:[])}))
  vi.stubGlobal('fetch',request)
  const w=mount(ServicePanel,{props:{identity:{user:{id:'admin',role:'admin'},scopes:['execute','manage','delegate']}}})
  await flushPromises()
  await w.find('.service-menu').findAll('button').find(b=>b.text().includes('用户与权限'))!.trigger('click')
  await flushPromises()
  await w.findAll('button').find(b=>b.text()==='编辑用户')!.trigger('click')
  await w.find('fieldset input[value="local/manager"]').setValue(true)
  await w.find('textarea').setValue('fixed-one\nfixed-two\nfixed-one')
  expect(user.environments).toEqual([])
  await w.find('form').trigger('submit'); await flushPromises()
  const call=request.mock.calls.find(c=>String(c[0]).endsWith('/users')&&c[1]?.method==='POST')!
  expect(JSON.parse(call[1].body)).toMatchObject({environments:['local/manager'],profile_names:['fixed-one','fixed-two']})
  w.unmount()
})
