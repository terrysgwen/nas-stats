# NAS-Stats 极简多功能 NAS 仪表盘

一个专为家用 NAS、家庭实验室打造的高性能、轻量级现代化导航与系统监控面板。支持飞牛 fnOS 音乐无缝集成、系统状态实时监控、服务卡片管理与批量导入等丰富功能。

---

## ✨ 核心特性

- 🚀 **极简高效**: Python FastAPI + 原生现代化响应式前端，零复杂依赖，内存占用极低。
- 📦 **纯粹存储**: 统一将所有持久化数据保存在 `./data` 目录（SQLite 数据库与账户授权凭据），与应用代码完全解耦。
- 🎵 **飞牛音乐集成**: 原生对接飞牛 fnOS 音乐 API（5888 端口），支持实时拉取服务端歌单、黑胶唱片转盘动效、随机/单曲/列表播放控制与独立运行排查日志。
- 📊 **系统监控集成**: 原生支持对接 iStoreOS 软路由、Proxmox VE (PVE) 虚拟化、MoviePilot 影视下载等监控组件。
- 🧭 **服务导航中心**: 灵活管理、批量导入导出应用卡片，自动探测服务运行状态，卡片跳转支持自动适配当前访问宿主域名。
- 🔐 **轻量鉴权机制**: 基于 JWT 与本地密码哈希的开箱即用鉴权，支持记住登录状态。

---

## 🚀 快速启动 (Docker Compose)

### 1. 准备目录与配置文件

创建项目目录并在该目录下编写 `docker-compose.yml`：

```yaml
services:
  nas-stats:
    image: terryhellwen/nas-stats:latest
    container_name: nas-stats
    restart: unless-stopped
    ports:
      - "8980:8980"
    volumes:
      - ./data:/app/data
    environment:
      - TZ=Asia/Shanghai
```

### 2. 启动容器

```bash
docker compose up -d
```

启动完成后，在浏览器中访问：`http://<NAS_IP>:8980`。

> **默认初始管理账号与密码**：
> - 用户名：`admin`
> - 初始密码：`admin123`（或 `admin`）
> *登录系统管理面板后可随时在“界面设置与个性化”中修改密码。*

---

## 🛠️ GitHub Actions 自动构建与发布到 Docker Hub

本项目自带 `.github/workflows/docker-publish.yml`，支持通过 GitHub Actions 自动构建跨架构（`linux/amd64` 与 `linux/arm64`）镜像并推送到 Docker Hub。

### 自动化推送配置步骤：

1. **将项目推送至 GitHub**:
   ```bash
   git init
   git add .
   git commit -m "feat: initial commit for nas-stats"
   git remote add origin https://github.com/<你的用户名>/nas-stats.git
   git branch -M main
   git push -u origin main
   ```

2. **在 GitHub 仓库中配置 Secrets**:
   进入你的 GitHub 仓库 -> **Settings** -> **Secrets and variables** -> **Actions** -> **Repository secrets**，添加以下两个密钥：
   - `DOCKERHUB_USERNAME`: 你的 Docker Hub 账号用户名。
   - `DOCKERHUB_TOKEN`: 你的 Docker Hub Personal Access Token（进入 Docker Hub -> Account Settings -> Security -> Access Tokens 创建）。

3. **触发自动构建与发布**:
   - 每次向 `main` 分支 `push` 代码时，GitHub Actions 会自动编译并打上 `latest` 标签推送到 Docker Hub。
   - 也可以通过打 Git Tag（例如 `git tag v1.0.0 && git push origin v1.0.0`）自动发布带版本号的镜像标签。

---

## 📂 目录结构说明

```text
nas-stats/
├── .github/workflows/
│   └── docker-publish.yml   # GitHub Actions 自动化构建与发布流水线
├── data/                    # 【核心数据目录】所有数据库、日志和凭据均保存在此
│   ├── .gitkeep
│   ├── auth.json            # 账户鉴权配置（首次启动自动生成）
│   ├── nas-stats.db         # 服务导航与系统监控数据库
│   ├── music.db             # 飞牛音乐配置与本地播放队列数据库
│   └── music.log            # 飞牛音乐独立运行日志
├── monitors/                # 监控适配器模块 (PVE, iStoreOS, MoviePilot 等)
├── static/                  # 前端静态资源 (index.html, nvr.html)
├── auth_manager.py          # 鉴权管理模块
├── database.py              # SQLite 数据库模型与迁移
├── docker-compose.yml       # Docker Compose 编排模板
├── Dockerfile               # 跨平台 Docker 镜像定义
├── fnmusic_client.py        # 飞牛音乐 API 客户端
├── main.py                  # FastAPI 主服务入口
├── main_defaults.py         # 干净的默认预置服务列表
├── music_db.py              # 音乐模块数据库管理
├── music_logger.py          # 音乐独立日志管理器
└── requirements.txt         # Python 运行依赖
```

---

## 🔒 隐私与安全

- 本仓库默认移除了所有私有网络 IP 与敏感账户密码，首次启动若 `data/` 为空，系统会自动初始化干净的环境。
- 持久化数据（`data/*.db`, `data/auth.json`, `data/*.log`）均已被 `.gitignore` 和 `.dockerignore` 排除，保证不会意外提交至公网仓库。


---

## 💬 参与贡献与提交规范

欢迎提出宝贵的反馈与建议！为了确保沟通顺畅与高效维护：

- **提交信息规范 (Git Commit)**：本项目的 **Git 提交信息（Commit Message）全程统一使用纯中文**，确保在 GitHub Actions 运行记录与提交历史中直观可读。
- **语言规范**：本项目的 **GitHub Issues 及 Pull Requests 全程统一使用中文** 进行记录与交流。
- **缺陷反馈 (Bug)**：若遇到运行报错或异常行为，请优先查阅已有 Issues，并按照 **Bug 报告模板** 完整填写运行平台、复现步骤及关键日志。
- **功能建议 (Feature)**：如果您对仪表盘、系统监控或飞牛音乐模块有新的需求或交互优化想法，欢迎使用 **功能建议模板** 提出。
- **使用咨询 (Question)**：若遇到部署配置、反向代理或服务对接等疑问，欢迎使用 **使用咨询模板** 进行交流。

---

## 📝 版本更新记录

### v2.3.0
- 🎨 **主题管理重构**：将所有主题选项完整收归至「⚙️ 系统管理」的“🎨 界面与样式”面板中，移除顶栏额外控件，保持顶栏纯净清爽。
- 💎 **高级质感主题**：新增暗房胶片、午夜冷蓝、极客终端、工程蓝图、纸本工坊等 Hallmark 高级质感主题与深色磨砂亚克力风格，支持随心切换。
- 🎵 **飞牛音乐体验增强**：升级拟真黑胶唱片高光反光质感，优化流媒体外网代理与音频缓存机制。
- 🌏 **全中文生态完善**：全量中文化 GitHub Actions 工作流、构建流水线步骤与中文 Issues 模板系统。
