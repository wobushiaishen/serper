import requests
import json
from bs4 import BeautifulSoup
import os
import re
from DrissionPage import ChromiumPage, ChromiumOptions
import time
import threading
from urllib.parse import urlparse

SERPER_API_KEY = "c1260f4908aaa9209616a9e7348fffdbf08a44dd"  
SEARCH_QUERY = "红岩网校"                        
RESULT_COUNT = 5                  
headers = {
        'X-API-KEY': SERPER_API_KEY,
        'Content-Type': 'application/json'
    }
    
    # 构建请求数据
data = {
        "q": SEARCH_QUERY,
        "num": RESULT_COUNT
    }

# 定义搜索函数
def simple_serper_search():
    try:
        
        response = requests.post( "https://google.serper.dev/search", headers=headers, data=json.dumps(data))
        response.raise_for_status()  # 检查请求是否成功
        
    # 解析返回的JSON数据
        response_result = response.json()
    #查看是否有answerBox和knowledgeGraph
        results ={}
        box_result = []
        knowledge_result = []
        
        if 'answerBox' in response_result:
            print("存在 answerBox:")
            print(response_result['answerBox'])
            box_result.append(response_result['answerBox'])
            results["answerBox"] = box_result
        if 'knowledgeGraph' in response_result:
            print("存在 knowledgeGraph:")
            print(response_result['knowledgeGraph'])
            knowledge_result.append(response_result['knowledgeGraph'])
            results["knowledgeGraph"] = knowledge_result
    #解析organic结果
        result = []    
        for i, item in enumerate(response_result['organic'], 1):
            
            """print(f"结果 {i}:")
            print("*" * 100)
            
            print(f"  链接: {item.get('link', '无链接')}")            
            print("*" * 100)"""
            result.append(item.get('title', '无标题'))
            result.append(item.get('snippet', '无摘要'))
            result.append(item.get('link', '无链接'))
            results[f"result{i}"] = result
            result = []  # 重置result列表以准备下一次循环
        return results
        
    except requests.exceptions.RequestException as e:
        print(f"网络请求出错: {e}")
    except json.JSONDecodeError as e:
        print(f"解析JSON响应出错: {e}")
    except  Exception as e:
        print(f"发生未知错误: {e}")

#实际实施
results = simple_serper_search()
#print("搜索结果:", result)

#匹配关键词排序
def match_select_func(results,words):
    match_count = {}
    # 遍历 results 中的各个结果，计算关键词匹配次数
    for result_key in ['result1', 'result2', 'result3']:
        #将result中的前两个元素合并为一个字符串,匹配标题和摘要
        if result_key in results:
            result_list = results[result_key]
            text = result_list[0] + result_list[1]
            count = 0
            # 统计关键词匹配次数
            for word in words:
                if word in text:
                    count += 1
            match_count[result_key] = count
    
    # 按匹配次数排序（降序）
    sorted_results = sorted(match_count.items(), key=lambda x: x[1], reverse=True)
    
    # 提取前三个结果的链接（第三个元素）
    links = []
    for i in range(min(3, len(sorted_results))):
        result_key = sorted_results[i][0]
        link = results[result_key][2]
        links.append(link)
    
    return links
"""words = ["红岩网校", "重庆邮电", "学习平台"]
top_links = match_select_func(results, words)
print("匹配结果前三的链接：", top_links)"""

##匹配关键词排序
"""注意：这里的words是LLM根据用户输入与搜索结果生成的关键词"""
words = ["红岩网校", "重庆邮电", "学习平台"]
selected_links = match_select_func(results, words)
print("匹配结果前三的链接：", selected_links)

def craw1(url):
    """requests 抓取（快，适合大多数非百度站点）"""
    try:
        r = requests.get(url, timeout=10)
        return r.text
    except Exception as e:
        print(f"requests 抓取失败: {url} - {e}")
        return ""


def craw2(url, page):
    """浏览器抓取（速度优先）：只负责返回 html，不做解析"""
    try:
        page.get(url)
        page.wait.doc_loaded()
        return page.html or ""
    except Exception as e:
        print(f"浏览器抓取失败: {url} - {e}")
        return ""

def parse1(html_content):
    finder = BeautifulSoup(html_content, 'html.parser')

    text_span1 = finder.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    text_span2 = finder.find_all('p')
    text_span3 = finder.find_all('span')
    text_span4 = finder.find_all('div')
    text_span5 = finder.find_all('li')
    text_spans = text_span1 + text_span2 + text_span3 + text_span4 + text_span5

    contents = [span.get_text(" ", strip=True) for span in text_spans]
    return contents

def parse2(html):
    """百度轻量解析：只取 title/description/keywords，并返回 list[str] 方便走 filter_chinese"""
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
    
def filter_chinese(contents):
    filtered_contents = []
    for content in contents:
        chinese_only = re.sub(r'[^\u4e00-\u9fff]', '', content)
        if chinese_only and len(chinese_only) >= 6:
            filtered_contents.append(chinese_only)

    # 去重（保留顺序）
    seen = set()
    dedup = []
    for x in filtered_contents:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup


def super_threading_for_txt_craw(urls, crawed_processed, use_threading=True, max_threads=8):
    def worker(u):
        html = craw1(u)
        # 调用 parse1（原版全量解析）
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

def chrome_super_for_txt_craw(urls, crawed_processed):
    _co = ChromiumOptions()
    _co.set_paths(user_data_path=os.path.abspath("drission_profile_baidu"))
    _co.headless(True)
    _co.no_imgs(True)

    page = ChromiumPage(_co)
    try:
        for u in urls:
            # 1. 抓取 HTML
            html = craw2(u, page)
            
            # 2. 调用 parse2（百度轻量解析）
            parts, blocked = parse2(html)

            if blocked:
                crawed_processed.append({"url": u, "content": [], "blocked": True})
                continue

            # 3. 过滤中文并保存
            filtered = filter_chinese(parts)
            crawed_processed.append({"url": u, "content": filtered, "blocked": False})
    finally:
        try:
            page.quit()
        except Exception:
            pass

def judge_super_craw(urls, crawed_processed, use_threading_for_requests=True):
    baidu_urls = []
    normal_urls = []

    for u in urls:
        host = (urlparse(u).netloc or "").lower()
        if "baike.baidu.com" in host or host.endswith(".baidu.com") or host == "baidu.com":
            baidu_urls.append(u)
        else:
            normal_urls.append(u)

    # 先抓普通站：requests + 多线程（快）
    if normal_urls:
        super_threading_for_txt_craw(
            normal_urls,
            crawed_processed,
            use_threading=use_threading_for_requests,
            max_threads=8
        )

    # 再抓百度：浏览器（稳）
    if baidu_urls:
        chrome_super_for_txt_craw(baidu_urls, crawed_processed)

selected_links = [
    'https://baike.baidu.com/item/%E7%BA%A2%E5%B2%A9%E7%BD%91%E6%A0%A1/1103299',
    'https://redrock.team/',
    'https://github.com/RedrockTeam/about-us'
]

crawed_processed = []
judge_super_craw(selected_links, crawed_processed, use_threading_for_requests=True)

print(crawed_processed)