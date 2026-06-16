#!/bin/bash
# Lumen 一键部署脚本
# 自动化部署内核维护工作流系统

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 配置
VENV_DIR="venv"
USE_VENV=true

# 打印函数
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${GREEN}"
    echo "======================================"
    echo "  Lumen 内核维护工作流系统 - 部署"
    echo "======================================"
    echo -e "${NC}"
}

# 检查Python版本
check_python() {
    print_info "检查Python环境..."

    if ! command -v python3 &> /dev/null; then
        print_error "未找到 python3，请安装 Python 3.8+"
        exit 1
    fi

    PYTHON_VERSION=$(python3 -c 'import sys; print(sys.version_info.major * 10 + sys.version_info.minor)')
    if [ "$PYTHON_VERSION" -lt 38 ]; then
        print_error "Python版本过低，需要 3.8+，当前: $(python3 --version)"
        exit 1
    fi

    print_success "Python版本: $(python3 --version)"
}

# 创建虚拟环境
create_virtualenv() {
    print_info "创建Python虚拟环境..."

    # 检查是否存在虚拟环境
    if [ -d "$VENV_DIR" ]; then
        print_info "虚拟环境已存在，使用现有环境"
    else
        python3 -m venv "$VENV_DIR"
        print_success "虚拟环境创建完成: $VENV_DIR"
    fi

    # 激活虚拟环境
    source "$VENV_DIR/bin/activate"
    print_success "虚拟环境已激活"
}

# 安装依赖
install_dependencies() {
    print_info "安装项目依赖..."

    if [ -f "requirements.txt" ]; then
        # 使用虚拟环境的pip
        if [ "$USE_VENV" = true ]; then
            pip install -r requirements.txt -q
        else
            pip3 install -r requirements.txt -q --break-system-packages || pip3 install -r requirements.txt -q
        fi
        print_success "依赖安装完成"
    else
        print_error "未找到 requirements.txt"
        exit 1
    fi
}

# 创建配置文件
create_config_files() {
    print_info "创建配置文件..."

    # 主工作流配置
    if [ ! -f "maintenance_config.json" ]; then
        if [ -f "maintenance_config.example.json" ]; then
            cp maintenance_config.example.json maintenance_config.json
            print_success "创建 maintenance_config.json（请编辑API key）"
        else
            print_warning "未找到配置模板，请手动创建 maintenance_config.json"
        fi
    else
        print_info "maintenance_config.json 已存在"
    fi

    # 自测试配置
    if [ ! -f "self_test_config.json" ]; then
        print_warning "self_test_config.json 不存在，自测试功能需手动配置"
    else
        print_info "self_test_config.json 已存在"
    fi
}

# 初始化目录
init_directories() {
    print_info "初始化目录结构..."

    mkdir -p knowledge_base
    mkdir -p outputs
    mkdir -p self_test_reports

    print_success "目录创建完成"
}

# 安装Git hook
install_git_hooks() {
    print_info "配置Git hooks..."

    if [ -d ".githooks" ] && [ -f ".githooks/commit-msg" ]; then
        cp .githooks/commit-msg .git/hooks/commit-msg 2>/dev/null || true
        chmod +x .git/hooks/commit-msg 2>/dev/null || true
        print_success "Git commit-msg hook 已安装"
    else
        print_info "未配置Git hooks"
    fi
}

# 验证安装
verify_installation() {
    print_info "验证安装..."

    # 检查关键文件
    REQUIRED_FILES=(
        "main.py"
        "config.py"
        "requirements.txt"
    )

    for file in "${REQUIRED_FILES[@]}"; do
        if [ ! -f "$file" ]; then
            print_error "缺失关键文件: $file"
            exit 1
        fi
    done

    # 检查关键模块（使用虚拟环境的python）
    if [ "$USE_VENV" = true ]; then
        "$VENV_DIR/bin/python" -c "
import sys
try:
    import langgraph
    import langchain
    import langchain_openai
    print('核心模块导入成功')
except ImportError as e:
    print(f'导入失败: {e}')
    sys.exit(1)
" || {
            print_error "核心模块验证失败"
            exit 1
        }
    else
        python3 -c "
import sys
try:
    import langgraph
    import langchain
    import langchain_openai
    print('核心模块导入成功')
except ImportError as e:
    print(f'导入失败: {e}')
    sys.exit(1)
" || {
            print_error "核心模块验证失败"
            exit 1
        }
    fi

    print_success "安装验证通过"
}

# 显示使用示例
show_usage_examples() {
    echo ""
    echo -e "${YELLOW}使用示例：${NC}"
    echo ""

    if [ "$USE_VENV" = true ]; then
        echo -e "${BLUE}1. 激活虚拟环境（首次使用）：${NC}"
        echo "   source venv/bin/activate"
        echo ""
        echo -e "${BLUE}2. 运行内核故障分析工作流：${NC}"
        echo "   python main.py --input \"问题描述\" --config maintenance_config.json"
        echo ""
        echo -e "${BLUE}3. 运行自迭代验证（模拟模式）：${NC}"
        echo "   python self_test_main.py --fault_type deadlock --max_iterations 5"
        echo ""
        echo -e "${BLUE}4. LangGraph Studio调试：${NC}"
        echo "   langgraph dev"
        echo ""
        echo -e "${YELLOW}下一步：${NC}"
        echo -e "1. 编辑 ${GREEN}maintenance_config.json${NC} 配置API key"
        echo -e "2. 运行示例测试验证系统功能"
        echo ""
    else
        echo -e "${BLUE}1. 运行内核故障分析工作流：${NC}"
        echo "   python main.py --input \"问题描述\" --config maintenance_config.json"
        echo ""
        echo -e "${BLUE}2. 运行自迭代验证（模拟模式）：${NC}"
        echo "   python self_test_main.py --fault_type deadlock --max_iterations 5"
        echo ""
        echo -e "${BLUE}3. LangGraph Studio调试：${NC}"
        echo "   langgraph dev"
        echo ""
        echo -e "${YELLOW}下一步：${NC}"
        echo -e "1. 编辑 ${GREEN}maintenance_config.json${NC} 配置API key"
        echo -e "2. 运行示例测试验证系统功能"
        echo ""
    fi
}

# 主流程
main() {
    print_header

    check_python
    create_virtualenv
    install_dependencies
    create_config_files
    init_directories
    install_git_hooks
    verify_installation

    print_success "✓ 部署完成！"

    show_usage_examples
}

# 执行主流程
main