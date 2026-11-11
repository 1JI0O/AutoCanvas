<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { ElMessage, vLoading } from "element-plus";
import { api } from "../api";
import { date } from "../format";
import { useApiQuery } from "../queries";

const settings = useApiQuery("settings", "/api/settings", 5000);

const cache = useQueryClient();
const assignments = useQuery<any[]>({
  queryKey: ["assignments"],
  queryFn: () => api("/api/assignments"),
  refetchInterval: 10000,
  retry: false,
});
const refreshingAssignments = ref(false),
  assignmentRunIds = ref<string[]>([]);
const assignmentRuns = useQuery<any[]>({
  queryKey: ["assignment-refresh"],
  queryFn: () => api("/api/executions"),
  enabled: computed(() => assignmentRunIds.value.length > 0),
  refetchInterval: 1000,
});
watch(assignmentRuns.data, (rows) => {
  const ids = assignmentRunIds.value;
  if (!ids.length || !rows) return;
  const done = ids.map((id) => rows.find((r) => r.id === id));
  if (done.some((r) => !r || ["pending", "running"].includes(r.status))) return;
  assignmentRunIds.value = [];
  refreshingAssignments.value = false;
  assignments.refetch();
  if (done.every((r) => r.status === "succeeded"))
    ElMessage.success("作业列表已更新");
  else
    ElMessage.error("部分课程同步未完成，请在执行记录查看原因或检查登录状态");
});
watch(assignmentRuns.error, (error) => {
  if (error && assignmentRunIds.value.length) {
    assignmentRunIds.value = [];
    refreshingAssignments.value = false;
    ElMessage.error("暂时无法读取同步进度，请在执行记录查看结果");
  }
});
async function refreshAssignments() {
  refreshingAssignments.value = true;
  try {
    const result = await api("/api/assignments/sync", "POST", {});
    if (!result.execution_ids.length) {
      refreshingAssignments.value = false;
      ElMessage.info("没有可同步的课程，请先同步课表");
      return;
    }
    // Discard the previous result before tracking reused execution IDs.
    cache.removeQueries({ queryKey: ["assignment-refresh"] });
    assignmentRunIds.value = result.execution_ids;
  } catch (error) {
    refreshingAssignments.value = false;
    ElMessage.error((error as Error).message);
  }
}
const courses = useQuery<any[]>({
  queryKey: ["courses"],
  queryFn: () => api("/api/courses"),
  retry: false,
});
const search = ref("");
function courseName(id: unknown) {
  return (
    courses.data.value?.find((c) => String(c.id) === String(id))?.name ||
    String(id)
  );
}
const visible = computed(() =>
  (assignments.data.value || []).filter((a) =>
    `${a.name} ${courseName(a.course_id)}`
      .toLowerCase()
      .includes(search.value.toLowerCase()),
  ),
);
function safeLink(url: string) {
  try {
    const u = new URL(url);
    return ["http:", "https:"].includes(u.protocol) ? u.href : undefined;
  } catch {
    return undefined;
  }
}
</script>

<template>
  <p class="muted">
    {{
      settings.data.value
        ? settings.data.value.current.auto_assignments
          ? "作业自动同步已开启，需全局自动调度运行才会定期更新。"
          : "作业自动同步已关闭，仍可点击“立即刷新”手动更新。"
        : "正在读取作业同步设置…"
    }}
    <RouterLink to="/switches">管理作业同步开关</RouterLink>
  </p>
  <p class="muted">
    {{
      settings.data.value
        ? settings.data.value.current.download_assignment_attachments
          ? "附件下载已开启，刷新作业时会下载附件。"
          : "附件下载已关闭，刷新作业仅同步正文与信息；已有文件保留。"
        : "正在读取附件下载设置…"
    }}
    <RouterLink to="/settings">修改附件下载开关</RouterLink>
  </p>
  <div class="simple-toolbar">
    <el-input
      v-model="search"
      placeholder="搜索作业或课程"
      clearable
    /><el-button :loading="refreshingAssignments" @click="refreshAssignments">{{
      refreshingAssignments ? "正在同步作业" : "立即刷新"
    }}</el-button>
  </div>
  <el-alert
    v-if="assignments.isError.value"
    :title="assignments.error.value?.message"
    type="error"
    :closable="false"
  />
  <el-table
    v-else
    :data="visible"
    v-loading="assignments.isLoading.value"
    empty-text="暂无作业"
  >
    <el-table-column label="作业" min-width="240"
      ><template #default="{ row }"
        ><a
          v-if="safeLink(row.html_url)"
          :href="safeLink(row.html_url)"
          target="_blank"
          rel="noopener noreferrer"
          >{{ row.name }} ↗</a
        ><span v-else>{{ row.name }}</span></template
      ></el-table-column
    >
    <el-table-column label="课程" min-width="180"
      ><template #default="{ row }">{{
        courseName(row.course_id)
      }}</template></el-table-column
    >
    <el-table-column label="截止时间" min-width="180"
      ><template #default="{ row }">{{
        row.due_at ? date(row.due_at) : "未设置"
      }}</template></el-table-column
    >
  </el-table>
</template>

<style scoped>
.simple-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.simple-toolbar > .el-input {
  flex: 1;
}
@media (max-width: 900px) {
  .simple-toolbar {
    flex-wrap: wrap;
  }
}
</style>
