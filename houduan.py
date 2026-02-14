


# ====================== 【需要手动填写的配置区域】 ======================
# 请在使用前手动填写以下信息

# 1. 你的LLM API信息
LLM_API_KEY =   # 需要填写：你的API密钥
LLM_BASE_URL =   # 需要填写：你的平台URL（智谱AI的完整URL，不需要拼接）
LLM_MODEL =   # 需要填写：你的模型名，如 qwen-max

# 2. Serper 搜索 API 信息
SERPER_API_KEY =    # 需要填写：你的 Serper API Key
SERPER_ENDPOINT = 
SERPER_RESULT_COUNT = 10 #可选，默认是这么多

# ====================== 【代码主体区域】 ======================
import uvicorn
import time
import json
from typing import Optional, Dict, Any, List, Tuple
from fastapi import FastAPI, HTTPException, Depends, Request, Header, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import asyncio
import requests
import os
import uuid
import secrets
import traceback

import re
import threading
from urllib.parse import urlparse

import fitz  # PyMuPDF
import docx
from bs4 import BeautifulSoup
from DrissionPage import ChromiumPage, ChromiumOptions


# 当为 True 时，整个流程使用本地模拟结果（不调用外部 LLM）
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
    "http://localhost:8080",      # Nginx 默认端口
    "http://127.0.0.1:8080"
]

# ====================== 工具信息 ======================
SEARCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "common_search",
            "description": "标准搜索接口：先调用 Serper 搜索，再筛选 3 个最有信息链接并抓取中文内容，返回给模型用于总结。",
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


# ====================== 浏览器预启动（单用户版本） ======================
_global_baidu_browser_page: Optional[ChromiumPage] = None
_global_baidu_browser_ready_at: Optional[float] = None


def prepare_baidu_browser() -> Dict[str, Any]:
    global _global_baidu_browser_page
    global _global_baidu_browser_ready_at

    if _global_baidu_browser_page is not None:
        return {
            "success": True,
            "message": "browser already prepared",
            "ready_at": _global_baidu_browser_ready_at,
        }

    _co = ChromiumOptions()
    # 兼容不同 DrissionPage 版本/类型检查：优先用 set_user_data_path
    _co.set_user_data_path(os.path.abspath("drission_profile_baidu"))
    _co.headless(True)
    _co.no_imgs(True)

    page = ChromiumPage(_co)
    _global_baidu_browser_page = page
    _global_baidu_browser_ready_at = time.time()

    return {
        "success": True,
        "message": "browser prepared",
        "ready_at": _global_baidu_browser_ready_at,
    }


def get_prepared_baidu_browser() -> Optional[ChromiumPage]:
    return _global_baidu_browser_page


def close_prepared_baidu_browser() -> None:
    global _global_baidu_browser_page
    global _global_baidu_browser_ready_at

    if _global_baidu_browser_page is None:
        return

    try:
        _global_baidu_browser_page.quit()
    except Exception:
        pass

    _global_baidu_browser_page = None
    _global_baidu_browser_ready_at = None


# ====================== 知识库处理函数（PDF/Word 解析与检索） ======================

def kb_split_into_sentences(text: str) -> List[str]:
    """将文本按中英文句子结尾符号分割，并过滤掉太短的行"""
    # 匹配：。 ！ ？ . ! ? \n
    parts = re.split(r'([。！？.!？\n])', text)
    sentences = []
    for i in range(0, len(parts)-1, 2):
        s = parts[i].strip() + parts[i+1]
        if len(s) > 2:
            sentences.append(s)
    # 补齐最后一段
    if len(parts) % 2 != 0:
        s = parts[-1].strip()
        if len(s) > 2:
            sentences.append(s)
    return sentences

