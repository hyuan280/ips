#!/bin/bash
#
# 内网设备上报脚本 (客户端) —— 配置来自 client.ini
#
# 流程:
#   1. 查找配置文件: --config 参数 > /etc/ips/client.ini > ./client.ini
#      找不到则打印配置文件路径与示例内容并退出
#   2. 解析 [client] 与 [servers] 两节
#   3. 依次请求各外网服务器取公网IP, 上报 (server, device, ip) 到统一中心
#
# 用法:
#   update_ip.sh [device]                    # 设备标识, 覆盖配置中的 device
#   update_ip.sh --config /path/client.ini   # 指定配置文件
#   update_ip.sh --dry-run                   # 只解析并打印配置, 不发起任何请求
#
# 定时执行示例 (crontab, 脚本安装于 /usr/local/bin):
#   */10 * * * * /usr/local/bin/update_ip.sh >> /var/log/ips/client.log 2>&1

set -u

# ---------- 参数解析 ----------
OPT_CONFIG=""
OPT_DRY_RUN=0
DEVICE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --config|-c) OPT_CONFIG="${2:-}"; shift 2 ;;
        --dry-run)   OPT_DRY_RUN=1; shift ;;
        -*) echo "未知参数: $1" >&2; exit 2 ;;
        *) DEVICE="$1"; shift ;;
    esac
done

CONFIG_HINTS=("/etc/ips/client.ini" "./client.ini")

# ---------- 配置文件 ----------

print_example() {
    cat <<'EOF'
# ============================================================
# client.ini — 客户端配置文件示例
# 保存到上面的路径 (或 ./client.ini, 或用 --config 指定) 后重试
#
# [client]
# device      设备标识 (也可用命令行参数覆盖)
# ips_url     统一中心地址 (POST /api/update)
# token_file  中心 tokens.json 路径, 用于取本设备的上传 token
# token_name  上述文件中本设备对应的名字
#
# [servers]
# 每行一个外网服务器: 名字 = URL
# 名字会作为上报记录的 server 字段; URL 为取公网IP的地址
# ============================================================
[client]
device = nas
ips_url = http://localhost:33121/api/update
token_file = /etc/ips/tokens.json
token_name = nas

[servers]
# 部署时替换为真实地址与 token
# shenzhen-01 = http://YOUR_SERVER_IP:33122/api/myip/YOUR_TOKEN
# beijing-01  = http://YOUR_SERVER_IP:33122/api/myip/YOUR_TOKEN
EOF
}

