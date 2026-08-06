#!/bin/bash
# =====================================================================
# 脚本名称: task_runner.sh
# 功能描述: 任务执行与日志记录脚本，自动运行指定任务并记录执行时间和结果
# 运行方式: ./task_runner.sh
# 配置说明: 运行前需修改下方 TASK_SCRIPT 和 LOG_FILE 为实际路径
#
# 可选增强:
#   配合 crontab 实现定时执行，例如每天凌晨 2 点运行：
#     0 2 * * * /path/to/task_runner.sh
#
# 依赖命令: date, bash
# =====================================================================

# 定义任务脚本路径（请替换为实际的任务脚本绝对路径）
TASK_SCRIPT="/path/to/your/task_script.sh"

# 定义日志文件路径（请替换为实际的日志文件绝对路径）
LOG_FILE="/path/to/your/log_file.log"

# 执行任务，并记录执行时间和结果到日志文件
# 功能: 运行 TASK_SCRIPT，将标准输出和错误输出都重定向到日志文件
run_task() {
    echo "Task started at $(date)" >> "$LOG_FILE"
    if $TASK_SCRIPT >> "$LOG_FILE" 2>&1; then
        echo "Task completed successfully" >> "$LOG_FILE"
    else
        echo "Task failed" >> "$LOG_FILE"
    fi
    echo "Task ended at $(date)" >> "$LOG_FILE"
}

# 主函数
main() {
    run_task
}

# 调用主函数
main
