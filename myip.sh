#!/bin/bash
# 调试: 从外网服务器取本机公网 IP (部署时把 YOUR_SERVER_IP / YOUR_TOKEN 换成真实值)

curl -s --connect-timeout 5 "http://YOUR_SERVER_IP:33122/api/myip/YOUR_TOKEN"