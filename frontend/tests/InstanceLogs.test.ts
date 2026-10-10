// @vitest-environment jsdom
import {mount} from '@vue/test-utils'
import {afterEach,expect,it} from 'vitest'
import InstanceLogs from '../src/InstanceLogs.vue'

const wrappers:ReturnType<typeof mount>[]=[]
const entries=Array.from({length:200},(_,i)=>({time:1700000000+i,action:'session',message:`日志 ${i+1}`}))
function open(data=entries){const w=mount(InstanceLogs,{props:{name:'Chrome',entries:data,busy:false}});wrappers.push(w);return w}
afterEach(()=>wrappers.splice(0).forEach(w=>w.unmount()))

it('paginates newest first with 20, 50 or 200 rows and resets the page after changing size',async()=>{
  const w=open(),next=w.findAll('button').find(b=>b.text()==='下一页')!
  expect(w.findAll('tbody tr')).toHaveLength(20)
  expect(w.find('tbody tr').text()).toContain('日志 200')
  expect(w.find('[role=status]').text()).toBe('1–20 / 200 条')
  await next.trigger('click')
  expect(w.find('tbody tr').text()).toContain('日志 180')
  expect(w.find('[role=status]').text()).toBe('21–40 / 200 条')
  await w.find('select[aria-label="每页日志条数"]').setValue('50')
  expect(w.findAll('tbody tr')).toHaveLength(50)
  expect(w.find('[role=status]').text()).toBe('1–50 / 200 条')
  for(let i=0;i<3;i++)await next.trigger('click')
  expect(w.find('tbody tr').text()).toContain('日志 50')
  expect(next.attributes('disabled')).toBeDefined()
  await w.find('select[aria-label="每页日志条数"]').setValue('200')
  expect(w.findAll('tbody tr')).toHaveLength(200)
  expect(w.findAll('button').filter(b=>b.text()==='上一页'||b.text()==='下一页').every(b=>b.attributes('disabled')!==undefined)).toBe(true)
  expect(entries[0].message).toBe('日志 1')
})

it('keeps a valid page after refresh and disables the top refresh button while loading',async()=>{
  const w=open(),next=w.findAll('button').find(b=>b.text()==='下一页')!
  await next.trigger('click')
  const refresh=w.find('.instance-log-controls button')
  await refresh.trigger('click')
  expect(w.emitted('refresh')).toHaveLength(1)
  await w.setProps({entries:[...entries,{time:1700000200,action:'start',message:'新日志'}],busy:true})
  expect(w.find('[role=status]').text()).toBe('21–40 / 201 条')
  expect(refresh.attributes('disabled')).toBeDefined()
  await w.setProps({entries:entries.slice(0,7),busy:false,error:'刷新失败，请重试'})
  expect(w.findAll('tbody tr')).toHaveLength(7)
  expect(w.find('[role=status]').text()).toBe('1–7 / 7 条')
  expect(w.find('[role=alert]').text()).toContain('刷新失败')
  expect(refresh.attributes('disabled')).toBeUndefined()
})

it('handles an empty log and renders message content as text',async()=>{
  const w=open([])
  expect(w.find('tbody').text()).toContain('暂无日志')
  expect(w.find('[role=status]').text()).toBe('0–0 / 0 条')
  await w.setProps({entries:[{time:1700000000,action:'error',message:'<script>bad()</script>\n错误详情'}]})
  expect(w.find('.instance-log-message').text()).toContain('<script>bad()</script>')
  expect(w.find('script').exists()).toBe(false)
})

it('searches every page and combines action and local-time filters before paginating',async()=>{
  const data=Array.from({length:60},(_,i)=>({time:new Date(`2026-10-09T10:${String(i).padStart(2,'0')}:30`).getTime()/1000,action:i%2?'stop':'start',message:`记录 ${i} ${i===0?'SPECIAL':''}`}))
  const w=open(data)
  await w.findAll('button').find(b=>b.text()==='下一页')!.trigger('click')
  await w.find('input[aria-label="日志关键词"]').setValue('special')
  expect(w.findAll('tbody tr')).toHaveLength(1)
  expect(w.find('tbody tr').text()).toContain('SPECIAL')
  expect(w.find('[role=status]').text()).toBe('1–1 / 1 条')
  await w.find('input[aria-label="日志关键词"]').setValue('')
  await w.find('select[aria-label="日志操作类型"]').setValue('start')
  await w.find('input[aria-label="日志开始时间"]').setValue('2026-10-09T10:10')
  await w.find('input[aria-label="日志结束时间"]').setValue('2026-10-09T10:20')
  expect(w.findAll('tbody tr')).toHaveLength(6)
  expect(w.find('tbody tr').text()).toContain('记录 20')
  expect(w.find('[role=status]').text()).toBe('1–6 / 6 条')
  await w.find('input[aria-label="日志开始时间"]').setValue('2026-10-09T11:00')
  expect(w.find('[role=alert]').text()).toContain('开始时间不能晚于结束时间')
  await w.findAll('button').find(b=>b.text()==='清除筛选')!.trigger('click')
  expect(w.findAll('tbody tr')).toHaveLength(20)
  expect(w.find('[role=status]').text()).toBe('1–20 / 60 条')
  expect(w.find('[role=alert]').exists()).toBe(false)
})
