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
    image: your-dockerhub-username/nas-stats:latest
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
