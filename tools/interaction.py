def ask_user(question, context=None):
    """向用户追问补充信息
    在非交互环境（如管道/自动化测试）中输入流可能已关闭，此时返回中性回答让Agent继续
    """
    print("\n" + "=" * 50)
    print("💬 Agent需要补充信息")
    print("=" * 50)
    if context:
        print(f"背景：{context}")
    print(f"问题：{question}")
    try:
        answer = input("请输入回答（直接回车表示跳过）：").strip()
    except EOFError:
        answer = ""
    if not answer:
        answer = "（用户未提供补充信息。请基于现有内容继续分析，并在最终报告中标注该信息缺失。）"
    return {"success": True, "question": question, "answer": answer}
