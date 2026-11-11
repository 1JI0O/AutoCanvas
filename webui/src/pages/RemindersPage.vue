<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";
import { api, fileUrl } from "../api";
import { useApiQuery } from "../queries";
import { useWorkspace } from "../useWorkspace";
import { date } from "../format";

const feed = useApiQuery("reminders", "/api/reminders", 5000);
const attendance = useApiQuery("attendance", "/api/attendance", 5000);
const { courses, courseName } = useWorkspace();
const enabled = ref(false),
  pollSeconds = ref(60),
  revision = ref<string | null>(null);
const smtpRevision = ref<string | null>(null),
  busy = ref(false);
const rules = ref<
  Record<string, { name: string; enabled: boolean; recipients: string }>
>({});
const smtp = ref({
  enabled: false,
  host: "smtp.163.com",
  port: 465,
  security: "ssl",
  user: "",
  password: "",
});
function load(section: "courses" | "smtp") {
  const data = feed.data.value;
  if (!data) return;
  if (section === "courses") {
    enabled.value = data.config.enabled;
    pollSeconds.value = data.config.poll_seconds;
    rules.value = Object.fromEntries(
      Object.entries(data.config.courses).map(([id, raw]) => {
        const row = raw as {
          name: string;
          enabled: boolean;
          recipients: string[];
        };
        return [id, { ...row, recipients: row.recipients.join("\n") }];
      }),
    );
    revision.value = data.revision;
  } else {
    smtp.value = { ...data.smtp, password: "" };
    smtpRevision.value = data.smtp_revision;
  }
}
watch(
  feed.data,
  (data) => {
    if (data && revision.value === null) load("courses");
    if (data && smtpRevision.value === null) load("smtp");
  },
  { immediate: true },
);
watch(
  courses,
  (items) => {
    for (const c of items)
      if (!rules.value[c.id])
        rules.value[c.id] = { name: c.name, enabled: false, recipients: "" };
  },
  { immediate: true },
);
const rows = computed(() => {
  const all = { ...rules.value };
  for (const c of courses.value)
    if (!all[c.id])
      all[c.id] = { name: c.name, enabled: false, recipients: "" };
  return Object.entries(all).map(([id, row]) => ({ id, ...row }));
});
function rule(id: string) {
  if (!rules.value[id])
    rules.value[id] = { name: courseName(id), enabled: false, recipients: "" };
  return rules.value[id]!;
}
async function save(section: "courses" | "smtp") {
  busy.value = true;
  try {
    const values =
      section === "smtp"
        ? smtp.value
        : {
            enabled: enabled.value,
            poll_seconds: pollSeconds.value,
            courses: Object.fromEntries(
              Object.entries(rules.value).map(([id, row]) => [
                id,
                {
                  name: row.name,
                  enabled: row.enabled,
                  recipients: row.recipients
                    .split(/[\s,，;；]+/)
                    .map((v) => v.trim())
                    .filter(Boolean),
                },
              ]),
            ),
          };
    await api("/api/reminders", "PATCH", {
      section,
      values,
      revision: section === "smtp" ? smtpRevision.value : revision.value,
    });
    await feed.refetch();
    load(section);
    ElMessage.success("已写入配置文件，自动生效");
  } catch (error) {
    ElMessage.error((error as Error).message);
  } finally {
    busy.value = false;
  }
}
async function test(courseId: string) {
  busy.value = true;
  try {
    await api("/api/reminders/test", "POST", { course_id: courseId });
    await feed.refetch();
    ElMessage.success("测试邮件已加入队列，请查看发送结果和收件箱");
  } catch (error) {
    ElMessage.error((error as Error).message);
  } finally {
    busy.value = false;
  }
}
const states: Record<string, string> = {
  pending: "等待发送",
  sending: "发送中",
  sent: "邮件服务已接受",
  failed: "邮件服务拒绝",
  unknown: "结果不确定，未重发",
  cancelled: "已取消",
  expired: "提醒已过期",
};
</script>

