# IPS 统一收集系统（多外网服务器版）

收集内网设备的外网出口 IP，统一存到中心服务器，支持按服务器 / 时间范围查询。
多台外网服务器回显来源 IP，内网设备定时上报，中心以 JSON 存储并按
`(ip, server, device)` 去重——重复上报只刷新时间。

## 架构

```
  内网设备 (update_ip.sh, crontab 定时)
    │  curl 请求外网服务器取公网IP
    ▼
  外网服务器 1..N  (myip_server.py + myip.service)
    │  回显"这台服务器看到的你的出口IP"
    ▼  POST /api/update {ip, server, device}
  统一中心服务器  (ips_server.py + ips.service)
    │  JSON 存储 /etc/ips/records.json, 去重键 (ip, server, device)
    ▼  GET /api/ips?server=&start=&end=
  查询方 (人工 / 防火墙放行脚本)
```

- 外网服务器：部署在公网，内网设备访问 `GET /api/myip/<token>` 得到自己的公网出口 IP。
- 内网设备：crontab 定时运行 `update_ip.sh`，从多台外网服务器取 IP 后上报中心。
- 中心：唯一存储点，记录 `{ip, server, device, time}`，提供查询接口。

## 目录结构

```
ips/
├── Makefile                构建/安装入口
├── ips_server.py           中心服务 (Flask)
├── myip_server.py          外网服务器服务 (Flask)
├── update_ip.sh            内网设备上报脚本 (读取 client.ini)
├── client.ini.example      客户端配置模板 (安装时生成 client.ini)
├── myip.sh                 调试: curl 外网服务器取 IP
├── new_token.sh            生成 token 片段
├── systemd/
│   ├── ips.service.in      中心 unit 模板
│   └── myip.service.in     外网服务器 unit 模板
└── build/units/            make units 生成物 (gitignore)
```

## 构建与安装

```bash
make check                # 语法检查 (py_compile + bash -n)
make units                # 生成 unit 到 build/units (预览)
sudo make install         # 安装全部 (中心 + 外网服务器 + 设备端脚本)
sudo make install-ips     # 只装中心: ips_server.py + ips.service
sudo make install-myip    # 只装外网服务器: myip_server.py + myip.service
sudo make uninstall       # 卸载全部 (目录空则删, 非空提示保留)
```

安装布局（可用变量覆盖）：

| 变量     | 默认值                  | 说明                 |
|----------|-------------------------|----------------------|
| BINDIR   | /usr/local/bin          | 脚本安装目录         |
| UNITDIR  | /etc/systemd/system     | systemd unit 目录    |
| WORKDIR  | /etc/ips                | 数据/配置目录        |
| IPS_PORT | 33121                   | 中心服务端口         |
| MYIP_PORT| 33122                   | 外网服务器端口       |
| DESTDIR  | (空)                    | 打包/暂存前缀        |

本机持久化覆盖：在仓库根目录放一个 `Makefile.local`（已 gitignore，不随
仓库提交），Makefile 开头会自动 include，其中定义的变量优先于默认值：

```makefile
# Makefile.local (本机示例)
BINDIR     ?= /opt/ips/bin
WORKDIR    ?= /var/lib/ips
IPS_PORT   ?= 33121
MYIP_PORT  ?= 33122
```

优先级：命令行传参 > Makefile.local > Makefile 默认值。
端口改动后重新 `make units` 查看生成结果，或直接 `make install` 生效。

示例：`sudo make install-ips BINDIR=/opt/ips/bin WORKDIR=/var/lib/ips`
或打包：`make install DESTDIR=/tmp/pkg`

安装时自动从 `systemd/*.service.in` 生成 unit 并写对路径；改路径后重新
`make install`（或 `make units` 看生成结果）即可。

## 部署步骤

### 1. 中心服务器（一台）

```bash
sudo make install-ips
sudo systemctl daemon-reload
sudo systemctl enable --now ips.service
```

首次启动会自动创建 `/etc/ips/tokens.json`，内含一个默认 token
（`your_secret_upload_token_here_change_me`），**务必立即修改**：
`new_token.sh [名字]` 可生成新 token 片段，追加进 tokens.json 即可。

