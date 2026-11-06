"""脱敏管道（REQ-PRIV-001~006 / 技术方案 §11.3 / DEV-AGT-009）。

脱敏范围仅限个人敏感信息（2026-10-29 产品决策，经卷宗实测校准）：
- 不脱敏：机构/公司名、案号、统一社会信用代码、机关单位对外公开的座机/举报电话
  —— 这些可公开查询，误遮反而破坏阅卷上下文（用户批注：案号、信用代码、机构电话不用打码）；
- 脱敏：姓名、身份证号、个人手机号、个人住址、银行卡号、邮箱、车牌。

识别层（D-001~D-003）：
- 规则层（强置信，身份证 / 银行卡 / 手机号 / 邮箱 / 车牌），
  带 GB 11643 / Luhn 校验：校验通过按高置信，失败降置信进入人工确认；
  数字类支持 15 位老身份证、空格/短横分隔（OCR 常见）与 +86 前缀；
  无语境且校验不通过的长数字串（发票号/流水/OCR 噪声）直接丢弃，不误打码；
- 启发式（中置信，姓名 / 地址），默认 needs_human，可被人工确认；
- 案件自定义词典（redaction_terms，confidence=1.0）；
- 跨行合并（_merge_crossline）：PDF/OCR 断行切断的实体先合并再检测，
  命中后换行随打码吸收、未命中版式还原。

策略层（REQ-PRIV-004 / D-011）：
- light     直接标识符（身份证/银行卡/手机号/邮箱/车牌）；
- standard  light + 姓名（默认档）；
- strict     全部个人敏感类型（含地址与自定义词典）；
- custom    案件自选类型集合。

打码层（mask_text，「一键脱敏」）：
- 部分打码（_partial_mask）：只遮关键段、保留首尾/行政区划等可读上下文
  （王志远→王*远、522628********0037），避免全打码破坏阅卷分析；

映射层（REQ-PRIV-002/003 / D-005 / D-010）：
- 同一案件同一实体复用稳定别名（人物A / 机构A / …），编号取该类型已有最大序号 +1；
- 原文经 DPAPI 加密（security.protect）落 redaction_entities.original_enc，
  明文不出现在普通数据库字段、审计 metadata 与日志；
- 不同案件各自独立映射，别名无跨案件可比信息；
- 查找复用采用"解密比对"：DPAPI 密文非确定，不能作等值索引。

外发语义（REQ-PRIV-006）：
- apply() 对策略范围内实体一律替换（宁可多遮不漏遮）；
- 范围内的高敏感直接标识符若处于 needs_human（低置信），返回 blockedBy 阻断外发，
  由调用方（graph / MCP 网关）引导用户先在预览中人工确认；
- preview() 面向本地 UI，返回原文差异项（本机受信环境）；apply() 返回值不含原文。

审计（§18）：只记类型计数与策略档位，不记原文、别名明文映射或词典内容。
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from . import security
from .cases import ENTITY_TYPE_ORDER, get_case
from .storage import get_db
from .workspace import now_iso


class RedactionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CONFIRM_THRESHOLD = 0.9  # ≥ 视为可自动确认；低于则 needs_human（REQ-PRIV-006）

# 高敏感直接标识符（PRD §12.3）：未确认时阻断外发
HIGH_SENSITIVITY_TYPES = frozenset({"id_card", "bank_card", "phone", "email", "plate"})

# 不参与脱敏的公开/机构信息（可公开查询；误遮破坏阅卷上下文）：
# org 机构名、case_number 案号、credit_code 统一社会信用代码
EXCLUDED_TYPES = frozenset({"org", "case_number", "credit_code"})

_POLICY_LIGHT = frozenset({"id_card", "bank_card", "phone", "email", "plate"})
POLICY_TYPES: dict[str, frozenset[str]] = {
    "light": _POLICY_LIGHT,
    "standard": _POLICY_LIGHT | {"name"},
    "strict": frozenset(ENTITY_TYPE_ORDER.keys()) - EXCLUDED_TYPES,
}

ALIAS_PREFIX: dict[str, str] = {
    "id_card": "身份证号",
    "bank_card": "银行卡",
    "phone": "电话",
    "email": "邮箱",
    "plate": "车牌",
    "name": "人物",
    "address": "地址",
    "custom": "敏感词",
}

# ---------------------------------------------------------------------------
# 规则层：校验与正则
# ---------------------------------------------------------------------------

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def _id_checksum_ok(value: str) -> bool:
    try:
        total = sum(int(value[i]) * _ID_WEIGHTS[i] for i in range(17))
    except ValueError:
        return False
    return _ID_CHECK_CHARS[total % 11] == value[17].upper()


def _luhn_ok(value: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(value)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# 数字串模式不用 \b（\w 含 CJK，中文邻接会漏检），改用数字环视防止长串内截断。
# OCR/排版常在号码中插入空格或短横（"5226 2819 8204 1500 37"、"139-8546-2317"），
# 正则允许分隔符，校验前用 _compact_digits 压回纯数字。
_SEP = r"[ \u3000\-]"
_ID_CARD_RE = re.compile(rf"(?<![\dXx])\d(?:{_SEP}?\d){{16}}{_SEP}?[\dXx](?![\dXx])")
# 15 位老身份证（GB 11643 前身）：6 位行政区划 + 6 位出生日期 + 3 位顺序码，无校验位
_ID_CARD_15_RE = re.compile(rf"(?<!\d)\d(?:{_SEP}?\d){{14}}(?!\d)")
# 证件字段名锚点（前文出现"身份证号/公民身份号码/证号"）：提升无校验位号码的置信
_ID_CONTEXT_RE = re.compile(r"(?:身份证|身份号码|公民身份|证件号|证号)\s*[：:号]?\s*$")
_BANK_CARD_RE = re.compile(rf"(?<!\d)\d(?:{_SEP}?\d){{15,18}}(?!\d)")
_PHONE_MOBILE_RE = re.compile(rf"(?<![\d+])(?:\+?86{_SEP}?)?1[3-9]\d(?:{_SEP}?\d){{8}}(?!\d)")
_PHONE_LANDLINE_RE = re.compile(
    rf"(?<![\d(（])(?:[（(]0\d{{2,3}}[）)]|0\d{{2,3}}){_SEP}?\d{{7,8}}(?:{_SEP}?转{_SEP}?\d{{1,6}})?(?!\d)"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PLATE_RE = re.compile(
    r"(?:京|沪|津|渝|冀|晋|蒙|辽|吉|黑|苏|浙|皖|闽|赣|鲁|豫|鄂|湘|粤|桂|琼|川|贵|云|藏|陕|甘|青|宁|新|使|领)"
    r"[A-HJ-NP-Z](?:[A-HJ-NP-Z0-9]{5}|[A-HJ-NP-Z0-9]{4}[挂学警港澳])(?![0-9A-Za-z])"
)
# 银行卡语境锚点（卡号/账号/账户/开户行）：无此语境且 Luhn 不通过的 16-19 位数字
# 多为发票号、单据流水等 OCR 噪声，直接丢弃（用户批注：乱码被打码了）
_BANK_CONTEXT_RE = re.compile(r"(?:卡号|账号|帐号|账户|帐户|开户|银行卡|储蓄卡|信用卡|卡内|转入|转至)\s*[：:为]?\s*$")
# 统一社会信用代码标签：其值常为 18 位纯数字，与银行卡同形但不应打码（用户批注：信用代码不用打码）
_CREDIT_CODE_LABEL_RE = re.compile(
    r"(?:统一社会信用代码|统一社会代码|社会信用代码|信用代码|纳税人识别号|纳税人识别码)\s*[：:]?\s*$"
)
# 电话语境标签：无分隔符的裸数字串须有此标签才按座机采信
# （发票号/流水号"052001806104"与区号+号码同形，无语境一律不采信）
_PHONE_CONTEXT_RE = re.compile(r"(?:电话|座机|手机|联系方式|联系|传真|热线|总机|Tel|TEL|tel)\s*[：:为]?\s*$")
# 机构公开号码标签（举报电话/值班电话/总机/传真/诉讼服务…）：
# 机关对外公布的号码可公开查询，打码反而破坏阅卷上下文（用户批注：机构号码可以不用打码）
_ORG_PHONE_LABEL_RE = re.compile(
    r"(?:举报|投诉|信访|值班|总机|传真|热线|监督|纪检|监察|立案|诉讼服务|"
    r"办公室|政治部|研究室|审管办|档案室)"
    r"(?!人)[\u4e00-\u9fa5]{0,4}(?:电话|号码|联系方式|热线)?\s*[：:]?\s*$"
)

# 地址：省/市 → 区/县 → 乡镇 → 路/街 → 门牌号（完整链高置信；无门牌的中置信待确认）
# 省级用有限枚举、行政区划名内排除叙述字（系/于/为/在/坐落…），
# 避免"被告系凯里市…""房屋坐落于贵州省…"把叙述语吞进地址区间；
# 直辖市/直管市（"贵阳市南明区"）仍可命中。
_PROVINCE_NAMES = (
    "内蒙古|黑龙江|北京|天津|上海|重庆|河北|山西|辽宁|吉林|江苏|浙江|安徽|福建|江西|"
    "山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|广西|西藏|"
    "宁夏|新疆|香港|澳门"
)
_PROVINCE_SUFFIX = "维吾尔自治区|壮族自治区|回族自治区|特别行政区|自治区|省|市"
_PROVINCE = rf"(?:(?:{_PROVINCE_NAMES})(?:{_PROVINCE_SUFFIX})?)?"
# 行政区划名内的单个字符：遇到叙述字或上一级行政后缀即失败，
# 正则从更靠后的位置重新开始匹配，避免"被告住所地贵州省凯里市…"把叙述语吞进地址区间；
# 名称内不含行政后缀字，也保证"黔东南苗族侗族自治州"不会被"贵州"+"州"错切。
_ADMIN_CH = r"(?:(?!系|于|为|在|住|址|坐|落|该|所|地|籍|贯|的|其|现|将|已|原|被|告|诉|省|市|州|盟|县|区|旗|镇|乡|街)[\u4e00-\u9fa5])"
_CITY = rf"{_ADMIN_CH}{{2,10}}(?:市|州|盟)"
_CITY_LOOSE = rf"{_ADMIN_CH}{{1,8}}(?:市|州|盟|县|区)"
# 楼栋房号链（"5栋2单元601室""3号楼1201"）：可重复，避免在"5栋"处截断
_ADDR_UNIT_CHAIN = r"(?:[\u4e00-\u9fa5A-Za-z0-9]{0,12}?(?:室|单元|幢|栋|号楼|号院|座)){0,4}"
# 门牌段号（"人民南路四段1号""中山路二段20号"）：段号在门牌号之前
_ADDR_SECTION = r"(?:[一二三四五六七八九十百]{1,3}|\d{1,3})段"
_ADDR_FULL_RE = re.compile(
    _PROVINCE + _CITY +
    rf"{_ADMIN_CH}{{2,8}}(?:区|县|旗|市|镇)"
    rf"(?:{_ADMIN_CH}{{2,8}}(?:镇|乡|街道))?"
    rf"[\u4e00-\u9fa5A-Za-z0-9]{{2,18}}?(?:路|街|大道|道|巷|弄)"
    rf"(?:{_ADDR_SECTION})?\d{{1,5}}号{_ADDR_UNIT_CHAIN}"
)
_ADDR_PARTIAL_RE = re.compile(
    _PROVINCE + _CITY +
    rf"{_ADMIN_CH}{{2,8}}(?:区|县)"
    r"[\u4e00-\u9fa5A-Za-z0-9]{2,18}?(?:路|街|大道|巷|弄)(?!号?\d)"
)
# 住宅小区地址（无路名格式）："贵阳市观山湖区世纪城小区龙泉苑12栋3单元502室" /
# "贵阳市南明区花果园R2区7栋1804室" / "…云岩区××大厦A栋2506室"
_ADDR_RESIDENTIAL_RE = re.compile(
    _PROVINCE + _CITY +
    rf"{_ADMIN_CH}{{1,8}}(?:区|县|旗|镇)"
    r"[\u4e00-\u9fa5A-Za-z0-9]{2,26}?"
    r"(?:"
    r"\d{1,5}(?:栋|幢|号楼|号院|座|单元|室)"  # 数字楼号链：12栋 / 3单元 / 502室
    r"(?:[\u4e00-\u9fa5A-Za-z]{0,6}?\d{1,5}(?:单元|室|座))*"  # 后续链：3单元502室
    r"|"
    r"[A-Za-z](?:栋|幢|座)\d{0,5}(?:单元|室|座)?"  # 字母楼号：A栋2506室
    r")"
)
# 乡镇级行政区划链（无路名门牌）："贵州省黔东南州凯里市三棵树镇挂丁村三组"；
# 须带村/组/社区末级——镇/街道本身是广域行政区（"被告系凯里市三棵树镇居民"），不构成个人住址
_ADDR_TOWN_RE = re.compile(
    _PROVINCE + _CITY +
    rf"(?:{_ADMIN_CH}{{1,8}}(?:市|区|县|旗))?"
    rf"{_ADMIN_CH}{{1,8}}(?:镇|乡|街道)"
    r"[\u4e00-\u9fa5]{1,10}?(?:村(?!民)|组|屯|寨|社区)"
    r"(?:[\u4e00-\u9fa5]{0,4}?[组队])?"
)
# 市/州 + 路/街 + 门牌（无区县中间层）："凯里市韶山南路3号"
_ADDR_CITY_ROAD_RE = re.compile(
    _CITY_LOOSE +
    rf"[\u4e00-\u9fa5A-Za-z0-9]{{2,16}}?(?:路|街|大道|巷|弄)"
    rf"(?:{_ADDR_SECTION})?\d{{1,5}}号{_ADDR_UNIT_CHAIN}"
)
# 市/州 + 小区/花园等住宅区名（无门牌）："凯里市恒诚帝都小区"
_ADDR_COMMUNITY_RE = re.compile(
    _CITY_LOOSE +
    r"[\u4e00-\u9fa5A-Za-z0-9]{2,14}?"
    r"(?:小区|花园|公寓|家园|新村|山庄|大厦|广场|苑)"
    r"(?:[\u4e00-\u9fa5A-Za-z0-9]{0,8}?(?:栋|幢|单元|室|号楼))?"
)
# 地址前的引导词（"现住…""住址为…""户籍地："）：属于叙述，不并入打码区间
_ADDR_LEAD_RE = re.compile(
    r"^(?:现居住于|现居住|现住址|现住|户籍地址|户籍所在地|户籍地|住所地|经常居住地|经常居住于|"
    r"常居于|常住在|常住|居住地|暂住地|暂住|工作单位地址|单位地址|送达地址|送达|住址|住所|"
    r"地址|籍贯|住)[：:为于在]?"
)
# 发票/表单标签被跨行合并进地址串（"账号凯里市韶山南路3号"）：裁掉标签本身
_ADDR_LABEL_RE = re.compile(
    r"^(?:账号|帐号|户名|卡号|开户行|开户|收款人|付款人|纳税人|名称|单位|电话|联系人)\s*[：:]?"
)
# 机关自有地址（"黔东南州凯里市凯丰一路3号凯里市人民法院政治部"）：
# 与机构名同理，公开可查且是阅卷定位锚点，不脱敏
_ORG_ADDR_TAIL_RE = re.compile(
    r"(?:人民法院|人民检察院|人民政府|公安局|派出所|司法局|政治部|纪检|街道办)"
)

# 姓名启发式：角色词/字段名锚定 + 边界前瞻（D-003：宁缺勿伤）；敬称后缀独立模式
# 强角色锚：角色词后可直接跟姓名（"被告人张伟""审判员覃庆伟"）
_NAME_ROLE = (
    "委托诉讼代理人|诉讼代理人|法定代理人|指定代理人|代理人|辩护人|法定代表人|"
    "被上诉人|上诉人|被申请人|申请人|委托人|当事人|被害人|被告人|犯罪嫌疑人|嫌疑人|"
    "保证人|债权人|债务人|原告人|原告|被告|第三人|负责人|证人|"
    "审判长|审判员|助理审判员|人民陪审员|法官助理|书记员|翻译人员|鉴定人|"
    "收款人|付款人|汇款人|开户人|持卡人|联系人|报案人|举报人|担保人|"
    "签收人|承租人|投保人|被保险人|继承人|受害人|受害者|"
    "复核人|开票人|制单人|经办人|审核人|办税人|领款人|代办人"
)
# 合同相对方（甲方/乙方…）：后多接谓语（"乙方向甲方"），须显式冒号才当姓名字段；
# 允许"甲方代表""乙方签约人"等职务后缀（"甲方代表：孙小梅"）
_NAME_CONTRACT_ROLE = (
    r"(?:甲方|乙方|丙方|丁方|买方|卖方|出租方|承租方)"
    r"(?:代表|签约人|签字人|经办人|负责人|联系人)?"
)


def _spaced_words(words: str) -> str:
    """职务词逐字允许空白：判决书落款常排成"审　判　员　　覃庆伟"。"""
    return "|".join(r"\s*".join(w) for w in words.split("|"))


_NAME_ROLE_RE = _spaced_words(_NAME_ROLE)
# 字段名锚定（笔录/起诉书常见"姓名：张三""户名：李四"）
_NAME_FIELD = r"姓\s*名|户\s*名"
_NAME_BOUNDARY = (
    r"(?=[，。；、：:！？（(）)《》〈〉\s]|性别|民族|年龄|出生|职业|文化|籍贯|住址|现住|户籍|"
    r"工作单位|身份|电话|证件|与|和|就|将|因|对|向|从|系|称|诉|辩|到|在|未|不|签|经|于|指|"
    r"共|即|均|另|构成|行为|一致|等|男|女|审|协|担|办|出|兼|负|受|主|组|合|参|陪|代|犯|证|违|"
    # 姓名直接接机构主体词（"被告杨某某物业…"）：姓名在此截断，机构词不并入
    r"物业|业委会|业主|公司|集团|银行|医院|学校|事务所|单位|法院|本院|合同|纠纷|案件|$)"
)
_NAME_RE = re.compile(
    rf"(?:{_NAME_ROLE_RE}|{_NAME_FIELD})\s*[：:，,、]?\s*([\u4e00-\u9fa5]{{2,4}}?){_NAME_BOUNDARY}"
)
# 合同相对方须显式冒号（"乙方：王小明"）；允许"承租方（乙方）："与"乙方（签字）："的括号注记
_NAME_CONTRACT_RE = re.compile(
    rf"(?:{_NAME_CONTRACT_ROLE})(?:[（(]?[^）)：:]{{0,6}}[）)])?\s*[：:]\s*"
    rf"([\u4e00-\u9fa5]{{2,4}}?){_NAME_BOUNDARY}"
)
# "由潘虹担任书记员""张春梅任审判员"：姓名在前、职务在后，无角色锚
_NAME_ASSUME_RE = re.compile(
    r"(?:由|经)?\s*([\u4e00-\u9fa5]{2,3}?)(?:担任|出任|兼任|系)"
    r"(?:[\u4e00-\u9fa5]{0,6})?(?:书\s*记\s*员|审\s*判\s*员|审\s*判\s*长|法\s*官|"
    r"检\s*察\s*官|代\s*理\s*人|翻\s*译\s*人\s*员|鉴\s*定\s*人|法\s*警)"
)
# 顿号并列姓名（"被告人张伟、李娜"）：仅接前一个已识别姓名，避免扩散
_NAME_LIST_RE = re.compile(rf"、\s*([\u4e00-\u9fa5]{{2,4}}?){_NAME_BOUNDARY}")
_NAME_HONORIFIC_RE = re.compile(r"([\u4e00-\u9fa5]{2,4})(?:先生|女士|同志|律师|经理|法官|检察官)")
# 单字姓 + 职务（"王律师""李法官"）：姓氏本身即敏感信息；
# 排除"担任书记员""出任审判员"等动词末字被误当姓氏
_NAME_TITLE_RE = re.compile(r"(?<![担出兼委聘续转调任])([\u4e00-\u9fa5])(?=律师|法官|检察官|警官|书记员)")
_NAME_STOPWORDS = frozenset(
    {"本人", "双方", "各方", "单位", "代表", "本方", "对方", "其他", "上述", "原审", "一审",
     "二审", "再审", "本院", "委托", "代理", "法定", "法院", "人民", "检察", "公安", "公司",
     "银行", "医院", "学校", "政府", "委员会", "事务所", "有限", "股份", "原告", "被告"}
)
# 常见非姓名词（动词/法律术语）：命中即丢弃，避免"被告驾驶""原告陈述"被当人名
_NAME_NOT_A_NAME = frozenset(
    {"驾驶", "陈述", "辩称", "认可", "同意", "否认", "确认", "证实", "供述", "承认", "申请",
     "起诉", "上诉", "答辩", "主张", "认为", "提出", "提交", "举证", "质证", "作证", "签字",
     "盖章", "授权", "委托", "出具", "承诺", "保证", "支付", "偿还", "借款", "转账", "签收",
     "未到", "未出庭", "到庭", "缺席", "代理", "授权委托", "关系", "身份", "户籍", "住址"}
)
# 姓名片段中出现这些法律主体词，说明截取串跨入了下一个句子成分（"向甲方""告业主"）
_NAME_LEGAL_WORDS = (
    "原告", "被告", "上诉", "申请", "第三", "甲方", "乙方", "丙方", "丁方", "业主", "当事",
    "法院", "本院", "公司", "单位", "委员", "银行", "事务", "物业", "合同", "纠纷", "案件",
)
_SURNAMES = frozenset(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
# 复姓（两字姓）：用于姓名边界判定
_COMPOUND_SURNAMES = (
    "欧阳", "上官", "司马", "诸葛", "东方", "皇甫", "尉迟", "公孙", "慕容", "长孙",
    "宇文", "司徒", "鲜于", "独孤", "南宫", "万俟", "闻人", "夏侯", "赫连", "澹台",
)


# ---------------------------------------------------------------------------
# 检测层
# ---------------------------------------------------------------------------

class _Hit:
    __slots__ = ("etype", "start", "end", "text", "confidence")

    def __init__(self, etype: str, start: int, end: int, text: str, confidence: float) -> None:
        self.etype = etype
        self.start = start
        self.end = end
        self.text = text
        self.confidence = confidence


# 重叠消解优先级：编号小者优先；同优先级取更长区间
_TYPE_PRIORITY = {
    "custom": 0, "id_card": 1, "bank_card": 2, "phone": 3, "email": 4,
    "plate": 5, "address": 6, "name": 7,
}


def _compact_digits(value: str) -> str:
    """去掉号码中的空格/短横等分隔符（OCR 断字），保留字母与校验位。"""
    return re.sub(r"[ \u3000\-]", "", value)


def _scan_id_card(text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    for m in _ID_CARD_RE.finditer(text):
        raw = m.group()
        digits = _compact_digits(raw)
        if len(digits) != 18:
            continue
        # 出生日期段合法性：避免把 18 位纯数字流水号误当身份证
        if not (digits[6:10].isdigit() and 1900 <= int(digits[6:10]) <= 2099):
            continue
        if not ("01" <= digits[10:12] <= "12" and "01" <= digits[12:14] <= "31"):
            continue
        # 明确标注为统一社会信用代码 / 纳税人识别号：公开可查，不脱敏
        ctx = text[max(0, m.start() - 24): m.start()]
        if _CREDIT_CODE_LABEL_RE.search(ctx) is not None and _ID_CONTEXT_RE.search(ctx) is None:
            continue
        hits.append(_Hit("id_card", m.start(), m.end(), raw, 0.99 if _id_checksum_ok(digits) else 0.6))
    # 15 位老身份证（GB 11643 前身）：6 位区划 + YYMMDD + 3 位顺序码，本身无校验位；
    # 前面有证件字段名时按已确认处理，否则中置信待人工确认
    for m in _ID_CARD_15_RE.finditer(text):
        digits = _compact_digits(m.group())
        if len(digits) != 15:
            continue
        if not ("01" <= digits[8:10] <= "12" and "01" <= digits[10:12] <= "31"):
            continue
        anchored = _ID_CONTEXT_RE.search(text[max(0, m.start() - 12): m.start()]) is not None
        hits.append(_Hit("id_card", m.start(), m.end(), m.group(), 0.92 if anchored else 0.75))
    return hits


def _scan_bank_card(text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    for m in _BANK_CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if not 16 <= len(digits) <= 19:
            continue
        ctx = text[max(0, m.start() - 24): m.start()]
        # 明确标注为统一社会信用代码 / 纳税人识别号：公开可查，不按银行卡处理
        if _CREDIT_CODE_LABEL_RE.search(ctx) is not None:
            continue
        luhn = _luhn_ok(digits)
        # 无卡号语境且 Luhn 不通过：多为发票号/单据流水/OCR 噪声，直接丢弃
        if not luhn and _BANK_CONTEXT_RE.search(ctx) is None:
            continue
        hits.append(_Hit("bank_card", m.start(), m.end(), m.group(), 0.98 if luhn else 0.7))
    return hits


def _scan_phone(text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    for m in _PHONE_MOBILE_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if digits.startswith("86") and len(digits) == 13:
            digits = digits[2:]
        hits.append(_Hit("phone", m.start(), m.end(), m.group(), 0.98 if len(digits) == 11 else 0.9))
    for m in _PHONE_LANDLINE_RE.finditer(text):
        raw = m.group()
        ctx = text[max(0, m.start() - 24): m.start()]
        # 机关对外公布的座机（举报电话/值班/总机等）：可公开查询，不打码
        if _ORG_PHONE_LABEL_RE.search(ctx) is not None:
            continue
        # 无分隔符又无电话标签：与发票号/流水号同形（"052001806104"），不采信
        if not re.search(r"[ \u3000\-（(）)]", raw) and _PHONE_CONTEXT_RE.search(ctx) is None:
            continue
        hits.append(_Hit("phone", m.start(), m.end(), raw, 0.95))
    return hits


def _scan_email(text: str) -> list[_Hit]:
    return [_Hit("email", m.start(), m.end(), m.group(), 0.99) for m in _EMAIL_RE.finditer(text)]


def _scan_plate(text: str) -> list[_Hit]:
    return [_Hit("plate", m.start(), m.end(), m.group(), 0.98) for m in _PLATE_RE.finditer(text)]


def _address_hit(text: str, start: int, end: int, conf: float) -> _Hit | None:
    """裁掉地址前的叙述引导词与表单标签（"现住""住址为""账号"），只遮真实地址串。"""
    raw = text[start:end]
    for pattern in (_ADDR_LEAD_RE, _ADDR_LABEL_RE):
        lead = pattern.match(raw)
        if lead and lead.end() < len(raw) - 3:
            start += lead.end()
            raw = raw[lead.end():]
    if len(raw) < 4:
        return None
    return _Hit("address", start, end, raw, conf)


def _scan_address(text: str) -> list[_Hit]:
    candidates: list[tuple[int, int, float]] = []
    candidates += [(m.start(), m.end(), 0.92) for m in _ADDR_FULL_RE.finditer(text)]
    candidates += [(m.start(), m.end(), 0.9) for m in _ADDR_RESIDENTIAL_RE.finditer(text)]
    candidates += [(m.start(), m.end(), 0.88) for m in _ADDR_TOWN_RE.finditer(text)]
    candidates += [(m.start(), m.end(), 0.86) for m in _ADDR_CITY_ROAD_RE.finditer(text)]
    candidates += [(m.start(), m.end(), 0.84) for m in _ADDR_COMMUNITY_RE.finditer(text)]
    candidates += [(m.start(), m.end(), 0.72) for m in _ADDR_PARTIAL_RE.finditer(text)]
    hits: list[_Hit] = []
    for start, end, conf in candidates:
        hit = _address_hit(text, start, end, conf)
        if hit is None:
            continue
        # 机关自有地址（"…3号凯里市人民法院政治部"）：机构信息不脱敏。
        # 机关名可能落在候选区间末尾之后，故同时检查后续紧邻文字（遇标点即停）。
        if _ORG_ADDR_TAIL_RE.search(text[hit.start:hit.end + 16]) is not None:
            continue
        hits.append(hit)
    return hits


def _name_confidence(name: str) -> float | None:
    """姓名置信：姓氏开头 0.9；复姓 0.9；非姓氏开头返回 None（直接丢弃，避免"驾驶""陈述"）。"""
    if name in _NAME_STOPWORDS or name in _NAME_NOT_A_NAME:
        return None
    # 截取串跨入下一句成分（"向甲方""告业主""理张春梅"）：整串丢弃而非部分打码
    if any(w in name for w in _NAME_LEGAL_WORDS):
        return None
    if name[:2] in _COMPOUND_SURNAMES and len(name) >= 3:
        return 0.9
    if name[0] in _SURNAMES and 2 <= len(name) <= 4:
        return 0.9
    return None


def _scan_name(text: str) -> list[_Hit]:
    hits: list[_Hit] = []
    seen: set[tuple[int, int]] = set()

    def add(name: str, start: int, end: int) -> bool:
        conf = _name_confidence(name)
        if conf is None or (start, end) in seen:
            return False
        seen.add((start, end))
        hits.append(_Hit("name", start, end, name, conf))
        return True

    for m in _NAME_RE.finditer(text):
        add(m.group(1), m.start(1), m.end(1))
    for m in _NAME_CONTRACT_RE.finditer(text):
        add(m.group(1), m.start(1), m.end(1))
    for m in _NAME_ASSUME_RE.finditer(text):
        add(m.group(1), m.start(1), m.end(1))
    for m in _NAME_HONORIFIC_RE.finditer(text):
        add(m.group(1), m.start(1), m.end(1))
    for m in _NAME_TITLE_RE.finditer(text):
        surname = m.group(1)
        if surname in _SURNAMES and (m.start(1), m.end(1)) not in seen:
            seen.add((m.start(1), m.end(1)))
            hits.append(_Hit("name", m.start(1), m.end(1), surname, 0.9))
    # 顿号并列（"被告人张伟、李娜、王强"）：仅在已识别姓名后逐段延伸
    for host in list(hits):
        pos = host.end
        while True:
            m = _NAME_LIST_RE.match(text, pos)
            if m is None or not add(m.group(1), m.start(1), m.end(1)):
                break
            pos = m.end(1)
    return hits


def _scan_custom(text: str, terms: list[str]) -> list[_Hit]:
    hits: list[_Hit] = []
    for term in sorted(set(terms), key=len, reverse=True):
        start = text.find(term)
        while start >= 0:
            hits.append(_Hit("custom", start, start + len(term), term, 1.0))
            start = text.find(term, start + len(term))
    return hits


_SCANNERS = {
    "id_card": _scan_id_card,
    "bank_card": _scan_bank_card,
    "phone": _scan_phone,
    "email": _scan_email,
    "plate": _scan_plate,
    "address": _scan_address,
    "name": _scan_name,
}


def detect_raw(text: str, types: set[str], custom_terms: list[str]) -> list[_Hit]:
    """全类型扫描 + 重叠消解，返回按 start 升序的命中列表。"""
    hits: list[_Hit] = []
    if "custom" in types and custom_terms:
        hits.extend(_scan_custom(text, custom_terms))
    for etype, scanner in _SCANNERS.items():
        if etype in types:
            hits.extend(scanner(text))
    hits.sort(key=lambda h: (_TYPE_PRIORITY[h.etype], -(h.end - h.start), h.start))
    accepted: list[_Hit] = []
    taken: list[tuple[int, int]] = []
    for hit in hits:
        if any(hit.start < e and hit.end > s for s, e in taken):
            continue
        accepted.append(hit)
        taken.append((hit.start, hit.end))
    accepted.sort(key=lambda h: h.start)
    return accepted


def _hit_status(confidence: float) -> str:
    return "confirmed" if confidence >= CONFIRM_THRESHOLD else "needs_human"


# ---------------------------------------------------------------------------
# 策略与别名
# ---------------------------------------------------------------------------

def _policy_types(case: dict[str, Any]) -> frozenset[str]:
    policy = case.get("redactionPolicy") or "standard"
    if policy == "custom":
        custom = frozenset(case.get("redactionCustomTypes") or [])
        if not custom:
            raise RedactionError("自定义脱敏档位未配置类型集合，请先在案件设置中选择")
        return custom
    return POLICY_TYPES[policy]


def _letters(n: int) -> str:
    """1→A, 26→Z, 27→AA（Excel 列序号）。"""
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("A") + r) + out
    return out


def _letters_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _next_alias(existing_aliases: list[str], etype: str) -> str:
    prefix = ALIAS_PREFIX[etype]
    max_idx = 0
    for alias in existing_aliases:
        if alias.startswith(prefix):
            suffix = alias[len(prefix):]
            if suffix.isalpha() and suffix.isupper():
                max_idx = max(max_idx, _letters_index(suffix))
    return f"{prefix}{_letters(max_idx + 1)}"


def _row_to_entity(row: dict[str, Any], include_original: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "caseId": row["case_id"],
        "type": row["entity_type"],
        "typeLabel": ENTITY_TYPE_ORDER.get(row["entity_type"], row["entity_type"]),
        "alias": row["alias"],
        "confidence": row["confidence"],
        "status": row["status"],
        "createdAt": row["created_at"],
    }
    if include_original:
        out["original"] = security.unprotect(row["original_enc"])
    return out


# ---------------------------------------------------------------------------
# 映射持久层
# ---------------------------------------------------------------------------

def _load_case_terms(case_id: str) -> list[str]:
    rows = get_db().query("SELECT term FROM redaction_terms WHERE case_id = ?", (case_id,))
    return [r["term"] for r in rows]


def _ensure_entities(case_id: str, hits: list[_Hit]) -> list[dict[str, Any]]:
    """为命中建立/复用案件级映射行（D-010 稳定别名）。

    DPAPI 密文非确定 → 不能按 original_enc 等值查询；改为解密比对同类型已有行。
    同一 (类型, 原文) 复用既有别名；新实体按类型最大序号 +1 编号。
    返回与 hits 一一对应的完整行（含 id/alias/status/confidence）。
    """
    if not hits:
        return []
    db = get_db()
    types_present = {h.etype for h in hits}
    placeholders = ",".join("?" for _ in types_present)
    existing_rows = db.query(
        f"SELECT * FROM redaction_entities WHERE case_id = ? AND entity_type IN ({placeholders})",
        (case_id, *types_present),
    )
    # (类型, 原文) → 行；同时维护各类型别名全集（含未命中类型外行，用于编号不冲突）
    all_case_rows = db.query("SELECT alias, entity_type FROM redaction_entities WHERE case_id = ?", (case_id,))
    alias_pool: dict[str, list[str]] = {}
    for r in all_case_rows:
        alias_pool.setdefault(r["entity_type"], []).append(r["alias"])

    index: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing_rows:
        index[(row["entity_type"], security.unprotect(row["original_enc"]))] = row

    resolved: list[dict[str, Any]] = []
    to_insert: list[tuple[dict[str, Any], _Hit]] = []
    ts = now_iso()
    for hit in hits:
        key = (hit.etype, hit.text)
        row = index.get(key)
        if row is not None:
            resolved.append(row)
            continue
        alias = _next_alias(alias_pool.get(hit.etype, []), hit.etype)
        row = {
            "id": str(uuid.uuid4()),
            "case_id": case_id,
            "entity_type": hit.etype,
            "original_enc": security.protect(hit.text),
            "alias": alias,
            "confidence": hit.confidence,
            "status": _hit_status(hit.confidence),
            "created_at": ts,
        }
        index[key] = row
        alias_pool.setdefault(hit.etype, []).append(alias)
        resolved.append(row)
        to_insert.append((row, hit))

    if to_insert:
        type_counts: dict[str, int] = {}
        with db.transaction() as conn:
            for row, hit in to_insert:
                conn.execute(
                    """
                    INSERT INTO redaction_entities(id, case_id, entity_type, original_enc, alias,
                                                   confidence, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (row["id"], case_id, row["entity_type"], row["original_enc"], row["alias"],
                     row["confidence"], row["status"], row["created_at"]),
                )
                type_counts[row["entity_type"]] = type_counts.get(row["entity_type"], 0) + 1
            from . import audit

            audit.append(
                conn, action="redaction.map", resource_type="case", resource_id=case_id,
                case_id=case_id,
                metadata={"entitiesCreated": len(to_insert), "types": type_counts},
            )
    return resolved


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def resolve_policy(case_id: str) -> tuple[str, frozenset[str]]:
    """返回（档位名, 类型集合），供 UI 与外发检查使用。"""
    case = get_case(case_id)
    return case.get("redactionPolicy") or "standard", _policy_types(case)


