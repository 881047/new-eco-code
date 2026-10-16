<!-- 系统与邮件（运维组，2026-10-08 批4 新增）。
     合并原 StatusView（数据水位，45 行单表）与 NotifyView（邮件记录，39 行单表）——
     两页信息密度都极低，各自独占一个导航项不划算；合并后共用页头，区块各自保留 id
     （#status / #notify）以便今日行动中枢的深链锚点继续可用。

     为什么用"包住"而不是"删掉"：契约测试 REQUIRED_FILES 只增不删，
     删文件会红；且两个视图本身是独立可复用单元，保留文件、由本页组合更稳。 -->
<template>
  <div>
    <div class="page-title">系统与邮件</div>

    <StatusView embedded />
    <NotifyView embedded />
  </div>
</template>

<script>
import StatusView from './StatusView.vue'
import NotifyView from './NotifyView.vue'

export default {
  name: "OpsView",
  components: { StatusView, NotifyView },
  /* 数据加载留在被包住的视图自己的 mounted 里（各自 ensure 自己的域），
     本页不重复 ensure —— 避免与子视图双调。 */
}
</script>
