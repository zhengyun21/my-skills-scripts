#!/bin/bash
# =====================================================================
# 脚本名称: file_manager.sh
# 功能描述: 文件管理工具箱（备份、压缩、查找、比较、权限设置）
# 运行方式: ./file_manager.sh <操作> [参数...]
#
# 支持的操作:
#   backup         备份单个文件
#                   用法: ./file_manager.sh backup <源文件> <备份目录>
#                   示例: ./file_manager.sh backup /etc/hosts /tmp/backup
#
#   compress       压缩整个目录为 tar.gz
#                   用法: ./file_manager.sh compress <源目录> <输出文件.tar.gz>
#                   示例: ./file_manager.sh compress /home/user/docs docs.tar.gz
#
#   find           按文件名关键词查找文件
#                   用法: ./file_manager.sh find <目录> <关键词>
#                   示例: ./file_manager.sh find /var/log error
#
#   compare        比较两个文件内容是否一致
#                   用法: ./file_manager.sh compare <文件1> <文件2>
#                   示例: ./file_manager.sh compare a.txt b.txt
#
#   set_permission 设置文件权限
#                   用法: ./file_manager.sh set_permission <文件> <权限>
#                   示例: ./file_manager.sh set_permission script.sh 755
#
# 依赖命令: cp, tar, find, ls, diff, chmod
# =====================================================================

# 1. 文件备份
# 功能: 将源文件复制到备份目录，文件名自动追加时间戳
backup_file() {
    local file="$1"
    local backup_dir="$2"
    local backup_file="${backup_dir}/$(basename ${file}).$(date +%Y-%m-%d_%H-%M-%S)"
    cp "$file" "$backup_file"
    echo "File $file backed up to $backup_file"
}

# 2. 文件压缩
# 功能: 将指定目录打包压缩为 .tar.gz 文件
compress_directory() {
    local dir="$1"
    local output_file="$2"
    tar -czf "$output_file" "$dir"
    echo "Directory $dir compressed to $output_file"
}

# 3. 文件查找
# 功能: 在指定目录下按文件名关键词查找文件
find_files() {
    local directory="$1"
    local keyword="$2"
    echo "Files containing '$keyword' in directory $directory:"
    find "$directory" -type f -name "*$keyword*" -exec ls {} \;
}

# 4. 文件比较
# 功能: 比较两个文件内容是否相同，输出比较结果
compare_files() {
    local file1="$1"
    local file2="$2"
    if diff "$file1" "$file2" >/dev/null; then
        echo "Files $file1 and $file2 are identical."
    else
        echo "Files $file1 and $file2 are different."
    fi
}

# 5. 文件权限设置
# 功能: 修改指定文件的权限（如 755、644 等）
set_file_permission() {
    local file="$1"
    local permission="$2"
    chmod "$permission" "$file"
    echo "Permission of file $file set to $permission"
}

# 6. 异常处理
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
        backup) backup_file "$2" "$3" ;;
        compress) compress_directory "$2" "$3" ;;
        find) find_files "$2" "$3" ;;
        compare) compare_files "$2" "$3" ;;
        set_permission) set_file_permission "$2" "$3" ;;
        *) handle_error "Invalid action: $action" ;;
    esac
}

# 调用主函数，并传入所有命令行参数
main "$@"
