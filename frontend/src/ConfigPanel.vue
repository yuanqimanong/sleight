<script setup lang="ts">
import {computed,ref} from 'vue'
type Row=Record<string,any>
const props=defineProps<{templates:Row[],environments:Row[],selected:string,plugins:Row[],pluginPath:string,busy:boolean}>()
const emit=defineEmits<{'create-template':[],'edit-template':[template:Row],'delete-template':[template:Row],'select-environment':[id:string],'update:pluginPath':[path:string],install:[],refresh:[],apply:[],verify:[],deploy:[]}>()
const section=ref('templates'),search=ref('')
const visibleTemplates=computed(()=>props.templates.filter(t=>[t.name,t.proxy_display,...(t.extension_paths||[])].join(' ').toLocaleLowerCase().includes(search.value.toLocaleLowerCase())))
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">BROWSER CONFIGURATION</div><h1>插件与代理<span class="dot">.</span></h1><p>配置模板供实例使用；Manager 插件先安装，再下发。</p></div><button v-if="section==='templates'" class="primary" :disabled="busy" @click="emit('create-template')">＋ 新建模板</button></div>
  <nav class="management-tabs config-tabs" aria-label="插件与代理功能"><button :class="{chosen:section==='templates'}" :aria-current="section==='templates'?'page':undefined" @click="section='templates'">配置模板 <small>{{templates.length}}</small></button><button :class="{chosen:section==='plugins'}" :aria-current="section==='plugins'?'page':undefined" @click="section='plugins'">Manager 插件管理</button></nav>
  <template v-if="section==='templates'">
    <div class="configuration-intro"><div><b>一份模板，复用到多个实例。</b><p>保存代理和插件目录，在新建或编辑实例时选择。已有实例需通过编辑更新。</p></div><label class="template-search">查找模板<input v-model="search" type="search" placeholder="搜索名称、代理或插件目录"></label></div>
    <div v-if="templates.length" class="template-grid"><article v-for="t in visibleTemplates" :key="t.id" class="panel configuration-card"><div class="card-head"><h2>{{t.name}}</h2><span class="badge" :class="t.proxy_display?'yellow':'neutral'">{{t.proxy_display?'已配置代理':'直连'}}</span></div><dl><div><dt>浏览器代理</dt><dd class="mono">{{t.proxy_display||'不使用代理'}}</dd></div><div><dt>自动加载插件</dt><dd>{{(t.extension_paths||[]).length}} 个目录</dd></div></dl><details v-if="t.extension_paths?.length" class="template-paths"><summary>查看插件目录</summary><code v-for="path in t.extension_paths" :key="path">{{path}}</code></details><div class="card-actions"><button :disabled="busy" @click="emit('edit-template',t)">编辑模板</button><button class="text-button danger-text" :disabled="busy" @click="emit('delete-template',t)">删除</button></div></article></div>
    <div v-if="!templates.length" class="panel empty"><div class="empty-symbol">＋</div><h2>先创建一份配置模板。</h2><p>可只配置代理、只配置插件，或两者一起保存。</p><button class="primary" :disabled="busy" @click="emit('create-template')">新建模板</button></div>
    <p v-else-if="!visibleTemplates.length" class="panel empty">没有匹配的模板。<button class="text-button" @click="search=''">清除搜索</button></p>
  </template>
  <template v-else>
    <section class="panel plugin-environment"><div><div class="eyebrow">STEP 01</div><h2>选择 Manager 环境</h2><p>插件将安装到所选环境的 Docker 数据目录。</p></div><label>目标环境<select :value="selected" :disabled="busy" @change="emit('select-environment',($event.target as HTMLSelectElement).value)"><option value="native">选择一个 Manager 环境</option><option v-for="e in environments" :key="e.key" :value="e.key">{{e.host}} / {{e.name}}</option></select></label></section>
    <div v-if="!environments.length" class="panel empty"><h2>还没有 Manager 环境。</h2><p>本机浏览器直接使用模板中的插件目录，无需在这里安装。</p><button @click="emit('deploy')">去部署 Cloak Manager →</button></div>
    <p v-else-if="selected==='native'" class="configuration-hint">选择环境后即可安装、查看和下发插件。本机原生浏览器请在配置模板中填写本机插件目录。</p>
    <div v-else class="plugin-management-grid">
      <section class="panel plugin-install"><div class="eyebrow">STEP 02</div><h2>安装插件</h2><p>将控制台所在机器的解包目录复制到 Manager。</p><form @submit.prevent="emit('install')"><label>插件解包目录<input :value="pluginPath" @input="emit('update:pluginPath',($event.target as HTMLInputElement).value)" required placeholder="完整目录路径，需包含 MV3 manifest.json"></label><p class="fine">使用解压后的 MV3 插件，目录中需包含 manifest.json。</p><button class="primary" :disabled="busy||!pluginPath.trim()">安装到此环境</button></form><div class="configuration-hint"><b>安装后如何使用？</b><p>新实例：把容器内插件目录保存到配置模板。已有实例：在右侧选择插件并下发。</p></div></section>
      <section class="panel plugin-inventory"><div class="card-head"><div><div class="eyebrow">STEP 03</div><h2>已安装插件 <span class="inventory-count">{{plugins.length}}</span></h2></div><button class="text-button" :disabled="busy" @click="emit('refresh')">↻ 刷新</button></div><article v-for="p in plugins" :key="p.dirname" class="plugin-item"><div><b>{{p.name||p.dirname}}</b><small>{{p.version||'版本未提供'}}</small></div><code>{{p.container_path}}</code></article><div v-if="!plugins.length" class="plugin-empty"><p>此环境还没有插件。先在左侧安装。</p></div><div class="card-actions plugin-actions"><button class="primary" :disabled="busy||!plugins.length" @click="emit('apply')">选择插件并下发</button><button :disabled="busy" @click="emit('verify')">检查运行中实例</button></div><p class="fine">下发前会显示影响范围。运行中实例的检查结果显示在页面下方操作日志中。</p></section>
    </div>
  </template>
</template>
