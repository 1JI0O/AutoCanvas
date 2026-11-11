<script setup lang="ts">
import { computed, ref, watch } from "vue";

import { useApiQuery as query, useAction } from "../queries";

const { action, busy } = useAction();
const config = query("settings", "/api/settings", 5000);
const draft = ref<Record<string, any>>({}),
  revision = ref(0),
  dirty = ref(false);
watch(
  config.data,
  (value) => {
    if (value && !dirty.value) {
      draft.value = JSON.parse(JSON.stringify(value.desired));
      revision.value = value.revision;
    }
  },
  { immediate: true },
);
function editList(key: string, value: string) {
  draft.value[key] = value
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  dirty.value = true;
}
async function saveSettings() {
  const result = await action(
    "/api/settings",
    { values: draft.value, revision: revision.value },
    "PATCH",
  );
  if (result) {
    dirty.value = false;
    draft.value = JSON.parse(JSON.stringify(result.desired));
    revision.value = result.revision;
  }
}
const groups = computed<string[]>(() => [
  ...new Set<string>(
    (config.data.value?.schema || []).map((f: any) => f.group),
  ),
]);
async function reloadSettings() {
  dirty.value = false;
  const result = await config.refetch();
  if (result.data) {
    draft.value = JSON.parse(JSON.stringify(result.data.desired));
    revision.value = result.data.revision;
  }
}
</script>
<template>
  <div class="settings-note">
    配置保存到当前运行目录。处理参数会等正在运行的任务结束后应用；监听地址、端口和数据目录需重启服务。修改数据目录不会迁移已有文件。
    作业附件默认不下载，可在下方“作业”独立开启；关闭后保留已有文件，当前正在下载的单个附件可能完成，后续附件会跳过。
  </div>
  <el-alert
    v-if="config.data.value?.restart_required.length"
    :title="'等待重启：' + config.data.value.restart_required.join('、')"
    type="warning"
    :closable="false"
  /><el-alert
    v-if="config.data.value?.waiting_for_idle.length"
    :title="'等待空闲：' + config.data.value.waiting_for_idle.join('、')"
    type="info"
    :closable="false"
  />
  <section v-for="group in groups" :key="group" class="panel">
    <h2>{{ group }}</h2>
    <div class="settings-grid">
      <div
        v-for="field in config.data.value.schema.filter(
          (f: any) => f.group === group,
        )"
        :key="field.key"
        class="setting"
      >
        <label :for="'setting-' + field.key"
          >{{ field.label
          }}<small>{{
            { restart: "重启生效", idle: "空闲后生效", immediate: "即时生效" }[
              field.apply as string
            ]
          }}</small></label
        ><el-switch
          v-if="field.type === 'boolean'"
          :id="'setting-' + field.key"
          v-model="draft[field.key]"
          @change="dirty = true"
        /><el-input-number
          v-else-if="field.type === 'number'"
          :id="'setting-' + field.key"
          v-model="draft[field.key]"
          :controls="false"
          @change="dirty = true"
        /><el-input
          v-else-if="field.type === 'list'"
          :id="'setting-' + field.key"
          :model-value="(draft[field.key] || []).join('\n')"
          type="textarea"
          :rows="3"
          placeholder="每行一项"
          @update:model-value="(v: string) => editList(field.key, v)"
        /><el-input
          v-else
          :id="'setting-' + field.key"
          v-model="draft[field.key]"
          @input="dirty = true"
        />
        <small
          class="subtext"
          v-if="
            JSON.stringify(draft[field.key]) !==
            JSON.stringify(config.data.value.current[field.key])
          "
          >运行中：{{
            Array.isArray(config.data.value.current[field.key])
              ? config.data.value.current[field.key].join("、")
              : config.data.value.current[field.key]
          }}</small
        >
      </div>
    </div>
  </section>
  <div class="save-bar">
    <el-button @click="reloadSettings">重新加载设置</el-button>
    <span>{{ dirty ? "有未保存的修改" : "设置已与服务同步" }}</span
    ><el-button
      type="primary"
      :loading="busy"
      :disabled="!dirty"
      @click="saveSettings"
      >保存全部设置</el-button
    >
  </div>
</template>