def detect(case_id: str, text: str, types: list[str] | None = None) -> list[dict[str, Any]]:
    """纯检测（不建映射、不落库）：返回命中明细，供 UI 快速扫描。"""
    _, case_types = resolve_policy(case_id)
    active = frozenset(types) if types else case_types
    terms = _load_case_terms(case_id)
    return [
        {
            "type": h.etype,
            "typeLabel": ENTITY_TYPE_ORDER.get(h.etype, h.etype),
            "original": h.text,
            "start": h.start,
            "end": h.end,
            "confidence": round(h.confidence, 3),
            "proposedStatus": _hit_status(h.confidence),
        }
        for h in detect_raw(text, set(active), terms)
    ]


# ---------------------------------------------------------------------------
# 跨行合并 + 部分打码
# ---------------------------------------------------------------------------

_MASKABLE_WS = " \t\r\n\v\f\u3000\u00a0"


def _is_word_char(ch: str) -> bool:
    """文字字符（字母/数字/汉字/下划线）：跨行合并只发生在两侧均为文字字符时。"""
    return ch.isalnum() or ch == "_"


def _merge_crossline(text: str) -> tuple[str, list[int], dict[int, str]]:
    """合并被 PDF/OCR 换行切断的实体串（"522628198204\\n150037" → 完整身份证号）。

    仅当空白两侧均为文字字符时合并（标点边界不动，正文段落版式不受影响）。
    返回 (规范化文本, imap[normalized下标→原文下标], gaps[规范化下标→被吸收空白串])：
    mask_text 在规范化文本上检测、在原文上重建 —— 命中区间的换行由打码串吸收，
    未命中区间的换行原样还原（版式换行不丢失）。
    """
    chars: list[str] = []
    imap: list[int] = []
    gaps: dict[int, str] = {}
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _MASKABLE_WS:
            j = i
            while j < n and text[j] in _MASKABLE_WS:
                j += 1
            if chars and _is_word_char(chars[-1]) and j < n and _is_word_char(text[j]):
                gaps[len(chars)] = text[i:j]
                i = j
                continue
        chars.append(ch)
        imap.append(i)
        i += 1
    return "".join(chars), imap, gaps


