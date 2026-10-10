<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, stateLabel } from './api'
import SettingsPanel from './SettingsPanel.vue'
import LaunchTemplatesPanel from './LaunchTemplatesPanel.vue'
import './management.css'

type Row = Record<string, any>
const props = defineProps<{ identity: Row, initialTab?: string }>()
const emit = defineEmits<{ navigate: [page: string, tab?: string] }>()
const admin = computed(() => props.identity.user?.role === 'admin' && props.identity.scopes?.includes('manage'))
const groups = computed(() => [
  { name: '运行管理', items: [
    { id: 'sessions', name: '运行会话', note: '查看占用与回收状态' },
    { id: 'audit', name: '操作记录', note: '查询调用与执行结果' },
  ] },
  { name: '用户与接入', items: [
    ...(admin.value ? [{ id: 'users', name: '用户与权限', note: '授权环境、额度与默认配置' }] : []),
    { id: 'tokens', name: '访问 token', note: '为代码和 MCP 签发凭据' },
    ...(admin.value ? [{ id: 'launch-templates', name: '创建模板', note: '浏览器的默认启动参数' }] : []),
  ] },
  ...(admin.value ? [{ name: '系统设置', items: [
    { id: 'alerts', name: '阈值告警', note: '资源阈值与通知' },
    { id: 'storage', name: '数据库', note: '连接、迁移与备份' },
  ] }] : []),
])
const tab = ref(props.initialTab || 'sessions')
const section = computed(() => groups.value.flatMap(g => g.items).find(item => item.id === tab.value))
function chooseTab(id: string) { tab.value = groups.value.some(g => g.items.some(item => item.id === id)) ? id : 'sessions' }
watch(groups, () => chooseTab(tab.value), { immediate: true })
watch(() => props.initialTab, value => { if (value) chooseTab(value) })

const users = ref<Row[]>([]), tokens = ref<Row[]>([]), profiles = ref<Row[]>([]), sessions = ref<Row[]>([]), audit = ref<Row[]>([])
const environments = ref<Row[]>([]), templates = ref<Row[]>([]), launchTemplates = ref<Row[]>([])
const error = ref(''), notice = ref(''), loading = ref(false), busy = ref(false), lastUpdated = ref(0)
const dialog = ref(''), dialogError = ref(''), preview = ref(''), revealed = ref(''), copyMessage = ref('')
const dialogElement = ref<HTMLElement>(), secretField = ref<HTMLTextAreaElement>()
const draft = reactive<Row>({}), fixedNames = ref('')
const credential = reactive<Row>({ name: '', user_id: props.identity.user?.id, ttl: 0, scopes: ['execute'] })
const lifetime = ref(0)
const scopeOptions = [
  { id: 'execute', name: '浏览器执行', note: '代码、fin 和 MCP 使用浏览器。' },
  { id: 'manage', name: '配置管理', note: '管理权限仍受所属用户角色限制。' },
  { id: 'delegate', name: '身份委托', note: 'pyp 接入使用，通常只选此项。' },
]
const query = ref(''), statusFilter = ref('all'), pageNumber = ref(1), pageSize = ref(20)
const tableView = computed(() => ['sessions', 'users', 'tokens', 'audit'].includes(tab.value))
let revision = 0, disposed = false
let timer: ReturnType<typeof setInterval>