def kb_search_sentences(query: str, kb_sentences: List[str]) -> str:
    """在句子列表中检索关键词匹配度最高的句子，并带上上下文"""
    if not kb_sentences:
        return ""
    
    # 1) 提取关键词
    keywords = extract_keywords_5_with_llm(query)
    if not keywords:
        keywords = [w for w in re.split(r"\s+", query) if w][:5]
    
    # 2) 对每个句子打分
    scores = []
    for idx, sentence in enumerate(kb_sentences):
        score = 0
        for kw in keywords:
            if kw.lower() in sentence.lower():
                score += 1
        scores.append((idx, score))
    
    # 3) 按分数排序，取 Top 3-5
    sorted_scores = sorted(scores, key=lambda x: x[1], reverse=True)
    top_indices = [idx for idx, score in sorted_scores[:5] if score > 0]
    
    if not top_indices:
        return ""
    
    # 4) 获取上下文（前后各1句）并去重拼接
    result_indices = set()
    for idx in top_indices:
        result_indices.add(idx)
        if idx > 0: result_indices.add(idx - 1)
        if idx < len(kb_sentences) - 1: result_indices.add(idx + 1)
    
    sorted_final_indices = sorted(list(result_indices))
    context_parts = [kb_sentences[idx] for idx in sorted_final_indices]
    
    return "\n".join(context_parts)

# ====================== 工具实现（保持 tool-call 流程不变） ======================

def simple_serper_search(query: str, num: int = SERPER_RESULT_COUNT) -> Dict[str, Any]:
    headers = {
        'X-API-KEY': SERPER_API_KEY,
        'Content-Type': 'application/json'
    }

    data = {
        "q": query,
        "num": num
    }

    try:
        response = requests.post(SERPER_ENDPOINT, headers=headers, data=json.dumps(data), timeout=30)
        response.raise_for_status()

        response_result = response.json()

        results: Dict[str, Any] = {}
        box_result = []
        knowledge_result = []

        if 'answerBox' in response_result:
            box_result.append(response_result['answerBox'])
            results["answerBox"] = box_result
        if 'knowledgeGraph' in response_result:
            knowledge_result.append(response_result['knowledgeGraph'])
            results["knowledgeGraph"] = knowledge_result

        result = []
        organic = response_result.get('organic', []) or []
        for i, item in enumerate(organic, 1):
            result.append(item.get('title', '无标题'))
            result.append(item.get('snippet', '无摘要'))
            result.append(item.get('link', '无链接'))
            results[f"result{i}"] = result
            result = []

        return results

    except requests.exceptions.RequestException as e:
        return {"error": f"网络请求出错: {e}"}
    except json.JSONDecodeError as e:
        return {"error": f"解析JSON响应出错: {e}"}
    except Exception as e:
        return {"error": f"发生未知错误: {e}"}


def match_select_func(results: Dict[str, Any], words: List[str]) -> List[str]:
    match_count = {}
    for result_key in ['result1', 'result2', 'result3']:
        if result_key in results:
            result_list = results[result_key]
            text = str(result_list[0]) + str(result_list[1])
            count = 0
            for word in words:
                if word and (word in text):
                    count += 1
            match_count[result_key] = count

    sorted_results = sorted(match_count.items(), key=lambda x: x[1], reverse=True)

    links = []
    for i in range(min(3, len(sorted_results))):
        result_key = sorted_results[i][0]
        link = results[result_key][2]
        links.append(link)

    return links


def craw1(url: str) -> str:
    try:
        r = requests.get(url, timeout=10)
        return r.text
    except Exception as e:
        print(f"requests 抓取失败: {url} - {e}")
        return ""


def craw2(url: str, page: ChromiumPage) -> str:
    try:
        page.get(url)
        page.wait.doc_loaded()
        return page.html or ""
    except Exception as e:
        print(f"浏览器抓取失败: {url} - {e}")
        return ""


def parse1(html_content: str) -> List[str]:
    finder = BeautifulSoup(html_content, 'html.parser')

    text_span1 = finder.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    text_span2 = finder.find_all('p')
    text_span3 = finder.find_all('span')
    text_span4 = finder.find_all('div')
    text_span5 = finder.find_all('li')
    text_spans = text_span1 + text_span2 + text_span3 + text_span4 + text_span5

    contents = [span.get_text(" ", strip=True) for span in text_spans]
    return contents


