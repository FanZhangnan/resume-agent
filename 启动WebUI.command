#!/bin/bash
# 简历优化Agent Web UI 一键启动器（macOS双击运行）
cd "$(dirname "$0")" || exit 1
echo "================================================"
echo "  简历优化 Agent · Web UI 启动器"
echo "================================================"

PY=./venv/bin/python
if [ ! -x "$PY" ]; then
  echo "→ 未找到虚拟环境，正在创建 venv ..."
  python3 -m venv venv || { echo "✗ 创建venv失败，请先安装Python3"; read -r -n1 -p "按任意键退出"; exit 1; }
fi

# 检查依赖是否齐全，缺失则自动安装
"$PY" - <<'CHECK'
import importlib.util, sys
missing = [m for m in ("fastapi", "uvicorn", "multipart", "openai") if importlib.util.find_spec(m) is None]
sys.exit(1 if missing else 0)
CHECK
if [ $? -ne 0 ]; then
  echo "→ 首次运行：正在安装依赖（约1分钟）..."
  "$PY" -m pip install -r requirements.txt -q || { echo "✗ 依赖安装失败，请检查网络"; read -r -n1 -p "按任意键退出"; exit 1; }
fi

echo "→ 启动服务：http://127.0.0.1:7860 （关闭本窗口即停止）"
( sleep 1.5; open "http://127.0.0.1:7860" ) &
exec "$PY" webui/server.py
