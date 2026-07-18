# 1688 浏览器桥接采集实施计划 (1688 Browser Bridge Collection Plan)

1. 先补服务端红灯测试：`supplier_collecting` 必须返回浏览器任务，结果提交后必须自动续跑。
2. 先补扩展红灯测试：后台和工作台内容脚本必须识别 supplier task，并选择 1688 正常标签页。
3. 最小修改 `local_server.py`：发布 supplier task，接收浏览器结果，正式 runner 不再装配无头 supplier worker。
4. 最小修改扩展：增加 1688 权限、任务路由和 `supplier_content.js`，保留现有 Ozon 行为。
5. 运行定向测试、完整 Python/Node 回归和语法检查。
6. 重启 8765 服务、一次性重载扩展并恢复当前批次实战。
7. 将设计、修复、测试和运行结果同步到 E 盘 Ozon 工作台项目库。