def _mask_digits(text: str, keep_head: int, keep_tail: int) -> str:
    """按数字位打码，保留首 keep_head / 末 keep_tail 位，分隔符（空格、短横）原样保留。

    OCR 断字的号码（"5226 2819 8204 1500 37"）打码后仍是可读版式（"522628**********0037"）。
    """
    positions = [i for i, ch in enumerate(text) if ch.isdigit()]
    if not positions:
        return "*" * len(text)
    hide = set(positions[keep_head: len(positions) - keep_tail if keep_tail else len(positions)])
    return "".join("*" if i in hide else ch for i, ch in enumerate(text))


def _partial_mask(text: str, etype: str) -> str:
    """部分打码：只遮关键段、保留可读上下文（全打码会让 Agent 阅卷失去上下文）。

    王志远→王*远；522628********0037（前 6 行政区划 + 末 4）；138****5678；
    贵阳市观山湖区**********（保留省市区县，遮小区门牌）。
    """
    n = len(text)
    if n == 0:
        return text
    if etype in ("name", "custom"):
        if n == 1:
            return "*"
        if n == 2:
            return text[0] + "*"
        return text[0] + "*" * (n - 2) + text[-1]
    if etype == "id_card":
        if sum(ch.isdigit() for ch in text) < 10:
            return "*" * n
        return _mask_digits(text, 6, 4)
    if etype == "phone":
        # 座机保留区号（"0851-****3456"），手机保留前 3 后 4（"138****5678"）
        m_area = re.match(r"[（(]?(0\d{2,3})[）)]?", text)
        if m_area:
            return _mask_digits(text, len(m_area.group(1)), 4)
        return _mask_digits(text, 3, 4)
    if etype == "bank_card":
        if sum(ch.isdigit() for ch in text) <= 8:
            return "*" * n
        return _mask_digits(text, 4, 4)
    if etype == "email":
        local, sep, domain = text.partition("@")
        if not sep:
            return "***"
        return (local[:1] if local else "") + "***" + sep + domain
    if etype == "address":
        # 保留省市区县（公开行政区划，利于阅卷定位），遮街道/小区/门牌
        m_end = 0
        for m in re.finditer(r"省|自治区|市|盟|州|区|县|旗|镇|乡", text):
            m_end = m.end()
        if 0 < m_end < n:
            return text[:m_end] + "*" * (n - m_end)
        keep = min(3, n - 1)
        return text[:keep] + "*" * (n - keep)
    if etype == "plate":
        if n <= 4:
            return "*" * n
        return text[:2] + "*" * (n - 4) + text[-2:]
    return "*" * n


