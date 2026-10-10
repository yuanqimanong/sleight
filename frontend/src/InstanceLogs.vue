<script setup lang="ts">
import {computed, reactive, ref, watch} from 'vue'

type Entry={time:number,action:string,message:string}
const props=defineProps<{name:string,entries:Entry[],busy:boolean,error?:string}>()
const emit=defineEmits<{refresh:[]}>()
const page=ref(1),pageSize=ref(20)
const filters=reactive({keyword:'',action:'',from:'',to:''})
const actions=computed(()=>[...new Set([filters.action,...props.entries.map(e=>e.action)])].filter(Boolean).sort())
const filtering=computed(()=>!!(filters.keyword.trim()||filters.action||filters.from||filters.to))
const invalidRange=computed(()=>!!(filters.from&&filters.to&&new Date(filters.from)>new Date(filters.to)))
const filtered=computed(()=>{
  if(invalidRange.value)return []
  const keyword=filters.keyword.trim().toLocaleLowerCase()
  const from=filters.from?new Date(filters.from).getTime()/1000:-Infinity
  const to=filters.to?new Date(filters.to).getTime()/1000+60:Infinity
  return props.entries.filter(e=>(!keyword||`${e.action}\n${e.message}`.toLocaleLowerCase().includes(keyword))&&(!filters.action||e.action===filters.action)&&e.time>=from&&e.time<to)
})
const total=computed(()=>filtered.value.length)
const pageCount=computed(()=>Math.max(1,Math.ceil(total.value/pageSize.value)))
const start=computed(()=>(page.value-1)*pageSize.value)
const visible=computed(()=>[...filtered.value].reverse().slice(start.value,start.value+pageSize.value))
watch(pageSize,()=>{page.value=1})
watch(()=>[filters.keyword,filters.action,filters.from,filters.to],()=>{page.value=1})
watch(pageCount,count=>{page.value=Math.min(page.value,count)})
function clearFilters(){Object.assign(filters,{keyword:'',action:'',from:'',to:''})}
</script>

<template>
  <div class="instance-log-heading">
    <h2>{{name}} · 实例日志</h2>
  </div>
  <div class="instance-log-toolbar">
    <span>{{filtering?`找到 ${total} 条 / 共 ${entries.length} 条`:`共 ${total} 条`}} · 最新在前</span>
    <div class="instance-log-controls">
      <label>每页
        <select v-model.number="pageSize" aria-label="每页日志条数">
          <option :value="20">20 条</option>
          <option :value="50">50 条</option>
          <option :value="200">200 条</option>
        </select>
      </label>
      <button :disabled="busy" @click="emit('refresh')">↻ {{busy?'刷新中…':'刷新'}}</button>
    </div>
  </div>
  <div class="instance-log-filters" role="search" aria-label="日志筛选">
    <label class="instance-log-search">关键词<input v-model="filters.keyword" type="search" aria-label="日志关键词" placeholder="搜索操作或日志内容"></label>
    <label>操作类型<select v-model="filters.action" aria-label="日志操作类型"><option value="">全部操作</option><option v-for="action in actions" :key="action" :value="action">{{action}}</option></select></label>
    <label>开始时间<input v-model="filters.from" type="datetime-local" aria-label="日志开始时间" :max="filters.to||undefined"></label>
    <label>结束时间<input v-model="filters.to" type="datetime-local" aria-label="日志结束时间" :min="filters.from||undefined"></label>
    <button :disabled="!filtering" @click="clearFilters">清除筛选</button>
  </div>
  <p v-if="invalidRange" class="alert danger" role="alert">开始时间不能晚于结束时间。</p>
  <p v-if="error" class="alert danger" role="alert">{{error}}</p>
  <div class="instance-log-table-wrap">
    <table class="instance-log-table" aria-label="实例操作日志">
      <thead><tr><th scope="col">时间</th><th scope="col">操作</th><th scope="col">日志内容</th></tr></thead>
      <tbody>
        <tr v-for="(entry,index) in visible" :key="start+index">
          <td><time :datetime="new Date(entry.time*1000).toISOString()">{{new Date(entry.time*1000).toLocaleString()}}</time></td>
          <td class="mono">{{entry.action}}</td>
          <td class="instance-log-message">{{entry.message}}</td>
        </tr>
        <tr v-if="!total"><td colspan="3" class="instance-log-empty">{{entries.length?'没有符合筛选条件的日志，请调整或清除筛选。':'暂无日志。实例启动、停止及 SDK / MCP 会话状态会记录在这里。'}}</td></tr>
      </tbody>
    </table>
  </div>
  <nav class="instance-log-pagination" aria-label="日志分页">
    <span role="status">{{total?start+1:0}}–{{Math.min(start+pageSize,total)}} / {{total}} 条</span>
    <div>
      <button :disabled="page===1" @click="page--">上一页</button>
      <span>第 {{page}} / {{pageCount}} 页</span>
      <button :disabled="page===pageCount" @click="page++">下一页</button>
    </div>
  </nav>
</template>
