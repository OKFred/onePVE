# 可选SLS云端审计

入口：08 → 13「SLS云端审计」。默认关闭，只有明确配置已有目标、核实账号当月剩余免费额度并批准本宿主预算后才能启用。不创建云端资源，不自动切换计费模式，不安装额外SDK；本地72小时审计独立运行。

## 免费额度与费用边界

中国站**按使用功能计费**的官方免费额度如下，所有地域、Project共享主账号额度，非资源包抵扣时适用：

| 计费项 | 官方额度 |
|---|---|
| 热存储 | 500 MB／月 |
| 压缩后读写流量合计 | 500 MB／月 |
| Standard日志索引 | 500 MB／月 |
| 读写请求（成功、失败均计） | 100万次／月 |
| 活跃Shard | 31个×天／月 |

新Logstore默认的**按写入数据量计费，原始写入免费额度为零**；其中“30天免费”仅指存储权益。Query／Lite索引、外网读取、加工、投递、扫描、高级计算等有独立计费，不能套用上表。以[按功能计费表](https://help.aliyun.com/zh/sls/billable-items)和[按写入计费表](https://help.aliyun.com/zh/sls/billing-items-in-the-pay-per-data-write-mode)为准。

阿里云[不支持设置扣费上限](https://help.aliyun.com/zh/user-center/support/billing-faqs)，预算告警不是硬停，账单和用量统计有延迟。[免费额度由账号全部Project共享](https://help.aliyun.com/zh/sls/billing-overview/)，本工具不能证明全账号仍有额度，也不能限制其它主机、写入者或控制台操作。没有实际账号核验时保持关闭。

本工具默认每月100 MiB／100000次请求，上限200 MiB／100000次；按PB原始大小与压缩后大小的较大值扣字节。所有VM共用宿主预算，每次网络尝试**之前**加锁、持久预扣，失败／超时不退款，元数据检查也扣请求。VM删除／重建、轮换凭据、关闭再启用都不重置预算；同月不能补回或追加。预算损坏／缺失、超限、未审批新账期或月末最后5分钟时停止请求，不自动重试。新月需重新核实并审批；宿主root删除或回滚整份账本、宿主时钟错误不能靠本地软件防止。

这些限制只约束本工具，不构成零费用承诺。还需为现有Shard、存储、其他写入和读取预留额度。停止上传不等于停止已有云资源的全部费用；3天TTL内的存量仍占用存储。

## 目标与凭据

准备一个专用中国站Project／Logstore：Standard、`ChargeByFunction`、3天TTL、1个实际活跃Shard、关闭自动分裂和WebTracking、无索引／写入处理器／客户KMS配置。启用和每轮有数据时，程序只读检查上述配置及Project实际owner；不符合则停发，不擅自修改。元数据检查不能证明剩余免费额度。

当前采用无索引模式，主要用于异地保留；不自动做云端SQL扫描、拉取或建立索引。结构化查询需另行评估索引及读取费用；本地仍可导出CSV／JSON。上传包含已开启本地审计的工作机流元数据、VMID／owner、匿名宿主标识及稳定event_id；不上传凭据、Cookie、URL或报文内容。

凭据仅保存在PVE宿主的root专有JSON文件中，目录不可重定向，文件0600。不放到客机、仓库、命令行参数或聊天中。可用RAM专用账号或短期STS，JSON格式：

```json
{"access_key_id":"YOUR_RAM_ACCESS_KEY_ID","access_key_secret":"YOUR_RAM_ACCESS_KEY_SECRET"}
```

STS另加 `security_token`。推荐最小权限：`log:PostLogStoreLogs`、`log:GetLogStore`、`log:ListShards`、`log:GetIndex`限定目标Logstore；`log:GetProject`限定Project；`log:GetLogStoreMeteringMode`按官方权限表需要 `Resource: "*"`，但只授予这一项读取动作，不授予创建、修改、删除、查日志或全管理员权限。[PutLogs](https://help.aliyun.com/zh/sls/developer-reference/api-sls-2020-12-30-putlogs) · [GetProject](https://help.aliyun.com/zh/sls/developer-reference/api-sls-2020-12-30-getproject)

## 使用与恢复

替换真实账号ID、接入点、资源名及宿主凭据路径；本地配置命令不访问云端，始终关闭上传：

```bash
python3 onepve.py sls configure --account-id YOUR_16_DIGIT_ACCOUNT_ID \
  --endpoint cn-hangzhou.log.aliyuncs.com --project YOUR_PROJECT --logstore YOUR_LOGSTORE \
  --credential-file /root/onepve-sls-credentials.json
python3 onepve.py sls status
```

在费用中心核实**全账号**共享剩余额度，并给本宿主预留额度后，显式批准本月预算并启用：

```bash
python3 onepve.py sls approve-budget --raw-mib 100 --requests 100000 --account-free-verified
python3 onepve.py sls enable
python3 onepve.py sls status
python3 onepve.py sls disable
```

5分钟定时器轮询本地SQLite，只有数据变化才发请求；每次最多100条／64 KiB原始PB一批，每台工作机一轮最多一批，额外计入5个元数据检查请求。预算按北京时间自然月，不自动跨月重置。凭据过期、网络失败、云配置变化或预算问题会持久HALT；核实问题后显式 `enable` 才恢复。免密保存不提供；`disable`保留本地日志、云资源、游标和预算。

状态位于 `/var/lib/onepve/sls/`；预算在其 `budget/` 子目录，独立于VM状态。服务运行固定源码快照，不依赖原仓库位置。不要删除账本、游标或手工改摘要来恢复上传。初次本地配置中断可显式重试；已有游标丢失则拒绝自动重放。目标Project／Logstore变更需要单独迁移，不隐式清空游标或预算。

拉取新代码后，显式执行 `enable` 重新检查并更新服务源码快照；原预算不重置。可用 `journalctl -u onepve-sls-upload.service` 查看本地执行错误，`status`只读显示定时器、unit摘要、停止原因和剩余预算。

本地记录会原位更新，所以游标使用 `last_seen_utc,id`；同一流后续结束／快照仍会上传。POST前保存未确认批次，只有HTTP200确认后推进游标；进程被杀或结果未知，下次停发。显式恢复可能重复，event_id可辅助去重，不保证exactly-once。达到预算后本地继续保留72小时，超过本地TTL尚未上传的记录不能事后补齐。
