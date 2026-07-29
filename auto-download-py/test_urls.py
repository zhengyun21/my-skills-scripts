#!/usr/bin/env python3
"""测试所有软件下载URL的可用性"""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT, "Range": "bytes=0-1023"}


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_by_key(data, key_path):
    keys = key_path.split(".")
    for k in keys:
        if isinstance(data, dict):
            data = data.get(k)
        else:
            return None
    return data


def test_url(url, name):
    """测试单个URL是否可下载，先HEAD再GET"""
    try:
        resp = requests.head(url, headers=HEADERS, timeout=15, allow_redirects=True)
        status = resp.status_code
        if status in (200, 206, 302, 301):
            size = resp.headers.get("content-length", "unknown")
            content_type = resp.headers.get("content-type", "unknown")
            return True, f"状态:{status}, 大小:{size}, 类型:{content_type}"
        elif status == 404:
            pass
        else:
            return False, f"HTTP状态:{status}"
    except Exception:
        pass

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True, stream=True)
        status = resp.status_code
        if status in (200, 206):
            size = resp.headers.get("content-length", "unknown")
            content_type = resp.headers.get("content-type", "unknown")
            resp.close()
            return True, f"GET状态:{status}, 大小:{size}, 类型:{content_type}"
        else:
            return False, f"GET-HTTP状态:{status}"
    except requests.exceptions.Timeout:
        return False, "超时"
    except requests.exceptions.ConnectionError as e:
        return False, f"连接错误: {str(e)[:80]}"
    except Exception as e:
        return False, f"错误: {str(e)[:80]}"


def resolve_and_test(sw):
    """解析下载链接并测试"""
    name = sw["name"]
    sw_type = sw["type"]
    download_url = None

    try:
        if sw_type == "fixed_url":
            download_url = sw["url"]

        elif sw_type == "api":
            api_url = sw["api_url"]
            params = sw.get("params", {})
            resp = requests.get(api_url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            download_url = get_by_key(data, sw["link_key"])

            if isinstance(download_url, list) and sw.get("link_filter", ""):
                link_filter = sw["link_filter"]
                if link_filter.startswith("regex:"):
                    pattern = re.compile(link_filter[6:])
                    for item in download_url:
                        candidate = (
                            item.get("browser_download_url")
                            or item.get("downloadUrl")
                            or item.get("url", "")
                        )
                        if pattern.search(candidate):
                            download_url = candidate
                            break

        elif sw_type == "page_parse":
            page_url = sw["download_page"]
            resp = requests.get(page_url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            if "link_selector" in sw:
                el = soup.select_one(sw["link_selector"])
                if el:
                    download_url = el.get(sw.get("link_attr", "href"))
                    if download_url:
                        if download_url.startswith("/"):
                            parsed = urlparse(page_url)
                            download_url = f"{parsed.scheme}://{parsed.netloc}{download_url}"
                        elif not download_url.startswith("http"):
                            download_url = page_url.rstrip("/") + "/" + download_url.lstrip("/")
            elif "link_pattern" in sw:
                pattern = re.compile(sw["link_pattern"])
                match = pattern.search(resp.text)
                if match:
                    download_url = match.group(0)

            # 综合智能解析
            if not download_url:
                all_urls = set(re.findall(r'https?://[^"\'<>\\\s]+', resp.text))
                for script in soup.find_all("script"):
                    if script.string:
                        script_urls = re.findall(r'https?://[^"\'<>\\\s]+', script.string)
                        all_urls.update(script_urls)
                for el in soup.find_all(attrs={"onclick": True}):
                    onclick = el.get("onclick", "")
                    onclick_urls = re.findall(r'https?://[^"\'<>\\\s]+', onclick)
                    all_urls.update(onclick_urls)

                exclude_extensions = ['.json', '.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.woff2', '.ttf', '.eot', '.map', '.txt', '.xml', '.html', '.htm']
                exclude_patterns = ['manifest.json', 'favicon', 'logo', 'icon', 'static/', 'assets/', 'css/', 'js/', 'img/', 'images/']
                
                candidates = []
                keywords = sw.get("link_keywords", [".exe", "download", "setup", "install", "client", "desktop"])
                for u in all_urls:
                    u_lower = u.lower()
                    if any(kw in u_lower for kw in keywords):
                        if any(ext in u_lower for ext in exclude_extensions):
                            continue
                        if any(pat in u_lower for pat in exclude_patterns):
                            continue
                        candidates.append(u)

                if candidates:
                    priority_keywords = [".exe", "setup", "install", ".msi", ".msix"]
                    priority_candidates = [u for u in candidates if any(kw in u.lower() for kw in priority_keywords)]
                    test_list = priority_candidates + [u for u in candidates if u not in priority_candidates]
                    
                    exclude_types = ['text/html', 'text/css', 'javascript', 'image', 'application/json', 'font/', 'text/xml', 'text/plain', 'image/svg']
                    
                    for u in test_list[:30]:
                        try:
                            r = requests.head(u, headers=HEADERS, timeout=8, allow_redirects=True)
                            if r.status_code in (200, 206):
                                ct = r.headers.get('content-type', '')
                                cl = r.headers.get('content-length', '0')
                                if not any(et in ct for et in exclude_types) and int(cl) > 102400:
                                    download_url = u
                                    break
                        except:
                            pass
                    
                    if not download_url:
                        for u in test_list[:15]:
                            try:
                                r = requests.get(u, headers=HEADERS, timeout=8, stream=True)
                                if r.status_code in (200, 206):
                                    ct = r.headers.get('content-type', '')
                                    cl = r.headers.get('content-length', '0')
                                    if not any(et in ct for et in exclude_types) and (cl == '0' or int(cl) > 102400):
                                        download_url = u
                                        break
                            except:
                                pass

        if not download_url or not isinstance(download_url, str):
            return False, f"无法解析下载链接", None

        # 测试下载链接
        ok, info = test_url(download_url, name)
        return ok, info, download_url

    except Exception as e:
        return False, f"解析失败: {str(e)[:100]}", None


def main():
    config = load_config()
    software_list = [sw for sw in config["software"] if sw.get("enabled", True)]

    print("=" * 80)
    print(f"  测试 {len(software_list)} 个软件下载URL")
    print("=" * 80)

    results = []
    for i, sw in enumerate(software_list, 1):
        print(f"\n[{i}/{len(software_list)}] {sw['name']} ({sw['type']})...", end=" ", flush=True)
        ok, info, url = resolve_and_test(sw)
        status = "✓ 可用" if ok else "✗ 失败"
        print(status)
        print(f"    URL: {(url or 'N/A')[:100]}")
        print(f"    信息: {info}")
        results.append({
            "name": sw["name"],
            "id": sw["id"],
            "type": sw["type"],
            "ok": ok,
            "info": info,
            "url": url,
        })

    print("\n" + "=" * 80)
    print("  汇总")
    print("=" * 80)
    ok_count = sum(1 for r in results if r["ok"])
    print(f"总计: {len(results)} 个, 可用: {ok_count}, 失败: {len(results) - ok_count}")

    print("\n失败列表:")
    for r in results:
        if not r["ok"]:
            print(f"  - {r['name']} ({r['id']}): {r['info']}")

    return results


if __name__ == "__main__":
    main()
