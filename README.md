# mi-be3600-local-monitor · 小米 BE3600 本地流量监控面板

> ⚠️ **风险声明（请先阅读）**
> 本项目为**个人学习用途**，通过局域网内对小米路由器**私有 Web 接口**（`/cgi-bin/luci`）抓包后实现。
> 小米**未公开**该接口，接口可能随固件更新而失效或变动；本项目不保证长期可用。
> 使用本项目产生的一切后果**由使用者自行承担**，请勿用于商用或任何违背路由器厂商条款的场景。
> 本项目仅为第三方自制工具，**与小米官方无关**。

针对 **小米路由器 BE3600（2.5G 版）** 的本地流量监控面板：定时读取连接设备与实时速率，
输出一个暗色 ECharts 网页仪表盘。**无需开启 SSH**，**纯 Python 标准库**实现。

---

## ✨ 功能特性

- **实时设备列表**：名称、IP、接入方式（有线 / 2.4G / 5G）、实时上下行速率、今日流量
- **设备表排序**：点击表头按 设备名 / 接入 / 频段 / 实时速率 / 今日流量 升降序切换
- **总流量折线图**：全网下载（蓝）/上传（橙）双曲线 + 区间统计（总 / 平均 / 峰值）
- **多时间窗口**：近10分钟 / 近1小时 / 近1天 / 近1周 / 近1月 一键切换
- **路由器状态卡片**：在线设备、今日总下载/上传、运行时长、WAN 实时、CPU、内存、固件版本
- **数据落盘持久化**：当日流量与趋势历史写入数据卷，重启不丢失
- **北京时间显示**：时间戳统一按 UTC+8 展示，不依赖容器时区

---

## 📦 依赖

**本项目无任何第三方依赖。**

- **后端**：仅使用 Python 标准库（`hashlib / http.server / json / os / random / re / sys / threading / time / urllib / datetime`）。
  无需 `pip install`，无需执行 `requirements.txt` 安装步骤。
- **前端**：图表使用运行时从 CDN 加载的 ECharts，无需 npm、无前端依赖包。
- **环境**：Python ≥ 3.8；Linux / Windows / macOS 均可；与路由器同局域网，可访问其后台地址。

---

## 🚀 快速部署

### 方式 A：飞牛 OS / 任意 NAS 用 Docker Compose（推荐）

1. 将本项目放到 NAS（例如 `/vol1/docker/mi-be3600-local-monitor`），新建数据目录：

   ```bash
   mkdir -p miwifi-data
   ```

2. 编辑 `docker-compose.yml`，将 `ROUTER_PASSWORD` 改为你的**路由器后台管理密码**。

3. 启动并强制重建：

   ```bash
   docker compose up -d --force-recreate
   ```

4. 浏览器打开 `http://<NAS的IP>:10000`。

### 方式 B：不装 Docker 裸跑

```bash
cp config.example.json config.json   # 编辑 config.json，把密码改成你的路由器后台管理密码
python3 mi_be3600_local_monitor.py
```

浏览器访问 `http://<这台电脑的IP>:10000`。

---

## ⚙️ 配置

支持环境变量（Docker 用 `environment`）或 `/app/config.json`（JSON，覆盖环境变量）。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ROUTER_HOST` | `192.168.31.1` | 路由器后台地址 |
| `ROUTER_USER` | `admin` | 后台登录用户名（固定） |
| `ROUTER_PASSWORD` | 必填 | 路由器后台管理密码 |
| `POLL_INTERVAL` | `30` | 采集间隔（秒） |
| `RETAIN_DAYS` | `30` | 历史数据保留天数 |
| `DATA_FILE` | `/data/miwifi_data.json` | 当日/历史流量数据文件 |
| `WEB_HOST` | `0.0.0.0` | 仪表盘监听地址 |
| `WEB_PORT` | `10000` | 仪表盘端口 |

---

## 🛠️ 常用命令

| 命令 | 作用 |
| --- | --- |
| `python3 mi_be3600_local_monitor.py` | 启动采集 + 仪表盘 |
| `python3 mi_be3600_local_monitor.py --check` | 仅测试登录是否成功 |
| `python3 mi_be3600_local_monitor.py --once` | 单次采集并打印文本报告 |
| `docker compose logs -f mi-be3600-local-monitor` | 查看容器日志 |

---

## 📈 仪表盘说明

- **卡片**：在线设备、今日总下载/上传、运行时长、WAN 实时、CPU 负载、内存、固件版本。
- **折线图**：蓝=下载、橙=上传；长窗口自动聚合降采样，统计按原始数据精确计算。
- **设备表**：表头点击排序，▲/▼ 指示方向。

---

## 💾 数据存储

| 文件 | 内容 |
| --- | --- |
| `miwifi-data/miwifi_data.json` | 每台设备当日及历史日流量累计（每轮采集后落盘） |
| `miwifi-data/trend.jsonl` | 全网流量趋势历史（`时间戳|下载|上传`，append-only，保留30天） |

---

## 🕐 时区

脚本已内置**北京时间（UTC+8）**显示（`bjtime`，不依赖容器时区），无需额外配置。
若需改为其他时区，修改脚本中的 `BJ_OFFSET`（秒）即可。

---

## 🧾 已知问题

- 小米官方固件**未开放**这些接口，固件升级后接口可能变动，本项目**不保证长期维护**。
- 不提供设备会话/连接历史列表。
- 连续多次错误登录可能触发路由器临时封锁发起方 IP，需重启路由器解除。
- 图表库 CDN 加载失败时会自动尝试备用源，不影响设备表格数据。

---

## 📜 开源协议

本项目基于 **MIT License** 开源。详见 [LICENSE](LICENSE)。