def parse2(html: str) -> Tuple[List[str], bool]:
    if not html:
        return [], False

    if "百度安全验证" in html:
        return [], True

    title = ""
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()

    desc = ""
    m = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
    if m:
        desc = re.sub(r"\s+", " ", m.group(1)).strip()

    keywords = ""
    m = re.search(r'<meta[^>]+name=["\']keywords["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
    if m:
        keywords = re.sub(r"\s+", " ", m.group(1)).strip()

    parts = []
    if title:
        parts.append(title)
    if keywords:
        parts.append(keywords)
    if desc:
        parts.append(desc)

    return parts, False


def filter_chinese(contents: List[str]) -> List[str]:
    filtered_contents = []
    for content in contents:
        chinese_only = re.sub(r'[^\u4e00-\u9fff]', '', str(content))
        if chinese_only and len(chinese_only) >= 6:
            filtered_contents.append(chinese_only)

    seen = set()
    dedup = []
    for x in filtered_contents:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup


def super_threading_for_txt_craw(urls: List[str], crawed_processed: List[Dict[str, Any]], use_threading: bool = True, max_threads: int = 8):
    def worker(u):
        html = craw1(u)
        content = parse1(html)
        filtered = filter_chinese(content)
        crawed_processed.append({"url": u, "content": filtered, "blocked": False})

    if not use_threading:
        for u in urls:
            worker(u)
        return

    threads = []
    for u in urls:
        t = threading.Thread(target=worker, args=(u,))
        threads.append(t)
        t.start()

        if len(threads) >= max_threads:
            for tt in threads:
                tt.join()
            threads = []

    for tt in threads:
        tt.join()


def chrome_super_for_txt_craw(urls: List[str], crawed_processed: List[Dict[str, Any]]):
    page = get_prepared_baidu_browser()
    if page is None:
        prepare_baidu_browser()
        page = get_prepared_baidu_browser()

    if page is None:
        for u in urls:
            crawed_processed.append({"url": u, "content": [], "blocked": True})
        return

    for u in urls:
        html = craw2(u, page)
        parts, blocked = parse2(html)

        if blocked:
            crawed_processed.append({"url": u, "content": [], "blocked": True})
            continue

        filtered = filter_chinese(parts)
        crawed_processed.append({"url": u, "content": filtered, "blocked": False})


def judge_super_craw(urls: List[str], crawed_processed: List[Dict[str, Any]], use_threading_for_requests: bool = True):
    baidu_urls = []
    normal_urls = []

    for u in urls:
        host = (urlparse(u).netloc or "").lower()
        if "baike.baidu.com" in host or host.endswith(".baidu.com") or host == "baidu.com":
            baidu_urls.append(u)
        else:
            normal_urls.append(u)

    if normal_urls:
        super_threading_for_txt_craw(
            normal_urls,
            crawed_processed,
            use_threading=use_threading_for_requests,
            max_threads=8
        )

    if baidu_urls:
        chrome_super_for_txt_craw(baidu_urls, crawed_processed)


def extract_keywords_5_with_llm(user_question: str) -> List[str]:
    prompt = (
        "请从用户问题中提取5个用于网页搜索的关键词（尽量是名词/专有名词/关键短语），用 JSON 数组输出。\n"
        "要求：只输出 JSON 数组，不要输出多余文字。\n"
        f"用户问题：{user_question}"
    )

    llm_data = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是关键词提取器。"},
            {"role": "user", "content": prompt},
        ],
    }

    for attempt in range(2): # 增加重试
        try:
            resp = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json=llm_data,
                timeout=40, # 增加超时
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"].get("content", "")
                # 清洗 JSON 格式（防止 LLM 输出 markdown 代码块）
                json_str = re.sub(r'```json\s*|```', '', content).strip()
                arr = json.loads(json_str)
                if isinstance(arr, list):
                    return [str(x).strip() for x in arr if str(x).strip()][:5]
        except Exception as e:
            print(f"关键词提取尝试 {attempt+1} 失败: {e}")
            if attempt == 1: break
    return []

