<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { douyinApi } from '@/api'
import type { DouyinAccount } from '@/types'
import { formatDateTime, formatRelative } from '@/utils/format'

const account = ref<DouyinAccount | null>(null)
const loading = ref(false)
const loggingIn = ref(false)
const checking = ref(false)
const qrcodeBust = ref(Date.now())
const qrcodeVisible = ref(true)

let poller: number | null = null

const session = computed(() => account.value?.login_session ?? null)
const isDouyin = computed(() => account.value?.provider === 'douyin')

const statusMeta = computed(() => {
  switch (account.value?.status) {
    case 'logged_in':
      return { label: '登录态有效', type: 'success' as const }
    case 'expired':
      return { label: '登录态已失效', type: 'danger' as const }
    default:
      return { label: '未登录', type: 'info' as const }
  }
})

const qrcodeUrl = computed(() => `${douyinApi.qrcodeUrl()}&bust=${qrcodeBust.value}`)

async function load() {
  loading.value = true
  try {
    account.value = await douyinApi.account()
    syncPolling()
  } catch {
    account.value = null
  } finally {
    loading.value = false
  }
}

function stopPolling() {
  if (poller) {
    window.clearInterval(poller)
    poller = null
  }
}

function startPolling() {
  if (poller) return
  poller = window.setInterval(async () => {
    try {
      const data = await douyinApi.loginStatus()
      if (account.value) {
        account.value = {
          ...account.value,
          status: (data.account_status as string) || account.value.status,
          login_session: data.login_session,
        }
      }
      const status = data?.login_session?.status
      if (status === 'success') {
        stopPolling()
        loggingIn.value = false
        ElMessage.success('登录成功，登录态已保存')
        await load()
      } else if (status === 'failed' || status === 'expired') {
        stopPolling()
        loggingIn.value = false
        ElMessage.error(data?.login_session?.message || '登录未完成，请重试')
      } else if (data?.login_session?.qrcode_ready) {
        // 二维码截图就绪后刷新一次展示
        qrcodeVisible.value = true
      }
    } catch {
      stopPolling()
      loggingIn.value = false
    }
  }, 2500)
}

function syncPolling() {
  if (account.value?.login_session?.running) {
    loggingIn.value = true
    startPolling()
  } else if (loggingIn.value) {
    stopPolling()
    loggingIn.value = false
  }
}

async function handleLogin() {
  try {
    const result = await douyinApi.login()
    ElMessage.success(result.message)
    loggingIn.value = true
    qrcodeBust.value = Date.now()
    if (account.value) {
      account.value.login_session = {
        ...account.value.login_session,
        status: 'waiting',
        running: true,
        message: '等待扫码登录…',
      }
    }
    startPolling()
  } catch {
    /* 拦截器已提示 */
  }
}

async function handleCheck() {
  checking.value = true
  try {
    const result = await douyinApi.check()
    if (result.ok) {
      ElMessage.success(result.message)
    } else {
      ElMessage.warning(result.message)
    }
    await load()
  } catch {
    /* 拦截器已提示 */
  } finally {
    checking.value = false
  }
}