中心参数（默认值见 Makefile 生成的 unit）：

```
python3 ips_server.py --port 33121 --data-file /etc/ips/records.json \
    --token-file /etc/ips/tokens.json --log-file /var/log/ips/api.log \
    [--no-upload-https]
```

### 2. 外网服务器（1..N 台）

```bash
sudo make install-myip
sudo sh -c 'echo 你的token > /etc/ips/myip.token && chmod 600 /etc/ips/myip.token'
sudo systemctl daemon-reload
sudo systemctl enable --now myip.service
```

token 读取优先级：`--token` 参数 > `/etc/ips/myip.token` 文件 > 内置兜底默认。
多台服务器可共用同一 token 或各自配置。

### 3. 内网设备（每台）

设备端脚本 `update_ip.sh` 的配置全部来自 `client.ini`（不写在脚本里）。在
内网设备上执行客户端完整安装（脚本 + 配置模板一体）：

```bash
sudo make install-client          # 安装 update_ip.sh / myip.sh 到 BINDIR, 生成配置模板
sudo vi /etc/ips/client.ini       # 填写真实配置
/usr/local/bin/update_ip.sh --dry-run   # 确认解析结果后再加入 crontab
```

`make install-client` 生成 `/etc/ips/client.ini`（已存在则跳过，不会覆盖你的配置）。

`client.ini` 结构：

```ini
[client]
device = nas                              # 设备标识 (命令行参数可覆盖)
ips_url = http://localhost:33121/api/update   # 统一中心地址
token_file = /etc/ips/tokens.json         # 中心 tokens.json (取上传token)
token_name = nas                          # 设备在 tokens.json 里的名字

[servers]                                 # 外网服务器: 名字 = URL
# shenzhen-01 = http://YOUR_SERVER_IP:33122/api/myip/YOUR_TOKEN
# beijing-01  = http://YOUR_SERVER_IP:33122/api/myip/YOUR_TOKEN
```

脚本运行时的配置文件查找顺序：`--config` 参数 > `/etc/ips/client.ini` >
`./client.ini`。若找不到配置文件，会打印配置文件路径和示例内容并提示编写。

脚本用法：

```bash
update_ip.sh                  # 正常执行 (读取配置文件)
update_ip.sh --dry-run        # 只解析并打印配置, 不发起请求 (部署前检查)
update_ip.sh --config /path/client.ini   # 指定配置文件
update_ip.sh my-laptop        # 覆盖 device 标识
```

`[servers]` 每行一个，名字（可含连字符）即上报记录的 `server` 字段。
测试：`update_ip.sh --dry-run` 看到解析结果无误后，配置 crontab：

```cron
*/10 * * * * /usr/local/bin/update_ip.sh >> /var/log/ips/client.log 2>&1
```

### 4. 用户使用示例：ufw 动态白名单同步

场景：中心记录了各内网设备的外网 IP。防火墙管理机从中心拉取 IP 列表，
自动增删 ufw 放行规则，实现"IP 变了自动更新、消失的自动清理"的动态白名单。

仓库内示例脚本 `ufw_sync-ip.sh`：

- 调用 `GET /api/ips?format=text&server=<SERVER_NAME>&start=<DAYS天前>` 获取
  指定外网服务器、最近 N 天内的去重 IP 列表（纯文本格式）
- 与 ufw 中带注释 `DYNAMIC_WHITELIST` 的规则逐条对比：
  期望存在但缺失的 → 添加放行；不再期望的 → 删除（按编号倒序删，避免错位）
- 操作过程记录到 `/var/log/ufw-sync-ip.log`

配置（脚本头部变量；可在同目录放一个 `user.ini` 覆盖，不污染示例本身）：

| 变量         | 默认值                         | 说明                                    |
|--------------|--------------------------------|-----------------------------------------|
| IP_LIST_URL | http://localhost:33121/api/ips | 中心查询地址                            |
| SERVER_NAME | you-server                     | 按哪个外网服务器过滤（与上报的 server 字段一致） |
| DAYS         | 2                              | 取最近 N 天内的 IP                      |
| PORTS        | (22 3389)                      | 需要放行的端口列表                      |
| UFW_COMMENT  | DYNAMIC_WHITELIST              | 规则注释，用于识别/管理这批规则         |