def mask_text(case_id: str, text: str) -> dict[str, Any]:
    """文档级脱敏打码（用户主动操作，「一键脱敏」/「脱敏处理」按钮）。

    - 与外发脱敏（apply/别名）相互独立：不建实体映射、不落库、无阻断语义；
    - 部分打码（_partial_mask）：只遮关键段（王志远→王*远），保留可读上下文，
      避免全打码破坏阅卷分析；
    - 跨行合并（_merge_crossline）：PDF 解析断行切断的实体先合并再检测，
      命中区间的换行随打码吸收，未命中区间版式不变；
    - 扫描类型沿用案件脱敏档位（resolve_policy）；
    - 返回 {maskedText, total, counts（类型中文标签 → 计数）}。
    """
    policy, types = resolve_policy(case_id)
    terms = _load_case_terms(case_id)
    source = text or ""
    normalized, imap, gaps = _merge_crossline(source)
    hits = detect_raw(normalized, set(types), terms)
    hit_by_start = {h.start: h for h in hits}
    out: list[str] = []
    counts: dict[str, int] = {}
    k = 0
    while k < len(normalized):
        hit = hit_by_start.get(k)
        if hit is not None:
            out.append(_partial_mask(hit.text, hit.etype))
            counts[hit.etype] = counts.get(hit.etype, 0) + 1
            k = hit.end
            continue
        if k in gaps:
            out.append(gaps[k])  # 未命中的合并空白原样还原（版式不变）
        out.append(source[imap[k]])
        k += 1
    return {
        "caseId": case_id,
        "policy": policy,
        "maskedText": "".join(out),
        "total": len(hits),
        "counts": {ENTITY_TYPE_ORDER.get(t, t): n for t, n in counts.items()},
    }


