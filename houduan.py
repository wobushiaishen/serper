


# ====================== 【需要手动填写的配置区域】 ======================
# 请在使用前手动填写以下信息

# 1. 你的LLM API信息
LLM_API_KEY = "b3e763f3af704bd0ad503ab453b116c4.7a2ao2K5SbmC4XrY"  # 需要填写：你的API密钥
LLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"  # 需要填写：你的平台URL（智谱AI的完整URL，不需要拼接）
LLM_MODEL = "glm-4-flash-250414"  # 需要填写：你的模型名，如 qwen-max
# 2. 你的MCP服务器信息（IQS Search MCP Server）
IQS_MCP_API_KEY = "须填"  #阿里云的搜索MCP（单个）
IQS_MCP_SERVER_URL = "https://iqs-mcp.aliyuncs.com/mcp-servers/iqs-mcp-server-search"  # streamableHttp# ====================== 【代码主体区域】 ======================
import uvicorn
import time
import json
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, HTTPException, Depends, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
import asyncio
import requests
import os
import pickle
import uuid
import secrets
import traceback


# 当为 True 时，整个流程使用本地模拟结果（不调用外部 LLM / MCP）
# 注意：按要求永远不能打开离线模式
OFFLINE_MODE = False



# 3. 前端网站地址（登录成功后跳转）
FRONTEND_CHAT_URL = "http://localhost:5500/chatscreen.html"  # 前端聊天页面地址

# 4. CORS允许的前端地址
ALLOWED_ORIGINS = [
    "http://localhost:5500",      # 前端开发服务器
    "http://127.0.0.1:5500",
    "http://localhost:3000",      # 备用端口
    "http://127.0.0.1:3000",
]

# 5. 你的MCP工具信息 - IQS Search MCP Server
YOUR_MCP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "common_search",
            "description": "标准搜索接口：提供增强的网络开放域实时搜索能力，返回 markdown 格式结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索问题（长度：>=2 and <=500）"}
                },
                "required": ["query"]
            }
        }
    }
]

# 全局变量，用于保存MCP协议所需的会话ID
mcp_session_id = None

# ====================== MCP核心处理函数 ======================
def call_mcp_tool_protocol(tool_name, arguments):
    """严格按 MCP JSON-RPC 调用 IQS Search MCP（streamableHttp）。"""
    global mcp_session_id

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": IQS_MCP_API_KEY,
    }

    # 1) initialize 获取 sessionId
    if mcp_session_id is None:
        init_request = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "IQSClient", "version": "1.0"},
            },
        }
        resp = requests.post(IQS_MCP_SERVER_URL, json=init_request, headers=headers, timeout=30)
        try:
            resp_data = resp.json()
        except Exception:
            resp_data = None

        sid = None
        if isinstance(resp_data, dict):
            sid = resp_data.get("result", {}).get("sessionId") or resp_data.get("result", {}).get("session_id")
            sid = sid or resp_data.get("sessionId") or resp_data.get("session_id")
        if not sid:
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("MCP-Session-Id") or resp.headers.get("session-id")

        mcp_session_id = sid
        if not mcp_session_id:
            # IQS Search MCP 的 initialize 可能不返回 sessionId；兼容处理：使用请求 id 作为临时 sessionId
            mcp_session_id = init_request["id"]
            print(f"⚠️ initialize 未返回 sessionId，使用临时 sessionId={mcp_session_id}")

    # 2) tools/call
    call_request = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
            "sessionId": mcp_session_id,
        },
    }

    # 有些服务需要 header 也带 session；若服务不认 sessionId，也不会影响
    headers_with_session = dict(headers)
    if mcp_session_id:
        headers_with_session["mcp-session-id"] = str(mcp_session_id)

    resp = requests.post(IQS_MCP_SERVER_URL, json=call_request, headers=headers_with_session, timeout=60)
    try:
        data = resp.json()
    except Exception:
        return {"success": False, "error": f"MCP 返回非 JSON，status={resp.status_code}", "details": resp.text[:500]}

    if isinstance(data, dict) and data.get("error"):
        return {"success": False, "error": data.get("error")}

    # IQS 的结果通常在 result 里（可能是 markdown 字符串或结构体）
    return {"success": True, "result": data.get("result", data)}

