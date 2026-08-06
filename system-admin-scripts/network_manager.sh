#!/bin/bash
# =====================================================================
# 脚本名称: network_manager.sh
# 功能描述: 网络管理工具箱（查看连接、连通性检测、流量查看、防火墙管理）
# 运行方式: ./network_manager.sh <操作> [参数...]
#
# 支持的操作:
#   view_connections   查看当前网络连接（监听端口）
#                       用法: ./network_manager.sh view_connections
#
#   check_connectivity 检测目标主机端口连通性
#                       用法: ./network_manager.sh check_connectivity <主机> <端口>
#                       示例: ./network_manager.sh check_connectivity 8.8.8.8 53
#
#   view_traffic        查看网络接口流量信息
#                       用法: ./network_manager.sh view_traffic
#
#   manage_firewall     管理防火墙规则（查看/添加/删除）
#                       用法: ./network_manager.sh manage_firewall <子操作> [端口]
#                       子操作:
#                         view_rules    查看当前规则
#                         add_rule      添加允许指定端口入站的规则
#                         delete_rule   删除指定端口入站的规则
#                       示例:
#                         ./network_manager.sh manage_firewall view_rules
#                         ./network_manager.sh manage_firewall add_rule 8080
#                         ./network_manager.sh manage_firewall delete_rule 8080
#
# 依赖命令: netstat, nc (netcat), ifconfig, iptables
# 注意事项: 防火墙相关操作需 root 权限；部分系统 ifconfig 需安装 net-tools
# =====================================================================

# 1. 查看网络连接
# 功能: 列出当前所有监听的 TCP/UDP 端口
view_network_connections() {
    echo "Network connections:"
    netstat -tuln
}

# 2. 检查网络连通性
# 功能: 使用 nc 检测目标主机指定端口是否可达
check_network_connectivity() {
    local host="$1"
    local port="$2"
    echo "Checking connectivity to $host:$port ..."
    if nc -zv "$host" "$port" >/dev/null 2>&1; then
        echo "Connection to $host:$port successful."
    else
        echo "Connection to $host:$port failed."
    fi
}

# 3. 查看网络流量
# 功能: 显示各网络接口的配置与流量统计信息
view_network_traffic() {
    echo "Network traffic:"
    ifconfig
}

# 4. 防火墙管理
# 功能: 查看、添加、删除 iptables 入站规则（针对 TCP 端口）
manage_firewall() {
    local action="$1"
    case $action in
        view_rules) iptables -L ;;
        add_rule) iptables -A INPUT -p tcp --dport "$2" -j ACCEPT ;;
        delete_rule) iptables -D INPUT -p tcp --dport "$2" -j ACCEPT ;;
        *) echo "Invalid action: $action" ;;
    esac
}

# 5. 异常处理
# 功能: 输出错误信息（用于未知操作时提示）
handle_error() {
    local error_message="$1"
    echo "Error: $error_message"
}

# 主函数
# 功能: 根据传入的第一个参数分发到对应的子功能
main() {
    local action="$1"
    case $action in
        view_connections) view_network_connections ;;
        check_connectivity) check_network_connectivity "$2" "$3" ;;
        view_traffic) view_network_traffic ;;
        manage_firewall) manage_firewall "$2" "$3" ;;
        *) handle_error "Invalid action: $action" ;;
    esac
}

# 调用主函数，并传入所有命令行参数
main "$@"