def preview(case_id: str, text: str) -> dict[str, Any]:
    """外发差异预览（REQ-PRIV-005）：建立稳定映射并返回逐项 原文→别名 差异。

    本机受信 UI 场景，包含原文；低置信项进入人工确认（needs_human）。
    """
    policy, types = resolve_policy(case_id)
    terms = _load_case_terms(case_id)
    hits = [h for h in detect_raw(text, set(types), terms)]
    rows = _ensure_entities(case_id, hits)

    items: list[dict[str, Any]] = []
    occurrence: dict[str, int] = {}
    for hit, row in zip(hits, rows):
        occurrence[row["id"]] = occurrence.get(row["id"], 0) + 1
        items.append(
            {
                "entityId": row["id"],
                "type": hit.etype,
                "typeLabel": ENTITY_TYPE_ORDER.get(hit.etype, hit.etype),
                "original": hit.text,
                "alias": row["alias"],
                "confidence": round(hit.confidence, 3),
                "status": row["status"],
                "occurrence": occurrence[row["id"]],
            }
        )
    needs_human = [i for i in items if i["status"] == "needs_human"]
    blocked = [i for i in items if i["status"] == "needs_human" and i["type"] in HIGH_SENSITIVITY_TYPES]
    return {
        "caseId": case_id,
        "policy": policy,
        "totalHits": len(items),
        "items": items,
        "needsHumanCount": len(needs_human),
        "blockedCount": len(blocked),
        "sampleRedactedText": _replace_spans(text, hits, rows)[:400],
    }


