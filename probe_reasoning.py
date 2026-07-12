"""探测当前网关/模型支持哪些推理强度（reasoning_effort）取值

用法：
  ./venv/bin/python probe_reasoning.py            # 使用.env里的网关与AGENT_MODEL
  ./venv/bin/python probe_reasoning.py gpt-5.6-sol  # 指定模型

原理：对每个候选值发送一条极小请求（"只回复数字1"），观察：
  - 是否被网关接受（HTTP 200 vs 4xx参数错误）
  - reasoning_tokens 是否随档位变化（不变化=网关收下了但没生效）
会产生几次极小的API消耗。
"""
import json
import sys
import urllib.error
import urllib.request

import config  # 自动加载.env

CANDIDATES = [None, "none", "minimal", "low", "medium", "high", "xhigh", "max"]


def probe(model, effort):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复数字1"}],
        "max_tokens": 3000,
    }
    if effort is not None:
        body["reasoning_effort"] = effort
    req = urllib.request.Request(
        config.API_BASE_URL.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + config.API_KEY,
                 "Content-Type": "application/json",
                 "User-Agent": "curl/8.7.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
            usage = data.get("usage") or {}
            details = usage.get("completion_tokens_details") or {}
            return True, usage.get("completion_tokens"), details.get("reasoning_tokens")
    except urllib.error.HTTPError as error:
        message = error.read().decode(errors="replace")[:200].replace("\n", " ")
        return False, f"HTTP {error.code}", message
    except Exception as error:  # noqa: BLE001
        return False, type(error).__name__, str(error)[:150]


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else config.MODEL_NAME
    if not config.API_KEY:
        print("未检测到API密钥（.env或环境变量）")
        return
    print(f"网关：{config.API_BASE_URL} ｜ 模型：{model}\n")
    print(f"{'档位':>10} ｜ 结果")
    print("-" * 60)
    accepted = []
    for effort in CANDIDATES:
        ok, a, b = probe(model, effort)
        label = str(effort) if effort else "(不传参数)"
        if ok:
            print(f"{label:>10} ｜ ✅ 接受   completion={a}  reasoning_tokens={b}")
            if effort:
                accepted.append(effort)
        else:
            print(f"{label:>10} ｜ ❌ 拒绝   {a}: {b}")
    print("-" * 60)
    if accepted:
        print(f"该网关接受的档位：{', '.join(accepted)}")
        print("提示：若各档位 reasoning_tokens 完全相同，说明网关收下参数但未生效。")
    else:
        print("所有带参数的请求都被拒绝：该网关/模型不支持 reasoning_effort。")


if __name__ == "__main__":
    main()
