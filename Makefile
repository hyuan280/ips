# ============================================================
# IPS 统一收集系统 — Makefile
#
# 用法:
#   make check              全部语法检查 (py_compile / bash -n)
#   make units              生成 systemd unit 到 build/units (不安装, 可预览)
#   make install            安装全部 (install-ips + install-myip)
#   make install-ips        只装中心组件 (ips_server.py + ips.service)
#   make install-myip       只装外网服务器组件 (myip_server.py + myip.service)
#   make install-client     客户端完整安装: update_ip.sh + client.ini 配置模板
#   make uninstall / uninstall-ips / uninstall-myip   对应卸载
#   make clean              清理 build/ 与 __pycache__
#
#   install 会自动创建 $(BINDIR) / $(UNITDIR) / $(WORKDIR) 目录;
#   uninstall 删除文件后, 若目录已空则删除目录, 非空则打印提示保留。
#
# 示例:
#   sudo make install                               # 默认安装
#   sudo make install-myip                          # 只装 myip
#   make install-ips BINDIR=/opt/ips/bin \
#        UNITDIR=/usr/lib/systemd/system WORKDIR=/var/lib/ips
#   make install DESTDIR=/tmp/pkg                   # 打包
# ============================================================

# 本地覆盖: 存在 Makefile.local 时, 其中定义的变量优先
-include Makefile.local

BINDIR   ?= /usr/local/bin
UNITDIR  ?= /etc/systemd/system
WORKDIR  ?= /etc/ips
IPS_PORT ?= 33121
MYIP_PORT ?= 33122

PYTHON  ?= /usr/bin/python3
MKDIR_P ?= install -d
INSTALL ?= install
RM      ?= rm -f

IPS_SRC    := ips_server.py
MYIP_SRC   := myip_server.py
COMMON_SH  := update_ip.sh myip.sh new_token.sh
ALL_SCRIPTS := $(IPS_SRC) $(MYIP_SRC) $(COMMON_SH)
CLIENT_SCRIPTS := update_ip.sh myip.sh
CLIENT_INI := client.ini.example
UNITS      := ips.service myip.service
GEN_UNITS  := $(addprefix build/units/,$(UNITS))

.PHONY: all check check-ips check-myip units install install-ips install-myip \
        install-client uninstall uninstall-ips uninstall-myip clean

all: check units

# 目录为空则删除, 非空则提示保留 (卸载时调用)
define try_rmdir
	@if [ -d "$(1)" ]; then \
		rmdir "$(1)" 2>/dev/null && echo "OK  已删除空目录 $(2)" \
		|| echo "提示: $(2) 非空, 目录保留"; \
	fi
endef

# ---------- 语法检查 ----------

check: check-ips check-myip

check-ips:
	@$(PYTHON) -m py_compile $(IPS_SRC)
	@bash -n $(COMMON_SH)
	@echo "OK  ips 组件语法检查通过"

check-myip:
	@$(PYTHON) -m py_compile $(MYIP_SRC)
	@echo "OK  myip 组件语法检查通过"

# ---------- unit 生成 ----------

units: $(GEN_UNITS)

build/units/%.service: systemd/%.service.in
	@$(MKDIR_P) build/units
	@sed -e 's|@BINDIR@|$(BINDIR)|g' \
	     -e 's|@WORKDIR@|$(WORKDIR)|g' \
	     -e 's|@IPS_PORT@|$(IPS_PORT)|g' \
	     -e 's|@MYIP_PORT@|$(MYIP_PORT)|g' $< > $@
	@echo "OK  生成 $@"

# ---------- 安装 ----------

install: install-ips install-myip

install-ips: check-ips build/units/ips.service
	@$(MKDIR_P) $(DESTDIR)$(BINDIR) $(DESTDIR)$(UNITDIR) $(DESTDIR)$(WORKDIR)
	@$(INSTALL) -m 0755 $(IPS_SRC) $(COMMON_SH) $(DESTDIR)$(BINDIR)/
	@$(INSTALL) -m 0644 build/units/ips.service $(DESTDIR)$(UNITDIR)/
	@echo "OK  已安装 ips 组件: 脚本 -> $(BINDIR), unit -> $(UNITDIR)"
	@echo "    下一步: systemctl daemon-reload && systemctl enable --now ips.service"

install-myip: check-myip build/units/myip.service
	@$(MKDIR_P) $(DESTDIR)$(BINDIR) $(DESTDIR)$(UNITDIR) $(DESTDIR)$(WORKDIR)
	@$(INSTALL) -m 0755 $(MYIP_SRC) $(COMMON_SH) $(DESTDIR)$(BINDIR)/
	@$(INSTALL) -m 0644 build/units/myip.service $(DESTDIR)$(UNITDIR)/
	@echo "OK  已安装 myip 组件: 脚本 -> $(BINDIR), unit -> $(UNITDIR)"
	@echo "    下一步: systemctl daemon-reload && systemctl enable --now myip.service"

# 客户端完整安装: 客户端脚本 + 配置文件
install-client: check-ips
	@$(MKDIR_P) $(DESTDIR)$(BINDIR) $(DESTDIR)$(WORKDIR)
	@$(INSTALL) -m 0755 $(CLIENT_SCRIPTS) $(DESTDIR)$(BINDIR)/
	@if [ -f $(DESTDIR)$(WORKDIR)/client.ini ]; then \
		echo "提示: $(WORKDIR)/client.ini 已存在, 跳过生成 (按需自行修改)"; \
	else \
		$(INSTALL) -m 0644 $(CLIENT_INI) $(DESTDIR)$(WORKDIR)/client.ini; \
		echo "OK  已生成配置模板 $(WORKDIR)/client.ini (请填写真实值)"; \
	fi
	@echo "OK  已安装客户端: 脚本 -> $(BINDIR), 配置 -> $(WORKDIR)/client.ini"
	@echo "    请填写配置后运行 $(BINDIR)/update_ip.sh --dry-run 检查"

# ---------- 卸载 ----------

uninstall: uninstall-ips uninstall-myip
	@$(RM) $(addprefix $(DESTDIR)$(BINDIR)/,$(COMMON_SH))
	@$(call try_rmdir,$(DESTDIR)$(BINDIR),$(BINDIR))
	@$(call try_rmdir,$(DESTDIR)$(UNITDIR),$(UNITDIR))
	@$(call try_rmdir,$(DESTDIR)$(WORKDIR),$(WORKDIR))
	@echo "OK  已卸载全部"

uninstall-ips:
	@$(RM) $(DESTDIR)$(BINDIR)/$(IPS_SRC) $(DESTDIR)$(UNITDIR)/ips.service
	@echo "OK  已卸载 ips 组件"
	@$(call try_rmdir,$(DESTDIR)$(BINDIR),$(BINDIR))
	@$(call try_rmdir,$(DESTDIR)$(WORKDIR),$(WORKDIR))

uninstall-myip:
	@$(RM) $(DESTDIR)$(BINDIR)/$(MYIP_SRC) $(DESTDIR)$(UNITDIR)/myip.service
	@echo "OK  已卸载 myip 组件"
	@$(call try_rmdir,$(DESTDIR)$(BINDIR),$(BINDIR))
	@$(call try_rmdir,$(DESTDIR)$(WORKDIR),$(WORKDIR))

# ---------- 清理 ----------

clean:
	@rm -rf build
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name '*.py[cd]' -delete 2>/dev/null || true
	@echo "OK  已清理"
