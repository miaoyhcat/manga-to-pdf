"""前端回归检查：把 main.py 里的内联 JS 抽出来跑 node --check。

用途：页面里的 JS 写错一个括号，Python 侧完全感知不到（只是字符串），
但浏览器会整段脚本不执行，表现为「页面能打开、列表空、所有按钮没反应」。
这个脚本能在部署前把这类错误挡住。

用法：
    python scripts/check_frontend.py app/main.py
"""
import re
import subprocess
import sys
import tempfile
import os

path = sys.argv[1] if len(sys.argv) > 1 else "app/main.py"
src = open(path, encoding="utf-8").read()

blocks = re.findall(r"<script>(.*?)</script>", src, re.S)
if not blocks:
    print("没有找到内联脚本")
    sys.exit(1)

print(f"找到 {len(blocks)} 段内联脚本")
bad = 0
for i, b in enumerate(blocks):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(b)
        tmp = f.name
    try:
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        if r.returncode == 0:
            print(f"  第 {i} 段: OK")
        else:
            bad += 1
            print(f"  第 {i} 段: 语法错误\n{r.stderr.strip()[:600]}")
    except FileNotFoundError:
        print("  未安装 node，跳过")
        sys.exit(0)
    finally:
        os.unlink(tmp)

# 常见坑：页面挂在子路径下时不能用根路径请求接口
if re.search(r"fetch\(\s*BASE\s*\+\s*['\"]/", src):
    print("警告：仍存在 BASE + '/...' 形式的绝对路径拼接")
    bad += 1

print("通过" if bad == 0 else f"失败 {bad} 处")
sys.exit(1 if bad else 0)
