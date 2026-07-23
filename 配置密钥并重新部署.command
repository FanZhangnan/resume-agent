#!/bin/bash
# 双击运行：把本地 .env 里的得否网关密钥同步到 Vercel Preview/Production，然后重新部署
set -e
cd "$(dirname "$0")"

if command -v vercel >/dev/null 2>&1; then
  VERCEL="vercel"
else
  VERCEL="npx -y vercel@latest"
fi

echo "==== 同步环境变量到 Vercel Preview/Production ===="

# 从本地 .env 读取（值不会显示在屏幕上）
get_env() { grep -m1 "^$1=" .env 2>/dev/null | cut -d= -f2- ; }

API_KEY_VALUE="$(get_env OPENAI_API_KEY)"
BASE_URL_VALUE="https://api.wangdefou.studio/v1"

if [ -z "$API_KEY_VALUE" ]; then
  echo "❌ 本地 .env 里没找到 OPENAI_API_KEY，请先补上再运行"
  read -n 1 -s -r -p "按任意键关闭..."
  exit 1
fi

set_env() {  # set_env 名称 值 环境
  name="$1"
  value="$2"
  environment="$3"
  $VERCEL env rm "$name" "$environment" --yes >/dev/null 2>&1 || true
  printf '%s' "$value" | $VERCEL env add "$name" "$environment" >/dev/null
  echo "✅ 已写入 $name（$environment）"
}

for environment in preview production; do
  set_env OPENAI_API_KEY "$API_KEY_VALUE" "$environment"
  set_env AGENT_BASE_URL "$BASE_URL_VALUE" "$environment"

done

echo ""
echo "==== 重新部署到 production（环境变量改动需要重新部署才生效）===="
$VERCEL deploy --prod

echo ""
echo "==== 完成！用上面输出的 URL 重新测试一次 ===="
read -n 1 -s -r -p "按任意键关闭窗口..."
echo ""
