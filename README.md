# NAS 服务导航

飞牛 NAS 服务导航页，自动发现局域网服务并提供统一入口。支持实时流量监控、服务管理和移动端适配。

## 功能特性

- **服务导航** — 卡片式展示所有 NAS 服务，点击直达
- **端口扫描** — 自动扫描局域网开放端口，识别常见服务
- **实时状态** — 展示 qBittorrent / Transmission 上下行速度和错误数
- **路由监控** — 接入 iKuai 路由器，显示 WAN 口速率、CPU、内存、连接数
- **服务管理** — 可视化管理服务列表，支持排序、显隐、编辑
- **图标自定义** — 支持 emoji、自动抓取 favicon、手动上传图片、URL 加载
- **批量导入** — 支持 JSON 批量导入和 URL 自动识别
- **Bing 背景** — 每日自动更换 Bing 壁纸
- **响应式设计** — 适配手机和平板

## 技术栈

- **后端:** Python + FastAPI + httpx + Uvicorn
- **前端:** 原生 HTML/CSS/JavaScript，无框架依赖
- **部署:** Docker

## 快速开始

### Docker Compose（推荐）

1. 编辑 `config.json`，填入你的 NAS IP 和服务账号信息：

```json
{
  "port": 8980,
  "nas_ip": "192.168.1.100",
  "services": {
    "qbittorrent": {
      "enabled": true,
      "url": "http://192.168.1.100:8080",
      "username": "admin",
      "password": "your_password"
    },
    "transmission": {
      "enabled": true,
      "url": "http://192.168.1.100:9091",
      "username": "admin",
      "password": "your_password"
    },
    "ikuai": {
      "enabled": true,
      "url": "http://192.168.1.1",
      "username": "admin",
      "password": "your_password"
    }
  }
}
```

2. 启动服务：

```bash
docker compose up -d
```

3. 访问 `http://你的NAS_IP:8980`

### 手动构建

```bash
docker build -t nas-stats .
docker run -d --name nas-stats --network host \
  -v $(pwd)/config.json:/app/config.json:ro \
  -v $(pwd)/data:/app/data \
  nas-stats
```

## 配置说明

| 字段 | 说明 |
|------|------|
| `port` | 服务监听端口，默认 8980 |
| `nas_ip` | NAS 的局域网 IP 地址 |
| `cors_origins` | CORS 允许的源，默认 `["*"]` |
| `services.qbittorrent` | qBittorrent 连接配置 |
| `services.transmission` | Transmission 连接配置 |
| `services.ikuai` | iKuai 路由器连接配置 |

每个服务配置中 `enabled` 控制是否启用，设为 `false` 可关闭对应功能。

## 数据持久化

- `config.json` — 配置文件（只读挂载）
- `data/services.json` — 服务列表数据（自动创建）

## API 接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/services` | GET | 获取服务列表 |
| `/api/services` | POST | 创建服务 |
| `/api/services/<id>` | PUT | 更新服务 |
| `/api/services/<id>` | DELETE | 删除服务 |
| `/api/services/reorder` | PUT | 更新排序 |
| `/api/services/export` | GET | 导出配置 |
| `/api/scan` | POST | 扫描 NAS 端口 |
| `/api/fetch-meta` | POST | 获取网页元信息 |
| `/api/load-icon-url` | POST | 从 URL 加载图标 |
| `/api/live-stats` | GET | 获取实时流量数据 |
| `/api/bing-bg` | GET | 获取 Bing 背景图 |

## 截图

![桌面端](https://via.placeholder.com/800x450?text=Desktop+Screenshot)
![移动端](https://via.placeholder.com/375x667?text=Mobile+Screenshot)

## License

MIT
