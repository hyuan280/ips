#!/bin/bash

# ========== 配置区域 ==========

# IPS 中心服务URL
IP_LIST_URL="http://localhost:33121/api/ips"
# 获取哪个服务器的IP列表
SERVER_NAME="you-server"
# 获取几天前的IP列表
DAYS=2
# 需要放行的端口
PORTS=(22 3389)
# 规则注释，用于识别
UFW_COMMENT="DYNAMIC_WHITELIST"

# ==============================
TEMP_FILE="/tmp/current_ip_list.txt"
LOGFILE="/var/log/ufw-sync-ip.log"
OPT_CONFIG=""
OPT_DRY_RUN=0

# 保证解析输出时使用英文格式，避免正则匹配失败
export LC_ALL=C

timestamp() {
    date "+%Y-%m-%d %H:%M:%S"
}

# 0. 解析命令行参数
while [ $# -gt 0 ]; do
    case "$1" in
        --config|-c) OPT_CONFIG="${2:-}"; shift 2 ;;
        --dry-run)   OPT_DRY_RUN=1; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

[ -f user.ini.example ] && source user.ini.example
[ -f user.ini ] && source user.ini
if [ "$OPT_CONFIG"x != ""x ] && [ -f "$OPT_CONFIG" ]; then
    source "$OPT_CONFIG"
fi

# 1. 获取远程IP列表
echo "$(timestamp) 🔍 获取IP列表: $IP_LIST_URL" | tee -a "$LOGFILE"
C_TIME=$(date "+%s")
N_TIME=$((${C_TIME}-60*60*24*${DAYS}))
U_TIME=$(date --date="@${N_TIME}" "+%Y-%m-%d")
IP_URL="${IP_LIST_URL}?format=text&server=${SERVER_NAME}&start=${U_TIME}"
if ! curl -s --connect-timeout 10 "$IP_URL" -o "$TEMP_FILE"; then
    echo "$(timestamp) ❌ 获取IP列表失败，请检查URL" | tee -a "$LOGFILE"
    exit 1
fi

# 过滤有效IPv4地址
grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' "$TEMP_FILE" > "${TEMP_FILE}_valid"
mv "${TEMP_FILE}_valid" "$TEMP_FILE"

if [ ! -s "$TEMP_FILE" ]; then
    echo "$(timestamp) ⚠️ IP列表为空" | tee -a "$LOGFILE"
    exit 0
fi

echo "$(timestamp) 📋 当前IP列表:" | tee -a "$LOGFILE"
cat "$TEMP_FILE" | tee -a "$LOGFILE"
echo "$(timestamp) 📋 要放行的端口:" | tee -a "$LOGFILE"
echo "${PORTS[*]}" | tee -a "$LOGFILE"

if ! command -v ufw &>/dev/null; then
    echo "$(timestamp) ⚠️ 系统没有ufw防火墙" | tee -a "$LOGFILE"
    exit 0
fi

# 2. 获取UFW中已有的白名单规则 (按 IP:端口 粒度)
declare -A existing_rule_key   # key = "ip:port" -> 1
declare -A existing_rule_num   # key = "ip:port" -> 规则编号

while IFS= read -r line; do
    if echo "$line" | grep -q "$UFW_COMMENT"; then
        # 提取规则编号 (如 [ 1] 中的 1)
        if [[ "$line" =~ \[[[:space:]]*([0-9]+)\] ]]; then
            rule_num="${BASH_REMATCH[1]}"
            # 提取端口和协议 (如 "22/tcp")
            port_proto=$(echo "$line" | cut -d']' -f2 | awk '{print $1}')
            port=$(echo "$port_proto" | cut -d'/' -f1)
            # 提取IP (示例行: "[ 1] 22/tcp ALLOW IN 192.168.1.1 # DYNAMIC_WHITELIST")
            ip=$(echo "$line" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            if [ "$ip"x != ""x ]; then
                key="${ip}:${port}"
                existing_rule_key["$key"]=1
                existing_rule_num["$key"]="$rule_num"
            fi
        fi
    fi
done < <(ufw status numbered 2>/dev/null)

# 3. 构建期望的规则集合 (新IP列表 × 端口列表)
declare -A desired_rule_key
while read -r ip; do
    [ -n "$ip" ] && for port in "${PORTS[@]}"; do
        desired_rule_key["${ip}:${port}"]=1
    done
done < "$TEMP_FILE"

# 4. 添加缺失的规则 (期望存在但当前不存在)
for key in "${!desired_rule_key[@]}"; do
    if [ -z "${existing_rule_key[$key]}" ]; then
        ip="${key%:*}"
        port="${key#*:}"
        if [ "$OPT_DRY_RUN" -eq 1 ]; then
            echo "$(timestamp) [dry-run] ➕ 添加规则: $ip -> port $port" | tee -a "$LOGFILE"
        else
            echo "$(timestamp) ➕ 添加规则: $ip -> port $port" | tee -a "$LOGFILE"
            ufw allow from "$ip" to any port "$port" comment "$UFW_COMMENT" 2>/dev/null
        fi
    fi
done

# 5. 删除多余的规则 (当前存在但期望中不存在)
to_delete=()
for key in "${!existing_rule_key[@]}"; do
    if [ -z "${desired_rule_key[$key]}" ]; then
        rule_num="${existing_rule_num[$key]}"
        if [ -n "$rule_num" ]; then
            to_delete+=("$rule_num")
            ip="${key%:*}"
            port="${key#*:}"
            echo "$(timestamp) ➖ 计划删除规则: $ip -> port $port (rule #$rule_num)" | tee -a "$LOGFILE"
        fi
    fi
done

# 按编号降序删除 (避免编号变化导致删除错位)
if [ ${#to_delete[@]} -gt 0 ]; then
    printf '%s\n' "${to_delete[@]}" | sort -rn -u | while read -r num; do
        if [ "$OPT_DRY_RUN" -eq 1 ]; then
            echo "$(timestamp) [dry-run] ➖ 删除规则 #$num" | tee -a "$LOGFILE"
        else
            echo "y" | ufw --force delete "$num" 2>/dev/null
            if [ $? -eq 0 ]; then
                echo "$(timestamp)   ✓ 已删除规则 #$num" | tee -a "$LOGFILE"
            else
                echo "$(timestamp)   ✗ 删除规则 #$num 失败" | tee -a "$LOGFILE"
            fi
        fi
    done
fi

if [ "$OPT_DRY_RUN" -eq 1 ]; then
    echo "$(timestamp) ✅ --dry-run 同步完成" | tee -a "$LOGFILE"
else
    echo "$(timestamp) ✅ 同步完成" | tee -a "$LOGFILE"
fi