def apply(case_id: str, text: str) -> dict[str, Any]:
    """按案件档位脱敏文本（外发前调用，GOAL-004：先脱敏再授权）。

    返回 redactedText（不含任何原文）与 blockedBy（范围内低置信高敏感实体 id）：
    blockedBy 非空时调用方必须阻断外发并引导人工确认（REQ-PRIV-006）。
    """
    policy, types = resolve_policy(case_id)
    terms = _load_case_terms(case_id)
    hits = detect_raw(text, set(types), terms)
    rows = _ensure_entities(case_id, hits)
    redacted = _replace_spans(text, hits, rows)

    needs_human_ids = [row["id"] for row in rows if row["status"] == "needs_human"]
    blocked_by = [
        row["id"] for row in rows
        if row["status"] == "needs_human" and row["entity_type"] in HIGH_SENSITIVITY_TYPES
    ]
    if blocked_by:
        from . import audit

        audit.log_standalone(
            action="redaction.blocked", resource_type="case", resource_id=case_id,
            case_id=case_id, result="denied",
            metadata={"policy": policy, "blockedCount": len(blocked_by)},
        )
    return {
        "caseId": case_id,
        "policy": policy,
        "redactedText": redacted,
        "replacedCount": len(rows),
        "entities": [
            {"id": r["id"], "type": r["entity_type"], "alias": r["alias"], "status": r["status"]}
            for r in rows
        ],
        "needsHumanIds": needs_human_ids,
        "blockedBy": blocked_by,
    }