用法：

```bash
cd /usr/local/src/ips
sudo ./ufw_sync-ip.sh               # root 权限操作 ufw
```

定时同步（crontab）：

```cron
*/10 * * * * sudo /topath/ufw_sync-ip.sh >> /var/log/ufw-sync-ip.log 2>&1
```

注意：
- 需要 root 权限（ufw 操作），且先确认 ufw 已启用：`sudo ufw status`
- `SERVER_NAME` 必须与设备上报时的服务器名一致（client.ini 中 `[servers]` 的名字）
- 中心查询接口返回的是"该服务器最近一段时间看到过的 IP"，配合
  `start`/`end` 参数即可限定时间窗口（脚本用 `start`=N 天前，`end` 不传取到现在）

### 5. 阿里云安全组动态白名单同步

场景：阿里云 ECS 的安全组入方向规则按 IP 白名单放行指定端口。管理机从中心
拉取 IP 列表，对账安全组规则，实现与 ufw 版相同的动态白名单。

仓库内脚本 `aliyun_sg_sync.py`（Python SDK 版，不依赖 aliyun CLI）：

- 调用 `GET /api/ips?format=text&server=<SERVER_NAME>&start=<DAYS天前>` 获取 IP 列表
- 用阿里云 ECS Python SDK 对账 `SECURITY_GROUP_ID` 的入方向规则：
  `authorize-security-group` 添加缺失规则（SourceCidrIp=ip/32）；
  `revoke-security-group` 按规则ID删除过期规则（默认 PRUNE=1, 可配 PRUNE=0 只增不删）
- 只操作 Description == `SG_COMMENT` 标记的规则，绝不触碰其他规则；
  端口范围规则（如 25800/25810）不纳入管理，防止误删
- 空 IP 列表直接退出，绝不在空列表下执行删除
- `--dry-run` 只打印增删计划，不真改

依赖与凭据：

```bash
pip install alibabacloud_ecs20140526 alibabacloud_tea_openapi alibabacloud_credentials
```

凭据读取顺序：配置文件 `ALIYUN_AK/ALIYUN_SK` → 环境变量
`ALIBABA_CLOUD_ACCESS_KEY_ID/ALIBABA_CLOUD_ACCESS_KEY_SECRET`。
RAM 权限需含：`ecs:DescribeSecurityGroupAttribute`、`ecs:AuthorizeSecurityGroup`、
`ecs:RevokeSecurityGroup`。

配置（user.ini，与 ufw 版共用文件）：

| 变量             | 默认值                           | 说明                                    |
|------------------|----------------------------------|-----------------------------------------|
| ALIYUN_REGION    | cn-shenzhen                      | 安全组所属地域                          |
| SECURITY_GROUP_ID| （必填）                          | 目标安全组ID                            |
| SG_PROTOCOL      | TCP                              | 规则协议 TCP/UDP/ICMP/GRE/ALL           |
| SG_COMMENT       | DYNAMIC_WHITELIST                | 规则描述标记，用于识别/管理这批规则     |
| PRUNE            | 1                                | 1=同步删除过期规则, 0=只加不删           |
| ALIYUN_AK/SK     | （留空走环境变量）                | 访问凭据                                |
| PORTS            | 继承 ufw 配置的端口列表           | 需要放行的端口                          |

用法：

```bash
cd /usr/local/src/ips
python3 aliyun_sg_sync.py --dry-run          # 先预览增删计划
python3 aliyun_sg_sync.py                    # 实际同步
```

定时同步（crontab）：

```cron
*/10 * * * * python3 /topath/aliyun_sg_sync.py >> /var/log/aliyun-sg-sync.log 2>&1
```

注意：
- `SECURITY_GROUP_ID` 在 user.ini 里必须填真实安全组ID（示例文件用占位符）
- RAM 用户建议单独授权这三个 ecs 动作 + 限定该安全组资源，不要给全量 ECS 权限

