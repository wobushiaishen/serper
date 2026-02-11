# Web 应用 Docker 化部署

本项目提供了一套完整的 Docker 配置，用于将 Web 应用容器化部署。

## 结构

- `houduan.py`: FastAPI 后端服务。
- `getin.html`, `chatscreen.html`, `*.jpg`: 前端静态资源。
- `Dockerfile.backend`: 用于构建后端服务的 Dockerfile。
- `nginx.conf`: Nginx 配置文件，用于托管前端静态资源和反向代理 API 请求。
- `docker-compose.yml`: Docker Compose 文件，用于编排和管理前后端服务。
- `env.example`: 环境变量模板文件。

## 快速开始

### 1. 配置环境变量

首先，复制环境变量模板文件并填入你的 API 密钥：

```bash
cp env.example .env
```

然后，编辑 `.env` 文件，将 `your_api_key_here` 替换为你的阿里云 MCP API 密钥。

### 2. 构建并启动服务

使用 Docker Compose 构建并以后台模式启动所有服务：

```bash
docker-compose up --build -d
```

### 3. 访问应用

- **前端页面**: 打开浏览器访问 `http://localhost:8080`。
- **后端 API**: API 服务运行在 `http://localhost:8000`，前端页面已通过 Nginx 代理 `/api/` 请求到此地址。

### 4. 停止服务

要停止并移除容器，请运行：

```bash
docker-compose down
```