async def process_mcp_chat_message(user_input: str, session: Dict) -> tuple[str, Optional[Dict]]:
    """
    处理聊天消息的核心函数 - 整合了MCP魔改之后.py的核心逻辑
    返回: (响应文本, 媒体信息字典)
    """
    
    # 按要求：永远不能打开离线模式
    if OFFLINE_MODE:
        raise RuntimeError("OFFLINE_MODE 被禁止，请将 OFFLINE_MODE 设置为 False 并重启服务。")

    # 在线模式：调用实际的MCP处理逻辑
    try:
        # ========== 第一个REQUEST：咨询LLM是否需要MCP ==========
        system_prompt = """你是一个带联网搜索能力的助手。

当你需要获取最新信息、引用网页内容、或对事实进行核实时，请调用工具 common_search。

工具返回的内容是 markdown 文本，你需要基于该文本给出简明回答，并在答案中保留必要的引用/来源链接（如果工具结果中有）。
"""
        
        # 构建包含对话历史的消息列表
        messages = [{"role": "system", "content": system_prompt}]
        
        # 添加对话历史
        if session and "history" in session and session["history"]:
            for msg in session["history"]:
                messages.append({"role": "user", "content": msg["user"]})
                messages.append({"role": "assistant", "content": msg["assistant"]})
        
        # 添加当前用户消息
        messages.append({"role": "user", "content": user_input})
        
        llm_data = {
            "model": LLM_MODEL,
            "messages": messages,
            "tools": YOUR_MCP_TOOLS,
            "tool_choice": "auto"
        }

        response = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
            json=llm_data,
            timeout=30
        )
        llm_result = response.json()
        llm_message = llm_result["choices"][0]["message"]

        print("=== LLM 返回 tool_calls ===")
        try:
            print(llm_message.get("tool_calls"))
        except Exception:
            print("<无法读取 tool_calls>")

        # 判断是否需要调用 MCP（IQS common_search）
        need_mcp = False
        tool_to_call = None
        tool_arguments = {}

        if "tool_calls" in llm_message and llm_message["tool_calls"]:
            need_mcp = True
            tool_call = llm_message["tool_calls"][0]
            tool_to_call = tool_call["function"]["name"]
            args_raw = tool_call["function"].get("arguments")
            if isinstance(args_raw, str):
                try:
                    tool_arguments = json.loads(args_raw)
                except Exception:
                    tool_arguments = {}
            elif isinstance(args_raw, dict):
                tool_arguments = args_raw
            else:
                tool_arguments = {}

        print("=== 解析到的工具调用 ===")
        print("tool_to_call =", tool_to_call)
        print("tool_arguments =", tool_arguments)

        if need_mcp and tool_to_call:
            mcp_result = call_mcp_tool_protocol(tool_to_call, tool_arguments)
            print("=== MCP 返回 ===")
            try:
                print(json.dumps(mcp_result, ensure_ascii=False)[:800])
            except Exception:
                print(str(mcp_result)[:800])

            final_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
                llm_message,
                {
                    "role": "tool",
                    "content": json.dumps(mcp_result, ensure_ascii=False),
                    "tool_call_id": (llm_message.get("tool_calls") or [{}])[0].get("id") if llm_message.get("tool_calls") else None
                }
            ]

            final_data = {
                "model": LLM_MODEL,
                "messages": final_messages
            }

            final_response = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json=final_data,
                timeout=30
            )
            
            if final_response.status_code != 200:
                return f"处理过程中发生错误: LLM 二次请求失败，状态码={final_response.status_code}，响应={final_response.text[:500]}", None
            
            try:
                final_result = final_response.json()
            except Exception as e:
                return f"处理过程中发生错误: LLM 二次请求返回非JSON: {e}，响应={final_response.text[:500]}", None
            
            if not isinstance(final_result, dict) or "choices" not in final_result or not final_result["choices"]:
                return f"处理过程中发生错误: LLM 二次请求响应缺少 choices，响应={json.dumps(final_result, ensure_ascii=False)[:800]}", None
            
            final_message = final_result["choices"][0].get("message", {})
            final_answer = final_message.get("content", "无内容")
            return final_answer, None

        final_answer = llm_message.get("content", "无内容")
        return final_answer, None

    except Exception as e:
        print("=== process_mcp_chat_message 异常堆栈 ===")
        print(traceback.format_exc())
        return f"处理过程中发生错误: {str(e)}", None

