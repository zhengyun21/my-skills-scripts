#!/usr/bin/env python3
"""
常用软件自动下载工具
支持自动从官网获取最新版本链接并下载到本地。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import questionary
from questionary import Style
import requests
from bs4 import BeautifulSoup

# ---------- 常量 ----------
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": USER_AGENT}
CHUNK_SIZE = 1024 * 1024  # 1MB

_session = requests.Session()
_session.verify = False
_session.headers.update(HEADERS)
try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    pass


def _bypass_js_cookie_challenge(url: str, html_content: str) -> bool:
    """处理简单的 JS Cookie 反爬虫（如腾讯 EdgeOne 初级验证）"""
    script_match = re.search(r"<script>(.*?)</script>", html_content, re.DOTALL)
    if not script_match:
        return False
    js_code = script_match.group(1)
    if "document.cookie" not in js_code and "__tst_status" not in js_code and "EO_Bot" not in js_code:
        return False

    js_patched = re.sub(
        r'setTimeout\([^)]+\);?\s*$',
        'setTimeout=function(){};console.log("COOKIE_JSON:"+JSON.stringify(document._c));',
        js_code
    )
    node_code = f'''
    var _c = {{}};
    var document = {{
        get cookie() {{
            var parts = [];
            for (var k in _c) parts.push(k + "=" + _c[k]);
            return parts.join("; ");
        }},
        set cookie(v) {{
            var eq = v.indexOf("=");
            if (eq > 0) _c[v.substring(0, eq).trim()] = v.substring(eq + 1).split(";")[0];
        }},
        _c: _c
    }};
    var location = {{ href: "{url}", search: "" }};
    var window = {{}};
    {js_patched}
    '''
    try:
        result = subprocess.run(
            ["node", "-e", node_code],
            capture_output=True, text=True, timeout=10
        )
        match = re.search(r"COOKIE_JSON:(.*)", result.stdout)
        if match:
            import json as _json
            cookies = _json.loads(match.group(1))
            for k, v in cookies.items():
                _session.cookies.set(k, v)
            return True
    except Exception:
        pass
    return False


def load_config() -> dict:
    """加载配置文件"""
    if not CONFIG_FILE.exists():
        print(f"[错误] 配置文件不存在: {CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_by_key(data: dict, key_path: str):
    """通过点分隔的路径从嵌套字典中取值，如 'data.downloadUrl'"""
    keys = key_path.split(".")
    for k in keys:
        if isinstance(data, dict):
            data = data.get(k)
        else:
            return None
    return data


def extract_domains(software_list: list) -> list[str]:
    """从软件列表中提取所有下载域名"""
    domains = set()
    for sw in software_list:
        url = ""
        if sw["type"] == "fixed_url":
            url = sw.get("url", "")
        elif sw["type"] == "api":
            url = sw.get("api_url", "")
        elif sw["type"] == "page_parse":
            url = sw.get("download_page", "")
        if url:
            try:
                hostname = urlparse(url).hostname
                if hostname:
                    domains.add(hostname)
            except Exception:
                pass
    return sorted(domains)


def check_network(software_list: list) -> dict:
    """检查网络环境和各软件下载源的可达性"""
    print("\n[网络检查] 正在检查网络环境...")

    results = {}

    # 1. 基础连通性：DNS 53端口
    dns_ok = False
    for dns_host in [("8.8.8.8", 53), ("114.114.114.114", 53)]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            start = time.time()
            sock.connect(dns_host)
            elapsed = (time.time() - start) * 1000
            sock.close()
            dns_ok = True
            print(f"  [基础] DNS 连通性: ✓ 正常 ({dns_host[0]}:{dns_host[1]}, 延迟 {elapsed:.0f}ms)")
            break
        except Exception:
            continue
    if not dns_ok:
        print("  [基础] DNS 连通性: ✗ 异常，可能无法下载")

    # 2. 各软件来源域名可达性
    domains = extract_domains(software_list)
    for domain in domains:
        reachable = False
        try:
            addr_info = socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
            ip = addr_info[0][4][0]
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect((ip, 443))
            sock.close()
            reachable = True
        except Exception:
            pass
        results[domain] = reachable
        status = "✓ 可达" if reachable else "✗ 不可达"
        print(f"  [来源] {domain:30s} -> {status}")

    reachable_count = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"  网络检查完成: {reachable_count}/{total} 个来源可达\n")
    return results


def interactive_select(software_list: list, default_dir: str) -> tuple[list, Path]:
    """交互式选择：箭头键导航，空格勾选，回车确认"""
    print("\n" + "=" * 60)
    print("  常用软件自动下载工具")
    print("=" * 60)

    # 构建选项列表，每项格式: "微信 (fixed_url)"
    # 标题用富文本列表形式，让文字始终走 class:text，不受选中状态影响，
    # 这样只有前面的圆圈会因选中而变色，整行文字保持原色。
    choices = []
    for sw in software_list:
        label = f"{sw['name']} ({sw['type']})"
        choices.append(
            questionary.Choice(title=[("class:text", label)], value=sw["id"])
        )

    # 自定义样式：空格勾选后，实心圆圈 ● 显示为绿色（而非灰色实心）
    custom_style = Style([
        ("selected", "fg:green noreverse"),  # 选中项的圆圈标记显示绿色
        ("pointer", "fg:cyan bold"),
    ])

    # 复选框选择
    selected_ids = questionary.checkbox(
        "请选择要下载的软件（空格勾选，回车确认）:",
        choices=choices,
        style=custom_style,
    ).ask()

    if not selected_ids:
        print("未选择任何软件，退出。")
        sys.exit(0)

    # 根据 id 找回软件配置
    selected_sw = [sw for sw in software_list if sw["id"] in selected_ids]
    print(f"\n已选择: {', '.join(sw['name'] for sw in selected_sw)}")

    # 输入下载目录
    download_dir_str = questionary.text(
        "请输入下载目录:",
        default=default_dir,
    ).ask()

    download_dir = Path(download_dir_str.strip() or default_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"下载目录: {download_dir}\n")
    return selected_sw, download_dir


def resolve_fixed_url(sw: dict) -> tuple[str, str]:
    """策略1: 固定URL，直接返回"""
    return sw["url"], sw["filename"]


def resolve_api_url(sw: dict) -> tuple[str, str]:
    """策略2: 调用API获取最新版下载链接和版本号"""
    api_url = sw["api_url"]
    params = sw.get("params", {})
    version_key = sw.get("version_key", "")

    try:
        resp = _session.get(api_url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(f"API请求失败: {e}")

    # GitHub Releases 风格：在 assets 数组中按资产名正则匹配，取其下载地址
    asset_name_pattern = sw.get("asset_name_pattern", "")
    if asset_name_pattern:
        pattern = re.compile(asset_name_pattern)
        download_url = None
        for asset in data.get("assets", []):
            if pattern.search(asset.get("name", "")):
                download_url = asset.get("browser_download_url")
                break
        if not download_url:
            raise RuntimeError(f"asset_name_pattern '{asset_name_pattern}' 未匹配到任何资产")
    else:
        link_key = sw["link_key"]
        link_filter = sw.get("link_filter", "")
        download_url = get_by_key(data, link_key)
        if not download_url:
            raise RuntimeError(f"API返回中未找到链接字段 '{link_key}'")

        # 如果结果是一个数组，通过 link_filter 筛选
        if isinstance(download_url, list) and link_filter:
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
                else:
                    raise RuntimeError(f"link_filter '{link_filter}' 未匹配到任何项")
            else:
                raise RuntimeError("link_filter 仅支持 'regex:' 前缀")

        if not isinstance(download_url, str):
            raise RuntimeError(f"无法从 '{link_key}' 提取有效下载链接")

    version = ""
    if version_key:
        version = get_by_key(data, version_key) or ""

    filename = sw["filename"]
    if "{version}" in filename and version:
        filename = filename.replace("{version}", version)
    elif "{version}" in filename:
        filename = filename.replace("{version}", "latest")

    return download_url, filename


def resolve_page_parse(sw: dict) -> tuple[str, str]:
    """策略3: 解析HTML页面获取下载链接"""
    page_url = sw["download_page"]
    try:
        resp = _session.get(page_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"页面请求失败: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    download_url = None

    # 方式A: CSS选择器
    if "link_selector" in sw:
        el = soup.select_one(sw["link_selector"])
        if not el:
            raise RuntimeError(f"未找到匹配元素: {sw['link_selector']}")
        download_url = el.get(sw.get("link_attr", "href"))
        if not download_url:
            raise RuntimeError(f"元素上未找到属性: {sw.get('link_attr', 'href')}")
        if download_url.startswith("/"):
            parsed = urlparse(page_url)
            download_url = f"{parsed.scheme}://{parsed.netloc}{download_url}"
        elif not download_url.startswith("http"):
            download_url = page_url.rstrip("/") + "/" + download_url.lstrip("/")

    # 方式B: 正则匹配
    elif "link_pattern" in sw:
        pattern = re.compile(sw["link_pattern"])
        match = pattern.search(resp.text)
        if match:
            download_url = match.group(0)

    # 方式C: 综合智能解析（当A和B都失败时）
    if not download_url:
        all_urls = set(re.findall(r'https?://[^"\'<>\\\s]+', resp.text))
        
        # 从 script 标签中提取更多URL
        for script in soup.find_all("script"):
            if script.string:
                script_urls = re.findall(r'https?://[^"\'<>\\\s]+', script.string)
                all_urls.update(script_urls)
        
        # 从 onclick 属性中提取URL
        for el in soup.find_all(attrs={"onclick": True}):
            onclick = el.get("onclick", "")
            onclick_urls = re.findall(r'https?://[^"\'<>\\\s]+', onclick)
            all_urls.update(onclick_urls)

        # 排除非安装包的文件类型
        exclude_extensions = ['.json', '.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.woff', '.woff2', '.ttf', '.eot', '.map', '.txt', '.xml', '.html', '.htm']
        exclude_patterns = ['manifest.json', 'favicon', 'logo', 'icon', 'static/', 'assets/', 'css/', 'js/', 'img/', 'images/']
        
        # 筛选候选链接
        candidates = []
        keywords = sw.get("link_keywords", [".exe", "download", "setup", "install", "client", "desktop"])
        for u in all_urls:
            u_lower = u.lower()
            if any(kw in u_lower for kw in keywords):
                # 排除非安装包的文件
                if any(ext in u_lower for ext in exclude_extensions):
                    continue
                if any(pat in u_lower for pat in exclude_patterns):
                    continue
                candidates.append(u)

        if candidates:
            # 优先测试包含特定关键词的链接
            priority_keywords = [".exe", "setup", "install", ".msi", ".msix"]
            priority_candidates = [u for u in candidates if any(kw in u.lower() for kw in priority_keywords)]
            test_list = priority_candidates + [u for u in candidates if u not in priority_candidates]
            
            exclude_types = ['text/html', 'text/css', 'javascript', 'image', 'application/json', 'font/', 'text/xml', 'text/plain', 'image/svg']
            
            for u in test_list[:30]:
                try:
                    r = _session.head(u, timeout=8, allow_redirects=True)
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
                        r = _session.get(u, timeout=8, stream=True)
                        if r.status_code in (200, 206):
                            ct = r.headers.get('content-type', '')
                            cl = r.headers.get('content-length', '0')
                            if not any(et in ct for et in exclude_types) and (cl == '0' or int(cl) > 102400):
                                download_url = u
                                break
                    except:
                        pass

    if not download_url:
        raise RuntimeError(f"无法从页面解析下载链接: {page_url}")

    return download_url, sw["filename"]


def resolve_download_url(sw: dict) -> tuple[str, str]:
    """根据软件类型解析下载链接，返回 (download_url, filename)"""
    sw_type = sw["type"]
    print(f"  [{sw['name']}] 正在获取最新版下载链接...")

    if sw_type == "fixed_url":
        return resolve_fixed_url(sw)
    elif sw_type == "api":
        return resolve_api_url(sw)
    elif sw_type == "page_parse":
        return resolve_page_parse(sw)
    else:
        raise RuntimeError(f"未知的下载类型: {sw_type}")


def download_file(url: str, filepath: Path, name: str) -> bool:
    """下载文件，带进度条和重试，支持简单 JS 反爬虫绕过"""
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = _session.get(url, stream=True, timeout=30)
            resp.raise_for_status()

            chunk = next(resp.iter_content(chunk_size=512), b"")
            ct = resp.headers.get("content-type", "")
            is_html = "html" in ct.lower() or chunk.lstrip()[:5] == b"<html" or chunk.lstrip()[:7] == b"<script"
            is_pe = chunk[:2] == b"MZ"

            if is_html and not is_pe:
                html_text = chunk.decode("utf-8", errors="ignore") + resp.text
                print(f"\n  [{name}] 检测到反爬虫验证页面，尝试自动绕过...")
                if _bypass_js_cookie_challenge(url, html_text):
                    print(f"  [{name}] 反爬虫 Cookie 已获取，重新请求...")
                    resp.close()
                    time.sleep(1)
                    resp = _session.get(url, stream=True, timeout=30)
                    chunk = next(resp.iter_content(chunk_size=512), b"")
                    ct = resp.headers.get("content-type", "")
                    is_html = "html" in ct.lower() or chunk.lstrip()[:5] == b"<html"
                    is_pe = chunk[:2] == b"MZ"
                    if is_html and not is_pe:
                        print(f"  [{name}] 警告: 仍需进一步验证（可能需要人机验证）")
                else:
                    print(f"  [{name}] 警告: 无法自动绕过反爬虫")

            if not is_pe and is_html:
                resp.close()
                raise RuntimeError("下载链接返回的是 HTML 页面，可能需要人机验证或链接已失效")

            total_size = int(resp.headers.get("content-length", 0))
            downloaded = len(chunk)
            start_time = time.time()

            with open(filepath, "wb") as f:
                if chunk:
                    f.write(chunk)
                for data in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if data:
                        f.write(data)
                        downloaded += len(data)
                        if total_size > 0:
                            percent = downloaded / total_size * 100
                            elapsed = time.time() - start_time
                            speed = downloaded / elapsed / 1024 / 1024 if elapsed > 0 else 0
                            bar_len = 30
                            filled = int(bar_len * downloaded / total_size)
                            bar = "█" * filled + "░" * (bar_len - filled)
                            print(
                                f"\r  [{name}] {bar} {percent:.1f}% "
                                f"({downloaded / 1024 / 1024:.1f}/{total_size / 1024 / 1024:.1f}MB, "
                                f"{speed:.1f}MB/s)",
                                end="",
                            )
            print()
            return True

        except Exception as e:
            print(f"\n  [{name}] 第{attempt}次下载失败: {e}")
            if attempt < max_retries:
                print(f"  [{name}] 等待3秒后重试...")
                time.sleep(3)
            else:
                return False

    return False


def verify_file(filepath: Path, name: str) -> bool:
    """验证下载的文件是否有效"""
    if not filepath.exists():
        return False
    size_mb = filepath.stat().st_size / 1024 / 1024
    if size_mb < 0.5:  # 小于0.5MB的安装包可能有问题
        print(f"  [{name}] 警告: 文件过小 ({size_mb:.2f}MB)，可能下载不完整")
        return False
    print(f"  [{name}] 下载完成，文件大小: {size_mb:.2f}MB")
    return True


def process_software(sw: dict, download_dir: Path) -> bool:
    """处理单个软件的下载流程"""
    name = sw["name"]
    try:
        # 1. 解析下载链接
        download_url, filename = resolve_download_url(sw)
        print(f"  [{name}] 链接: {download_url[:80]}...")
        print(f"  [{name}] 文件名: {filename}")

        # 2. 下载
        filepath = download_dir / filename
        print(f"  [{name}] 开始下载...")
        if not download_file(download_url, filepath, name):
            print(f"  [{name}] 下载失败!")
            return False

        # 3. 验证
        if not verify_file(filepath, name):
            return False

        return True

    except Exception as e:
        print(f"  [{name}] 错误: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="常用软件自动下载工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python auto_download.py                        # 交互模式，勾选软件并下载
  python auto_download.py --ids wechat,qq        # CLI模式，下载指定软件
  python auto_download.py --list                 # 列出所有可用软件
  python auto_download.py --exclude chrome,edge  # CLI模式，排除指定软件
        """,
    )
    parser.add_argument(
        "--ids",
        type=str,
        default="",
        help="指定要下载的软件ID，多个用逗号分隔（如: wechat,qq,chrome）",
    )
    parser.add_argument(
        "--exclude",
        type=str,
        default="",
        help="排除的软件ID，多个用逗号分隔",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出所有可用软件",
    )
    parser.add_argument(
        "--dir",
        type=str,
        default="",
        help="指定下载目录（默认使用config中的download_dir）",
    )
    args = parser.parse_args()

    config = load_config()
    all_software = [sw for sw in config["software"] if sw.get("enabled", True)]
    default_dir = str(SCRIPT_DIR / config["download_dir"])
    max_workers = config.get("max_workers", 3)

    # --list：列出软件后退出
    if args.list:
        print("\n可用软件列表:")
        print("-" * 60)
        for sw in config["software"]:
            status = "[启用]" if sw.get("enabled", True) else "[禁用]"
            print(f"  {sw['id']:20s} {status} {sw['name']} ({sw['type']})")
        print("-" * 60)
        return

    # CLI 模式：指定了 --ids 或 --exclude
    if args.ids or args.exclude:
        target_ids = set()
        if args.ids:
            target_ids = set(i.strip() for i in args.ids.split(",") if i.strip())

        exclude_ids = set()
        if args.exclude:
            exclude_ids = set(i.strip() for i in args.exclude.split(",") if i.strip())

        selected = []
        for sw in all_software:
            if target_ids and sw["id"] not in target_ids:
                continue
            if sw["id"] in exclude_ids:
                continue
            selected.append(sw)

        download_dir = Path(args.dir) if args.dir else Path(default_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        if not selected:
            print("没有需要下载的软件。使用 --list 查看可用列表。")
            return

        print(f"\n{'='*60}")
        print(f"准备下载 {len(selected)} 个软件到: {download_dir}")
        print(f"{'='*60}\n")

        # 网络检查
        check_network(selected)
    else:
        # 交互模式：无选择参数
        if not all_software:
            print("没有可用的软件。")
            return

        selected, download_dir = interactive_select(all_software, default_dir)
        if not selected:
            return

        # 网络检查
        check_network(selected)

    # 开始下载
    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_software, sw, download_dir): sw
            for sw in selected
        }
        for future in as_completed(futures):
            sw = futures[future]
            try:
                if future.result():
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                print(f"  [{sw['name']}] 未知错误: {e}")
                fail_count += 1

    print(f"\n{'='*60}")
    print(f"下载完成: 成功 {success_count} 个, 失败 {fail_count} 个")
    print(f"文件保存位置: {download_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()