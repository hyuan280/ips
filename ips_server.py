#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# 统一 IP 收集中心服务 (ips.service) —— 多外网服务器版
#
# 架构:
#   内网设备 --(curl 取公网IP)--> 多台外网服务器 (myip_server.py 回显来源IP)
#   内网设备 --(POST /api/update {ip,server,device})--> 本中心, 统一存储
#   查询     --(GET /api/ips?server=&start=&end=)----->  按服务器/时间范围返回 IP
#
# 存储: JSON 文件, 每条记录 {ip, server, device, time}
# 去重: 相同 (ip, server, device) 重复上报只刷新 time, 不新增记录
#
# 用法: python3 ips_server.py [--port 33121] [--data-file /etc/ips/records.json]
#            [--token-file /etc/ips/tokens.json] [--log-file /var/log/ips/api.log]
#            [--no-upload-https]
# 全部配置走命令行参数, 不使用环境变量; systemd 在 ExecStart 里传参。

import os
import json
import re
import time
import fcntl
import threading
import argparse
from datetime import datetime
from flask import Flask, request, jsonify
from functools import wraps
from werkzeug.middleware.proxy_fix import ProxyFix

# ========== 命令行参数 ==========
parser = argparse.ArgumentParser(description="统一 IP 收集中心服务")
parser.add_argument('--port', type=int, default=33121, help='监听端口 (默认 33121)')
parser.add_argument('--data-file', default='/etc/ips/records.json',
                    help='JSON 存储文件 (默认 /etc/ips/records.json)')
parser.add_argument('--token-file', default='/etc/ips/tokens.json',
                    help='token 配置文件 (默认 /etc/ips/tokens.json)')
parser.add_argument('--log-file', default='/var/log/ips/api.log',
                    help='日志文件 (默认 /var/log/ips/api.log)')
parser.add_argument('--no-upload-https', action='store_true',
                    help='上传接口不强制 HTTPS (默认强制)')
ARGS = parser.parse_args()

PORT = ARGS.port
DATA_FILE = ARGS.data_file
TOKEN_FILE = ARGS.token_file
LOG_FILE = ARGS.log_file
UPLOAD_REQUIRE_HTTPS = not ARGS.no_upload_https
UPLOAD_REQUIRE_TOKEN = True
GET_IPS_ALLOW_HTTP = True

app = Flask(__name__)
# 信任 Nginx 代理
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

_lock = threading.RLock()  # 可重入: update_ip/health 持有锁时会再进入 load_records()
IP_PATTERN = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')

DEFAULT_TOKENS = {
    "upload_client_1": {
        "token": "your_secret_upload_token_here_change_me",
        "description": "家庭服务器上传IP用",
        "created_at": time.time()
    }
}


def now_ts():
    return time.time()


def ts_to_str(ts):
    try:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def init_config():
    """初始化 token 配置文件"""
    if not os.path.exists(TOKEN_FILE):
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'w') as f:
            json.dump(DEFAULT_TOKENS, f, indent=2)
        os.chmod(TOKEN_FILE, 0o600)
        print(f"已创建默认配置: {TOKEN_FILE}")
        print("请立即修改 tokens.json 中的默认token！")


def log_message(message):
    """记录日志"""
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass
    print(message.strip())


# ---------- 存储 (JSON) ----------

# 文件锁: 与 DATA_FILE 同目录, 跨进程互斥 (fcntl.flock 与文件描述符绑定, 线程间同样生效)
LOCK_FILE = DATA_FILE + '.lock'


def _acquire_file_lock():
    """获取跨进程互斥写锁; 返回持有锁的文件对象, 用完后须 flock UN + close"""
    lf = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        return lf
    except Exception:
        lf.close()
        raise


def _load_unlocked():
    """读取全部记录(调用方须持有锁); 文件不存在/损坏时返回空列表"""
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('records', [])
    except FileNotFoundError:
        return []
    except Exception as e:
        log_message(f"读取记录文件失败({e}), 按空列表处理")
        return []


def _save_unlocked(records):
    """原子写 JSON 存储 (调用方须持有锁): 临时文件 + fsync + os.replace"""
    tmp = DATA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'records': records, 'updated_at': now_ts()},
                  f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DATA_FILE)


def load_records():
    """读取全部记录 (只读路径: 查询/健康检查)"""
    with _lock:
        return _load_unlocked()


def save_records(records):
    """整体写记录 (保持接口, 供测试/工具用)"""
    with _lock:
        _save_unlocked(records)


