"""静态检查：函数里的死代码（return/raise 之后还有同层语句）

⚠ 覆盖 FunctionDef / If / For / While / **ExceptHandler**。
  漏掉 ExceptHandler 就抓不到这个真实案例：
      try: ...
      except ImportError:
          return _fail()      # ← 在 except 里
  —— 而"函数被从中间截断"那个 bug 正是这个形状。
"""
import ast, io, sys
from pathlib import Path
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)
CHECKED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.If,
           ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
           ast.With, ast.AsyncWith)

bad = 0
files = sorted(list(Path('host').glob('*.py')) + list(Path('scripts').glob('*.py')))
for f in files:
    try:
        tree = ast.parse(f.read_text(encoding='utf-8'))
    except SyntaxError as e:
        print(f"  [语法错] {f}:{e}"); bad += 1; continue
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        if not isinstance(body, list):
            continue
        for i, st in enumerate(body[:-1]):
            if isinstance(st, TERMINATORS):
                nxt = body[i + 1]
                # 只有"语句"才算，文档字符串/注释不是
                if isinstance(nxt, ast.Pass):
                    continue
                name = getattr(node, 'name', None) or type(node).__name__
                print(f"  [死代码] {f}:{nxt.lineno} —— {name}() 里 "
                      f"{type(st).__name__} 之后还有语句，永远执行不到")
                bad += 1

print(f"\n  扫描 {len(files)} 个文件，{bad} 处问题")
sys.exit(1 if bad else 0)
