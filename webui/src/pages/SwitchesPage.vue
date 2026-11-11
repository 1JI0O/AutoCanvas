<script setup lang="ts">
import { computed } from "vue";
import { useApiQuery as query, useAction } from "../queries";

const { action, busy } = useAction();
const automation = query("automation", "/api/automation", 3000);
const attendance = query("attendance", "/api/attendance", 3000);
const reminders = query("reminders", "/api/reminders", 5000);
const settings = query("settings", "/api/settings", 5000);
const courses = query("courses", "/api/courses");
const courseName = (id: string) =>
  courses.data.value?.find((c: { id: string }) => c.id === id)?.name || id;
const signingScope = computed(
  () =>
    (attendance.data.value?.config?.course_ids || [])
      .map(courseName)
      .join("、") || "未选择课程，不会自动签到",
);
const reminderScope = computed(
  () =>
    Object.entries(reminders.data.value?.config?.courses || {})
      .filter(([, row]) => (row as { enabled: boolean }).enabled)
      .map(([id]) => courseName(id))
      .join("、") || "未选择课程，不会检测或发提醒",
);
const processingFlags = [
  {
    key: "auto_asr",
    title: "自动转写回放",
    detail:
      "全局自动调度运行时，为已启用课程安排回放语音转写；需要预先部署语音模型。",
  },
  {
    key: "auto_slides",
    title: "自动抽取回放 Slides",
    detail: "全局自动调度运行时，为已启用课程安排画面提取，会生成图片文件。",
  },
  {
    key: "auto_live",
    title: "自动直播语音监听",
    detail:
      "全局自动调度运行时，安排直播语音转写与关键词识别；与二维码自动签到分开控制。",
  },
];
async function toggleSigning(value: string | number | boolean) {
  if (!attendance.data.value) return;
  await action("/api/attendance", { enabled: Boolean(value) }, "PATCH");
}
async function toggleReminders(value: string | number | boolean) {
  const data = reminders.data.value;
  if (!data) return;
  await action(
    "/api/reminders",
    {
      section: "courses",
      values: { ...data.config, enabled: Boolean(value) },
      revision: data.revision,
    },
    "PATCH",
  );
}
async function toggleMail(value: string | number | boolean) {
  const data = reminders.data.value;
  if (!data) return;
  await action(
    "/api/reminders",
    {
      section: "smtp",
      values: { ...data.smtp, enabled: Boolean(value), password: "" },
      revision: data.smtp_revision,
    },
    "PATCH",
  );
}
async function toggleSetting(key: string, value: string | number | boolean) {
  if (!settings.data.value) return;
  await action(
    "/api/settings",
    {
      values: { [key]: Boolean(value) },
      revision: settings.data.value.revision,
    },
    "PATCH",
  );
}
</script>

