<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import ServicePanel from './ServicePanel.vue'
import FleetPanel from './FleetPanel.vue'
import ConfigPanel from './ConfigPanel.vue'
import { api, APIError, endpoint, stateLabel } from './api'

type Row = Record<string, any>
const page = ref('instances'), logged = ref(false), password = ref(''), error = ref(''), notice = ref(''), busy = ref(false)
const defaults = ref<Row>({}), platform = ref<Row>({}), hosts = ref<Row[]>([])
const templates = ref<Row[]>([]), jobs = ref<Row[]>([]), selectedJob = ref<Row | null>(null)
const selected = ref('native'), modal = ref('')
const activeSSE = ref<EventSource | null>(null), importMode = ref(false)
const form = reactive<Row>({}), newTemplate = reactive<Row>({ name: '', proxy: '', paths: '' })
const dark = ref(false)
const identity = ref<Row>({})
const administrator = computed(()=>identity.value.user?.role==='admin' && identity.value.scopes?.includes('manage'))
const navigation = computed(()=>[{id:'instances',name:'浏览器实例',icon:'◉'},{id:'service',name:'用户与运行',icon:'◇'},...(administrator.value?[{id:'environments',name:'部署环境',icon:'▣'},{id:'config',name:'插件与代理',icon:'⚒'}]:[])])
const windowIsAgent = window.location.pathname.includes('/agent/')
const agents = ref<Row[]>([]), agentPanel = ref<Row | null>(null), installedPlugins = ref<Row[]>([])
const pluginPath = ref(''), pluginNames = ref<string[]>([]), templateEditing = ref(''), preserveProxy = ref(false)
const checkedForm = ref(''), registeredRef = ref('')
const serviceTab=ref('sessions'), imageOptions=ref<Row[]>([]), imageWarning=ref(''), dockerCheck=ref<Row | null>(null)
const owned=new Set<string>()
let heartbeatTimer:ReturnType<typeof setInterval>
function navigate(next:string,tab='sessions'){serviceTab.value=tab;page.value=next}
function session(id:string){owned.add(id)}
async function heartbeats(){for(const id of owned){try{await api(`api/v1/sessions/${id}/heartbeat`,'POST',{})}catch(e){if(e instanceof APIError&&[401,403,404,410].includes(e.status))owned.delete(id)}}}
function theme() { dark.value = !dark.value; document.documentElement.dataset.theme = dark.value ? 'dark' : 'light'; try { localStorage.setItem('sleight_theme', dark.value ? 'dark' : 'light') } catch {} }
const target = computed(() => { const [host, name] = selected.value.split('/'); return { host, name } })
const environments = computed(() => hosts.value.flatMap(h => (h.deployments || []).map((d: Row) => ({...d, host: h.name, key: `${h.name}/${d.name}`}))))
const hostPath = (suffix: string) => `api/hosts/${encodeURIComponent(target.value.host)}/${suffix}?deployment=${encodeURIComponent(target.value.name)}`
let poll: ReturnType<typeof setInterval> | undefined

