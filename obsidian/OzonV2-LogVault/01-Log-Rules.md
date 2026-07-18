# Ozon V2 操作日志规则

## 每次动手前必须记录

- 本步属于哪一层：FastMCP / Application / Domain / Adapter / Docs / Ops。
- 会创建或修改哪些文件。
- 本步明确不做什么。
- 如何测试完成。

## 每次动手后必须记录

- 实际创建或修改的文件。
- 关键规则或行为变化。
- 执行过的校验。
- 是否触碰旧插件。
- 下一步建议。

## 固定边界

- 操作日志写入本 Obsidian vault。
- 项目契约仍以工作区内的 `ARCHITECTURE.md`、`docs/collection_contract.md`、`docs/build_plan.md` 为准。
- Obsidian 日志库不存业务代码。
- Obsidian 日志库不存运行数据。
- Obsidian 日志库不替代 evidence CSV。

## 推荐日志格式

```text
## HH:mm 操作标题

Layer:
- ...

Files:
- ...

Did:
- ...

Did not:
- ...

Verification:
- ...

Next:
- ...
```
