#!/bin/bash
# 双击运行：把 resume-agent 部署到 Vercel 生产环境
set -e
cd "$(dirname "$0")"

echo "==== Resume Agent → Vercel 生产部署 ===="

# 1. 找到 vercel CLI（没有全局安装就用 npx）
if command -v vercel >/dev/null 2>&1; then
  VERCEL="vercel"
else
  echo "未找到全局 vercel CLI，改用 npx（首次会自动下载）..."
  VERCEL="npx -y vercel@latest"
fi

# 2. 确认登录状态
if ! $VERCEL whoami >/dev/null 2>&1; then
  echo "尚未登录 Vercel，正在打开登录流程..."
  $VERCEL login
fi

# 3. 部署到生产（项目已通过 .vercel/project.json 关联 resume-agent）
echo ""
echo "开始部署到 production..."
$VERCEL deploy --prod

echo ""
echo "==== 部署完成，上面输出的 URL 即为线上地址 ===="
read -n 1 -s -r -p "按任意键关闭窗口..."
echo ""
