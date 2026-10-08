# nanyi12306

12306 火车票自动购票桌面工具（个人自用）。

基于 Python + PySide6，同时支持 Windows 和 macOS。通过 12306 官方接口实现扫码登录、余票监控、自动下单与候补兜底，下单成功后在 12306 官方 App 内手动完成支付。

> **合规说明**：本工具仅供个人使用，采用低频轮询（≥1.5s + 随机抖动），不绕过验证码、不调用任何第三方接口、不囤票不牟利。12306 禁止高频第三方请求，滥用可能导致账号被限制，请合理设置轮询间隔。

## 功能特性

- 官方 App 扫码登录，Cookie 本地持久化，免重复登录
- 车站名自动补全（内置车站代码表）
- 多任务并行监控：车次白名单、席别优先级、时间窗过滤
- 余票命中自动下单，排队状态实时展示
- 无票自动转候补（官方候补，兑现率最高的合规路径）
- 出票提醒：系统弹窗 + 声音 + 微信推送（Server酱/Bark 可选）
- Windows / macOS 单安装包分发

## 界面预览

三个主页面：**登录页**（二维码扫码）→ **任务页**（配置抢票条件）→ **监控页**（实时余票、排队状态、日志）。详见 [docs/界面设计.md](docs/界面设计.md)。

## 快速开始

```bash
# 需要 Python 3.11+
pip install -r requirements.txt
python main.py
```

打包分发（在对应平台上执行）：

```bash
python build/build.py        # 自动识别 Windows / macOS
```

产出：Windows 为 `dist/nanyi12306.exe`，macOS 为 `dist/nanyi12306.app`。

## 使用流程

1. 启动应用，用 12306 官方 App 扫描二维码登录
2. 新建抢票任务：选择出发地/目的地/日期，查询后勾选车次，设置席别优先级和乘车人
3. 点击「开始抢票」，切到监控页观察状态
4. 下单成功后应用弹窗并响铃，**10 分钟内**到 12306 App「未完成订单」中支付

## 项目结构

```
nanyi12306/
├── main.py                # 应用入口
├── ui/                    # PySide6 界面层
│   ├── main_window.py     # 主窗口 + 页面导航
│   ├── login_page.py      # 二维码登录页
│   ├── task_page.py       # 抢票任务配置页
│   ├── monitor_page.py    # 实时监控页
│   └── widgets.py         # 车站补全、任务卡片等组件
├── core/                  # 业务核心（后台线程）
│   ├── auth.py            # 扫码登录 + Cookie 管理
│   ├── stations.py        # 车站代码表
│   ├── query.py           # 余票查询与解析
│   ├── order.py           # 下单 / 候补链路
│   ├── worker.py          # QThread 抢票工作线程
│   └── notify.py          # 跨平台通知
├── data/                  # 运行时数据（Cookie、配置、缓存）
├── assets/                # 图标、提示音
├── build/                 # PyInstaller 构建配置
├── docs/                  # 项目文档
└── requirements.txt
```

## 文档索引

| 文档 | 说明 |
|---|---|
| [docs/架构设计.md](docs/架构设计.md) | 架构设计与技术选型 |
| [docs/12306接口说明.md](docs/12306接口说明.md) | 12306 接口逆向说明 |
| [docs/界面设计.md](docs/界面设计.md) | 界面与交互设计 |
| [docs/打包发布.md](docs/打包发布.md) | Windows / macOS 打包发布 |
| [docs/实现计划.md](docs/实现计划.md) | 实现计划与里程碑 |

## License

MIT（个人学习用途，请勿用于商业抢票）