async function handleLogout() {
  try {
    await ElMessageBox.confirm(
      '退出后将清除本地保存的登录态，下次发布需要重新扫码。确定继续吗？',
      '退出登录',
      { type: 'warning', confirmButtonText: '退出登录', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await douyinApi.logout()
    ElMessage.success(result.message)
    await load()
  } catch {
    /* 拦截器已提示 */
  }
}

function refreshQrcode() {
  qrcodeBust.value = Date.now()
  qrcodeVisible.value = true
}

onMounted(load)

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">抖音账号</h2>
        <p class="page-subtitle">
          扫码登录抖音创作者中心，登录态会保存在本机，后续发布无需重复登录。
        </p>
      </div>
      <div class="head-actions">
        <el-button :icon="'Refresh'" :loading="checking" @click="handleCheck">检查登录态</el-button>
        <el-button type="primary" :icon="'Iphone'" :loading="loggingIn" @click="handleLogin">
          扫码登录
        </el-button>
        <el-button :icon="'SwitchButton'" @click="handleLogout">退出登录</el-button>
      </div>
    </div>

    <el-alert
      v-if="account && !isDouyin"
      type="warning"
      show-icon
      :closable="false"
      style="margin-bottom: 16px"
      title="当前为 Mock 发布模式，不会真实上传到抖音"
      description="请到「系统配置 → 发布」把发布服务改为「抖音（真实发布）」，并确保已安装 playwright 与 chromium。Mock 模式用于在没有账号的情况下验证整条流水线。"
    />

    <el-alert
      v-if="session?.running"
      type="info"
      show-icon
      :closable="false"
      style="margin-bottom: 16px"
      title="已打开登录浏览器窗口，请使用抖音 App 扫码"
      :description="session.message || '扫码完成后本页面会自动检测到登录状态，无需手动刷新。'"
    />

    <div class="two-col">
      <div class="panel">
        <div class="panel-title">账号状态</div>

        <div v-if="account" class="status-row">
          <el-tag :type="statusMeta.type" effect="dark" size="large">{{ statusMeta.label }}</el-tag>
          <el-tag v-if="isDouyin" size="small" effect="plain">真实发布</el-tag>
          <el-tag v-else size="small" effect="plain" type="warning">Mock 模式</el-tag>
        </div>

        <el-descriptions v-if="account" :column="1" size="small" border style="margin-top: 16px">
          <el-descriptions-item label="账号名称">{{ account.nickname || account.name }}</el-descriptions-item>
          <el-descriptions-item label="登录态文件">
            <span class="mono">{{ account.storage_state_path }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="文件是否存在">
            <el-tag :type="account.storage_state_exists ? 'success' : 'info'" size="small" effect="plain">
              {{ account.storage_state_exists ? '已保存' : '不存在' }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="上次登录">
            {{ formatDateTime(account.last_login_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="上次检查">
            {{ formatRelative(account.last_check_at) }}
          </el-descriptions-item>
          <el-descriptions-item label="登录会话">
            {{ session?.status || '-' }}
            <span v-if="session?.message" class="muted">· {{ session.message }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <el-empty v-else description="账号信息加载失败" :image-size="80" />
      </div>

      <div class="panel">
        <div class="panel-title">
          <span>登录二维码</span>
          <el-button v-if="session?.qrcode_ready" text size="small" :icon="'Refresh'" @click="refreshQrcode">
            刷新截图
          </el-button>
        </div>

        <div v-if="session?.running || session?.qrcode_ready" class="qrcode-box">
          <img
            v-if="qrcodeVisible"
            :src="qrcodeUrl"
            alt="抖音登录二维码"
            class="qrcode-img"
            @error="qrcodeVisible = false"
          />
          <div v-else class="qrcode-placeholder muted">
            截图暂不可用，请直接在弹出的浏览器窗口中扫码
          </div>
          <div class="muted qrcode-tip">
            这是登录浏览器窗口的截图。若在远程/无显示器环境运行，可扫描此图中的二维码完成登录。
          </div>
        </div>

        <el-empty
          v-else
          description="点击右上角「扫码登录」后，这里会显示二维码"
          :image-size="80"
        />
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">发布须知</div>
      <ol class="notice-list">
        <li>
          <strong>首次登录需扫码。</strong>点击「扫码登录」后，本机将弹出一个真实的 Chromium 窗口，
          请使用抖音 App 扫码。登录态保存在 <span class="mono">data/auth/douyin_default.json</span>，之后无需重复登录。
        </li>
        <li>
          <strong>自动化而非官方接口。</strong>抖音没有面向个人创作者的官方开放 API，本功能基于创作者中心
          （creator.douyin.com）的浏览器自动化实现。平台改版可能导致选择器失效，请留意发布失败的日志。
          若长期失败，可在 <span class="mono">backend/app/providers/publisher/douyin.py</span>
          顶部的 <span class="mono">*_SELECTORS</span> 常量中更新选择器。
        </li>
        <li>
          <strong>控制发布频率。</strong>短时间内大量上传容易被风控。建议单账号每天不超过 3-5 条，
          并在「系统配置 → 发布」中启用定时发布做分流。
        </li>
        <li>
          <strong>版权与合规。</strong>搬运他人视频存在版权风险，请确认已获得授权，或内容本身符合平台的转载与合理使用规则。
          建议在作品简介中保留来源信息。
        </li>
        <li>
          <strong>AI 生成声明。</strong>本流水线包含 AI 配音与 AI 字幕，发布时会尝试在「作品声明」中如实勾选
          「内容由AI生成」。若自动勾选失败，请到抖音后台手动补充。
        </li>
      </ol>
    </div>
  </div>
</template>

<style scoped>
.head-actions {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.two-col {
  display: grid;
  grid-template-columns: 1.15fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}

@media (max-width: 1080px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}

.status-row {
  display: flex;
  align-items: center;
  gap: 10px;
}

.qrcode-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.qrcode-img {
  width: 240px;
  border: 1px solid var(--spark-border);
  border-radius: 10px;
  background: #fff;
  padding: 8px;
}

.qrcode-placeholder {
  width: 240px;
  height: 240px;
  border: 1px dashed var(--spark-border);
  border-radius: 10px;
  display: grid;
  place-items: center;
  text-align: center;
  font-size: 12.5px;
  padding: 16px;
}

.qrcode-tip {
  font-size: 12px;
  text-align: center;
  max-width: 320px;
  line-height: 1.6;
}

.notice-list {
  margin: 0;
  padding-left: 20px;
  line-height: 1.95;
  font-size: 13px;
  color: #33383f;
}

.notice-list li + li {
  margin-top: 6px;
}
</style>
