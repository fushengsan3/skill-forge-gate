"""Privacy boundaries for public repository access and approved AI requests."""
import os
import re
from urllib.parse import urlsplit

CAPABILITY_TERMS = {
    'pdf': ('pdf', 'pdf文件'), 'research': ('research', 'paper', 'academic', 'literature', 'arxiv', 'citation', '论文', '文献', '科研', '研究'),
    'image': ('image', 'photo', 'picture', 'graphic', '图片', '图像', '绘画', '画图', '绘图'),
    'slides': ('presentation', 'slides', 'powerpoint', 'ppt', '演示', '幻灯', '汇报'),
    'spreadsheet': ('spreadsheet', 'excel', 'csv', 'sheet', '表格', '电子表格'),
    'document': ('document', 'word', 'docx', 'text', 'writing', 'summar', '文档', '写作', '文章'),
    'notes': ('note', 'markdown', 'obsidian', '知识库', '笔记'),
    'search': ('search', 'find', 'query', '搜索', '查找', '检索'),
    'code': ('code', 'coding', 'programming', 'developer', 'software', 'refactor', 'debug', '代码', '编程', '开发'),
    'translation': ('translate', 'translation', '翻译'), 'video': ('video', '视频', '影片'),
    'audio': ('audio', 'music', 'voice', '音频', '语音', '音乐'),
    'design': ('design', 'interface', 'ui', '设计', '界面'),
    'data': ('data', 'analysis', 'analytics', '数据', '分析', '统计'),
    'diagram': ('diagram', 'chart', 'flowchart', '图表', '流程图', '图示'),
    'automation': ('automation', 'workflow', 'automate', '自动化', '工作流'),
    'security': ('security', 'audit', '安全', '审计'),
    'web': ('website', 'web', 'browser', 'frontend', 'react', '网页', '网站', '浏览器'),
    'testing': ('testing', 'test', '测试'), 'git': ('github', 'git', '版本控制'),
    'math': ('math', 'mathematics', '数学', '公式'),
}


def capability_tags(text):
    value = str(text or '').lower()
    return [tag for tag, words in CAPABILITY_TERMS.items() if any(word in value for word in words)]


def validate_ai_payload(payload, purpose):
    """Accept only public description text or fixed capability labels."""
    if not isinstance(payload, dict):
        raise ValueError('AI 输入格式不正确')
    if purpose == 'capabilities':
        if set(payload) != {'request_tags', 'candidates'}:
            raise ValueError('搜索输入包含额外信息')
        allowed = set(CAPABILITY_TERMS)
        tags = payload.get('request_tags')
        rows = payload.get('candidates')
        if not isinstance(tags, list) or any(x not in allowed for x in tags) or not isinstance(rows, list):
            raise ValueError('搜索输入包含非通用标签')
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'id', 'tags'} or type(row['id']) is not int:
                raise ValueError('搜索候选格式不正确')
            if not isinstance(row['tags'], list) or any(x not in allowed for x in row['tags']):
                raise ValueError('搜索候选包含非通用标签')
    elif purpose == 'public_descriptions':
        if set(payload) != {'items'}:
            raise ValueError('翻译输入包含额外信息')
        rows = payload.get('items')
        if not isinstance(rows, list):
            raise ValueError('翻译输入格式不正确')
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'id', 'text'} or type(row['id']) is not int or not isinstance(row['text'], str):
                raise ValueError('翻译条目格式不正确')
            if clean_public_description(row['text']) != row['text']:
                raise ValueError('翻译内容包含本机信息或密钥')
    else:
        raise ValueError('该内容不能发送到模型')


def git_env(base):
    env = {}
    for key, value in base.items():
        upper = key.upper()
        if upper.startswith('GIT_') or re.search(r'(TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL)', upper):
            continue
        if upper in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY') and value:
            parsed = urlsplit(value if '://' in value else 'http://' + value)
            if parsed.username or parsed.password:
                raise ValueError('代理地址包含凭据；隐私模式下不会发送代理凭据')
        env[key] = value
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', GIT_LFS_SKIP_SMUDGE='1')
    return env


def clean_public_description(text):
    """Remove paths, addresses and credential-shaped strings from public metadata."""
    text = str(text or '')[:2400]
    patterns = [
        r'(?i)\b[a-z]:[\\/][^\s,;"\']+', r'(?i)(?<!\w)(?:~\/|/(?:home|users|mnt|private|var|tmp)/)[^\s,;"\']+',
        r'(?<![\w:/])/(?:[A-Za-z0-9._~-]+/)+[A-Za-z0-9._~%-]*', r'\\\\[^\s\\/]+\\[^\s,;"\']+',
        r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', r'(?i)\bhttps?://[^\s,;"\']+',
        r'\b(?:\d{1,3}\.){3}\d{1,3}\b', r'(?i)\b(?:sk|ghp|github_pat|api[_-]?key)[-_][A-Za-z0-9_-]{10,}\b',
        r'\b1[3-9]\d{9}\b',
    ]
    for pattern in patterns:
        text = re.sub(pattern, '[已移除]', text)
    try:
        from daemon import credentials
        secret = credentials.get_secret(credentials.AI_TOKEN)
        if secret:
            text = text.replace(secret, '[已移除]')
    except Exception:
        pass
    return text


def allow_remote_audit():
    # L4/L5 would transmit local paths or skill contents; do not run remotely.
    return False


def allow_unverified_translation():
    # General scan descriptions may originate from local or user-provided sources.
    return False
