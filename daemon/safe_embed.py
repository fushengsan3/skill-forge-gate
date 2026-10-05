#!/usr/bin/env python3
"""
把 JSON 安全地嵌入 HTML 的 <script> 块。

直接用 json.dumps() 的结果做 str.replace() 注入有三个坑：

1. 字符串里的 </script> 会提前闭合脚本块，后面的内容被当作 HTML 解析执行。
2. <!-- 会把解析器带入 HTML 注释状态，同样能跳出脚本。
3. U+2028 / U+2029（行分隔符）在 ES2019 之前会让 JS 字符串字面量语法错误。

三个坑的成因相同：JSON 的转义规则与 HTML、JS 的都不是同一套。
所以这里把 < > & 和两个行分隔符统统转成 \\uXXXX —— 这些转义在 JSON 里合法，
JS 解析时会原样还原成原字符，数据本身不受任何影响。

严格说只转义 < 就足以堵住前两个坑；多转 > 和 & 是零代价的纵深防御。
"""
import json

# U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR
# 用 chr() 构造，源码里不出现不可见字符
_LINE_SEPARATOR = chr(0x2028)
_PARAGRAPH_SEPARATOR = chr(0x2029)


def json_for_script(obj) -> str:
    """序列化成可以安全放进 <script> 块里的 JSON 字面量。

    用法：
        html = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(data))

    注意不要用 json.dumps() 直接拼进 <script> —— 见模块顶部说明。
    """
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(_LINE_SEPARATOR, "\\u2028")
        .replace(_PARAGRAPH_SEPARATOR, "\\u2029")
    )