async function action(work: () => Promise<unknown>) {
  busy.value = true; error.value = ''; notice.value = ''
  try { await work() } catch (e) { error.value = e instanceof Error ? e.message : String(e); if (e instanceof APIError && e.status === 401) logged.value = false }
  finally { busy.value = false }
}
async function refresh() {
  if (!administrator.value) return
  const results=await Promise.all([api<Row[]>('api/hosts'),api<Row[]>('api/config/templates'),api<Row[]>('api/jobs')])
  hosts.value=results[0];templates.value=results[1];jobs.value=results[2]
  if(!windowIsAgent)agents.value=await api('api/agents')
  platform.value=await api('api/runtime/platform')
  if (selectedJob.value) selectedJob.value = jobs.value.find(j => j.id === selectedJob.value!.id) || selectedJob.value
}
async function boot() {
  try {
    identity.value = await api('api/auth/me'); logged.value = true
    if (administrator.value) { defaults.value = await api('api/defaults'); platform.value = await api('api/runtime/platform') } else { platform.value=identity.value.platform||{} }
    await refresh()
  } catch(e) { if (!(e instanceof APIError && e.status === 401)) error.value = String(e) }
}
async function login() { await action(async () => { await api('api/auth/login', 'POST', {token: password.value}); password.value = ''; await boot() }) }
async function showJob(id: string) {
  activeSSE.value?.close(); selectedJob.value = await api(`api/jobs/${id}`)
  if (selectedJob.value?.status !== 'running') return
  let cursor = (selectedJob.value?.log_offset || 0) + (selectedJob.value?.lines.length || 0)
  const source = new EventSource(endpoint(`api/jobs/${id}/events?after=${cursor}`)); activeSSE.value = source
  source.addEventListener('line', event => { const line = JSON.parse((event as MessageEvent).data); if(selectedJob.value && line.cursor > cursor) { selectedJob.value.lines.push(line.text); cursor = line.cursor } })
  source.addEventListener('done', event => { selectedJob.value = JSON.parse((event as MessageEvent).data); source.close(); void refresh().then(()=>{if(page.value==='config')return loadPlugins()}).catch(() => {}) })
  source.onerror = () => { void refresh().catch(() => {}) } // EventSource resumes with Last-Event-ID.
}
async function runJob(path: string, body: Row = {}) { const r = await api<Row>(path, 'POST', body); await showJob(r.job);if(page.value==='config'&&selectedJob.value?.status!=='running')await loadPlugins() }
function deployWizard() {
  Object.keys(form).forEach(k => delete form[k]); Object.assign(form, { ...defaults.value.spec, host_name: 'local', ssh: '', deployment_name: 'default', dir: platform.value.deployments_dir + '/manager', service: 'manager', filename: 'docker-compose.yaml' });
  form.deployment_name = 'manager-' + Date.now().toString(36); form.name = form.deployment_name; form.container_name = form.deployment_name; form.resource_key = form.deployment_name
  form.dir = platform.value.deployments_dir + '/' + form.deployment_name
  form.data_volume = platform.value.os==='Windows' ? form.deployment_name+'-store' : ''
  dockerCheck.value=null;void loadImages();checks.value = null; checkedForm.value = ''; registeredRef.value = ''; importMode.value = false; modal.value = 'deploy'
}
const checks = ref<Row | null>(null)
async function loadImages(refresh=false){try{const r=await api<Row>('api/v1/manager-images?refresh='+refresh);imageOptions.value=r.images;imageWarning.value=r.warning||'';const chosen=r.images.find((i:Row)=>i.image===form.image);if(modal.value==='deploy'&&!checks.value&&chosen?.digest)form.image=chosen.image+'@'+chosen.digest}catch{imageWarning.value='版本列表不可用，可手动填写固定镜像版本'}}
function selectImage(event:Event){const image=(event.target as HTMLSelectElement).value,r=imageOptions.value.find(i=>i.image===image);form.image=image+(r?.digest?'@'+r.digest:'')}
async function checkDocker(){await action(async()=>{dockerCheck.value=await api('api/runtime/docker');notice.value=dockerCheck.value?.message||''})}
async function registerAndCheck() { await action(async () => {
  await api<Row>('api/hosts', 'POST', {name: form.host_name, ssh: form.ssh, sudo:form.sudo,port:form.ssh_port,identity:form.identity,host_only: true})
  const ref = `${form.host_name}/${form.deployment_name}`
  if (registeredRef.value !== ref) { await api('api/deployments', 'POST', {host: form.host_name, name: form.deployment_name, spec: form}); registeredRef.value = ref }
  selected.value = ref; await nextTick(); await refresh()
  checks.value = await api(hostPath('preflight'), 'POST', {spec: form}); checkedForm.value = JSON.stringify(form)
  notice.value = '检查完成，确认后可部署'
}) }
async function deploy() { await action(async () => { await runJob(hostPath('deploy'), {spec: form}); modal.value = '' }) }
async function importDeployment() { await action(async () => {
  await api('api/hosts', 'POST', {name: form.host_name, ssh: form.ssh,sudo:form.sudo,port:form.ssh_port,identity:form.identity,host_only: true});
  await api(`api/hosts/${form.host_name}/import`, 'POST', {dir: form.dir, name: form.deployment_name, filename: form.filename, service: form.service});
  modal.value = ''; selected.value = `${form.host_name}/${form.deployment_name}`; await refresh(); page.value = 'environments'; notice.value = '已导入现有部署，网络、挂载和环境变量会被保留'
}) }
async function deployOperation(verb: string) {
  await action(async () => {
    const body = verb === 'upgrade' ? { image: form.image || defaults.value.default_image, backup: true } : {}
    await runJob(hostPath(verb), body); modal.value = ''
  })
}
async function saveTemplate() { await action(async () => {
  const body: Row = {...newTemplate, extension_paths: newTemplate.paths.split('\n').map((p: string) => p.trim()).filter(Boolean)}
  if (templateEditing.value) body.id = templateEditing.value
  if (preserveProxy.value) delete body.proxy
  await api('api/config/templates', 'POST', body);
  templateEditing.value = ''; preserveProxy.value = false; Object.assign(newTemplate, {name: '', proxy: '', paths: ''}); modal.value=''; await refresh(); notice.value = '插件和代理模板已保存'
}) }
function createTemplate(){templateEditing.value='';preserveProxy.value=false;Object.assign(newTemplate,{name:'',proxy:'',paths:''});error.value='';modal.value='template-edit'}
function editTemplate(t: Row) { templateEditing.value = t.id; preserveProxy.value = true; Object.assign(newTemplate,{name:t.name,proxy:'',paths:t.extension_paths.join('\n')});error.value='';modal.value='template-edit' }
async function loadPlugins() { const environment=selected.value;if(environment==='native'){installedPlugins.value=[];return};const plugins=await api<Row[]>(hostPath('extensions'));if(selected.value===environment)installedPlugins.value=plugins }
async function pushPlugin() { await action(async()=> { await runJob(hostPath('extensions/push'),{path:pluginPath.value}); pluginPath.value='' }) }
async function addAgent() { await action(async()=> { await api('api/agents','POST',{name:form.name,url:form.url,token:form.token}); form.token='';modal.value='';await refresh();notice.value='执行端已连接' }) }
watch(selected, () => { checks.value = null;installedPlugins.value=[];pluginNames.value=[];void action(async()=>{await refresh();if(page.value==='config')await loadPlugins()}) })
watch(form, () => { if(JSON.stringify(form)!==checkedForm.value) checks.value=null }, {deep:true})
watch(()=>form.host_name, name=> { const host=hosts.value.find(h=>h.name===name); if(host) Object.assign(form,{ssh:host.ssh,sudo:host.sudo,ssh_port:host.port,identity:host.identity}) })
watch(()=>form.ssh, value=>{if(modal.value==='deploy'&&!importMode.value)form.dir=value?'/srv/sleight/'+form.deployment_name:platform.value.deployments_dir+'/'+form.deployment_name})
watch(page, () => {error.value='';notice.value='';if(page.value==='config') void action(loadPlugins) })
function dialogKeys(e: KeyboardEvent) {
  if(e.key==='Escape') { modal.value=''; agentPanel.value=null }
  if(e.key==='Tab' && modal.value) {
    const nodes = [...document.querySelectorAll<HTMLElement>('.dialog input:not(:disabled),.dialog select:not(:disabled),.dialog textarea:not(:disabled),.dialog button:not(:disabled)')].filter(n=>n.offsetParent!==null)
    const first = nodes[0], last = nodes[nodes.length-1]
    if(e.shiftKey && document.activeElement===first) { e.preventDefault();last?.focus() }
    else if(!e.shiftKey && document.activeElement===last) { e.preventDefault();first?.focus() }
  }
}
watch(modal, async value => { if(value) { await nextTick(); ((document.querySelector('.dialog input:not([type=checkbox]),.dialog textarea,.dialog select') || document.querySelector('.dialog button')) as HTMLElement)?.focus() } })
onMounted(async () => { try { dark.value = localStorage.getItem('sleight_theme') === 'dark' } catch {} document.documentElement.dataset.theme = dark.value ? 'dark' : 'light'; await boot(); poll = setInterval(() => { if(logged.value && !busy.value && !document.hidden) void refresh().catch(() => {}) }, 10000) })
onMounted(()=>{document.addEventListener('keydown',dialogKeys);heartbeatTimer=setInterval(()=>void heartbeats(),20000)})
onUnmounted(() => { document.removeEventListener('keydown',dialogKeys); clearInterval(poll);clearInterval(heartbeatTimer); activeSSE.value?.close() })
</script>