# ====================== 应用启动事件 ======================
# 使用新的lifespan事件处理替换已弃用的on_event
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动和关闭时的生命周期管理"""
    # 启动时执行
    print("MCP智能助手API正在启动...")
    print(f"离线模式: {OFFLINE_MODE}")
    print(f"允许的来源: {ALLOWED_ORIGINS}")
    # 启动时强制检查运行时规则（不可启用离线模式）
    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        print(f"启动失败：{e}")
        raise

    asyncio.create_task(cleanup_old_sessions())
    
    yield
    
    # 关闭时执行（可选）
    print("MCP智能助手API正在关闭...")

# ====================== FastAPI应用初始化 ======================
app = FastAPI(
    title="MCP智能助手API",
    description="提供用户认证和智能聊天功能的API服务",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan  # 使用新的lifespan事件处理
)

# CORS中间件配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# ====================== 数据模型定义 ======================
class LoginRequest(BaseModel):
    username: str
    password: str

class LoginResponse(BaseModel):
    success: bool
    message: str
    redirect_url: Optional[str] = None
    token: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    stream: bool = False

class ChatResponse(BaseModel):
    success: bool
    response: str
    session_id: str
    has_media: bool = False
    media_info: Optional[Dict[str, Any]] = None
    timestamp: float
    tokens_used: Optional[int] = None

# ====================== 全局状态管理 ======================
conversation_sessions = {}
valid_tokens = set()

# ====================== 认证工具函数 ======================
def authenticate_user(username: str, password: str) -> bool:
    """验证用户名和密码（硬编码测试账户）"""
    return username == "测试员" and password == "hongyan"

def generate_token() -> str:
    """生成简单的访问令牌"""
    token = secrets.token_urlsafe(32)
    valid_tokens.add(token)
    return token

def verify_token(token: str) -> bool:
    """验证访问令牌"""
    return token in valid_tokens


# ====================== 运行时规则检查 ======================
def ensure_offline_mode_disabled():
    """
    确保绝对不允许打开离线模式。在每次会话前调用。
    如果检测到为 True，则抛出 RuntimeError。
    """
    if OFFLINE_MODE:
        raise RuntimeError("OFFLINE_MODE 被禁止，请将 OFFLINE_MODE 设置为 False 并重启服务。")

# ====================== 依赖注入：验证访问令牌 ======================
async def get_current_user(authorization: Optional[str] = Header(None)) -> bool:
    """验证访问令牌的依赖函数"""
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="缺少认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # 提取 Bearer token
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="无效的认证方案",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except ValueError:
        raise HTTPException(
            status_code=401,
            detail="无效的认证格式",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not verify_token(token):
        raise HTTPException(
            status_code=401,
            detail="无效的访问令牌或令牌已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True

# ====================== API端口1：用户登录 ======================
@app.post("/api/login", response_model=LoginResponse)
async def login(login_data: LoginRequest):
    """
    用户登录端口
    验证用户名密码，成功后返回前端地址和访问令牌
    """
    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    if authenticate_user(login_data.username, login_data.password):
        token = generate_token()
        
        return LoginResponse(
            success=True,
            message="登录成功",
            redirect_url=FRONTEND_CHAT_URL,
            token=token
        )
    else:
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误"
        )

# ====================== API端口2：智能聊天 ======================
@app.post("/api/chat")
async def chat_endpoint(
    chat_data: ChatRequest,
    request: Request,
    authenticated: bool = Depends(get_current_user)
):
    """
    智能聊天端口 - 接收前端消息并返回处理结果
    支持流式和非流式两种响应方式
    """
    # 每次会话前确保运行时规则（禁止离线模式）
    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))
    
    # 获取或创建会话ID
    session_id = chat_data.session_id or f"session_{int(time.time())}_{hash(chat_data.message) % 10000}"
    
    # 初始化或获取会话
    if session_id not in conversation_sessions:
        conversation_sessions[session_id] = {
            "history": [],
            "created_at": time.time(),
            "last_active": time.time(),
            "message_count": 0
        }
    
    # 获取会话对象
    session = conversation_sessions[session_id]

    session["last_active"] = time.time()
    session["message_count"] += 1
    
    # 如果使用流式响应
    if chat_data.stream:
        return StreamingResponse(
            stream_chat_response(chat_data.message, session_id, session),
            media_type="text/event-stream"
        )
    
    # 非流式响应
    try:
        user_input = chat_data.message
        response_text, media_info = await process_mcp_chat_message(user_input, session)
        
        # 更新会话历史
        session["history"].append({
            "user": user_input,
            "assistant": response_text,
            "timestamp": time.time(),
            "has_media": media_info is not None
        })
        
        # 限制历史记录长度
        if len(session["history"]) > 50:
            session["history"] = session["history"][-50:]
        
        # 构建响应
        has_media = media_info is not None
        
        return ChatResponse(
            success=True,
            response=response_text,
            session_id=session_id,
            has_media=has_media,
            media_info=media_info,
            timestamp=time.time(),
            tokens_used=len(response_text.split())
        )
        
    except Exception as e:
        print(f"聊天处理错误: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"处理消息时出错: {str(e)}"
        )

async def stream_chat_response(message: str, session_id: str, session: Dict):
    """
    流式响应生成器
    """
    try:
        response_parts = [
            "思考中",
            "正在分析您的问题",
            "正在调用相关工具",
            "正在生成回答",
            "回答完成"
        ]
        
        for part in response_parts:
            yield f"data: {json.dumps({'chunk': part})}\n\n"
            await asyncio.sleep(0.5)
        
        full_response, media_info = await process_mcp_chat_message(message, session)
        
        words = full_response.split()
        for i, word in enumerate(words):
            if i % 3 == 0:
                yield f"data: {json.dumps({'chunk': ' '.join(words[:i+1])})}\n\n"
                await asyncio.sleep(0.05)
        
        completion_data = {
            "complete": True,
            "session_id": session_id,
            "has_media": media_info is not None,
            "media_info": media_info
        }
        yield f"data: {json.dumps(completion_data)}\n\n"
        
    except Exception as e:
        error_data = {"error": str(e), "complete": True}
        yield f"data: {json.dumps(error_data)}\n\n"

# ====================== 辅助API端口 ======================
@app.get("/api/sessions/{session_id}")
async def get_session_info(session_id: str, authenticated: bool = Depends(get_current_user)):
    """获取会话信息"""
    if session_id in conversation_sessions:
        session = conversation_sessions[session_id]
        return {
            "session_id": session_id,
            "created_at": session["created_at"],
            "last_active": session["last_active"],
            "message_count": session["message_count"],
            "history_length": len(session["history"])
        }
    else:
        raise HTTPException(status_code=404, detail="会话不存在")

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, authenticated: bool = Depends(get_current_user)):
    """删除会话"""
    if session_id in conversation_sessions:
        del conversation_sessions[session_id]
        return {"success": True, "message": "会话已删除"}
    else:
        raise HTTPException(status_code=404, detail="会话不存在")

@app.get("/api/health")
async def health_check():
    """健康检查端口"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "active_sessions": len(conversation_sessions),
        "service": "MCP智能助手API"
    }

@app.get("/")
async def root():
    """根路径，显示API信息"""
    return {
        "service": "MCP智能助手API",
        "version": "2.0.0",
        "endpoints": {
            "login": "/api/login (POST)",
            "chat": "/api/chat (POST)",
            "health": "/api/health (GET)",
            "docs": "/api/docs"
        }
    }

# ====================== 会话清理任务 ======================
async def cleanup_old_sessions():
    """定期清理超过24小时不活动的会话"""
    while True:
        await asyncio.sleep(3600)
        current_time = time.time()
        expired_sessions = []
        
        for session_id, session in conversation_sessions.items():
            if current_time - session["last_active"] > 86400:
                expired_sessions.append(session_id)
        
        for session_id in expired_sessions:
            del conversation_sessions[session_id]
        
        if expired_sessions:
            print(f"已清理 {len(expired_sessions)} 个过期会话")



# ====================== 主程序入口 ======================
if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=False,  # 改为False避免警告
        log_level="info",
        access_log=True
    )