function userName(id: string) { return users.value.find(u => u.id === id)?.name || (id === props.identity.user?.id ? props.identity.user?.name : id) || '—' }
function environmentName(id: string) { return environments.value.find(e => e.id === id)?.name || (id === 'native' ? '本机浏览器' : id) || '—' }
function profileName(id: string) { return profiles.value.find(p => p.id === id)?.name || id || '—' }
function formatTime(value: number) { return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—' }
function scopeName(value: string) { return scopeOptions.find(s => s.id === value)?.name || value }
function tokenStatus(row: Row) { return row.revoked ? 'revoked' : row.expires && row.expires * 1000 <= (lastUpdated.value || Date.now()) ? 'expired' : 'current' }
const filteredRows = computed(() => {
  const rows = ({ sessions: sessions.value, users: users.value, tokens: tokens.value, audit: audit.value } as Record<string, Row[]>)[tab.value] || []
  const needle = query.value.trim().toLowerCase()
  return rows.filter(row => {
    if (statusFilter.value !== 'all') {
      if (tab.value === 'sessions' && (statusFilter.value === 'cleanup' ? !['cleanup', 'cleanup_pending', 'cleaning'].includes(row.state) : row.state !== statusFilter.value)) return false
      if (tab.value === 'users' && (statusFilter.value === 'active') !== Boolean(row.active)) return false
      if (tab.value === 'tokens' && tokenStatus(row) !== statusFilter.value) return false
      if (tab.value === 'audit' && (statusFilter.value === 'errors' ? row.status < 400 : row.status >= 400)) return false
    }
    const values = tab.value === 'sessions' ? [profileName(row.profile), row.id, userName(row.owner), row.owner] :
      tab.value === 'users' ? [row.name, row.id, environmentName(row.default_environment)] :
      tab.value === 'tokens' ? [row.name, row.id, userName(row.user_id), ...(row.scopes || []).map(scopeName)] : [row.path, row.method, userName(row.user), row.status]
    return !needle || values.join(' ').toLowerCase().includes(needle)
  })
})
const pages = computed(() => Math.max(1, Math.ceil(filteredRows.value.length / pageSize.value)))
const visibleRows = computed(() => filteredRows.value.slice((pageNumber.value - 1) * pageSize.value, pageNumber.value * pageSize.value))
watch([query, statusFilter, pageSize], () => { pageNumber.value = 1 })
watch(pages, count => { pageNumber.value = Math.min(pageNumber.value, count) })

async function refresh(background = false) {
  const current = ++revision, selected = tab.value
  if (!background) loading.value = true
  const sources: Record<string, typeof users> = { users, tokens, profiles, sessions, audit, environments, templates, launchTemplates }
  const paths: Record<string, string> = { environments: 'api/v1/environments' }
  if (selected === 'sessions') Object.assign(paths, { sessions: 'api/v1/sessions', profiles: 'api/v1/profiles' })
  if (selected === 'tokens') paths.tokens = 'api/v1/tokens'
  if (selected === 'audit') paths.audit = 'api/v1/audit'
  if (admin.value && ['sessions', 'tokens', 'audit', 'users'].includes(selected)) paths.users = 'api/v1/users'
  if (selected === 'users' && admin.value) Object.assign(paths, { templates: 'api/config/templates', launchTemplates: 'api/v1/launch-templates' })
  try {
    const result = await Promise.all(Object.entries(paths).map(async ([key, path]) => [key, await api<Row[]>(path)] as const))
    if (disposed || current !== revision || selected !== tab.value) return
    for (const [key, rows] of result) sources[key].value = rows
    lastUpdated.value = Date.now()
    error.value = ''
  } catch (e) {
    if (!disposed && current === revision) error.value = e instanceof Error ? e.message : String(e)
  } finally {
    if (current === revision) loading.value = false
  }
}
watch([tab, admin], () => {
  query.value = ''; statusFilter.value = 'all'; pageNumber.value = 1; error.value = ''; notice.value = ''
  if (!admin.value) { users.value = []; templates.value = []; launchTemplates.value = []; if (dialog.value === 'user') dialog.value = '' }
  void refresh()
}, { immediate: true })

async function work(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''; dialogError.value = ''; notice.value = ''
  try { await fn() } catch (e) { (dialog.value ? dialogError : error).value = e instanceof Error ? e.message : String(e) }
  finally { busy.value = false }
}
function editUser(row?: Row) {
  Object.keys(draft).forEach(key => delete draft[key])
  Object.assign(draft, row ? { ...row, environments: [...(row.environments || [])], profile_names: [...(row.profile_names || [])] } : {
    name: '', role: 'operator', active: true, limit: 1, default_environment: 'native', default_template: '', default_launch_template: '', environments: [], profile_names: [],
  })
  fixedNames.value = draft.profile_names.join('\n'); dialogError.value = ''; dialog.value = 'user'
}
function toggleEnvironment(id: string, checked: boolean) { draft.environments = checked ? [...new Set([...draft.environments, id])] : draft.environments.filter((value: string) => value !== id) }
async function saveUser() {
  await work(async () => {
    const body = { ...draft, name: draft.name.trim(), profile_names: [...new Set(fixedNames.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean))] }
    await api('api/v1/users', 'POST', body)
    dialog.value = ''; await refresh(); notice.value = '用户配置已保存。'
  })
}
function newToken() {
  Object.assign(credential, { name: '', user_id: props.identity.user?.id, ttl: 0, scopes: ['execute'] })
  lifetime.value = 0; revealed.value = ''; copyMessage.value = ''; dialogError.value = ''; dialog.value = 'token'
}
async function issue() {
  await work(async () => {
    const body = { ...credential, name: credential.name.trim(), ttl: lifetime.value === -1 ? credential.ttl : lifetime.value,
      user_id: admin.value ? credential.user_id : props.identity.user?.id,
      scopes: credential.scopes.filter((scope: string) => props.identity.scopes?.includes(scope)) }
    if (!body.scopes.length) throw new Error('至少选择一项已有权限。')
    const result = await api<Row>('api/v1/tokens', 'POST', body)
    revealed.value = result.token
    await refresh()
  })
}
async function copyToken() {
  try { await navigator.clipboard.writeText(revealed.value); copyMessage.value = '已复制，请存放在安全位置。' }
  catch { secretField.value?.focus(); secretField.value?.select(); copyMessage.value = '已选中 token，请按 Ctrl/Cmd+C 复制。' }
}
function closeDialog(saved = false) {
  if (busy.value || (revealed.value && !saved)) return
  dialog.value = ''; revealed.value = ''; dialogError.value = ''; copyMessage.value = ''
}
async function revoke(row: Row) {
  if (!window.confirm(`撤销「${row.name}」？使用此凭据的会话将停止，撤销后无法恢复。`)) return
  await work(async () => { await api(`api/v1/tokens/${row.id}`, 'DELETE'); await refresh(); notice.value = 'token 已撤销。' })
}
async function stop(row: Row) {
  if (!window.confirm(`结束「${profileName(row.profile)}」的会话？正在执行的操作会中断，随后进入回收。`)) return
  await work(async () => { await api(`api/v1/sessions/${row.id}`, 'DELETE'); await refresh(); notice.value = '已提交结束请求，请留意回收状态。' })
}
async function inspect(row: Row) {
  await work(async () => { const data = await api<Row>(`api/v1/sessions/${row.id}/call`, 'POST', { method: 'screenshot' }); preview.value = 'data:image/png;base64,' + data.$bytes })
}
async function recover() {
  await work(async () => { await api('api/v1/collect', 'POST', {}); await refresh(); notice.value = '已执行回收检查；清理失败的实例仍占用名额，后台会继续重试。' })
}
function dialogKeys(event: KeyboardEvent) {
  if (event.key === 'Escape') { if (preview.value) preview.value = ''; else closeDialog(); return }
  if (event.key !== 'Tab' || !dialog.value || !dialogElement.value) return
  const nodes = [...dialogElement.value.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')]
  const first = nodes[0], last = nodes[nodes.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
watch(dialog, async value => { if (value) { await nextTick(); dialogElement.value?.querySelector<HTMLElement>('input,select,textarea,button')?.focus() } })
onMounted(() => {
  document.addEventListener('keydown', dialogKeys)
  timer = setInterval(() => { if (!busy.value && !loading.value && !document.hidden && tableView.value) void refresh(true) }, 20000)
})
onUnmounted(() => { disposed = true; revision++; clearInterval(timer); document.removeEventListener('keydown', dialogKeys) })
</script>

<template>
  <div class="service-panel">
    <div class="page-heading service-heading">
      <div><div class="eyebrow">ACCESS &amp; OPERATIONS</div><h1>用户与运行<span class="dot">.</span></h1><p>管理接入权限、运行会话和服务设置。</p></div>
      <button class="service-instance-link" @click="emit('navigate', 'instances')">浏览器实例 →</button>
    </div>
    <div class="service-layout">
      <nav class="service-menu" :class="{ 'service-menu-personal': !admin }" aria-label="用户与运行功能">
        <div v-for="group in groups" :key="group.name" class="service-menu-group">
          <h2>{{ group.name }}</h2>
          <button v-for="item in group.items" :key="item.id" :class="{ selected: tab === item.id }" :aria-current="tab === item.id ? 'page' : undefined" @click="chooseTab(item.id)">
            <span>{{ item.name }}<span aria-hidden="true">↗</span></span><small>{{ item.note }}</small>
          </button>
        </div>
        <div class="service-account"><b>{{ identity.user?.name || '当前用户' }}</b><span v-if="identity.user?.name !== (admin ? '管理员' : '执行用户')" class="service-role">{{ admin ? '管理员' : '执行用户' }}</span><dl><div><dt>默认环境</dt><dd>{{ environmentName(identity.user?.default_environment) }}</dd></div><div><dt>并发上限</dt><dd>{{ identity.user?.limit || '—' }}</dd></div><div><dt>存储</dt><dd>{{ identity.storage === 'postgresql' ? 'PostgreSQL' : 'SQLite' }}</dd></div></dl></div>
      </nav>
      <div class="service-content" :aria-busy="loading || busy">
        <div v-if="error" class="alert danger" role="alert">{{ error }}<button class="text-button" :disabled="loading" @click="refresh()">重试</button></div>
        <div v-if="notice" class="alert success" role="status">{{ notice }}</div>
        <SettingsPanel v-if="admin && ['alerts', 'storage'].includes(tab)" :key="tab" :tab="tab" />
        <LaunchTemplatesPanel v-if="admin && tab === 'launch-templates'" />
        <section v-if="tableView" class="panel management-card">
          <div class="management-card-heading"><div><h2>{{ section?.name }}</h2><p v-if="tab === 'sessions'">会话由代码、MCP 或控制台建立。等待回收仍占用额度，确认停止后才释放。</p><p v-else-if="tab === 'users'">先创建用户，再配置可用环境、默认模板和并发上限。</p><p v-else-if="tab === 'tokens'">token 绑定用户，决定其他程序能够执行哪些操作。明文只在生成后显示一次。</p><p v-else>最近 200 条服务调用记录。浏览器启动与页面操作日志请到实例卡片查看。</p></div><div class="management-heading-actions"><button v-if="tab === 'users'" class="primary" :disabled="busy || loading" @click="editUser()">＋ 新增用户</button><button v-if="tab === 'tokens'" class="primary" :disabled="busy || loading" @click="newToken">＋ 生成 token</button><button v-if="tab === 'sessions' && admin" :disabled="busy || loading" @click="recover">重试回收</button></div></div>
          <div v-if="tab === 'sessions'" class="management-note">管理实例、网址和登录态，请使用「浏览器实例」。这里管理的是实例的运行占用。</div>
          <div class="management-toolbar">
            <label class="management-search">搜索<input v-model="query" type="search" :placeholder="tab === 'audit' ? '路径、操作、用户或状态码' : tab === 'sessions' ? '实例名称、会话 ID 或用户' : '名称、用户或 ID'"></label>
            <label>状态<select v-model="statusFilter"><option value="all">全部状态</option><template v-if="tab === 'sessions'"><option value="running">运行中</option><option value="starting">启动中</option><option value="cleanup">等待 / 正在回收</option><option value="released">已释放</option></template><template v-else-if="tab === 'users'"><option value="active">已启用</option><option value="inactive">已停用</option></template><template v-else-if="tab === 'tokens'"><option value="current">未到期</option><option value="expired">已过期</option><option value="revoked">已撤销</option></template><template v-else><option value="success">成功</option><option value="errors">失败</option></template></select></label>
            <button :disabled="loading || busy" @click="refresh()">{{ loading ? '刷新中…' : '刷新' }}</button>
          </div>
          <div class="management-table-wrap"><table class="management-table">
            <thead><tr v-if="tab === 'sessions'"><th scope="col">浏览器实例 / 会话</th><th scope="col">状态</th><th scope="col">所属用户</th><th scope="col">创建 / 租约到期</th><th scope="col">操作</th></tr><tr v-else-if="tab === 'users'"><th scope="col">用户</th><th scope="col">角色 / 状态</th><th scope="col">默认配置</th><th scope="col">并发上限</th><th scope="col">操作</th></tr><tr v-else-if="tab === 'tokens'"><th scope="col">用途 / token ID</th><th scope="col">所属用户</th><th scope="col">权限</th><th scope="col">状态 / 到期时间</th><th scope="col">操作</th></tr><tr v-else><th scope="col">时间</th><th scope="col">操作 / 路径</th><th scope="col">用户</th><th scope="col">结果</th><th scope="col">耗时</th></tr></thead>
            <tbody>
              <template v-for="row in visibleRows" :key="row.id">
                <tr v-if="tab === 'sessions'"><td><b>{{ profileName(row.profile) }}</b><small class="mono" :title="row.id">{{ row.id.slice(0, 12) }}</small></td><td><span class="badge" :class="row.state === 'running' ? 'green' : row.state === 'released' ? 'neutral' : 'yellow'">{{ stateLabel[row.state] || row.state }}</span><small v-if="row.error" class="danger-text">{{ row.error }}</small></td><td>{{ userName(row.owner) }}</td><td class="management-time">{{ formatTime(row.created) }}<small>{{ row.state === 'released' ? '会话已结束' : '租约 ' + formatTime(row.expires) }}</small></td><td><div class="management-row-actions"><button :disabled="busy || row.state !== 'running'" @click="inspect(row)">预览</button><button :disabled="busy || row.state === 'released'" @click="stop(row)">结束会话</button></div></td></tr>
                <tr v-else-if="tab === 'users'"><td><b>{{ row.name }}</b><small class="mono" :title="row.id">{{ row.id.slice(0, 12) }}</small></td><td>{{ row.role === 'admin' ? '管理员' : '执行用户' }}<small><span class="badge" :class="row.active ? 'green' : 'neutral'">{{ row.active ? '已启用' : '已停用' }}</span></small></td><td>{{ environmentName(row.default_environment) }}<small>{{ launchTemplates.find(t => t.id === row.default_launch_template)?.name || '内核默认参数' }} · {{ templates.find(t => t.id === row.default_template)?.name || '无插件与代理模板' }}</small></td><td>{{ row.limit }}</td><td><button :disabled="busy || loading" @click="editUser(row)">编辑用户</button></td></tr>
                <tr v-else-if="tab === 'tokens'"><td><b>{{ row.name }}</b><small class="mono" :title="row.id">{{ row.id.slice(0, 12) }}</small></td><td>{{ userName(row.user_id) }}</td><td><div class="management-scope-tags"><span v-for="scope in row.scopes" :key="scope">{{ scopeName(scope) }}</span></div></td><td><span class="badge" :class="tokenStatus(row) === 'current' ? 'green' : 'neutral'">{{ { current: '未到期', expired: '已过期', revoked: '已撤销' }[tokenStatus(row)] }}</span><small>{{ row.expires ? formatTime(row.expires) : '长期有效期' }}</small></td><td><button :disabled="row.revoked || busy" @click="revoke(row)">撤销</button></td></tr>
                <tr v-else><td class="management-time">{{ formatTime(row.time) }}</td><td><b>{{ row.method }}</b><small class="mono management-path">{{ row.path }}</small></td><td>{{ userName(row.user) }}</td><td><span class="badge" :class="row.status >= 400 ? 'red' : 'green'">{{ row.status }}</span></td><td>{{ row.duration_ms }} ms</td></tr>
              </template>
              <tr v-if="!visibleRows.length"><td colspan="5" class="management-empty"><template v-if="loading">正在加载…</template><template v-else-if="error">加载失败，请重试。</template><template v-else-if="query || statusFilter !== 'all'">没有符合筛选条件的记录。<button class="text-button" @click="query = ''; statusFilter = 'all'">清除筛选</button></template><template v-else><b>{{ tab === 'sessions' ? '暂无运行会话' : tab === 'users' ? '还没有用户' : tab === 'tokens' ? '还没有访问 token' : '暂无操作记录' }}</b><p>{{ tab === 'sessions' ? '启动浏览器实例或通过代码连接后，会话会显示在这里。' : tab === 'tokens' ? '点击「生成 token」，为 fin、代码或 MCP 创建接入凭据。' : tab === 'users' ? '点击「新增用户」开始配置。' : '使用浏览器或修改配置后，这里会记录对应的服务调用。' }}</p></template></td></tr>
            </tbody>
          </table></div>
          <div class="management-pagination"><span>共 {{ filteredRows.length }} 条<span v-if="lastUpdated"> · 更新于 {{ new Date(lastUpdated).toLocaleTimeString('zh-CN', { hour12: false }) }}</span></span><div><label>每页<select v-model.number="pageSize"><option :value="20">20 条</option><option :value="50">50 条</option><option :value="200">200 条</option></select></label><button :disabled="pageNumber <= 1" @click="pageNumber--" aria-label="上一页">←</button><span>{{ pageNumber }} / {{ pages }}</span><button :disabled="pageNumber >= pages" @click="pageNumber++" aria-label="下一页">→</button></div></div>
        </section>
      </div>
    </div>

    <div v-if="dialog" class="overlay" @click.self="closeDialog()">
      <section ref="dialogElement" class="panel dialog management-dialog" role="dialog" aria-modal="true" :aria-labelledby="dialog === 'user' ? 'user-dialog-title' : 'token-dialog-title'">
        <button v-if="!revealed" class="close" :disabled="busy" @click="closeDialog()" aria-label="关闭弹窗">×</button>
        <template v-if="dialog === 'user'">
          <div class="eyebrow">USER ACCESS</div><h2 id="user-dialog-title">{{ draft.id ? '编辑用户' : '新增用户' }}</h2><p>设置用户可以访问什么，以及新建浏览器时使用哪些默认配置。</p>
          <form @submit.prevent="saveUser">
            <fieldset class="management-form-section"><legend>基本信息</legend><div class="form-row"><label>用户名称<input v-model="draft.name" required maxlength="120" placeholder="例如 fin 采集用户"></label><label>角色<select v-model="draft.role"><option value="operator">执行用户</option><option value="admin">管理员</option></select></label><label>并发上限<input v-model.number="draft.limit" type="number" required min="1" max="100"></label></div><label class="check"><input v-model="draft.active" type="checkbox">允许使用服务</label><p v-if="!draft.active" class="management-warning">停用后，该用户的凭据将失效，运行会话会进入回收。</p></fieldset>
            <fieldset class="management-form-section"><legend>新建实例的默认配置</legend><label>默认运行环境<select v-model="draft.default_environment"><option v-if="draft.default_environment && !environments.some(e => e.id === draft.default_environment)" :value="draft.default_environment">{{ draft.default_environment }} · 原环境</option><option v-for="e in environments" :key="e.id" :value="e.id">{{ e.name }}</option></select></label><div class="form-row"><label>创建模板<select v-model="draft.default_launch_template"><option value="">内核默认配置</option><option v-if="draft.default_launch_template && !launchTemplates.some(t => t.id === draft.default_launch_template)" :value="draft.default_launch_template">原模板 · 保留配置</option><option v-for="t in launchTemplates" :key="t.id" :value="t.id">{{ t.name }}</option></select></label><label>插件与代理模板<select v-model="draft.default_template"><option value="">无模板</option><option v-if="draft.default_template && !templates.some(t => t.id === draft.default_template)" :value="draft.default_template">原模板 · 保留配置</option><option v-for="t in templates" :key="t.id" :value="t.id">{{ t.name }}</option></select></label></div><p class="fine">创建模板设置指纹、屏幕等启动参数；插件与代理模板管理扩展和网络出口。</p></fieldset>
            <fieldset class="management-form-section"><legend>可使用的浏览器</legend><p class="fine">默认环境自动授权。其他环境按需勾选。</p><div class="management-environment-options"><label v-for="e in environments" :key="e.id" class="check"><input type="checkbox" :value="e.id" :checked="e.id === draft.default_environment || draft.environments.includes(e.id)" :disabled="e.id === draft.default_environment" @change="toggleEnvironment(e.id, ($event.target as HTMLInputElement).checked)">{{ e.name }}<small v-if="e.id === draft.default_environment">默认</small></label></div><label>授权固定实例名称（可选，每行一个）<textarea v-model="fixedNames" rows="3" placeholder="Manager 中已有的实例名称"></textarea></label><p class="fine">固定实例仍受并发额度与独占租约限制，可用于 fin 的固定实例兜底。</p></fieldset>
            <p v-if="dialogError" class="alert danger" role="alert">{{ dialogError }}</p><div class="dialog-actions"><button type="button" :disabled="busy" @click="closeDialog()">取消</button><button class="primary" :disabled="busy || !draft.name?.trim()">{{ busy ? '保存中…' : '保存用户' }}</button></div>
          </form>
        </template>
        <template v-else>
          <div class="eyebrow">API CREDENTIAL</div><h2 id="token-dialog-title">{{ revealed ? '保存你的 token' : '生成访问 token' }}</h2>
          <form v-if="!revealed" @submit.prevent="issue">
            <p>一个用途使用一个 token，便于查找和撤销。</p><label>用途名称<input v-model="credential.name" required maxlength="120" placeholder="例如 fin 采集、MCP 助手"></label><div class="form-row"><label v-if="admin">所属用户<select v-model="credential.user_id"><option v-for="u in users" :key="u.id" :value="u.id" :disabled="u.active === false">{{ u.name }}{{ u.active === false ? ' · 已停用' : '' }}</option></select></label><label>有效期<select v-model.number="lifetime"><option :value="0">长期 · 主动撤销后失效</option><option :value="86400">1 天</option><option :value="604800">7 天</option><option :value="2592000">30 天</option><option :value="-1">自定义</option></select></label></div><label v-if="lifetime === -1">有效期（秒）<input v-model.number="credential.ttl" type="number" required min="1"></label>
            <fieldset class="management-form-section"><legend>允许的操作</legend><label v-for="scope in scopeOptions" :key="scope.id" class="management-scope-option" :class="{ unavailable: !identity.scopes?.includes(scope.id) }"><input type="checkbox" :value="scope.id" v-model="credential.scopes" :disabled="!identity.scopes?.includes(scope.id)"><span><b>{{ scope.name }}</b><small>{{ scope.note }}{{ !identity.scopes?.includes(scope.id) ? ' 当前凭据无此权限。' : '' }}</small></span></label></fieldset><p v-if="dialogError" class="alert danger" role="alert">{{ dialogError }}</p><div class="dialog-actions"><button type="button" :disabled="busy" @click="closeDialog()">取消</button><button class="primary" :disabled="busy || !credential.name.trim() || !credential.scopes.length">{{ busy ? '生成中…' : '生成 token' }}</button></div>
          </form>
          <div v-else class="management-token-result"><p class="management-warning">仅显示一次。关闭后无法再次查看，请先保存到程序配置中。</p><label>新 token<textarea ref="secretField" readonly :value="revealed" rows="3" aria-label="新 token" spellcheck="false"></textarea></label><p v-if="copyMessage" role="status">{{ copyMessage }}</p><p v-if="dialogError" class="alert danger" role="alert">{{ dialogError }}</p><div class="dialog-actions"><button :disabled="busy" @click="copyToken">复制 token</button><button class="primary" :disabled="busy" @click="closeDialog(true)">已保存，隐藏 token</button></div></div>
        </template>
      </section>
    </div>
    <div v-if="preview" class="overlay monitor" @click.self="preview = ''"><section class="panel viewer management-preview" role="dialog" aria-modal="true" aria-label="会话只读预览"><div class="card-head"><h2>当前页面 · 只读预览</h2><button @click="preview = ''">关闭 ×</button></div><img :src="preview" alt="当前浏览器页面"></section></div>
  </div>
</template>