## API

### 中心 — 上传

`POST /api/update`（HTTPS + Token，Token 放 `X-API-Token` 头或 `?token=`）

| 字段   | 必填 | 说明                                   |
|--------|------|----------------------------------------|
| ip     | 否   | 公网 IP；缺省用请求来源 IP             |
| server | 否   | 外网服务器标识，缺省 `unknown`         |
| device | 否   | 内网设备标识，缺省 token 对应客户端名  |

```bash
curl -X POST http://localhost:33121/api/update \
  -H "X-API-Token: xxx" -H "Content-Type: application/json" \
  -d '{"ip":"1.2.3.4","server":"shenzhen-01","device":"nas"}'
```

响应：`{"success":true,"ip":"1.2.3.4","server":"...","device":"...","is_new":true,"timestamp":"2026-08-29 12:00:00"}`

去重键为 `(ip, server, device)`：三者相同只刷新 `time` 并返回 `is_new:false`。

### 中心 — 查询

`GET /api/ips`（HTTP、无需 token）

| 参数   | 说明                                           |
|--------|------------------------------------------------|
| server | 外网服务器标识，精确匹配（缺省全部）           |
| start  | 起始时间：时间戳或 `YYYY-MM-DD[ HH:MM:SS]` 等  |
| end    | 结束时间（含）                                 |
| detail | `1` 返回完整记录(server/device/time_str)，缺省只返回去重 IP |
| format | `json`(默认) / `text`(每行一个 IP)             |
| limit  | 限制 IP 数（0=不限）                           |

```bash
curl "http://localhost:33121/api/ips"                                    # 全部去重IP
curl "http://localhost:33121/api/ips?server=shenzhen-01"                 # 按服务器
curl "http://localhost:33121/api/ips?start=2026-08-28&end=2026-08-30"    # 时间范围
curl "http://localhost:33121/api/ips?server=shenzhen-01&detail=1"        # 完整记录
curl "http://localhost:33121/api/ips?format=text"                        # 脚本友好
```

返回按时间倒序（最新在上），IP 全局去重；`detail=1` 时 `records` 带 `time_str` 可读时间。

### 中心 — 其他

- `GET /api/health`：健康检查 + 当前来源 IP + 记录数
- `GET /api/config`：配置与参数说明

### 外网服务器 — 取我的 IP

`GET /api/myip/<token>` → 纯文本返回请求者公网 IP（403 为 token 错误）
`GET /health` → 健康检查

## 存储格式

`/etc/ips/records.json`：

```json
{
  "records": [
    {"ip": "1.2.3.4", "server": "shenzhen-01", "device": "nas", "time": 1724904000.0}
  ],
  "updated_at": 1724904000.0
}
```

`time` 为 epoch 秒（中心接收时间）。如需从旧版纯文本 `ips.txt` 迁移，
可自行写一次性脚本：按行读取 IP，`server="unknown"`、`device="legacy"`、
`time=文件mtime` 写入 records 数组即可（去重键保证不会与新数据冲突）。

## 安全说明

- 上传强制 HTTPS + Token（`--no-upload-https` 可临时放开，生产勿用）。
- 中心透过 Nginx 反代时，`ProxyFix` 信任 `X-Forwarded-For/Proto`，请确保代理
  只附加可信头；外网服务器同理（`--no-trust-proxy` 可关闭）。
- tokens.json 权限 0600；myip.token 建议 0600。
- 查询接口只读、开放 HTTP，勿用于机密场景；如需收紧可自行在 Nginx 层加白名单。

## 常见问题

- 设备取不到 IP：检查 URL 中的 token、外网服务器防火墙是否放行 33122。
- 上传报 401：tokens.json 里 token 未配置或与设备脚本不一致。
- 上传报 426：走了 HTTP，需 HTTPS 或临时 `--no-upload-https`。
- 同一 IP 出现多条记录：必是 (server, device) 不同，属设计行为（同一 IP
  从不同外网服务器看到的记录都保留）；查询时全局去重即得唯一 IP 列表。