find_config() {
    if [ -n "$OPT_CONFIG" ]; then
        [ -f "$OPT_CONFIG" ] && { echo "$OPT_CONFIG"; return 0; }
        echo "指定的配置文件不存在: $OPT_CONFIG" >&2
        return 1
    fi
    local p
    for p in "${CONFIG_HINTS[@]}"; do
        [ -f "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

# 解析简单 INI: 生成 cfg_<section>_<key> 变量; [servers] 节收集到 cfg_servers_list 数组
declare -a cfg_servers_list=()
parse_ini() {
    local file="$1" section="" line key val
    while IFS= read -r line || [ -n "$line" ]; do   # || 处理无尾随换行的最后一行
        line="${line%%#*}"                           # 去注释
        line="${line#"${line%%[![:space:]]*}"}"      # 去行首空白
        line="${line%"${line##*[![:space:]]}"}"      # 去行尾空白
        [ -z "$line" ] && continue
        if [[ "$line" =~ ^\[(.*)\]$ ]]; then
            section="${BASH_REMATCH[1]}"
            continue
        fi
        if [[ "$line" =~ ^([A-Za-z0-9_-]+)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            val="${BASH_REMATCH[2]}"
            case "$section" in
                servers) cfg_servers_list+=("${key}|${val}") ;;
                *)       eval "cfg_${section}_${key}=\"$val\"" ;;
            esac
        fi
    done < "$file"
}

# ---------- 读取配置 ----------

CONFIG_FILE=$(find_config) || {
    echo "== 未找到客户端配置文件 ==" >&2
    echo "请创建配置文件 (查找顺序: --config > ${CONFIG_HINTS[*]})" >&2
    echo
    print_example >&2
    exit 1
}

parse_ini "$CONFIG_FILE"

IPS_URL="${cfg_client_ips_url:-}"
TOKEN_FILE="${cfg_client_token_file:-/etc/ips/tokens.json}"
[ -z "$DEVICE" ] && DEVICE="${cfg_client_device:-}"

# token 名: 配置 > device > 报错
TOKEN_NAME="${cfg_client_token_name:-$DEVICE}"
[ -z "$DEVICE" ] && DEVICE="$TOKEN_NAME"

SERVERS=("${cfg_servers_list[@]}")

# ---------- 校验 ----------

if [ -z "$IPS_URL" ]; then
    echo "配置文件缺少 ips_url (节 [client])" >&2
    exit 1
fi
if [ ${#SERVERS[@]} -eq 0 ]; then
    echo "配置文件没有配置任何外网服务器 (节 [servers])" >&2
    echo "请参考以下示例:" >&2
    print_example >&2
    exit 1
fi
if [ -z "${TOKEN_NAME}" ]; then
    echo "配置文件缺少 device/token_name (节 [client])" >&2
    exit 1
fi

# ---------- dry-run: 只展示解析结果 ----------

if [ "$OPT_DRY_RUN" -eq 1 ]; then
    echo "配置文件: $CONFIG_FILE"
    echo "  device     = ${DEVICE}"
    echo "  ips_url    = ${IPS_URL}"
    echo "  token_file = ${TOKEN_FILE}"
    echo "  token_name = ${TOKEN_NAME}"
    echo "  servers    = ${#SERVERS[@]} 个"
    for s in "${SERVERS[@]}"; do echo "    - $s"; done
    exit 0
fi

# ---------- 取上传 token ----------

TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null | jq -r ".${TOKEN_NAME}.token")
if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then
    echo "从 ${TOKEN_FILE} 读取 ${TOKEN_NAME}.token 失败" >&2
    exit 1
fi

# ---------- 上报 ----------

error=0
reported=0

for entry in "${SERVERS[@]}"; do
    SERVER_NAME="${entry%%|*}"
    SERVER_URL="${entry#*|}"

    # 1. 从外网服务器取当前公网IP
    if ! CURRENT_IP=$(curl -s --connect-timeout 5 --max-time 10 "${SERVER_URL}"); then
        echo "$(date): [${SERVER_NAME}] 请求公网ip失败: ${SERVER_URL}, ret=$?"
        error=$((error+1))
        continue
    fi

    CURRENT_IP=$(echo "${CURRENT_IP}" | tr -d '[:space:]')
    if [ "${CURRENT_IP}"x == ""x ] || [ "${CURRENT_IP}" == "null" ]; then
        echo "$(date): [${SERVER_NAME}] 不能获取公网ip: NULL"
        error=$((error+1))
        continue
    fi

    # 2. 上报到统一中心
    RESPONSE=$(curl -s --connect-timeout 5 --max-time 10 -X POST "${IPS_URL}" \
        -H "X-API-Token: ${TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"ip\": \"${CURRENT_IP}\", \"server\": \"${SERVER_NAME}\", \"device\": \"${DEVICE}\"}")

    if echo "${RESPONSE}" | grep -q '"success":true'; then
        echo "$(date): [${SERVER_NAME}] IP上报成功 - ${CURRENT_IP} (device=${DEVICE}) → ${RESPONSE}"
        reported=$((reported+1))
    else
        echo "$(date): [${SERVER_NAME}] IP上报失败 - ${CURRENT_IP} → ${RESPONSE}"
        error=$((error+1))
    fi
done

echo "$(date): 完成 report=${reported} error=${error} (device=${DEVICE})"
exit ${error}