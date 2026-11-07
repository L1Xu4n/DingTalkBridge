# UIAutomation 实验采集器

此文只描述早期 UIA/MSAA 后端。当前可运行的原生 hook 见根目录 README。

`native/DingTalkUia.cs` 使用进程外 UIAutomation。`src/dingtalkbridge/collector.py` 负责构建缓存、启动监督和结果入库。UIA 适配器只读取应用公开的可访问性信息，不等价于原生结构化消息；本机测试没有取得可靠聊天正文。

```powershell
.\scripts\Bridge.ps1 collect --mode probe
.\scripts\Bridge.ps1 collect --mode watch --duration 20 --include-screen-context
```

默认不保存窗口文字。显式开启 screen context 后，记录标为 `kind=screen_context`、`is_message=false`，账号未知，时间为观察时间。输入框、密码框、可写值和来源不明的节点会排除。MSAA 只用于结构和几何信息。

采集需要在用户交互桌面运行。不同 desktop 的零窗口结果不能证明钉钉未打开。程序若处于会重定向 AppData 的容器环境，可在本机 `config.local.json` 中指定独立 `data_dir`；采集器与 MCP 必须使用相同配置。

`probe` 检查结构，`snapshot` 进行一次观察，`watch` 监听可访问性事件。采集状态文件只含计数，正文通过 Store 的过滤、去重和保留期规则进入归档。此后端的老版目标路径配置与主原生采集器独立，不应将它的限制解释为当前 native 后端的结果。
