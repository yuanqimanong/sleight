<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { api } from './api'

type Row = Record<string, any>
const props = defineProps<{ tab: string }>()
const error = ref(''), notice = ref(''), busy = ref(false), loaded = ref(false)
const status = ref<Row>({ current: {}, pending: {} }), environments = ref<Row[]>([]), tested = ref(''), confirmation = ref('')
const alerts = reactive<Row>({ cpu_percent: 85, memory_percent: 80, duration_s: 60, cooldown_s: 900, target: 'host', webhook: '', signing_secret: '', configured: false, disable: false })
const storage = reactive<Row>({ backend: 'postgresql', host: '', port: 5432, username: '', password: '', database: '', schema: '', sslmode: 'prefer' })
async function work(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try { await fn() } catch (e) { error.value = e instanceof Error ? e.message : String(e) } finally { busy.value = false }
}
async function load() {
  loaded.value = false
  await work(async () => {
    if (props.tab === 'alerts') {
      const [settings, targets] = await Promise.all([api<Row>('api/v1/settings/alerts'), api<Row[]>('api/v1/environments')])
      Object.assign(alerts, settings, { webhook: '', signing_secret: '', disable: false }); environments.value = targets
    } else {
      status.value = await api('api/v1/settings/storage')
      if (status.value.current?.backend === 'postgresql') Object.assign(storage, status.value.current, { password: '' })
    }
    loaded.value = true
  })
}
async function saveAlerts() {
  await work(async () => {
    const body: Row = { cpu_percent: alerts.cpu_percent, memory_percent: alerts.memory_percent, duration_s: alerts.duration_s, cooldown_s: alerts.cooldown_s, target: alerts.target }
    if (alerts.disable || alerts.webhook.trim()) body.webhook = alerts.disable ? '' : alerts.webhook
    if (alerts.signing_secret.trim()) body.signing_secret = alerts.signing_secret
    Object.assign(alerts, await api('api/v1/settings/alerts', 'POST', body))
    alerts.webhook = ''; alerts.signing_secret = ''; alerts.disable = false
    notice.value = alerts.configured ? '告警设置已保存。持续超限时发送通知，恢复后发送恢复通知。' : '设置已保存，通知未启用。'
  })
}
async function testStorage() {
  await work(async () => {
    const snapshot = JSON.stringify(storage)
    const result = await api<Row>('api/v1/settings/storage/test', 'POST', JSON.parse(snapshot))
    if (snapshot !== JSON.stringify(storage)) { tested.value = ''; notice.value = '连接配置已更改，请重新测试当前配置。'; return }
    tested.value = snapshot; notice.value = result.message
  })
}
async function migrate() {
  await work(async () => {
    if (!tested.value || tested.value !== JSON.stringify(storage) || confirmation.value !== '迁移') throw new Error('请先测试当前连接，并输入「迁移」确认。')
    const result = await api<Row>('api/v1/settings/storage/migrate', 'POST', JSON.parse(tested.value))
    storage.password = ''; tested.value = ''; notice.value = result.message + ' 备份：' + result.backup
    status.value = await api('api/v1/settings/storage')
  })
}
watch(storage, () => { tested.value = ''; confirmation.value = '' }, { deep: true })
watch(() => props.tab, load)
onMounted(load)
</script>

