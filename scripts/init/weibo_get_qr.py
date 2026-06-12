"""微博登录步骤1：启动 Chromium headless，获取二维码

浏览器进程与本脚本完全分离，步骤2执行时浏览器保持运行。

输出 JSON 包含 QR 图片路径，供 agent 展示给用户扫码。

使用:
  python scripts/init/weibo_get_qr.py
  python scripts/init/weibo_get_qr.py --no-sandbox
  python scripts/init/weibo_get_qr.py --browser-path /opt/chrome/google-chrome
"""

import sys
import json
import asyncio
import subprocess
import argparse
from pathlib import Path
from urllib.request import urlretrieve
from tempfile import mkdtemp

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR.parent))

from common import setup_logging

logger = setup_logging("weibo_get_qr")
QR_IMAGE_PATH = Path("/tmp/weibo_login_qr.png")
SESSION_PATH = Path("/tmp/weibo_browser_session.json")

LOGIN_URL = "https://weibo.com/newlogin?tabtype=weibo&openLoginLayer=1"


def _check_nodriver():
    try:
        import nodriver  # noqa: F401
    except ImportError:
        print(
            f"错误: 未安装 nodriver 包。\n"
            f"请运行: {sys.executable} -m pip install 'nodriver>=0.50'\n"
            f"当前 Python: {sys.executable}",
            file=sys.stderr,
        )
        sys.exit(1)


def _find_chrome(browser_path: str = "") -> str:
    """查找 Chrome/Chromium 可执行文件路径"""
    if browser_path:
        path = Path(browser_path)
        if path.exists() and path.is_file():
            return str(path)
        raise FileNotFoundError(f"指定的浏览器路径不存在: {browser_path}")

    from nodriver.core.config import find_chrome_executable
    return find_chrome_executable()


def _free_port() -> int:
    import socket
    free_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_socket.bind(("127.0.0.1", 0))
    free_socket.listen(5)
    port: int = free_socket.getsockname()[1]
    free_socket.close()
    return port


def _build_chrome_args(port: int, sandbox: bool) -> list:
    """构建 Chrome headless 启动参数"""
    user_data_dir = mkdtemp(prefix="uc_")
    args = [
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-service-autorun",
        "--no-default-browser-check",
        "--homepage=about:blank",
        "--no-pings",
        "--password-store=basic",
        "--disable-infobars",
        "--disable-breakpad",
        "--disable-dev-shm-usage",
        "--disable-session-crashed-bubble",
        "--disable-search-engine-choice-screen",
        f"--user-data-dir={user_data_dir}",
        "--disable-session-crashed-bubble",
        "--disable-features=IsolateOrigins,site-per-process",
        "--headless=new",
        "--noerrdialogs",
        "--ozone-platform=headless",
        "--ozone-override-screen-size=800,600",
        "--use-angle=swiftshader-webgl",
        "--remote-debugging-host=127.0.0.1",
        f"--remote-debugging-port={port}",
    ]
    if not sandbox:
        args.append("--no-sandbox")
    return args


def _start_chrome_detached(chrome_path: str, port: int, sandbox: bool) -> subprocess.Popen:
    """启动 Chrome 并与当前进程组分离，脚本退出后浏览器继续运行"""
    args = [chrome_path] + _build_chrome_args(port, sandbox)
    logger.info(f"启动浏览器（分离模式）: {chrome_path}")
    logger.debug(f"浏览器参数: {' '.join(args)}")

    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info(f"浏览器 PID: {process.pid}, 调试端口: {port}")
    return process


async def _connect_and_get_qr(host: str, port: int, chrome_pid: int) -> str:
    """连接已有浏览器，打开微博登录页，获取二维码图片"""
    import nodriver as uc

    browser = await uc.Browser.create(host=host, port=port)

    session_info = {
        "host": browser.config.host,
        "port": browser.config.port,
        "websocket_url": browser.websocket_url,
        "chrome_pid": chrome_pid,
    }
    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SESSION_PATH, "w") as f:
        json.dump(session_info, f)
    logger.info(f"浏览器会话信息已保存: {SESSION_PATH}")

    tab = browser.main_tab
    await tab.get(LOGIN_URL)
    await tab.sleep(15)

    qr_data = {}
    for attempt in range(15):
        if attempt > 0:
            await tab.sleep(2)
        qr_result = await tab.evaluate("""
            (() => {
                const imgs = document.querySelectorAll('img');
                for (const img of imgs) {
                    if (img.width > 50 && /(?:v2\\.)?qr\\.weibo\\.cn/.test(img.src)) {
                        return JSON.stringify({found: true, src: img.src});
                    }
                }
                return JSON.stringify({found: false, total: imgs.length});
            })()
        """)
        qr_data = json.loads(qr_result)
        if qr_data.get("found"):
            break
        logger.info(f"等待二维码加载... ({attempt + 1}/15)")

    if qr_data.get("found"):
        logger.info(f"下载二维码: {qr_data['src'][:80]}...")
        urlretrieve(qr_data["src"], str(QR_IMAGE_PATH))
        logger.info(f"二维码已保存: {QR_IMAGE_PATH}")
        return qr_data["src"]
    else:
        img_count = qr_data.get("total", 0)
        if img_count > 10:
            hint = "页面可能加载了微博信息流而非登录页（检测到大量内容图片）。请重试或检查 IP 是否被限制。"
        elif img_count == 0:
            hint = "页面未加载任何图片，可能是网络不通或 Chromium 无法渲染页面。"
        else:
            hint = "页面已加载但未找到 QR 码图片，可能是微博页面结构发生了变化。"
        raise RuntimeError(f"二维码加载失败 (共 {img_count} 张图片)。{hint}")


def main():
    _check_nodriver()
    parser = argparse.ArgumentParser(description="微博登录 - 步骤1：获取二维码")
    parser.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                        help="禁用 Chromium 沙箱（Docker/snap 环境可能需要）")
    parser.add_argument("--browser-path", type=str, default="",
                        help="指定 Chromium 浏览器路径")
    args = parser.parse_args()

    try:
        chrome_path = _find_chrome(args.browser_path)
        port = _free_port()
        chrome_proc = _start_chrome_detached(chrome_path, port, args.sandbox)
        qr_url = asyncio.run(_connect_and_get_qr(host="127.0.0.1", port=port, chrome_pid=chrome_proc.pid))
    except Exception as e:
        msg = f"获取二维码失败: {e}"
        print(msg, file=sys.stderr)
        logger.error(msg)
        sys.exit(1)

    print(json.dumps({
        "action": "qr_ready",
        "qr_image_path": str(QR_IMAGE_PATH),
        "qr_image_url": qr_url,
        "session_file": str(SESSION_PATH),
        "message": "二维码已就绪，请用微博 App 扫描。完成后执行步骤2脚本等待登录。",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
