<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { api } from './api'

type Row = Record<string, any>
const rows = ref<Row[]>([]), error = ref(''), notice = ref(''), busy = ref(false), advanced = ref(''), source = ref('builtin-cloak-0')
const opened = ref(false), search = ref(''), dialogElement = ref<HTMLElement>()
const draft = reactive<Row>({ id: '', name: '', kind: 'cloak', note: '', config: {} })
const sourceRow = computed(() => rows.value.find(row => row.id === source.value))
const visible = computed(() => rows.value.filter(row => [row.name, row.note, kindName(row.kind)].join(' ').toLowerCase().includes(search.value.trim().toLowerCase())))
function kindName(kind: string) { return ({ native: '本机原生', fingerprint: 'fingerprint-chromium', cloak: 'Cloak' } as Record<string, string>)[kind] || kind }
async function load() { rows.value = await api('api/v1/launch-templates') }
function start(row?: Row) {
  const base = row || sourceRow.value || rows.value.find(row => row.kind === 'cloak') || rows.value[0]
  if (!base) return
  source.value = base.id
  Object.assign(draft, { id: row && !row.builtin ? row.id : '', name: row && !row.builtin ? row.name : '', kind: base.kind, note: base.note || '', config: JSON.parse(JSON.stringify(base.config || {})) })
  advanced.value = JSON.stringify(draft.config, null, 2); error.value = ''; opened.value = true
}
function close() { if (!busy.value) { opened.value = false; error.value = '' } }
async function save() {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    await api('api/v1/launch-templates', 'POST', { ...draft, name: draft.name.trim() })
    opened.value = false; notice.value = '创建模板已保存，可在新建实例或用户默认配置中选择。'
    await load()
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) } finally { busy.value = false }
}
function applyAdvanced() {
  try {
    const config = JSON.parse(advanced.value)
    if (!config || typeof config !== 'object' || Array.isArray(config)) throw new Error('object required')
    draft.config = config; error.value = ''
  } catch { error.value = '高级参数需要有效的 JSON 对象。' }
}
async function remove(row: Row) {
  if (!window.confirm(`删除模板「${row.name}」？已创建的实例保留原配置。`)) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await api('api/v1/launch-templates/' + row.id, 'DELETE'); await load(); notice.value = '创建模板已删除，已有实例不受影响。' }
  catch (e) { error.value = e instanceof Error ? e.message : String(e) } finally { busy.value = false }
}
function dialogKeys(event: KeyboardEvent) {
  if (event.key !== 'Tab' || !dialogElement.value) return
  const nodes = [...dialogElement.value.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')]
  const first = nodes[0], last = nodes[nodes.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
watch(opened, async value => { if (value) { await nextTick(); dialogElement.value?.querySelector<HTMLInputElement>('input')?.focus() } })
onMounted(async () => { busy.value = true; try { await load() } catch (e) { error.value = e instanceof Error ? e.message : String(e) } finally { busy.value = false } })
</script>

<template>
  <div class="launch-templates">
    <div v-if="!opened && error" class="alert danger" role="alert">{{ error }}</div><div v-if="notice" class="alert success" role="status">{{ notice }}</div>
    <section class="panel management-card">
      <div class="launch-template-toolbar"><div><h2>创建模板</h2><p>保存指纹、屏幕和语言等启动参数。插件与代理在独立页面配置。</p></div><button class="primary" :disabled="busy || !rows.length" @click="start()">＋ 新增创建模板</button></div>
      <div class="management-toolbar"><label class="management-search">查找模板<input v-model="search" type="search" placeholder="名称、内核或备注"></label></div>
      <div class="launch-template-grid"><article v-for="row in visible" :key="row.id" class="panel template-card"><div class="card-head"><h3>{{ row.name }}</h3><span class="badge" :class="row.builtin ? 'yellow' : 'neutral'">{{ row.builtin ? '内置模板' : '自定义' }}</span></div><dl><div><dt>内核</dt><dd>{{ kindName(row.kind) }}</dd></div><div><dt>时区 / 语言</dt><dd>{{ row.config.timezone || '内核默认' }} · {{ row.config.locale || '默认' }}</dd></div><div v-if="row.config.screen_width"><dt>屏幕</dt><dd>{{ row.config.screen_width }} × {{ row.config.screen_height }}</dd></div></dl><p>{{ row.note || '新建实例时使用这些默认启动参数。' }}</p><div class="card-actions"><button :disabled="busy" @click="start(row)">{{ row.builtin ? '复制为自定义' : '编辑模板' }}</button><button v-if="!row.builtin" :disabled="busy" @click="remove(row)">删除模板</button></div></article></div>
      <div v-if="!visible.length" class="management-empty"><p>{{ busy ? '正在加载模板…' : search ? '没有找到对应模板。' : '暂无创建模板。' }}</p></div>
    </section>
    <div v-if="opened" class="overlay" @click.self="close" @keydown.esc="close" @keydown="dialogKeys">
      <section ref="dialogElement" class="panel dialog management-dialog" role="dialog" aria-modal="true" aria-labelledby="launch-dialog-title"><button class="close" :disabled="busy" @click="close" aria-label="关闭模板弹窗">×</button><div class="eyebrow">BROWSER DEFAULTS</div><h2 id="launch-dialog-title">{{ draft.id ? '编辑创建模板' : '新增创建模板' }}</h2>
        <form @submit.prevent="save"><label v-if="!draft.id">从已有模板复制<select v-model="source" @change="start()"><option v-for="row in rows" :key="row.id" :value="row.id">{{ row.name }} · {{ kindName(row.kind) }}</option></select></label><label>模板名称<input v-model="draft.name" maxlength="100" required placeholder="例如 Win-US-自定义"></label><p class="fine">内核：{{ kindName(draft.kind) }}。保存后可用作用户默认配置，或在新建实例时选择。</p>
          <fieldset v-if="draft.kind === 'cloak'" class="management-form-section"><legend>语言与显示</legend><div class="form-row"><label>时区<input v-model="draft.config.timezone" placeholder="America/New_York"></label><label>语言<input v-model="draft.config.locale" placeholder="en-US"></label></div><div class="form-row"><label>屏幕宽度<input v-model.number="draft.config.screen_width" type="number" min="320"></label><label>屏幕高度<input v-model.number="draft.config.screen_height" type="number" min="240"></label></div><label>主题<select v-model="draft.config.color_scheme"><option value="light">浅色</option><option value="dark">深色</option><option value="no-preference">跟随系统</option></select></label></fieldset>
          <fieldset class="management-form-section"><legend>启动选项</legend><label v-if="draft.kind !== 'native'">长期实例默认指纹种子<input v-model.number="draft.config.fingerprint_seed" type="number" min="1" max="2147483647" placeholder="留空使用内核默认值"></label><label v-if="draft.kind !== 'cloak'" class="check"><input v-model="draft.config.headless" type="checkbox">无头模式</label><p class="fine">{{ draft.kind === 'cloak' ? 'Manager 0.1.x 使用有界面模式，显示服务在容器内运行。临时实例使用独立随机种子。' : '临时指纹实例不复用模板的固定种子。' }}</p></fieldset>
          <label>备注<textarea v-model="draft.note" rows="2" placeholder="用途或参数说明"></textarea></label><details class="management-backup-help" @toggle="advanced = JSON.stringify(draft.config, null, 2)"><summary>高级内核参数</summary><textarea v-model="advanced" rows="9" aria-label="高级模板 JSON" spellcheck="false"></textarea><button type="button" @click="applyAdvanced">应用 JSON 参数</button><p class="fine">现代 Manager 使用 GPU 家族，具体型号由内核决定。</p></details><p v-if="error" class="alert danger" role="alert">{{ error }}</p><div class="dialog-actions"><button type="button" :disabled="busy" @click="close">取消</button><button class="primary" :disabled="busy || !draft.name.trim()">{{ busy ? '保存中…' : '保存创建模板' }}</button></div>
        </form>
      </section>
    </div>
  </div>
</template>