<template>
  <div class="management-settings-grid">
    <p v-if="error" class="alert danger" role="alert">{{ error }}<button class="text-button" :disabled="busy" @click="load">重试</button></p>
    <p v-if="notice" class="alert success" role="status">{{ notice }}</p>
    <form v-if="tab === 'alerts'" class="panel management-settings-card" @submit.prevent="saveAlerts">
      <div class="card-head"><h2>阈值告警</h2><span class="badge" :class="alerts.configured ? 'green' : 'neutral'">{{ !loaded ? '读取中' : alerts.configured ? '通知已启用' : '通知未启用' }}</span></div>
      <p>监控 CPU 和内存持续超限。填写 Lark / 飞书 Webhook 后启用通知，无需保持页面打开。</p>
      <fieldset class="management-form-section"><legend>监控范围</legend><label>监控目标<select v-model="alerts.target"><option value="host">Sleight 执行主机</option><option v-if="alerts.target !== 'host' && !environments.some(e => e.id === alerts.target)" :value="alerts.target">原监控目标 · {{ alerts.target }}</option><option v-for="e in environments.filter(e => e.kind === 'cloak')" :key="e.id" :value="e.id">Manager 容器 · {{ e.name }}</option></select></label><p class="fine">主机使用 CPU 和物理内存占比；容器以配置的 CPU、内存上限为 100%。</p></fieldset>
      <fieldset class="management-form-section"><legend>触发规则</legend><div class="form-row"><label>CPU 阈值（%）<input v-model.number="alerts.cpu_percent" type="number" min="1" max="100" required></label><label>内存阈值（%）<input v-model.number="alerts.memory_percent" type="number" min="1" max="100" required></label></div><div class="form-row"><label>持续超限（秒）<input v-model.number="alerts.duration_s" type="number" min="5" max="86400" required></label><label>重复通知间隔（秒）<input v-model.number="alerts.cooldown_s" type="number" min="60" max="86400" required></label></div><p class="fine">每 5 秒采样。CPU 或内存超过阈值并达到持续时间后通知，恢复正常后通知一次。</p></fieldset>
      <fieldset class="management-form-section"><legend>通知渠道</legend><label>机器人 Webhook<input v-model="alerts.webhook" type="password" autocomplete="off" :placeholder="alerts.configured ? '已保存，留空保留原地址' : 'https://open.feishu.cn/open-apis/bot/v2/hook/…'" :disabled="alerts.disable"></label><label>签名密钥（可选）<input v-model="alerts.signing_secret" type="password" autocomplete="off" :placeholder="alerts.signing_configured ? '已保存，留空保留原密钥' : '机器人开启签名校验时填写'" :disabled="alerts.disable"></label><label v-if="alerts.configured" class="check"><input v-model="alerts.disable" type="checkbox">清除 Webhook，停用通知</label><p v-if="alerts.disable" class="management-warning">保存后停止发送告警和恢复通知。</p><p v-else class="fine">未保存过 Webhook 时，留空表示不启用通知。已有地址和密钥留空不会被覆盖。</p></fieldset>
      <div class="dialog-actions"><button class="primary" :disabled="busy || !loaded">{{ busy ? '处理中…' : '保存告警设置' }}</button></div>
    </form>
    <section v-else class="panel management-settings-card">
      <h2>数据库</h2><p>查看当前存储，或将配置与运行管理记录迁移到另一套数据库。</p>
      <div class="management-current-storage"><div><span>当前存储</span><b>{{ !loaded ? '正在读取…' : status.current?.backend === 'postgresql' ? 'PostgreSQL' : 'SQLite' }}</b></div><div><span>当前数据库</span><b>{{ status.current?.database || '—' }}</b></div><div class="storage-directory"><span>数据目录</span><code>{{ status.data_dir || '—' }}</code></div></div>
      <p v-if="status.restart_required" class="alert yellow">迁移已完成，写入已暂停。请重启 Sleight，使用新数据库。</p><p v-if="status.environment_override" class="alert yellow">当前由 SLEIGHT_DATABASE_URL 控制。使用此页面迁移前，需移除该环境变量并重启。</p>
      <form @submit.prevent="testStorage">
        <fieldset class="management-form-section"><legend>目标连接</legend><label>目标数据库<select v-model="storage.backend" :disabled="busy || status.restart_required"><option value="postgresql">PostgreSQL</option><option value="sqlite">SQLite · 新建本地文件</option></select></label><template v-if="storage.backend === 'postgresql'"><div class="form-row"><label>主机<input v-model="storage.host" required placeholder="内网数据库地址"></label><label>端口<input v-model.number="storage.port" type="number" min="1" max="65535" required></label></div><div class="form-row"><label>用户<input v-model="storage.username" required autocomplete="off"></label><label>密码<input v-model="storage.password" type="password" autocomplete="new-password"></label></div><div class="form-row"><label>数据库名<input v-model="storage.database" required></label><label>独立 schema（可选）<input v-model="storage.schema" placeholder="留空使用 public"></label></div><label>SSL<select v-model="storage.sslmode"><option v-for="value in ['disable', 'prefer', 'require', 'verify-ca', 'verify-full']" :key="value">{{ value }}</option></select></label></template><p class="fine">目标必须为空。SQLite 与 PostgreSQL 使用相同表结构，迁移包含配置、用户、token 和运行记录。</p><div class="management-inline-actions"><button :disabled="busy || !loaded || status.restart_required">{{ busy ? '处理中…' : '测试连接' }}</button><span v-if="tested" class="management-tested">✓ 当前配置已通过连接测试</span></div></fieldset>
        <div class="management-migration"><b>备份并迁移</b><p>先停止所有浏览器，等待运行任务与回收完成。旧数据库会保留，完成后需重启服务。</p><label>输入「迁移」确认<input v-model="confirmation" placeholder="迁移" :disabled="!tested || busy || status.environment_override || status.restart_required"></label><button type="button" class="primary" :disabled="busy || !tested || confirmation !== '迁移' || status.environment_override || status.restart_required" @click="migrate">备份并迁移，下次启动生效</button></div>
      </form>
      <details class="management-backup-help"><summary>备份位置与回滚方式</summary><p>备份保存在数据目录的 backups/，连接配置加密保存在 storage.json。迁移不会删除原数据库。</p><p>停止 Sleight，移走 storage.json 可回到默认 SQLite；此前使用 Web 配置数据库时，恢复 storage.previous.json。保留同一 secret.key，切换后新增数据不会自动写回旧库。</p></details>
    </section>
  </div>
</template>
