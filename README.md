# SakuraMedia 女优资料补全

从 JavDB 和 MinnanoAV 补全 SakuraMedia 本地女优资料。

## 安装

在 SakuraMedia 的「系统设置 → 插件」上传本插件的 ZIP 并启用。
MinnanoAV 在部分网络环境可能需要通过代理访问；如无法连接，请在部署 SakuraMedia 后端容器时自行配置代理。

## 工作方式

- 默认每天 05:00 运行，使用宿主调度时区；手动运行没有额外参数。
- 扫描未明确标记为男性的演员。可用 `subscribed_only` 只处理已订阅演员。
- 已订阅演员优先；其余按关联的非合集影片数量降序，同数按演员 ID 升序。
- 先查询 JavDB，仍有缺失字段时再查询 MinnanoAV。MinnanoAV 只接受姓名精确且唯一的匹配，避免写入同名演员资料。
- 只写入空值；不会覆盖人工或其他插件持有的字段。补全生日、身高、三围、罩杯、出生地和血型；性别由宿主影片元数据维护，插件不写性别。
- 默认每 3 天重试，最多 3 次。补齐、字段不可写或达到次数上限后停止处理该演员。

## 配置（可选）

在插件管理页通过表单配置重试间隔、尝试次数、订阅筛选和请求时间；未填写的项使用默认值。配置对象示例：

```json
{
  "retry_days": 3,
  "max_attempts": 3,
  "subscribed_only": false,
  "request_interval_seconds": 1,
  "timeout_seconds": 20
}
```
