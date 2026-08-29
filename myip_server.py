#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# 外网服务器上的"取公网IP"服务 (myip.service)
# 内网设备 curl http://<本机>:<port>/api/myip/<token> 即可获得自己的外网出口IP
# 多台外网服务器部署同一份脚本即可 (每台一个 systemd 服务)
#
# 用法: python3 myip_server.py [--port 33122] [--token xxxx]
#            [--token-file /etc/ips/myip.token] [--no-trust-proxy]
# token 读取优先级: --token 命令行参数 > --token-file 文件内容 > 内置默认
# 全部配置走命令行参数, 不使用环境变量。

import argparse
from flask import Flask, request, jsonify

# ========== 命令行参数 ==========
parser = argparse.ArgumentParser(description="外网服务器取公网IP服务")
parser.add_argument('--port', type=int, default=33122, help='监听端口 (默认 33122)')
parser.add_argument('--token', default='',
                    help='验证token (优先于 --token-file)')
parser.add_argument('--token-file', default='/etc/ips/myip.token',
                    help='token 文件, 每台外网服务器一份 (默认 /etc/ips/myip.token)')
parser.add_argument('--no-trust-proxy', action='store_true',
                    help='不信任 X-Forwarded-For / X-Real-IP 代理头 (默认信任)')
ARGS = parser.parse_args()

PORT = ARGS.port
TRUST_PROXY = not ARGS.no_trust_proxy

app = Flask(__name__)


def _token_from_file(path):
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception:
        return ''


VALID_TOKEN = (ARGS.token.strip()
               or _token_from_file(ARGS.token_file)
               or "CHANGE_ME_MYIP_TOKEN")   # 兜底默认, 部署时必须通过 --token/文件配置


def get_client_ip():
    """获取客户端真实 IP，支持代理头"""
    if TRUST_PROXY:
        forwarded = request.headers.get('X-Forwarded-For')
        if forwarded:
            return forwarded.split(',')[0].strip()
        real_ip = request.headers.get('X-Real-IP')
        if real_ip:
            return real_ip.strip()
    return request.remote_addr


@app.route('/api/myip/<token>', methods=['GET'])
def get_my_ip(token):
    """返回客户端的真实公网 IP (纯文本, 方便脚本直接使用)"""
    if token != VALID_TOKEN:
        return jsonify({'error': 'Invalid token', 'code': 403}), 403
    client_ip = get_client_ip()
    return f"{client_ip}\n", 200, {'Content-Type': 'text/plain'}


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'port': PORT}), 200


if __name__ == '__main__':
    print("=" * 50)
    print("Get My IP Service (外网服务器端) Starting...")
    print(f"监听端口: {PORT}")
    print(f"Token来源: {'命令行参数' if ARGS.token else ('文件 ' + ARGS.token_file if _token_from_file(ARGS.token_file) else '内置默认(!)' )}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)