def _replace_spans(text: str, hits: list[_Hit], rows: list[dict[str, Any]]) -> str:
    """从后往前替换区间，避免偏移失效。"""
    out = text
    for hit, row in sorted(zip(hits, rows), key=lambda p: p[0].start, reverse=True):
        out = out[: hit.start] + row["alias"] + out[hit.end:]
    return out


def restore(case_id: str, redacted_text: str) -> str:
    """本地恢复别名（REQ-PRIV-001）：仅限本机受信场景（产物展示/导出）。

    长别名优先替换，避免"人物A"吞掉"人物AB"的前缀。
    """
    rows = get_db().query(
        "SELECT alias, original_enc FROM redaction_entities WHERE case_id = ?", (case_id,)
    )
    mapping = {r["alias"]: security.unprotect(r["original_enc"]) for r in rows}
    out = redacted_text
    for alias in sorted(mapping, key=len, reverse=True):
        out = out.replace(alias, mapping[alias])
    return out


def list_entities(case_id: str, status: str | None = None) -> list[dict[str, Any]]:
    """案件实体映射清单（本机 UI；含解密原文用于差异核对）。"""
    sql = "SELECT * FROM redaction_entities WHERE case_id = ?"
    params: tuple[Any, ...] = (case_id,)
    if status:
        sql += " AND status = ?"
        params = (case_id, status)
    sql += " ORDER BY entity_type, alias"
    return [_row_to_entity(r, include_original=True) for r in get_db().query(sql, params)]