<template>
  <div class="console">
    <header class="top"><a class="wordmark" href="./">SLEIGHT<span>浏览器控制台</span></a><span class="version">v{{ defaults.version || '0.6' }}</span><span class="top-note">{{ platform.os || 'WINDOWS · LINUX · macOS' }} <span>{{ platform.arch }}</span></span><button class="text-button" @click="theme" :aria-label="dark?'切换浅色':'切换深色'">{{dark?'☀':'◐'}}</button></header>
    <main v-if="!logged" class="login-wrap"><form class="panel login" @submit.prevent="login"><div class="eyebrow">CONTROL YOUR BROWSERS</div><h1>打开你的<br>浏览器工作台。</h1><p>部署、监控、配置。一个地方就够了。</p><label>访问口令<input v-model="password" type="password" autocomplete="current-password" required placeholder="输入 Sleight 管理口令"></label><p class="alert danger" v-if="error" role="alert">{{error}}</p><button class="primary" :disabled="busy">{{busy ? '正在连接…' : '进入控制台 →'}}</button></form></main>
    <div v-else class="shell">
      <aside><div class="eyebrow">WORKSPACE</div><button v-for="item in navigation" :key="item.id" class="nav" :class="{active:page===item.id}" @click="page=item.id"><span>{{item.icon}}</span>{{item.name}}</button><div class="rail-note">先配置环境，再启动实例。<br>闲置时停止，保留登录态。</div></aside>
      <main class="workspace" :aria-busy="busy">
        <ServicePanel v-if="page==='service'" :identity="identity" :initial-tab="serviceTab" @navigate="navigate"/>
        <div class="alert danger" v-if="error" role="alert">{{error}}<button class="text-button" @click="error=''" aria-label="关闭错误">×</button></div>
        <div class="alert success" v-if="notice" role="status">{{notice}}</div>
        <FleetPanel v-if="page==='instances'" :identity="identity" :platform="platform" @navigate="navigate" @session="session"/>
        <template v-if="page==='environments'">
          <div class="page-heading"><div><div class="eyebrow">READY TO RUN</div><h1>部署环境<span class="dot">.</span></h1><p>原生浏览器随系统运行，Cloak Manager 在 Linux Docker 中运行。</p></div></div>
          <div class="environment-grid"><article class="panel environment"><div class="card-head"><h2>fingerprint-chromium</h2><span class="badge">本机 · {{platform.os}}</span></div><p>下载内核后即可新建实例。支持指纹种子和自动加载插件。</p><button class="primary" @click="action(()=>runJob('api/runtime/install',{name:'fingerprint-chromium'}))" :disabled="busy||!platform.fingerprint_supported">下载并安装</button><p class="fine">{{platform.fingerprint_supported?'当前架构有预编译包。':'当前架构没有预编译包。'}} 已安装的 Chrome / Edge 可直接用于本机原生实例。</p></article><article class="panel environment"><div class="card-head"><h2>Cloak Manager</h2><span class="badge">Linux Docker</span></div><p>使用 Linux Docker 或 Windows Docker Desktop。统一部署、限制资源，并管理浏览器实例。</p><button class="primary" @click="deployWizard">＋ 部署 / 导入 Manager</button><p class="fine">默认固定经过测试的版本；版本列表从官方 Docker Hub 获取。升级不会自动执行。</p></article></div>
          <article class="panel environment" v-if="!windowIsAgent"><div class="card-head"><h2>其他系统的执行端</h2><button @click="Object.assign(form,{name:'',url:'',token:''});modal='agent'">＋ 连接执行端</button></div><p>在目标 Windows、Linux 或 macOS 上运行 Sleight UI，再接入这里。浏览器安装、插件路径与代理都在目标系统执行。</p><div class="card-actions" v-for="a in agents" :key="a.id"><span>{{a.name}} · {{a.os}} / {{a.arch}}</span><button @click="agentPanel=a">打开控制台 ↗</button><button @click="Object.assign(form,{row:a});modal='agent-delete'">移除连接</button></div></article>
          <article class="panel environment" v-for="env in environments" :key="env.key"><div class="card-head"><h2>{{env.host}} / {{env.name}}</h2><span class="badge">CLOAK MANAGER</span></div><dl><div><dt>镜像</dt><dd class="mono">{{env.spec.image}}</dd></div><div><dt>资源限制</dt><dd>{{env.spec.mem_limit}} 内存 · {{env.spec.cpus}} CPU · {{env.spec.max_running}} 并发</dd></div><div><dt>部署目录</dt><dd class="mono">{{env.spec.dir}}</dd></div></dl><div class="card-actions"><button class="primary" @click="selected=env.key;page='instances'">管理实例 →</button><button @click="selected=env.key;form.image=defaults.default_image;modal='upgrade'">升级</button><button @click="selected=env.key;deployOperation('backup')" :disabled="busy">备份数据</button><button @click="selected=env.key;modal='rollback'">回滚</button></div></article>
        </template>
        <ConfigPanel v-if="page==='config'" :templates="templates" :environments="environments" :selected="selected" :plugins="installedPlugins" :plugin-path="pluginPath" :busy="busy" @create-template="createTemplate" @edit-template="editTemplate" @delete-template="t=>{Object.assign(form,{row:t});modal='template-delete'}" @select-environment="id=>selected=id" @update:plugin-path="path=>pluginPath=path" @install="pushPlugin" @refresh="action(loadPlugins)" @apply="pluginNames=installedPlugins.map(p=>p.dirname);modal='plugins-apply'" @verify="action(()=>runJob(hostPath('extensions/verify'),{launch:false}))" @deploy="navigate('environments')" />
        <details v-if="jobs.length&&['environments','config'].includes(page)" class="panel environment"><summary>最近部署与插件操作</summary><div class="card-actions"><button v-for="j in jobs.slice(0,8)" :key="j.id" @click="showJob(j.id)">{{j.kind}} · {{stateLabel[j.status]||j.status}}</button></div></details>
        <section v-if="selectedJob&&['environments','config'].includes(page)" class="panel environment"><div class="card-head"><h2>{{page==='config'?'插件操作日志':'部署日志'}} · {{selectedJob.kind}}</h2><span class="badge">{{stateLabel[selectedJob.status]||selectedJob.status}}</span></div><pre>{{selectedJob.lines?.join('\n')||'正在准备…'}}</pre><p v-if="selectedJob.error" class="alert danger">{{selectedJob.error}}</p><details v-if="selectedJob.result"><summary>操作结果</summary><pre>{{JSON.stringify(selectedJob.result,null,2)}}</pre></details><div class="card-actions"><button @click="showJob(selectedJob.id)">刷新日志</button><button @click="selectedJob=null">收起</button></div></section>
      </main>
    </div>
    <div v-if="modal" class="overlay" @click.self="modal=''" @keydown.esc="modal=''">
      <section class="panel dialog" role="dialog" aria-modal="true" :aria-label="modal==='template-edit'?(templateEditing?'编辑配置模板':'新建配置模板'):modal==='profile'?'新建浏览器实例':'部署操作'">
        <button class="close" @click="modal=''" aria-label="关闭弹窗">×</button>
        <form v-if="modal==='template-edit'" @submit.prevent="saveTemplate"><div class="eyebrow">CONFIGURATION TEMPLATE</div><h2>{{templateEditing?'编辑配置模板':'新建配置模板'}}</h2><label>模板名称<input v-model="newTemplate.name" required maxlength="100" placeholder="例如：Reuters 采集"></label><fieldset class="template-fieldset"><legend>浏览器代理</legend><label v-if="templateEditing" class="check"><input v-model="preserveProxy" type="checkbox">保留现有代理凭据</label><label>代理地址<input :disabled="preserveProxy" v-model="newTemplate.proxy" type="password" autocomplete="off" placeholder="http://host:port 或 socks5://user:pass@host:port"></label><p class="fine">留空使用直连。代理在浏览器运行的机器上使用；原生认证代理需经本机转发。</p></fieldset><fieldset class="template-fieldset"><legend>自动加载插件</legend><label>插件目录<textarea v-model="newTemplate.paths" rows="3" placeholder="每行一个目录；本机填本机路径，Manager 填 /data/extensions/插件目录"></textarea></label><p class="fine">目录中需包含 manifest.json。Chrome / Edge 自动加载受限，可在实例窗口手动安装。</p></fieldset><p v-if="error" class="alert danger" role="alert">{{error}}</p><div class="dialog-actions"><button type="button" @click="modal=''">取消</button><button class="primary" :disabled="busy">保存模板</button></div></form>
        <form v-if="modal==='deploy'" @submit.prevent="importMode?importDeployment():registerAndCheck()">
          <div class="eyebrow">DEPLOY YOUR ENVIRONMENT</div><h2>部署 Cloak Manager</h2>
          <div class="segmented"><button type="button" :class="{primary:!importMode}" @click="importMode=false;checks=null">新建部署</button><button type="button" :class="{primary:importMode}" @click="importMode=true;checks=null">导入现有 Compose</button></div>
          <template v-if="!importMode"><div class="deploy-step"><b>1 · 检测 Docker</b><p>本机使用 Linux containers。远端 Linux 可在高级选项填写 SSH，最后一起检查。</p><button type="button" @click="checkDocker" :disabled="busy">检测本机 Docker</button><p v-if="dockerCheck" :class="dockerCheck.ok?'':'danger-text'">{{dockerCheck.message}}</p></div><div class="deploy-step"><b>2 · 选择版本与资源</b><label>镜像版本<select :value="form.image?.split('@')[0]" @change="selectImage"><option v-if="!imageOptions.some(i=>i.image===form.image?.split('@')[0])" :value="form.image?.split('@')[0]">{{form.image?.split('@')[0]}}</option><option v-for="i in imageOptions" :key="i.image" :value="i.image">{{i.tag}} · {{i.architectures.join(' / ')||'架构待确认'}} · {{i.updated?.slice(0,10)}} {{i.verified?'· 已验证':''}}</option></select></label><div class="card-actions"><button type="button" @click="loadImages(true)" :disabled="busy">刷新官方版本</button><span class="fine">选择版本后固定 digest（若官方提供）。</span></div><p v-if="imageWarning" class="fine">{{imageWarning}}</p><div class="form-row"><label>内存上限<input v-model="form.mem_limit" required placeholder="4gb"></label><label>CPU 上限<input v-model.number="form.cpus" type="number" min="0.5" step="0.5" required></label><label>最大并发<input v-model.number="form.max_running" type="number" min="1" max="30" required></label></div><p class="fine">先使用默认资源限制。并发达到上限后等待回收，不会无限创建浏览器。</p></div></template>
          <details :open="importMode"><summary>{{importMode?'已有 Compose 的位置':'高级选项 · 远端主机、端口与目录'}}</summary><div class="form-row"><label>主机代号<input v-model="form.host_name" required></label><label>SSH 目标（留空 = 本机）<input v-model="form.ssh" placeholder="user@linux-host"></label></div><template v-if="form.ssh"><div class="form-row"><label>SSH 端口<input v-model.number="form.ssh_port" type="number" min="1" max="65535" placeholder="22"></label><label>SSH 私钥（可选）<input v-model="form.identity" placeholder="本机私钥路径"></label></div><label class="check"><input v-model="form.sudo" type="checkbox">远端使用免密 sudo</label></template><label>部署代号<input v-model="form.deployment_name" required></label><label>部署目录<input v-model="form.dir" required></label><template v-if="importMode"><div class="form-row"><label>Compose 文件<select v-model="form.filename"><option>docker-compose.yaml</option><option>docker-compose.yml</option><option>compose.yaml</option><option>compose.yml</option></select></label><label>Manager 服务名<input v-model="form.service" required></label></div><p class="fine">保留原 token、网络、插件挂载与环境变量。支持专用 bind mount，以及 Sleight 创建的 data 子路径命名卷。</p></template><template v-else><label>手动填写镜像（离线 / 私有仓库）<input v-model="form.image" required></label><div class="form-row"><label>监听地址<input v-model="form.bind_ip" required></label><label>端口<input v-model.number="form.port" type="number" min="1" max="65535" required></label></div><label class="check"><input v-model="form.expose" type="checkbox">开放内网地址（已配置网络访问控制）</label><div class="form-row"><label>Compose 项目名<input v-model="form.name" required></label><label>容器名<input v-model="form.container_name" required></label></div><label>Docker 数据卷<input v-model="form.data_volume" placeholder="留空使用宿主机目录"></label><p class="fine">Windows 默认自动创建命名卷；Linux 默认使用部署目录。保留数据后再升级镜像。</p><label>共享资源标识<input v-model="form.resource_key" required></label></template></details>
          <div v-if="!importMode" class="deploy-step"><b>3 · 检查并部署</b><p>目录、容器、数据卷名称已自动生成。检查成功后点击部署；进度保留在环境页。</p></div><div v-if="checks" class="preflight"><h3>部署前检查</h3><p v-for="c in checks.checks" :key="c.name" :class="c.level==='fail'?'danger-text':''"><b>{{c.level==='ok'?'✓':c.level==='fail'?'×':'!'}} {{c.name}}</b> {{c.detail}}<small>{{c.hint}}</small></p></div><p v-if="error" class="alert danger">{{error}}</p><div class="dialog-actions"><button type="button" @click="modal=''">取消</button><button v-if="importMode" class="primary" :disabled="busy">导入部署</button><template v-else><button :disabled="busy">检查环境</button><button type="button" class="primary" :disabled="busy||!checks||checks.blocked" @click="deploy">部署</button></template></div>
        </form>
        <form v-if="modal==='agent'" @submit.prevent="addAgent"><h2>连接远程 Sleight 执行端</h2><label>名称<input v-model="form.name" required></label><label>执行端地址<input v-model="form.url" required placeholder="http://linux-host:8700"></label><label>执行端 delegate 接入 token<input v-model="form.token" type="password" required autocomplete="off"></label><p class="fine">在执行端运行 sleight ui，签发仅有 delegate 权限的管理员接入 token。控制台会按实际用户委托操作。跨网络使用 HTTPS 或 SSH 隧道。</p><p class="alert danger" v-if="error">{{error}}</p><div class="dialog-actions"><button type="button" @click="modal=''">取消</button><button class="primary" :disabled="busy">检查并连接</button></div></form>
        <form v-if="modal==='plugins-apply'" @submit.prevent="action(async()=>{await runJob(hostPath('extensions/apply'),{only:pluginNames,restart:true});modal=''})"><h2>下发插件配置</h2><p class="alert yellow">会修改目标 Manager 的全部实例，并停止配置有变化的运行中实例。再次启动后插件才会生效。</p><label class="check" v-for="p in installedPlugins" :key="p.dirname"><input type="checkbox" :value="p.dirname" v-model="pluginNames">{{p.name || p.dirname}}</label><div class="dialog-actions"><button type="button" @click="modal=''">取消</button><button class="primary" :disabled="busy">确认下发并停止实例</button></div></form>
        <form v-if="modal==='template-delete'||modal==='agent-delete'" @submit.prevent="action(async()=>{await api(modal==='template-delete'?`api/config/templates/${form.row.id}`:`api/agents/${form.row.id}`,'DELETE');modal='';await refresh()})"><h2>移除「{{form.row.name}}」？</h2><p>已创建的浏览器实例和目标运行环境会保留。以后创建实例时不能再选择此配置或连接。</p><div class="dialog-actions"><button type="button" @click="modal=''">取消</button><button class="danger" :disabled="busy">确认移除</button></div></form>
        <form v-if="modal==='upgrade'" @submit.prevent="deployOperation('upgrade')"><h2>升级 Manager</h2><p>会先停止实例并备份数据，再更换镜像。完成后检查浏览器和插件。</p><label>新镜像<input v-model="form.image" required></label><div class="dialog-actions"><button type="button" @click="modal=''">取消</button><button class="primary" :disabled="busy">备份并升级</button></div></form>
        <form v-if="modal==='rollback'" @submit.prevent="deployOperation('rollback')"><h2>恢复升级前环境</h2><p class="alert yellow">恢复旧镜像、升级前 /data 和配置。升级后新增的登录态不会出现在恢复后的环境中，当前数据会另存备份。</p><p>fin 也需要安装与旧环境兼容的 SDK 版本。回滚期间实例会停止。</p><div class="dialog-actions"><button type="button" @click="modal=''">取消</button><button class="danger" :disabled="busy">备份当前数据并回滚</button></div></form>
      </section>
    </div>
    <div v-if="agentPanel" class="overlay monitor" @click.self="agentPanel=null"><section class="panel viewer"><div class="card-head"><h2>{{agentPanel.name}} · 远程执行端</h2><button @click="agentPanel=null">关闭控制台 ×</button></div><iframe :src="endpoint(`agent/${agentPanel.id}/`)" title="远程 Sleight 执行端控制台"></iframe></section></div>
  </div>
</template>
