# SakuraMedia 女优资料补全

从 JavDB 和 MinnanoAV 补全 SakuraMedia 本地女优资料。

## 安装

在 SakuraMedia 的「系统设置 → 插件」上传本插件的 ZIP 并启用。
MinnanoAV 在部分网络环境可能需要通过代理访问；如无法连接，请在部署 SakuraMedia 后端容器时自行配置代理。

## 工作方式

- 默认每天 05:00 运行，使用宿主调度时区；手动运行没有额外参数。
- 扫描未明确标记为男性的演员。可用 `subscribed_only` 只处理已订阅演员。
- 已订阅演员优先；其余按关联的非合集影片数量降序，同数按演员 ID 升序。
- 先查询 JavDB，仍有缺失字段时再补 MinnanoAV 资料。
- MinnanoAV 的搜索接口会被人机挑战，插件改用公开女优列表建立本地索引（姓名 → 资料页）。安装包内置当前约 2.5 万位演员的 JSON 索引，首次运行时生成 `data/actress_index.sqlite3` 查询缓存。
- 正常安装和升级不再全量抓取列表；每天只抓“追加日順”的最新 1–3 页。积压超过一次增量范围时才会回退到全量重建。
- 内置索引随插件包升级；旧版本或不完整的本地索引会在升级后由新内置索引替换。
- 索引命中多个同名演员时不猜测；资料页仍会校验姓名和别名，避免写入错误资料。
- 只写入空值；不会覆盖人工或其他插件持有的字段。补全生日、身高、三围、罩杯、出生地和血型；性别由宿主影片元数据维护，插件不写性别。
- 默认每 3 天重试，最多 3 次。补齐、字段不可写或达到次数上限后停止处理该演员。
- 列表页和资料页请求都遵循 `request_interval_seconds`，默认 1 秒一次。

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

## License

本项目采用 [PolyForm Noncommercial License 1.0.0](./LICENSE)，仅允许非商业用途。

Copyright (C) 2026 tinypinglite