def confirm_entities(case_id: str, entity_ids: list[str], action: str = "confirm") -> dict[str, Any]:
    """人工确认 / 驳回低置信实体（REQ-PRIV-006 / D-002）。

    - confirm：状态置 confirmed（保留别名）；
    - reject：误报删除映射行（启发式下次可能重现，可在档位中关闭该类型）。
    """
    if action not in ("confirm", "reject"):
        raise RedactionError(f"无效的确认动作：{action}")
    ids = [str(i) for i in entity_ids if str(i).strip()]
    if not ids:
        raise RedactionError("entityIds 不能为空")
    get_case(case_id)
    db = get_db()
    placeholders = ",".join("?" for _ in ids)
    rows = db.query(
        f"SELECT id, entity_type, status FROM redaction_entities WHERE case_id = ? AND id IN ({placeholders})",
        (case_id, *ids),
    )
    found = {r["id"] for r in rows}
    missing = set(ids) - found
    if missing:
        raise RedactionError(f"实体不存在或不属于该案件：{len(missing)} 个")
    type_counts: dict[str, int] = {}
    with db.transaction() as conn:
        if action == "confirm":
            conn.execute(
                f"UPDATE redaction_entities SET status = 'confirmed' WHERE case_id = ? AND id IN ({placeholders})",
                (case_id, *ids),
            )
        else:
            conn.execute(
                f"DELETE FROM redaction_entities WHERE case_id = ? AND id IN ({placeholders})",
                (case_id, *ids),
            )
        for r in rows:
            type_counts[r["entity_type"]] = type_counts.get(r["entity_type"], 0) + 1
        from . import audit

        audit.append(
            conn, action=f"redaction.{action}", resource_type="case", resource_id=case_id,
            case_id=case_id,
            metadata={"count": len(ids), "types": type_counts},
        )
    return {"ok": True, "action": action, "count": len(ids)}


# ---------------------------------------------------------------------------
# 自定义词典（redaction_terms）
# ---------------------------------------------------------------------------

def list_terms(case_id: str) -> list[dict[str, Any]]:
    rows = get_db().query(
        "SELECT * FROM redaction_terms WHERE case_id = ? ORDER BY created_at", (case_id,)
    )
    return [{"id": r["id"], "caseId": r["case_id"], "term": r["term"], "createdAt": r["created_at"]} for r in rows]


def add_term(case_id: str, term: str) -> dict[str, Any]:
    """添加案件自定义敏感词（strict 档 / custom 档勾选 custom 类型时生效）。"""
    from .cases import require_active_case

    require_active_case(case_id)
    term = (term or "").strip()
    if not term:
        raise RedactionError("敏感词不能为空")
    if len(term) > 50:
        raise RedactionError("敏感词过长（≤50 字符）")
    db = get_db()
    dup = db.query_one("SELECT id FROM redaction_terms WHERE case_id = ? AND term = ?", (case_id, term))
    if dup:
        raise RedactionError("该敏感词已存在")
    term_id = str(uuid.uuid4())
    import hashlib as _hl

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO redaction_terms(id, case_id, term, created_at) VALUES (?, ?, ?, ?)",
            (term_id, case_id, term, now_iso()),
        )
        from . import audit

        audit.append(
            conn, action="redaction.term.add", resource_type="case", resource_id=case_id,
            case_id=case_id,
            metadata={"termHash": _hl.sha256(term.encode("utf-8")).hexdigest()[:16]},
        )
    return {"id": term_id, "caseId": case_id, "term": term}


def remove_term(case_id: str, term_id: str) -> dict[str, Any]:
    from .cases import require_active_case

    require_active_case(case_id)
    db = get_db()
    with db.transaction() as conn:
        cur = conn.execute(
            "DELETE FROM redaction_terms WHERE id = ? AND case_id = ?", (term_id, case_id)
        )
        if cur.rowcount == 0:
            raise RedactionError("敏感词不存在")
        from . import audit

        audit.append(
            conn, action="redaction.term.remove", resource_type="case", resource_id=case_id,
            case_id=case_id,
        )
    return {"ok": True, "id": term_id}
