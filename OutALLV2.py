# -*- coding: utf-8 -*-
import time
import uuid
import hmac
import base64
import hashlib
import requests
import json
import subprocess
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= 配置区域 =================
# 1. 平台信息
APP_KEY = "APIkey"
APP_SECRET = "APISecret"
HOST = "平台IP地址"

# 2. 文件路径配置
INPUT_FILE = "cameras.txt"  # 资源编码列表文件
OUTPUT_DIR = r"D:\images"   # 图片保存文件夹
FAILED_LOG = "failed.txt"  # 失败清单

# 3. 并发配置 (关键参数)
# 建议设置 5~20 之间。太高会导致电脑卡顿，因为同时运行几十个 FFmpeg 会吃光 CPU。
MAX_WORKERS = 3

# 4. 基础URL
BASE_URL = f"https://{HOST}:443"
PATH = "/artemis/api/video/v1/cameras/previewURLs"
API_URL = BASE_URL + PATH

# 全局打印锁，防止多线程打印乱码
print_lock = threading.Lock()


# ===========================================

def _hmac_sha256_base64(secret: str, msg: str) -> str:
    dig = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(dig).decode("utf-8")


def build_headers_and_sign(method: str, path: str):
    accept = "*/*"
    content_type = "application/json"
    ts = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex

    sign_headers = {
        "x-ca-key": APP_KEY,
        "x-ca-nonce": nonce,
        "x-ca-timestamp": ts,
    }

    sign_headers_str = "\n".join([f"{k}:{sign_headers[k]}" for k in sorted(sign_headers.keys())])
    string_to_sign = f"{method}\n{accept}\n{content_type}\n{sign_headers_str}\n{path}"
    signature = _hmac_sha256_base64(APP_SECRET, string_to_sign)

    headers = {
        "Accept": accept,
        "Content-Type": content_type,
        "X-Ca-Key": APP_KEY,
        "X-Ca-Nonce": nonce,
        "X-Ca-Timestamp": ts,
        "X-Ca-Signature-Headers": ",".join(sorted(sign_headers.keys())),
        "X-Ca-Signature": signature,
    }
    return headers


def get_rtsp_url(camera_code):
    """获取播放地址"""
    body = {
        "cameraIndexCode": camera_code,
        "streamType": 0,
        "protocol": "rtsp",
        "transmode": 1,
        "expand": "streamform=rtp"
    }

    try:
        headers = build_headers_and_sign("POST", PATH)
        # timeout 设置短一点，快速失败
        r = requests.post(API_URL, headers=headers, json=body, verify=False, timeout=10)
        j = r.json()

        if j.get("code") == "0":
            return j["data"]["url"]
        return None
    except Exception:
        return None


def capture_snapshot(rtsp_url, save_path):
    """FFmpeg截图"""
    cmd = [
        'ffmpeg', '-y',
        '-rtsp_transport', 'tcp',
        '-i', rtsp_url,
        '-frames:v', '1',
        '-q:v', '2',
        save_path
    ]
    try:
        # 设置15秒超时，防止FFmpeg僵死
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return True
        return False
    except:
        return False


def process_single_camera(code, index, total):
    """单个点位的处理逻辑（将在线程池中运行）"""
    save_path = os.path.join(OUTPUT_DIR, f"{code}.jpg")

    # 如果图片已经存在且大小正常，跳过（断点续传功能）
    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
        with print_lock:
            print(f"[{index}/{total}] ⏭️ 跳过 (已存在): {code}")
        return code, True

    # 1. 获取URL
    url = get_rtsp_url(code)
    if not url:
        with print_lock:
            print(f"[{index}/{total}] ❌ URL获取失败: {code}")
        return code, False

    # 2. 截图
    if capture_snapshot(url, save_path):
        with print_lock:
            print(f"[{index}/{total}] ✅ 截图成功: {code}")
        return code, True
    else:
        with print_lock:
            print(f"[{index}/{total}] ⚠️ 截图失败 (超时/离线): {code}")
        return code, False


# ================= 主程序 =================
if __name__ == "__main__":
    requests.packages.urllib3.disable_warnings()

    # 1. 准备目录
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    # 2. 读取文件
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 找不到 {INPUT_FILE}，请创建并填入资源编码。")
        exit()

    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        camera_codes = [line.strip() for line in f if line.strip()]

    total_count = len(camera_codes)
    print(f"🚀 启动多线程模式 (并发数: {MAX_WORKERS})，共 {total_count} 个点位")
    print("-" * 40)

    start_time = time.time()
    failed_list = []
    success_count = 0

    # 3. 线程池执行
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # 提交所有任务
        # future_to_code 是一个字典，用来追踪每个任务对应的编码
        future_tasks = {
            executor.submit(process_single_camera, code, i + 1, total_count): code
            for i, code in enumerate(camera_codes)
        }

        # 处理结果（谁先做完谁先返回）
        for future in as_completed(future_tasks):
            code, is_success = future.result()
            if is_success:
                success_count += 1
            else:
                failed_list.append(code)

    end_time = time.time()
    duration = end_time - start_time

    # 4. 输出统计与失败清单
    print("\n" + "=" * 40)
    print(f"🏁 任务完成！耗时: {duration:.2f} 秒")
    print(f"✅ 成功: {success_count}")
    print(f"❌ 失败: {len(failed_list)}")

    if failed_list:
        with open(FAILED_LOG, 'w', encoding='utf-8') as f:
            for code in failed_list:
                f.write(code + "\n")
        print(f"📝 失败清单已保存至: {FAILED_LOG}")
    else:
        print("🎉 完美！全部成功！")