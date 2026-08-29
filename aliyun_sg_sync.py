#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""aliyun_sg_sync.py — 阿里云安全组 IP 白名单同步 (Python SDK 版)

从 IPS 中心拉取指定服务器的 IP 列表, 对账目标安全组入方向规则:
  1. 为列表中的每个 IP 添加指定端口的放行规则 (SourceCidrIp=ip/32)
  2. (可选, PRUNE=1) 删除"曾经放行但已不在列表"的过期规则
只操作 Description == SG_COMMENT 标记的规则, 绝不触碰其他规则。

依赖: python3 + pip 包:
  pip install alibabacloud_ecs20140526 alibabacloud_tea_openapi alibabacloud_credentials
凭据: ALIYUN_AK / ALIYUN_SK 写在配置文件 (user.ini), 脚本读入后创建 SDK client;
      留空则退回从环境变量 ALIBABA_CLOUD_ACCESS_KEY_ID/SECRET 读取。

用法:
  python3 aliyun_sg_sync.py --dry-run                    # 只打印增删计划, 不真改
  python3 aliyun_sg_sync.py [-c /path/to/user.ini]       # 正常同步

配置项 (user.ini, KEY=VALUE, # 注释):
  IP_LIST_URL  SERVER_NAME  DAYS  PORTS  SG_PROTOCOL
  ALIYUN_REGION  SECURITY_GROUP_ID  SG_COMMENT  PRUNE
  ALIYUN_AK  ALIYUN_SK  LOGFILE

安全保证:
  - 空 IP 列表直接退出 (绝不在空列表下执行删除)
  - PRUNE=0 只增不删; 端口范围规则不纳入管理, 不会误删
"""

# pyright: reportPossiblyUnboundVariable=false
import argparse
import datetime
import ipaddress
import os
import re
import sys
import urllib.request
from collections import OrderedDict

try:
    from alibabacloud_ecs20140526.client import Client as EcsClient
    from alibabacloud_ecs20140526 import models as ecs_models
    from alibabacloud_tea_openapi import models as open_api_models
    SDK_OK = True
except ImportError as e:
    SDK_OK = False
    _IMPORT_ERR = e

# ---------- 默认配置 (被配置文件覆盖) ----------
DEFAULTS = {
    "IP_LIST_URL": "http://localhost:33121/api/ips",
    "SERVER_NAME": "your-server",
    "DAYS": "2",
    "PORTS": "22 3389",
    "SG_PROTOCOL": "TCP",
    "ALIYUN_REGION": "cn-shenzhen",
    "SECURITY_GROUP_ID": "sg-xxxxxxxx",
    "SG_COMMENT": "DYNAMIC_WHITELIST",
    "PRUNE": "1",
    "ALIYUN_AK": "",
    "ALIYUN_SK": "",
    "LOGFILE": "/var/log/aliyun-sg-sync.log",
}

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def timestamp():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 配置加载 ----------

def _parse_kv_value(raw):
    """解析 KEY=VALUE 行的值, 兼容 bash 数组格式 PORTS=(a b c) 与普通字符串。"""
    v = raw.strip()
    if v.startswith("(") and v.endswith(")"):
        return " ".join(v[1:-1].split())
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    return v


def load_config(paths):
    """按顺序加载 KEY=VALUE 配置文件, 后者覆盖前者。返回 dict。"""
    cfg = dict(DEFAULTS)
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k:
                    cfg[k] = _parse_kv_value(v)
    return cfg


# ---------- 阿里云 SDK 封装 ----------

def create_client(region, ak, sk):
    """创建 ECS SDK client; 凭据支持: ak/sk 参数 > 环境变量。"""
    if not ak and not sk:
        ak = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID", "")
        sk = os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "")
    kwargs = {"region_id": region}
    if ak:
        kwargs["access_key_id"] = ak
    if sk:
        kwargs["access_key_secret"] = sk
    config = open_api_models.Config(**kwargs)
    # 显式 endpoint, 避免 SDK 默认走 intl/公共云探测
    config.endpoint = "ecs.%s.aliyuncs.com" % region
    return EcsClient(config)


def describe_ingress_rules(client, region, sg_id):
    """拉取入方向规则, 返回 Permission 对象列表 (SDK 响应体)。"""
    req = ecs_models.DescribeSecurityGroupAttributeRequest(
        region_id=region,
        security_group_id=sg_id,
        direction="ingress",
    )
    resp = client.describe_security_group_attribute(req)
    perms = resp.body.permissions.permission
    return perms if perms is not None else []


def authorize_rule(client, region, sg_id, ip, port, proto, comment, log):
    """添加一条入方向白名单规则。返回 (ok, msg)。"""
    req = ecs_models.AuthorizeSecurityGroupRequest(
        region_id=region,
        security_group_id=sg_id,
        ip_protocol=proto,
        port_range="%s/%s" % (port, port),
        source_cidr_ip="%s/32" % ip,
        policy="accept",
        priority="100",
        description=comment,
    )
    client.authorize_security_group(req)
    return True, None


def revoke_rule(client, region, sg_id, rule_id, log):
    """按规则ID删除一条规则 (最精确, 不会误删同 IP 同端口的其他规则)。"""
    req = ecs_models.RevokeSecurityGroupRequest(
        region_id=region,
        security_group_id=sg_id,
        security_group_rule_id=[rule_id],
    )
    client.revoke_security_group(req)
    return True, None


# ---------- 纯逻辑 (可脱离 SDK 测试) ----------

def extract_managed_rules(permissions, comment, proto):
    """从 Describe 响应提取受管规则: {(ip,port,protocol): rule_id}

    只认 Description==comment 且源为 IPv4、单端口 (start==end) 的规则;
    端口范围规则 (如 25700/25800) 一律忽略, 防误删。
    """
    rules = OrderedDict()
    for p in permissions:
        if (getattr(p, "description", None) or "") != comment:
            continue
        src = getattr(p, "source_cidr_ip", None) or ""
        if ":" in src or "/" not in src:
            continue
        ip = src.split("/")[0]
        if not _IPV4_RE.match(ip):
            continue
        try:
            ipaddress.IPv4Address(ip)
        except ipaddress.AddressValueError:
            continue
        pr = getattr(p, "port_range", None) or ""
        if "/" not in pr:
            continue
        start, end = pr.split("/", 1)
        if start != end or not start.isdigit():
            continue
        pproto = (getattr(p, "ip_protocol", "") or "").upper()
        if proto and pproto != proto.upper():
            continue
        rid = getattr(p, "security_group_rule_id", None) or ""
        rules[(ip, start, pproto)] = rid
    return rules


def _fetch_text_ips(url):
    """GET url, 解析逐行 IPv4, 返回去重排序列表。"""
    with urllib.request.urlopen(url, timeout=10) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    seen = []
    for line in text.splitlines():
        line = line.strip()
        if not _IPV4_RE.match(line):
            continue
        try:
            ipaddress.IPv4Address(line)
        except ipaddress.AddressValueError:
            continue
        if line not in seen:
            seen.append(line)
    return sorted(seen)


# ---------- 主流程 ----------

class Logger:
    def __init__(self, logfile):
        self.logfile = logfile
        if logfile:
            d = os.path.dirname(logfile)
            if d and not os.path.isdir(d):
                try:
                    os.makedirs(d, exist_ok=True)
                except OSError:
                    pass

    def log(self, msg):
        line = "%s %s" % (timestamp(), msg)
        print(line, flush=True)
        if self.logfile:
            try:
                with open(self.logfile, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                print("(无法写日志文件 %s: %s)" % (self.logfile, e), file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="阿里云安全组 IP 白名单同步")
    parser.add_argument("-c", "--config", help="配置文件路径 (默认查找 ./user.ini)")
    parser.add_argument("--dry-run", action="store_true", help="只打印增删计划, 不修改")
    args = parser.parse_args(argv)

    if not SDK_OK:
        print("缺少必需依赖: %s" % _IMPORT_ERR, file=sys.stderr)
        print("请安装: pip install alibabacloud_ecs20140526 alibabacloud_tea_openapi alibabacloud_credentials",
              file=sys.stderr)
        return 1

    # 配置优先级: --config > ./user.ini > ./user.ini.example
    here = os.path.dirname(os.path.abspath(sys.argv[0]))
    cfg = load_config([
        os.path.join(here, "user.ini.example"),
        os.path.join(here, "user.ini"),
        args.config,
    ])
    log = Logger(cfg["LOGFILE"]).log

    sg_id = cfg["SECURITY_GROUP_ID"]
    if not sg_id or sg_id.startswith(("CHANGE_ME", "sg-xxxx")):
        log("❌ SECURITY_GROUP_ID 未配置 (请检查配置文件)")
        return 1
    if not cfg["ALIYUN_AK"] and not os.environ.get("ALIBABA_CLOUD_ACCESS_KEY_ID"):
        log("⚠️  ALIYUN_AK 未配置且无环境变量凭据, SDK 将尝试其他凭据链")

    region = cfg["ALIYUN_REGION"]
    proto = cfg["SG_PROTOCOL"].upper()
    comment = cfg["SG_COMMENT"]
    prune = cfg["PRUNE"] in ("1", "true", "yes", "on")
    ports = [p for p in cfg["PORTS"].split() if p]
    if not ports:
        log("❌ PORTS 未配置")
        return 1

    # 1. 拉取 IP 列表
    url = cfg["IP_LIST_URL"]
    sep = "&" if "?" in url else "?"
    start_date = (datetime.date.today() - datetime.timedelta(days=int(cfg["DAYS"]))).isoformat()
    ip_url = "%s%sformat=text&server=%s&start=%s" % (url, sep, cfg["SERVER_NAME"], start_date)
    log("🔍 获取IP列表: %s" % ip_url)
    try:
        ip_list = _fetch_text_ips(ip_url)
    except Exception as e:
        log("❌ 获取IP列表失败: %s" % e)
        return 1

    if not ip_list:
        log("⚠️  IP列表为空, 退出 (不会执行任何增删)")
        return 0
    log("📋 当前IP列表 (%d 个): %s" % (len(ip_list), " ".join(ip_list)))
    log("📋 端口: %s  协议: %s  安全组: %s @ %s  模式: %s" % (
        " ".join(ports), proto, sg_id, region, "同步(+删)" if prune else "只增"))

    # 2. 创建 client 并拉取现有规则
    client = create_client(region, cfg["ALIYUN_AK"], cfg["ALIYUN_SK"])
    try:
        perms = describe_ingress_rules(client, region, sg_id)
    except Exception as e:
        log("❌ 查询安全组规则失败: %s" % e)
        log("   请检查: RAM 权限 ecs:DescribeSecurityGroupAttribute / 地域 / 安全组ID")
        return 1
    existing = extract_managed_rules(perms, comment, proto)
    log("🔍 现有受管规则 %d 条" % len(existing))

    # 3. 期望集合 = IP列表 × 端口
    desired = OrderedDict()
    for ip in ip_list:
        for port in ports:
            desired[(ip, port, proto)] = 1

    # 4. 新增
    add_ok = add_fail = 0
    for key in desired:
        if key in existing:
            continue
        ip, port, _ = key
        if args.dry_run:
            log("[dry-run] ➕ 添加规则: %s -> port %s/%s" % (ip, port, proto))
            continue
        try:
            ok, _ = authorize_rule(client, region, sg_id, ip, port, proto, comment, log)
            if ok:
                add_ok += 1
                log("➕ 已添加规则: %s -> port %s/%s" % (ip, port, proto))
        except Exception as e:
            add_fail += 1
            log("✗ 添加规则失败: %s -> port %s/%s (%s)" % (ip, port, proto, _short_err(e)))
    if not args.dry_run:
        log("   新增完成: 成功 %d, 失败 %d" % (add_ok, add_fail))

    # 5. 删除过期 (仅 PRUNE=1)
    del_ok = del_fail = 0
    for key, rid in existing.items():
        if key in desired:
            continue
        ip, port, pproto = key
        if not prune:
            if args.dry_run:
                log("[dry-run] ⏭ 过期规则(PRUNE=0 跳过): %s -> port %s/%s" % (ip, port, pproto))
            continue
        if args.dry_run:
            log("[dry-run] ➖ 删除过期规则: %s -> port %s/%s (rule %s)" % (ip, port, pproto, rid))
            continue
        try:
            ok, _ = revoke_rule(client, region, sg_id, rid, log)
            if ok:
                del_ok += 1
                log("➖ 已删除过期规则: %s -> port %s/%s (rule %s)" % (ip, port, pproto, rid))
        except Exception as e:
            del_fail += 1
            log("✗ 删除规则失败: %s -> port %s/%s (rule %s, %s)" % (ip, port, pproto, rid, _short_err(e)))
    if not args.dry_run:
        log("   删除完成: 成功 %d, 失败 %d" % (del_ok, del_fail))

    if args.dry_run:
        log("✅ [dry-run] 计划完成 (未执行任何修改)")
    else:
        log("✅ 同步完成")
    return 0


def _short_err(e):
    code = getattr(e, "code", None) or getattr(e, "status_code", None) or ""
    msg = str(e)
    return ("%s %s" % (code, msg)).strip()[:200]


if __name__ == "__main__":
    sys.exit(main())