<template>
  <p class="muted">
    开关修改后立即保存。自动扫码签到、检测提醒和邮件发送独立控制；关闭全局自动调度不会关闭它们。
  </p>
  <section class="panel switch-panel">
    <h2>直播签到与提醒</h2>
    <div class="switch-row">
      <div>
        <h3>自动扫码签到</h3>
        <p>允许识别到有效直播签到码后向学校提交。不受全局自动调度暂停影响。</p>
        <p>提交范围：{{ signingScope }}</p>
        <RouterLink to="/classroom-events"
          >配置允许自动提交的课程与查看结果 →</RouterLink
        >
      </div>
      <el-switch
        :model-value="!!attendance.data.value?.config?.enabled"
        :disabled="busy || !attendance.data.value || attendance.isError.value"
        :loading="busy"
        aria-label="自动扫码签到"
        @change="toggleSigning"
      />
    </div>
    <div class="switch-row">
      <div>
        <h3>直播签到检测与提醒</h3>
        <p>对选定课程识别签到二维码，保存截图与记录，并为收件人生成提醒。</p>
        <p>提醒范围：{{ reminderScope }}</p>
        <RouterLink to="/reminders">配置提醒课程与收件人 →</RouterLink>
      </div>
      <el-switch
        :model-value="!!reminders.data.value?.config?.enabled"
        :disabled="busy || !reminders.data.value || reminders.isError.value"
        :loading="busy"
        aria-label="直播签到检测与提醒"
        @change="toggleReminders"
      />
    </div>
    <div class="switch-row">
      <div>
        <h3>邮件发送</h3>
        <p>
          关闭后继续识别与留证，待发邮件最多保留十分钟。重新开启后过期提醒不会补发。
        </p>
        <RouterLink to="/reminders">配置邮件服务与查看发送记录 →</RouterLink>
      </div>
      <el-switch
        :model-value="!!reminders.data.value?.smtp?.enabled"
        :disabled="busy || !reminders.data.value || reminders.isError.value"
        :loading="busy"
        aria-label="邮件发送"
        @change="toggleMail"
      />
    </div>
  </section>
  <section class="panel switch-panel">
    <h2>作业同步与附件</h2>
    <div class="switch-row">
      <div>
        <h3>自动同步作业</h3>
        <p>
          默认关闭。开启且全局自动调度运行时，按作业同步间隔更新正文与信息；关闭后课程同步也不会附带同步作业，手动“立即刷新”仍可使用。
        </p>
        <p v-if="automation.data.value?.paused" class="paused-note">
          全局自动调度已暂停，开启此项只保存设置，恢复全局调度后才会自动同步。
        </p>
        <RouterLink to="/homework">查看作业与手动刷新 →</RouterLink>
      </div>
      <el-switch
        :model-value="!!settings.data.value?.current?.auto_assignments"
        :disabled="busy || !settings.data.value || settings.isError.value"
        :loading="busy"
        aria-label="自动同步作业"
        @change="
          (v: string | number | boolean) => toggleSetting('auto_assignments', v)
        "
      />
    </div>
    <div class="switch-row">
      <div>
        <h3>下载作业附件</h3>
        <p>
          默认关闭。关闭时手动刷新和自动同步只保存作业正文与信息；已有附件保留。当前正在下载的单个附件可能完成，后续附件会跳过。
        </p>
        <RouterLink to="/homework">查看作业 →</RouterLink>
      </div>
      <el-switch
        :model-value="
          !!settings.data.value?.current?.download_assignment_attachments
        "
        :disabled="busy || !settings.data.value || settings.isError.value"
        :loading="busy"
        aria-label="下载作业附件"
        @change="
          (v: string | number | boolean) =>
            toggleSetting('download_assignment_attachments', v)
        "
      />
    </div>
  </section>
  <section class="panel switch-panel">
    <h2>媒体与同步自动调度</h2>
    <div class="switch-row">
      <div>
        <h3>启用全局自动调度</h3>
        <p>
          恢复后会自动同步已启用课程和视频列表，并按设置安排媒体处理。作业是否自动同步、附件是否下载由上方两个独立开关决定。
        </p>
        <p>
          暂停后停止派发新的自动任务，已开始的任务可能继续；自动扫码签到和邮件提醒保持各自状态。
        </p>
      </div>
      <el-switch
        :model-value="!!automation.data.value && !automation.data.value.paused"
        :disabled="busy || !automation.data.value || automation.isError.value"
        :loading="busy"
        aria-label="启用全局自动调度"
        @change="
          (v: string | number | boolean) =>
            action('/api/automation', { paused: !v })
        "
      />
    </div>
    <div v-for="flag in processingFlags" :key="flag.key" class="switch-row">
      <div>
        <h3>{{ flag.title }}</h3>
        <p>{{ flag.detail }}</p>
        <p v-if="automation.data.value?.paused" class="paused-note">
          全局自动调度已暂停，此设置会保存，恢复调度后才会自动安排。单课规则可在课程页面覆盖。
        </p>
      </div>
      <el-switch
        :model-value="!!settings.data.value?.current?.[flag.key]"
        :disabled="busy || !settings.data.value || settings.isError.value"
        :loading="busy"
        :aria-label="flag.title"
        @change="(v: string | number | boolean) => toggleSetting(flag.key, v)"
      />
    </div>
    <p class="muted">
      媒体开关是全局默认值，已有单课规则仍然生效。<RouterLink to="/courses"
        >查看单课规则</RouterLink
      >
      · <RouterLink to="/settings">全部参数设置</RouterLink>
    </p>
  </section>
</template>

<style scoped>
.switch-panel {
  margin-top: 22px;
}
.switch-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 28px;
  padding: 20px 0;
  border-bottom: 1px solid var(--border, #e5e7eb);
}
.switch-row:last-child {
  border-bottom: 0;
}
.switch-row h3 {
  margin: 0 0 7px;
}
.switch-row p {
  margin: 6px 0;
  color: var(--muted, #64748b);
  line-height: 1.65;
}
.switch-row .el-switch {
  flex-shrink: 0;
}
.paused-note {
  font-size: 13px;
}
</style>