<template>
  <el-alert
    title="只检测直播签到二维码、记录并发送邮件；提醒开关独立于全局媒体自动化暂停。当前环境未启用语音关键词识别。"
    type="info"
    :closable="false"
  />
  <el-alert
    v-if="attendance.data.value?.config?.enabled"
    title="自动扫码签到目前已开启；如需仅提醒，请在“点名与二维码”关闭自动签到。"
    type="warning"
    :closable="false"
  />
  <el-alert
    v-else
    title="自动扫码签到已关闭"
    type="success"
    :closable="false"
  />
  <el-alert
    v-if="feed.isError.value"
    title="无法读取提醒配置；请检查配置文件，修改后点击重新载入。"
    type="error"
    :closable="false"
  />
  <section class="panel reminder-panel">
    <div class="workspace-toolbar">
      <div>
        <h2>课程与收件人</h2>
        <p class="muted">
          只有明确开启的课程会连接直播；空列表不检测任何课程。每门课程可填多个邮箱。
        </p>
      </div>
      <el-switch
        v-model="enabled"
        active-text="启用直播检测与提醒"
        aria-label="启用直播检测与提醒"
      />
    </div>
    <p
      v-if="feed.data.value && revision !== feed.data.value.revision"
      class="muted"
    >
      配置文件已变化，重新载入后可编辑最新配置。
    </p>
    <el-table :data="rows" empty-text="请先同步课程">
      <el-table-column label="课程" min-width="200"
        ><template #default="{ row }"
          >{{ row.name }}<br /><small class="muted">{{
            row.id
          }}</small></template
        ></el-table-column
      >
      <el-table-column label="是否提醒" width="115"
        ><template #default="{ row }"
          ><el-switch
            :model-value="row.enabled"
            :aria-label="row.name + '提醒'"
            @change="
              (v: string | number | boolean) =>
                (rule(row.id).enabled = Boolean(v))
            " /></template
      ></el-table-column>
      <el-table-column
        label="收件人（每行一个，也可用逗号分隔）"
        min-width="260"
        ><template #default="{ row }"
          ><el-input
            :model-value="row.recipients"
            type="textarea"
            :rows="2"
            :aria-label="row.name + '收件人'"
            placeholder="name@example.com"
            @update:model-value="
              (v: string) => (rule(row.id).recipients = v)
            " /></template
      ></el-table-column>
      <el-table-column label="验证" width="130"
        ><template #default="{ row }"
          ><el-button
            :disabled="
              busy ||
              !feed.data.value?.config?.enabled ||
              !feed.data.value?.config?.courses?.[row.id]?.enabled
            "
            @click="test(row.id)"
            >发测试邮件</el-button
          ></template
        ></el-table-column
      >
    </el-table>
    <div class="toolbar reminder-actions">
      <label
        >直播列表轮询间隔（秒）
        <el-input-number v-model="pollSeconds" :min="30" :max="3600"
      /></label>
      <el-button
        type="primary"
        :loading="busy"
        :disabled="revision === null"
        @click="save('courses')"
        >保存课程提醒</el-button
      >
      <el-button
        :disabled="busy"
        @click="
          async () => {
            await feed.refetch();
            load('courses');
          }
        "
        >重新载入课程配置</el-button
      >
    </div>
    <p class="muted">
      直播在正式上课后开始抽帧，每 0.5
      秒一次。同一课次同一次签到只记录一条、每个收件人提醒一次；二维码轮换不会重复发信。
    </p>
    <p class="muted">
      配置文件：<code>{{ feed.data.value?.paths?.courses }}</code>
    </p>
  </section>
  <section class="panel reminder-panel">
    <h2>邮件服务</h2>
    <el-form label-position="top">
      <div class="smtp-grid">
        <el-form-item label="SMTP 主机"
          ><el-input v-model="smtp.host"
        /></el-form-item>
        <el-form-item label="端口"
          ><el-input-number v-model="smtp.port" :min="1" :max="65535"
        /></el-form-item>
        <el-form-item label="加密"
          ><el-select v-model="smtp.security"
            ><el-option label="SSL / TLS（465）" value="ssl" /><el-option
              label="STARTTLS（587）"
              value="starttls" /></el-select
        ></el-form-item>
        <el-form-item label="发件邮箱 / 用户名"
          ><el-input v-model="smtp.user" autocomplete="off"
        /></el-form-item>
        <el-form-item label="SMTP 授权码"
          ><el-input
            v-model="smtp.password"
            type="password"
            autocomplete="new-password"
            :placeholder="
              feed.data.value?.has_password
                ? '已保存，留空保留原授权码'
                : '请填写授权码'
            "
        /></el-form-item>
      </div>
      <div class="toolbar">
        <el-switch
          v-model="smtp.enabled"
          active-text="启用邮件发送"
        /><el-button
          type="primary"
          :loading="busy"
          :disabled="smtpRevision === null"
          @click="save('smtp')"
          >保存邮件配置</el-button
        ><el-button
          :disabled="busy"
          @click="
            async () => {
              await feed.refetch();
              load('smtp');
            }
          "
          >重新载入邮件配置</el-button
        >
      </div>
    </el-form>
    <p class="muted">
      使用邮箱提供商的 SMTP
      服务，无需搭建邮件服务器。授权码不会由接口返回；只保存于服务器私有文件。
    </p>
    <p class="muted">
      配置文件：<code>{{ feed.data.value?.paths?.smtp }}</code>
    </p>
  </section>
  <section class="panel reminder-panel">
    <h2>直播发现状态</h2>
    <el-table
      :data="feed.data.value?.discovery || []"
      empty-text="等待下一轮已选课程直播查询"
    >
      <el-table-column label="课程" min-width="180"
        ><template #default="{ row }">{{
          courseName(row.course_id)
        }}</template></el-table-column
      >
      <el-table-column label="最近查询" min-width="170"
        ><template #default="{ row }">{{
          date(row.at)
        }}</template></el-table-column
      >
      <el-table-column prop="status" label="结果" />
      <el-table-column prop="live_count" label="直播课次数" />
    </el-table>
    <p
      v-for="v in attendance.data.value?.views || []"
      :key="v.course_id + ':' + v.lecture_id + ':' + v.view"
      class="muted"
    >
      {{ courseName(v.course_id) }} · 画面 {{ v.view }} · {{ v.status }} ·
      最近画面 {{ date(v.last_frame) }} · 解码 {{ v.decode_ms ?? "—" }} ms
    </p>
    <el-alert
      v-if="attendance.data.value?.error"
      :title="attendance.data.value.error"
      type="warning"
      :closable="false"
    />
  </section>
  <section class="panel reminder-panel">
    <h2>签到识别记录</h2>
    <el-table
      :data="feed.data.value?.detections || []"
      max-height="450"
      empty-text="尚未识别到直播签到二维码"
    >
      <el-table-column label="课程" min-width="180"
        ><template #default="{ row }">{{
          courseName(row.course_id)
        }}</template></el-table-column
      >
      <el-table-column label="识别时间" min-width="170"
        ><template #default="{ row }">{{
          date(row.at)
        }}</template></el-table-column
      >
      <el-table-column prop="lecture_id" label="课次" /><el-table-column
        prop="history"
        label="签到编号"
      />
      <el-table-column label="证据" width="100"
        ><template #default="{ row }"
          ><a
            v-if="row.image"
            :href="fileUrl(row.image)"
            target="_blank"
            rel="noopener"
            >查看截图 ↗</a
          ></template
        ></el-table-column
      >
    </el-table>
  </section>
  <section class="panel reminder-panel">
    <h2>邮件发送记录</h2>
    <el-table
      :data="feed.data.value?.deliveries || []"
      max-height="450"
      empty-text="尚未发送提醒或测试邮件"
    >
      <el-table-column label="课程" min-width="180"
        ><template #default="{ row }"
          >{{ courseName(row.course_id) }}
          <el-tag v-if="row.type === 'test'">测试</el-tag></template
        ></el-table-column
      >
      <el-table-column prop="recipient" label="收件人" min-width="200" />
      <el-table-column label="时间" min-width="170"
        ><template #default="{ row }">{{
          date(row.updated || row.at)
        }}</template></el-table-column
      >
      <el-table-column label="结果" min-width="180"
        ><template #default="{ row }">{{
          states[row.status] || row.status
        }}</template></el-table-column
      >
      <el-table-column prop="detail" label="详情" min-width="240" />
    </el-table>
    <p class="muted">
      SMTP
      接受不等于已进收件箱，请同时检查垃圾邮件。发送结果不确定时不会自动重发；测试邮件可用于核对配置。
    </p>
  </section>
</template>

<style scoped>
.reminder-panel {
  margin-top: 20px;
}
.reminder-actions {
  margin-top: 18px;
}
.smtp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0 20px;
}
code {
  overflow-wrap: anywhere;
}
</style>