def common_search_tool(query: str) -> Dict[str, Any]:
    # 1) 关键词（固定5个）
    keywords = extract_keywords_5_with_llm(query)
    if not keywords:
        keywords = [w for w in re.split(r"\s+", query) if w][:5]

    # 2) serper 搜索
    results = {}
    try:
        results = simple_serper_search(query=query, num=SERPER_RESULT_COUNT)
    except Exception as e:
        print(f"Serper 搜索失败: {e}")
        return {"success": False, "error": f"搜索服务异常: {str(e)}", "keywords": keywords}

    if isinstance(results, dict) and results.get("error"):
        return {"success": False, "error": results.get("error"), "keywords": keywords}

    # 3) top3 选择
    top_links = match_select_func(results, keywords)

    # 4) 爬虫抓取（增加整体 try...except）
    crawed_processed: List[Dict[str, Any]] = []
    try:
        judge_super_craw(top_links, crawed_processed, use_threading_for_requests=True)
    except Exception as e:
        print(f"爬虫执行异常: {e}")
        # 如果爬虫全崩，至少把搜索摘要带回去
        if not crawed_processed:
            crawed_processed.append({"url": "N/A", "content": ["抓取失败，请参考摘要"], "blocked": False})

    # 5) 提取结构化信息 (answerBox 和 knowledgeGraph)
    answer_box = results.get("answerBox")
    knowledge_graph = results.get("knowledgeGraph")

    return {
        "success": True,
        "keywords": keywords,
        "answerBox": answer_box,
        "knowledgeGraph": knowledge_graph,
        "top_links": top_links,
        "crawed": crawed_processed,
        "raw_search_organic": {k: v for k, v in results.items() if k.startswith('result')}
    }


# ====================== 搜索工具分发逻辑 ======================

def execute_internal_search(tool_name, arguments):
    """
    内部搜索分发器。不再包含任何外部协议逻辑。
    只负责根据工具名称调用本地实现的 Serper+爬虫搜索。
    """
    if tool_name == "common_search":
        q = (arguments or {}).get("query")
        if not isinstance(q, str) or len(q.strip()) < 2:
            return {"success": False, "error": "搜索查询无效"}
        return common_search_tool(q.strip())
    return {"success": False, "error": f"未知工具: {tool_name}"}