def update_record(ip, server, device, ts):
    """读-改-写 整体持 线程锁+文件锁:
       - 单进程多线程: _lock 保证串行, 文件锁无额外竞争
       - 多进程多 worker: 文件锁保证跨进程互斥, 不丢失更新
       返回 is_new"""
    with _lock:
        lf = _acquire_file_lock()
        try:
            records = _load_unlocked()
            is_new = upsert(records, ip, server, device, ts)
            _save_unlocked(records)
            return is_new
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            lf.close()


def upsert(records, ip, server, device, ts):
    """去重键 (ip, server, device): 已存在只刷新 time, 返回 False; 否则追加, 返回 True"""
    for r in records:
        if (r.get('ip') == ip and r.get('server') == server
                and r.get('device') == device):
            r['time'] = ts
            return False
    records.append({'ip': ip, 'server': server, 'device': device, 'time': ts})
    return True


# ---------- 安全 ----------

def is_secure_request():
    forwarded_proto = request.headers.get('X-Forwarded-Proto', '')
    if forwarded_proto == 'https':
        return True
    if request.is_secure:
        return True
    if request.headers.get('X-Forwarded-Ssl') == 'on':
        return True
    return False


def require_https(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if UPLOAD_REQUIRE_HTTPS and not is_secure_request():
            log_message(f"非HTTPS请求被拒绝 - {request.remote_addr}")
            return jsonify({
                'error': 'HTTPS required for this endpoint',
                'code': 426,
                'message': 'Please use HTTPS protocol'
            }), 426
        return func(*args, **kwargs)
    return wrapper


def require_upload_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not UPLOAD_REQUIRE_TOKEN:
            return func(*args, **kwargs)

        token = request.headers.get('X-API-Token') or request.args.get('token')
        if not token:
            return jsonify({'error': 'Missing token', 'code': 401}), 401

        try:
            with open(TOKEN_FILE, 'r', encoding='utf-8') as f:
                tokens_config = json.load(f)
        except Exception as e:
            log_message(f"读取token配置失败: {str(e)}")
            return jsonify({'error': 'Server configuration error', 'code': 500}), 500

        client_id = None
        for cid, info in tokens_config.items():
            if info.get('token') == token:
                client_id = cid
                break

        if client_id is None:
            log_message("上传验证失败: 无效token")
            return jsonify({'error': 'Invalid token', 'code': 401}), 401

        request.client_id = client_id
        return func(*args, **kwargs)
    return wrapper


# ---------- 上传 ----------

@app.route('/api/update', methods=['POST'])
@require_https
@require_upload_token
def update_ip():
    """上传/更新记录。支持 JSON body / 表单 / query 参数:
       ip     (必填, 缺省用请求来源IP)
       server (外网服务器标识, 缺省 'unknown')
       device (内网设备标识, 缺省取 token 对应的客户端名)
       认为重复 (ip, server, device) 只更新时间。"""
    try:
        server = device = None
        ip = None

        # 1. JSON body
        if request.is_json:
            data = request.get_json(silent=True) or {}
            ip = data.get('ip')
            server = data.get('server')
            device = data.get('device')
        # 2. 表单
        if not ip:
            ip = request.form.get('ip')
            server = server or request.form.get('server')
            device = device or request.form.get('device')
        # 3. query
        if not ip:
            ip = request.args.get('ip')
            server = server or request.args.get('server')
            device = device or request.args.get('device')
        # 4. 都没有 → 用来源IP
        if not ip:
            ip = request.remote_addr
            if request.headers.get('X-Forwarded-For'):
                ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()

        if not IP_PATTERN.match(ip or ''):
            return jsonify({'error': 'Invalid IP format', 'code': 400}), 400

        server = (server or '').strip() or 'unknown'
        device = (device or '').strip() or getattr(request, 'client_id', 'unknown')

        ts = now_ts()
        is_new = update_record(ip, server, device, ts)

        log_message(f"✓ IP更新成功: ip={ip} server={server} device={device} new={is_new}")

        return jsonify({
            'success': True,
            'ip': ip,
            'server': server,
            'device': device,
            'is_new': is_new,
            'timestamp': ts_to_str(ts),
            'message': 'IP updated successfully'
        }), 200

    except Exception as e:
        log_message(f"✗ 更新IP失败: {str(e)}")
        return jsonify({'error': str(e), 'code': 500}), 500


# ---------- 查询 ----------

def parse_time(s):
    """解析时间参数: 支持秒级/毫秒级时间戳 与 常见日期格式; 解析失败返回 None"""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    try:
        f = float(s)
        return f / 1000.0 if f > 1e12 else f
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M',
                '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


@app.route('/api/ips', methods=['GET'])
def get_ips():
    """按条件查询 IP 列表 (允许 HTTP, 不需要 token)
       参数:
         server  外网服务器标识, 精确匹配 (缺省: 全部)
         start   起始时间 (时间戳或 'YYYY-MM-DD[ HH:MM:SS]'), 含
         end     结束时间 (时间戳或日期), 含
         detail  1 时返回完整记录(server/device/time), 0 只返回去重IP列表
         format  json(默认) | text(每行一个IP)
         limit   限制返回的IP数量 (0=不限, 默认0)
    """
    try:
        server = request.args.get('server', '').strip()
        start = parse_time(request.args.get('start'))
        end = parse_time(request.args.get('end'))
        detail = request.args.get('detail', '0') == '1'
        format_type = request.args.get('format', 'json')
        limit = request.args.get('limit', default=0, type=int)

        records = load_records()

        matched = []
        for r in records:
            if server and r.get('server') != server:
                continue
            t = r.get('time', 0) or 0
            if start is not None and t < start:
                continue
            if end is not None and t > end:
                continue
            matched.append(r)

        # 时间倒序, 最新的在前
        matched.sort(key=lambda r: r.get('time', 0) or 0, reverse=True)

        # 去重 IP 列表 (保留时间最新的一条记录)
        seen = {}
        for r in matched:
            if r.get('ip') not in seen:
                seen[r['ip']] = r
        ip_list = list(seen.keys())
        if limit > 0:
            ip_list = ip_list[:limit]

        log_message(f"IP列表获取: from={request.remote_addr} "
                    f"server={server or '-'} start={ts_to_str(start) if start else '-'} "
                    f"end={ts_to_str(end) if end else '-'} -> {len(ip_list)}条")

        # 完整记录(查询窗口内的全部匹配记录, 不过滤limit)
        records_out = []
        for r in matched:
            rec = dict(r)
            rec.pop('time', None)
            rec['time_str'] = ts_to_str(r.get('time', 0))
            records_out.append(rec)

        payload = {
            'success': True,
            'count': len(ip_list),
            'ips': ip_list,
            'filter': {
                'server': server,
                'start': ts_to_str(start) if start is not None else '',
                'end': ts_to_str(end) if end is not None else '',
            },
            'timestamp': ts_to_str(now_ts()),
        }
        if detail:
            payload['records'] = records_out

        if format_type == 'text':
            response_text = '\n'.join(ip_list) + ('\n' if ip_list else '')
            return response_text, 200, {'Content-Type': 'text/plain'}

        return jsonify(payload), 200

    except Exception as e:
        log_message(f"✗ 获取IP列表失败: {str(e)}")
        return jsonify({'error': str(e), 'code': 500}), 500


# ---------- 工具接口 ----------

@app.route('/api/health', methods=['GET'])
def health_check():
    current_ip = request.remote_addr
    if request.headers.get('X-Forwarded-For'):
        current_ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
    with _lock:
        n = len(load_records())
    return jsonify({
        'status': 'healthy',
        'timestamp': ts_to_str(now_ts()),
        'record_count': n,
        'services': {
            'upload': 'https + token required',
            'download': 'http allowed, no token'
        },
        'current_protocol': request.headers.get('X-Forwarded-Proto', 'http'),
        'current_ip': current_ip
    }), 200


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify({
        'upload_endpoint': '/api/update',
        'upload_requires_https': UPLOAD_REQUIRE_HTTPS,
        'upload_requires_token': UPLOAD_REQUIRE_TOKEN,
        'download_endpoint': '/api/ips',
        'download_allows_http': GET_IPS_ALLOW_HTTP,
        'query_params': ['server', 'start', 'end', 'detail', 'format', 'limit'],
        'storage': {'format': 'json', 'file': DATA_FILE, 'dedup_key': '(ip, server, device)'}
    }), 200


if __name__ == '__main__':
    init_config()
    log_message("=" * 60)
    log_message("IP 收集中心服务 (多外网服务器版) 启动中...")
    log_message(f"JSON存储文件: {DATA_FILE}")
    log_message(f"Token配置文件: {TOKEN_FILE}")
    log_message(f"日志文件: {LOG_FILE}")
    log_message(f"上传强制HTTPS: {UPLOAD_REQUIRE_HTTPS}")
    log_message("=" * 60)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)