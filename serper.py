import requests
import json
from bs4 import BeautifulSoup
import os
import re
from DrissionPage import ChromiumPage, ChromiumOptions

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

def craw(url, page):
    """底层抓取：用同一个 page 去请求不同的 url"""
    try:
        page.get(url)
        page.wait.doc_loaded()  # 等文档加载完成
        return page.html or ""
    except Exception as e:
        print(f"抓取 {url} 失败: {e}")
        return ""

def parse(html_content):
    finder = BeautifulSoup(html_content, 'html.parser')

    # 尽量多抓一些标签里的文本
    text_span1 = finder.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    text_span2 = finder.find_all('p')
    text_span3 = finder.find_all('span')
    text_span4 = finder.find_all('div')
    text_span5 = finder.find_all('li')
    text_spans = text_span1 + text_span2 + text_span3 + text_span4 + text_span5

    contents = [span.get_text(" ", strip=True) for span in text_spans]
    return contents

def filter_chinese(contents):
    """只保留长度≥6的中文片段，并按顺序去重"""
    filtered_contents = []
    for content in contents:
        chinese_only = re.sub(r'[^\u4e00-\u9fff]', '', content)
        if chinese_only and len(chinese_only) >= 6:
            filtered_contents.append(chinese_only)

    seen = set()
    dedup = []
    for x in filtered_contents:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup

def gathered_craw(url, crawed_processed, page):
    """单个 url 的完整流程：抓取 + 解析 + 中文过滤"""
    html = craw(url, page)

    # 命中安全验证页就标记一下，避免误以为抓到内容
    if "百度安全验证" in html:
        print(f"⚠️ {url} 触发百度安全验证")
        crawed_processed.append({"url": url, "content": [], "blocked": True})
        return

    content = parse(html)
    filtered_content = filter_chinese(content)
    crawed_processed.append({"url": url, "content": filtered_content, "blocked": False})

def super_for_txt_craw(urls, crawed_processed):
    """自带打开浏览器 + 循环 urls 的多页面文本爬虫"""
    _co = ChromiumOptions()
    _co.set_paths(user_data_path=os.path.abspath("drission_profile_baidu"))
    _co.headless(True)      # 默认无头；如果要手动过验证，可以临时改成 False
    _co.no_imgs(True)       # 不加载图片，加快速度

    page = ChromiumPage(_co)

    try:
        for link in urls:
            gathered_craw(link, crawed_processed, page)
    finally:
        try:
            page.quit()
        except Exception:
            pass

#爬虫实际操作
selected_links = [
    'https://baike.baidu.com/item/%E7%BA%A2%E5%B2%A9%E7%BD%91%E6%A0%A1/1103299',
    'https://redrock.team/',
    'https://github.com/RedrockTeam/about-us'
]

crawed_processed = []

super_for_txt_craw(selected_links, crawed_processed)

print(crawed_processed)