async def process_ai_chat_message(
    user_input: str,
    session: Dict,
    mode: str = "general",
    search_enabled: bool = False,
    kb_enabled: bool = False
) -> tuple[str, Optional[Dict]]:
    """
    处理对话的核心逻辑：
    1. 确定模式 (mode): 通用/编程。
    2. [知识库优先]: 如果 kb_enabled，检索本地句子并标注 [用户上传资料参考]。
    3. 标注 [用户提问]。
    4. [搜索门控]: 如果 search_enabled，才向 LLM 提供搜索工具；否则不允许联网。
    """
    if OFFLINE_MODE:
        raise RuntimeError("OFFLINE_MODE 被禁止")

    try:
        # 1. 基础提示词
        if mode == "code":
            system_prompt = "你是一个专业的编程助手。请只回答编程相关问题，提供高质量、可直接运行的代码示例。"
        else:
            system_prompt = "你是一个通用的AI助手。"

        messages = [{"role": "system", "content": system_prompt}]

        # 2. 对话历史
        if session and "history" in session and session["history"]:
            for msg in session["history"]:
                messages.append({"role": "user", "content": msg["user"]})
                messages.append({"role": "assistant", "content": msg["assistant"]})

        # 3. [知识库检索] 先于搜索进行
        if kb_enabled:
            kb_sentences = session.get("kb_sentences", [])
            kb_context = kb_search_sentences(user_input, kb_sentences)
            if kb_context:
                messages.append({
                    "role": "system",
                    "content": f"[用户上传资料参考]:\n{kb_context}\n\n注意：请优先基于上述本地文档资料回答问题。"
                })

        # 4. [标注用户提问]
        messages.append({"role": "user", "content": f"[用户提问]: {user_input}"})

        # 5. [联网搜索工具门控]
        llm_data = {
            "model": LLM_MODEL,
            "messages": messages
        }
        if search_enabled:
            llm_data["tools"] = SEARCH_TOOLS
            llm_data["tool_choice"] = "auto"

        response = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
            json=llm_data,
            timeout=40
        )
        llm_result = response.json()
        
        if not isinstance(llm_result, dict) or "choices" not in llm_result or not llm_result["choices"]:
            return f"LLM 响应异常: {llm_result.get('error', {}).get('message', '未知错误')}", None

        llm_message = llm_result["choices"][0]["message"]

        # 6. 处理搜索调用（仅在开启搜索且 LLM 触发时）
        if search_enabled and "tool_calls" in llm_message and llm_message["tool_calls"]:
            tool_call = llm_message["tool_calls"][0]
            tool_name = tool_call["function"]["name"]
            tool_args = json.loads(tool_call["function"].get("arguments", "{}"))
            
            # 执行本地搜索实现
            search_result = execute_internal_search(tool_name, tool_args)
            
            # 标注搜索来源并二次总结
            final_messages = messages + [
                llm_message,
                {
                    "role": "tool",
                    "content": f"[互联网检索参考]:\n{json.dumps(search_result, ensure_ascii=False)}",
                    "tool_call_id": tool_call.get("id")
                }
            ]

            final_response = requests.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
                json={"model": LLM_MODEL, "messages": final_messages},
                timeout=40
            )
            final_result = final_response.json()
            return final_result["choices"][0]["message"].get("content", "无内容"), None

        return llm_message.get("content", "无内容"), None

    except Exception as e:
        print(traceback.format_exc())
        return f"处理过程中发生错误: {str(e)}", None


# ====================== 应用启动事件 ======================
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("AI智能助手API正在启动...")
    print(f"离线模式: {OFFLINE_MODE}")
    print(f"允许的来源: {ALLOWED_ORIGINS}")

    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        print(f"启动失败：{e}")
        raise

    asyncio.create_task(cleanup_old_sessions())

    yield

    try:
        close_prepared_baidu_browser()
    except Exception:
        pass

    print("AI智能助手API正在关闭...")


# ====================== FastAPI应用初始化 ======================
app = FastAPI(
    title="AI智能助手API",
    description="提供用户认证和智能聊天功能的API服务",
    version="2.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan
)

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
    mode: str = "general"  # "general" 或 "code"
    search_enabled: bool = False # 联网搜索独立开关
    kb_enabled: bool = False     # 知识库独立开关

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
    return username == "测试员" and password == "hongyan"


def generate_token() -> str:
    token = secrets.token_urlsafe(32)
    valid_tokens.add(token)
    return token


def verify_token(token: str) -> bool:
    return token in valid_tokens


# ====================== 运行时规则检查 ======================
def ensure_offline_mode_disabled():
    if OFFLINE_MODE:
        raise RuntimeError("OFFLINE_MODE 被禁止，请将 OFFLINE_MODE 设置为 False 并重启服务。")


# ====================== 依赖注入：验证访问令牌 ======================
async def get_current_user(authorization: Optional[str] = Header(None)) -> bool:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="缺少认证令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )

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


# ====================== API端口：预启动浏览器（单用户） ======================
@app.post("/api/websearch/prepare")
async def websearch_prepare(authenticated: bool = Depends(get_current_user)):
    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        return prepare_baidu_browser()
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"prepare browser failed: {e}")


@app.post("/api/websearch/close")
async def websearch_close(authenticated: bool = Depends(get_current_user)):
    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    try:
        close_prepared_baidu_browser()
        return {"success": True}
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"close browser failed: {e}")


