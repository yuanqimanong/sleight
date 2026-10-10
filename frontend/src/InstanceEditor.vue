<script setup lang="ts">
import {computed,onUnmounted,reactive,ref,watch} from 'vue'
import {api} from './api'
type Row=Record<string,any>
const props=defineProps<{configuration:Row,templates:Row[],launches:Row[],admin:boolean,platform:Row,busy:boolean,error:string}>()
const emit=defineEmits<{save:[changes:Row],cancel:[]}>()
const original=props.configuration
const form=reactive<Row>({...original,paths:(original.extension_paths||[]).join('\n'),fingerprint_seed:typeof original.fingerprint_seed==='number'?original.fingerprint_seed:''})
const kindNames:Record<string,string>={native:'本机原生',fingerprint:'fingerprint-chromium',cloak:'Cloak'}
const nameState=ref('')
const launches=computed(()=>props.launches.filter(t=>t.kind===original.kind))
const modern=computed(()=>original.generation==='modern')
const changes=computed(()=>{
  const result:Row={}
  for(const key of ['name','notes','template_id','headless','no_sandbox']){
    const value=key==='name'?form.name.trim():form[key]
    if(value!==original[key])result[key]=value
  }
  if(form.launch_template_id&&form.launch_template_id!==original.launch_template_id)result.launch_template_id=form.launch_template_id
  if(original.kind!=='native'&&form.fingerprint_seed!==''&&Number(form.fingerprint_seed)!==original.fingerprint_seed)result.fingerprint_seed=Number(form.fingerprint_seed)
  if(props.admin&&original.extensions==='automatic'){
    const paths=form.paths.split(/\r?\n/).map((p:string)=>p.trim()).filter(Boolean)
    if(JSON.stringify(paths)!==JSON.stringify(original.extension_paths||[]))result.extension_paths=paths
  }
  return result
})
let timer:ReturnType<typeof setTimeout>,generation=0
watch(()=>form.name,()=>{
  const id=++generation;clearTimeout(timer);nameState.value=''
  if(!form.name.trim()||form.name.trim()===original.name)return
  timer=setTimeout(async()=>{
    try{const r=await api<Row>(`api/v1/fleet/name?name=${encodeURIComponent(form.name)}&environment=${encodeURIComponent(original.environment)}&exclude=${encodeURIComponent(original.ref)}`);if(id===generation)nameState.value=r.available?'可用':'已存在'}catch{if(id===generation)nameState.value='提交时校验'}
  },350)
})
function selectLaunch(){const t=launches.value.find(t=>t.id===form.launch_template_id);if(t){form.headless=modern.value?false:t.config.headless??form.headless;if(original.kind!=='native'&&typeof t.config.fingerprint_seed==='number')form.fingerprint_seed=t.config.fingerprint_seed;if('no_sandbox' in t.config)form.no_sandbox=t.config.no_sandbox}}
onUnmounted(()=>{++generation;clearTimeout(timer)})
</script>

<template>
  <form @submit.prevent="emit('save',changes)">
    <h2>编辑「{{configuration.name}}」</h2>
    <p class="fine">{{kindNames[configuration.kind]}} · {{configuration.environment==='native'?'本机':configuration.environment}} · {{configuration.ephemeral?'临时实例':'长期实例'}}</p>
    <p class="alert yellow">保存保留实例 ID 和浏览器数据，启动配置在下次启动时生效。</p>
    <label>实例名称<input v-model="form.name" required maxlength="100" placeholder="给实例一个容易识别的名字"></label>
    <small :class="nameState==='已存在'?'danger-text':''">{{nameState==='已存在'?'同一环境中已有此名称':nameState}}</small>
    <template v-if="!configuration.metadata_only">
      <label>创建模板<select v-model="form.launch_template_id" @change="selectLaunch"><option value="">保留当前配置</option><option v-if="configuration.launch_template_id&&!launches.some(t=>t.id===configuration.launch_template_id)" :value="configuration.launch_template_id">原模板已删除 · 保留当前配置</option><option v-for="t in launches" :key="t.id" :value="t.id" :disabled="modern&&t.config.headless===true">{{t.name}}{{modern&&t.config.headless===true?' · 当前环境不支持无头模式':''}}</option></select></label>
      <label v-if="configuration.kind!=='native'">指纹种子<input v-model="form.fingerprint_seed" type="number" min="1" max="2147483647" placeholder="留空保留当前种子"></label>
      <label v-if="!modern" class="check"><input v-model="form.headless" type="checkbox">无头模式（不弹出浏览器窗口）</label>
      <label v-if="configuration.kind!=='cloak'&&platform.os==='Linux'" class="check"><input v-model="form.no_sandbox" type="checkbox">容器 / root 用户使用 no-sandbox</label>
      <label>插件与代理模板<select v-model="form.template_id"><option value="">用户默认配置</option><option v-if="configuration.template_id&&!templates.some(t=>t.id===configuration.template_id)" :value="configuration.template_id">原模板已删除 · 保留当前配置</option><option v-for="t in templates" :key="t.id" :value="t.id">{{t.name}}</option></select></label>
      <p class="fine">当前代理：{{configuration.proxy_display||'未配置'}}。选择其他模板会更新插件和代理。</p>
      <label v-if="admin&&configuration.extensions==='automatic'">插件目录覆盖<textarea v-model="form.paths" rows="2" placeholder="每行一个目录；清空后移除自动加载的插件"></textarea></label>
      <p v-if="configuration.kind==='native'&&configuration.extensions!=='automatic'" class="fine">此内核自动加载插件受限，可在实例窗口中手动安装。</p>
    </template>
    <p v-else class="fine">共享固定实例可修改自己的名称和备注；浏览器配置由管理员管理。</p>
    <label>备注<textarea v-model="form.notes" rows="2" maxlength="1000" placeholder="用途、登录账号说明；请勿填写密码"></textarea></label>
    <p v-if="error" class="alert danger" role="alert">{{error}}</p>
    <div class="dialog-actions"><button type="button" @click="emit('cancel')">取消</button><button class="primary" :disabled="busy||!form.name.trim()||nameState==='已存在'||!Object.keys(changes).length">{{busy?'保存中…':'保存修改'}}</button></div>
  </form>
</template>