@app.post("/api/kb/upload")
async def kb_upload(
    file: UploadFile = File(...),
    session_id: str = Header(...),
    authenticated: bool = Depends(get_current_user)
):
    """上传 PDF/DOCX 文件并解析为句子列表存入 session"""
    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    if session_id not in conversation_sessions:
        conversation_sessions[session_id] = {
            "history": [],
            "created_at": time.time(),
            "last_active": time.time(),
            "message_count": 0,
            "kb_sentences": []
        }
    
    session = conversation_sessions[session_id]
    session["last_active"] = time.time()

    filename = file.filename.lower()
    content = await file.read()
    text = ""

    try:
        if filename.endswith(".pdf"):
            doc = fitz.open(stream=content, filetype="pdf")
            for page in doc:
                text += page.get_text()
            doc.close()
        elif filename.endswith(".docx"):
            from io import BytesIO
            doc = docx.Document(BytesIO(content))
            text = "\n".join([para.text for para in doc.paragraphs])
        else:
            raise HTTPException(status_code=400, detail="不支持的文件格式，仅支持 PDF 和 DOCX")
    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"文件解析失败: {str(e)}")

    if not text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空或无法提取文本")

    # 按句子分割
    sentences = kb_split_into_sentences(text)
    session["kb_sentences"] = sentences

    return {
        "success": True, 
        "filename": file.filename, 
        "sentence_count": len(sentences),
        "message": "文件上传并解析完毕"
    }


@app.post("/api/kb/clear")
async def kb_clear(
    session_id: str = Header(...),
    authenticated: bool = Depends(get_current_user)
):
    """清除当前会话的知识库内容"""
    if session_id in conversation_sessions:
        conversation_sessions[session_id]["kb_sentences"] = []
    return {"success": True, "message": "知识库已清空"}


# ====================== API端口2：智能聊天 ======================
@app.post("/api/chat")
async def chat_endpoint(
    chat_data: ChatRequest,
    request: Request,
    authenticated: bool = Depends(get_current_user)
):
    try:
        ensure_offline_mode_disabled()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

    session_id = chat_data.session_id or f"session_{int(time.time())}_{hash(chat_data.message) % 10000}"

    if session_id not in conversation_sessions:
        conversation_sessions[session_id] = {
            "history": [],
            "created_at": time.time(),
            "last_active": time.time(),
            "message_count": 0
        }

    session = conversation_sessions[session_id]

    session["last_active"] = time.time()
    session["message_count"] += 1

    if chat_data.stream:
        return StreamingResponse(
            stream_chat_response(
                chat_data.message, 
                session_id, 
                session, 
                chat_data.mode, 
                chat_data.search_enabled, 
                chat_data.kb_enabled
            ),
            media_type="text/event-stream"
        )

    try:
        user_input = chat_data.message
        response_text, media_info = await process_ai_chat_message(
            user_input,
            session,
            chat_data.mode,
            chat_data.search_enabled,
            chat_data.kb_enabled
        )

        session["history"].append({
            "user": user_input,
            "assistant": response_text,
            "timestamp": time.time(),
            "has_media": media_info is not None
        })

        if len(session["history"]) > 50:
            session["history"] = session["history"][-50:]

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


async def stream_chat_response(message: str, session_id: str, session: Dict, mode: str = "general", search_enabled: bool = False, kb_enabled: bool = False):
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

        full_response, media_info = await process_ai_chat_message(message, session, mode, search_enabled, kb_enabled)

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
    if session_id in conversation_sessions:
        del conversation_sessions[session_id]
        return {"success": True, "message": "会话已删除"}
    else:
        raise HTTPException(status_code=404, detail="会话不存在")


@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "active_sessions": len(conversation_sessions),
        "service": "AI智能助手API",
    }


@app.get("/")
async def root():
    return {
        "service": "AI智能助手API",
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
        reload=False,
        log_level="info",
        access_log=True
    )
