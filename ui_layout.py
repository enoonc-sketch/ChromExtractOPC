#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ui_layout.py —— 桌面主窗口的导航结构（v3.2 新增；纯数据 + 纯函数，不依赖 tkinter，可单独测试）

v3.1 的主窗口是 14 个平铺的标签页，用户反馈“标签页太多”。v3.2 改成“左侧分组导航 + 右侧内容区”：

    监控  总览（新：仪表盘卡片） / 通道数据 / 谱图预览（含峰识别） / 告警
    数据  历史与对比 / 导出 CSV / 数据源 / 运行报表
    通讯  客户端连接 / Modbus 转发 / 通讯诊断 / 驱动 / 连接测试
    系统  常用设置 / 安全与账号 / 权限与审计 / 备份与还原
    运行日志：窗口底部可折叠面板（不占主区）

本模块只定义“有哪些页面、属于哪一组、需要什么权限、页面由哪些区域组成、每个区域来自 v3.1 的哪个
标签页的哪个区域”，以及懒加载、记住页面 / 窗口大小、总览卡片、状态栏要用到的纯函数。
窗口（desktop_ui.py）按这里的数据画导航、建页面；所有显示文字都是中文原文，显示时经 i18n.tr() 翻译。

只改界面组织，不改功能逻辑：每个区域仍由 v3.1 原来的代码画出（desktop_ui 里拆成 _sec_* 方法），
按钮背后的权限检查（desktop_ui.GUARDED）不变；导航上的锁只是让没有权限的页面一眼可见。
"""
import re

DEFAULT_PAGE = "overview"
DEFAULT_SIZE = (1100, 720)          # 默认窗口大小（v3.1 是 1180x800）
MIN_SIZE = (900, 600)               # 最小窗口大小
NAV_WIDTH_RANGE = (150, 340)        # 左侧导航宽度（像素）的上下限：按最长文字自适应，夹在这个范围里

# 锁图标（14x14 灰色 PNG，tk.PhotoImage(data=...) 直接可用；不用 emoji，Tk 8.6 显示非 BMP 字符不可靠）
LOCK_ICON_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAA4AAAAOCAYAAAAfSC3RAAAAWklEQVR42mNgIBMwYhPMK2/8j8yf1FmPoY6JkCZcYky4nDKps54R"
                 "m00MuJyIy0Z0cSZSAgRZMyM+f+DyAsk2EuXUiR11DBM76kjXSLaNNNPIgksiv6KJNjYCAMAwIKnFR8GcAAAAAElFTkSuQmCC")
LOCK_FALLBACK = "⊘ "                # 图标建不出来时用的文字前缀（BMP 字符）
ICON_PX = 14


class Page:
    """导航里的一个页面。

    pid      页面标识（存到配置 desktop_last_page）
    title    导航上显示的中文原文
    perm     打开页面需要的权限（audit.PERMISSIONS 的键；"view" = 所有人）
    builder  整页由 DesktopWindow 的这个方法构建（v3.1 原样搬过来的整页）；None = 由下面的区域组成
    intro    页面顶部（子标签上方）的说明 / 快捷按钮区：DesktopWindow 方法名，可为 None
    subtabs  ((子标签中文标题, (区域方法名, ...)), ...)；只有一个且标题为空 = 不分子标签，整页滚动
    form     页面里有“保存设置”表单字段（内容区底部显示保存栏）
    new      v3.2 新增的页面（不来自 v3.1）
    """

    def __init__(self, pid, title, perm, builder=None, intro=None, subtabs=(), form=False, new=False):
        self.pid, self.title, self.perm = pid, title, perm
        self.builder, self.intro = builder, intro
        self.subtabs = tuple((t, tuple(s)) for t, s in subtabs)
        self.form = bool(form)
        self.new = bool(new)

    def sections(self):
        return [s for _t, secs in self.subtabs for s in secs]

    def subtab_titles(self):
        return [t for t, _s in self.subtabs if t]

    def methods(self):
        """构建这个页面要用到的 DesktopWindow 方法名（测试核对都存在）。"""
        out = [m for m in (self.builder, self.intro) if m]
        return out + self.sections()

    def __repr__(self):
        return "Page(%r)" % self.pid


class Group:
    def __init__(self, gid, title, pages):
        self.gid, self.title, self.pages = gid, title, tuple(pages)

    def __repr__(self):
        return "Group(%r)" % self.gid


GROUPS = (
    Group("monitor", "监控", (
        Page("overview", "总览", "view", builder="_page_overview", new=True),
        Page("channels", "通道数据", "view", builder="_build_channels_tab"),
        Page("preview", "谱图预览", "view", builder="_build_preview_tab"),
        Page("alarms", "告警", "view", subtabs=(
            ("当前告警", ("_sec_run_alerts", "_sec_channel_rules")),
            ("设备检测", ("_sec_probes",)),
            ("通知设置", ("_sec_alert_settings", "_sec_quiet", "_sec_email")),
        ), form=True),
    )),
    Group("data", "数据", (
        Page("history", "历史与对比", "view", builder="_build_history_tab"),
        Page("export", "导出 CSV / 数据源", "export", subtabs=(
            ("", ("_sec_data_dirs", "_sec_source", "_sec_export_csv", "_sec_csv_remote")),
        ), form=True),
        Page("reports", "运行报表", "view", builder="_build_reports_tab"),   # 操作员可看 / 打开报表文件夹，生成需工程师
    )),
    Group("comm", "通讯", (
        Page("clients", "客户端连接", "view", builder="_build_clients_tab"),
        Page("modbus", "Modbus 转发", "modbus_map", builder="_build_modbus_tab"),
        Page("commdiag", "通讯诊断", "diagnose", intro="_comm_intro", subtabs=(
            ("IP 扫描", ("_sec_ip_scan",)),
            ("端口扫描", ("_sec_port_scan",)),
            ("串口诊断", ("_sec_serial",)),
        )),
        Page("tools", "驱动 / 连接测试", "diagnose", intro="_tools_intro", subtabs=(
            ("驱动与环境", ("_sec_env",)),
            ("连接测试", ("_sec_conn_test",)),
            ("Modbus Poll", ("_sec_modbus_poll",)),
            ("Modbus Slave", ("_sec_modbus_slave",)),
            ("稳定性测试", ("_sec_stability",)),
        ), form=True),
    )),
    Group("system", "系统", (
        Page("quick", "常用设置", "config", subtabs=(
            ("常用", ("_sec_quick_common", "_sec_quick_notify", "_sec_quick_startup", "_sec_quick_lang")),
            ("OPC UA 服务", ("_sec_opcua",)),
            ("高级设置", ("_sec_advanced",)),
        ), form=True),
        Page("security", "安全与账号", "security", intro="_security_intro", subtabs=(
            ("", ("_sec_accounts", "_sec_sec_report")),
        )),
        Page("audit", "权限与审计", "roles", subtabs=(
            ("", ("_sec_roles", "_sec_audit_log")),
        )),
        Page("backup", "备份与还原", "backup", subtabs=(
            ("", ("_sec_backup_export", "_sec_backup_restore")),
        )),
    )),
)

LOG_PANEL_TITLE = "运行日志"         # 底部可折叠面板

# ---------------------------------------------------------------------------
# v3.1 → v3.2 对照（单元测试逐一核对：每个旧标签页 / 区域都恰好有一个新位置）
# ---------------------------------------------------------------------------

# v3.1 的 14 个标签页（顺序与 v3.1 窗口一致）及其中的区域（LabelFrame 标题）；整页一块的写空元组
LEGACY_TABS = (
    ("运行状态", ("运行告警", "设备检测", "通道告警设置")),
    ("通道数据", ()),
    ("谱图预览", ()),
    ("历史对比", ()),
    ("客户端连接", ()),
    ("数据与导出", ("数据目录", "导出 CSV")),
    ("运行报表", ()),
    ("告警与通知", ("告警设置", "静默时段（计划停机 / 检修时不推送）", "邮件通知（可选）")),
    ("诊断工具", ("驱动与软件环境检测", "连接测试", "Modbus Poll 主站测试", "Modbus Slave 从站模拟器", "长时间稳定性测试")),
    ("通讯诊断", ("IP 扫描（在线主机）", "端口扫描（单个主机）", "串口扫描与诊断（RS-232 / RS-485 / RS-422）")),
    ("Modbus 转发", ()),
    ("常用设置", ("常用配置", "告警桌面提醒", "启动与登录", "界面语言")),
    ("系统设置", ("数据源", "OPC UA 服务", "CSV 导出", "高级设置", "登录账号", "安全体检")),
    ("备份与审计", ("导出配置备份", "从备份还原", "账号与角色", "操作审计日志")),
)
LEGACY_TITLES = tuple(t for t, _s in LEGACY_TABS)

# 整页搬过来的构建方法 → v3.1 标签页
WHOLE_SOURCES = {
    "_build_channels_tab": "通道数据",
    "_build_preview_tab": "谱图预览",
    "_build_history_tab": "历史对比",
    "_build_clients_tab": "客户端连接",
    "_build_reports_tab": "运行报表",
    "_build_modbus_tab": "Modbus 转发",
}

# 区域方法 → (v3.1 标签页, v3.1 区域标题)
SECTION_SOURCES = {
    "_sec_run_alerts": ("运行状态", "运行告警"),
    "_sec_probes": ("运行状态", "设备检测"),
    "_sec_channel_rules": ("运行状态", "通道告警设置"),
    "_sec_data_dirs": ("数据与导出", "数据目录"),
    "_sec_export_csv": ("数据与导出", "导出 CSV"),
    "_sec_alert_settings": ("告警与通知", "告警设置"),
    "_sec_quiet": ("告警与通知", "静默时段（计划停机 / 检修时不推送）"),
    "_sec_email": ("告警与通知", "邮件通知（可选）"),
    "_sec_env": ("诊断工具", "驱动与软件环境检测"),
    "_sec_conn_test": ("诊断工具", "连接测试"),
    "_sec_modbus_poll": ("诊断工具", "Modbus Poll 主站测试"),
    "_sec_modbus_slave": ("诊断工具", "Modbus Slave 从站模拟器"),
    "_sec_stability": ("诊断工具", "长时间稳定性测试"),
    "_sec_ip_scan": ("通讯诊断", "IP 扫描（在线主机）"),
    "_sec_port_scan": ("通讯诊断", "端口扫描（单个主机）"),
    "_sec_serial": ("通讯诊断", "串口扫描与诊断（RS-232 / RS-485 / RS-422）"),
    "_sec_quick_common": ("常用设置", "常用配置"),
    "_sec_quick_notify": ("常用设置", "告警桌面提醒"),
    "_sec_quick_startup": ("常用设置", "启动与登录"),
    "_sec_quick_lang": ("常用设置", "界面语言"),
    "_sec_source": ("系统设置", "数据源"),
    "_sec_opcua": ("系统设置", "OPC UA 服务"),
    "_sec_csv_remote": ("系统设置", "CSV 导出"),
    "_sec_advanced": ("系统设置", "高级设置"),
    "_sec_accounts": ("系统设置", "登录账号"),
    "_sec_sec_report": ("系统设置", "安全体检"),
    "_sec_backup_export": ("备份与审计", "导出配置备份"),
    "_sec_backup_restore": ("备份与审计", "从备份还原"),
    "_sec_roles": ("备份与审计", "账号与角色"),
    "_sec_audit_log": ("备份与审计", "操作审计日志"),
}

# v3.1 窗口里不在标签页内的部分 → v3.2 的位置（写进说明文档；菜单 / 工具栏方法有测试核对）
LEGACY_EXTRAS = (
    ("顶部服务状态与“启动 / 停止 / 重启服务”按钮", "顶部工具栏左侧（状态指示灯 + 三个按钮）"),
    ("顶部“自检”按钮", "菜单“服务 → 自检”"),
    ("顶部“打开网页设置页”按钮", "顶部工具栏右侧（同时在菜单“工具”）"),
    ("顶部“打开日志文件夹”按钮", "底部日志面板标题栏 + 菜单“文件”"),
    ("安全摘要行", "“总览”页卡片下方 + “安全与账号”页顶部"),
    ("“安全设置…”按钮", "“安全与账号”页顶部 + 菜单“工具”"),
    ("“智能诊断 / 数据传输检测 / 发送测试数据 / 485配型诊断”按钮", "“驱动 / 连接测试”页顶部快捷栏 + 菜单“工具”"),
    ("登录栏（当前用户、登录、注销、锁定、创建管理员…）", "顶部工具栏右侧"),
    ("菜单“语言 / Language”", "保留；顶部工具栏另有语言下拉框"),
    ("底部保存栏（保存设置 / 放弃修改）", "内容区底部，只在有表单字段的页面显示"),
    ("底部运行日志窗", "底部可折叠的“运行日志”面板（默认收起，有警告 / 错误时标题显示条数）"),
)

# ---------------------------------------------------------------------------
# 懒加载 / 只刷新可见页面
# ---------------------------------------------------------------------------

# 画到某个页面控件上的方法：页面没建或当前不可见时不执行，只记下最后一次的参数，
# 页面下次显示时按这里的顺序补调一次（“定时刷新只刷新当前可见页面”）。值是页面标识。
RENDER_GUARDS = (
    ("_render_overview", "overview"),
    ("_render_channel_table", "channels"),
    ("_pv_update_options", "preview"),
    ("_pv_select_key", "preview"),
    ("_pv_draw", "preview"),
    ("_pk_render", "preview"),
    ("_render_alerts", "alarms"),
    ("_render_channels", "alarms"),
    ("_render_quiet_list", "alarms"),
    ("_render_probes", "alarms"),
    ("_render_alerts_state", "alarms"),
    ("_render_data_dirs", "export"),
    ("_render_export_state", "export"),
    ("_rp_show", "reports"),
    ("_render_sessions", "clients"),
    ("_mb_render_map", "modbus"),
    ("_mb_render_status", "modbus"),
    ("_env_note", "tools"),
    ("_env_result", "tools"),
    ("_dc_show", "tools"),
    ("_render_nettest_result", "tools"),
    ("_st_render", "tools"),
    ("_qs_set", "quick"),
    ("_render_security_state", "security"),
    ("_users_refresh", "security"),
    ("_ur_render", "audit"),
    ("_bk_render_auto", "backup"),
)

# 页面每次显示时调用的刷新方法（在补调之后；多数是在后台线程取一次最新数据）
ON_SHOW = {
    "overview": ("_render_overview", "_refresh_records"),
    "channels": ("_refresh_records",),
    "preview": ("_pv_on_show", "_refresh_records"),
    "alarms": ("_alerts_now", "_poll_probes_now"),
    "clients": ("_refresh_sessions",),
    "modbus": ("_mb_render_status",),
    "tools": ("_poll_stability_now",),
    "security": ("_refresh_sec", "_users_refresh"),
    "audit": ("_ur_render",),
    "backup": ("_bk_render_auto",),
}

# 定时轮询：只有这些页面里有一个可见时才去取数据（其余时间只重新排定时器，不做事）
POLL_PAGES = {
    "records": ("overview", "channels", "preview"),     # 通道 / 谱图（服务没运行时要读数据库，较慢）
    "probes": ("alarms",),
    "stability": ("tools",),
    "security": ("overview", "security"),
}


class PageManager:
    """页面懒加载与“只刷新可见页面”的状态机（不依赖 tkinter，单元测试直接用）。

    build(pid)  首次显示某页时调用一次（窗口里就是建控件）；抛异常时该页记为已建（显示错误），不反复重试
    guard(name, pid, fn) 包装渲染方法：页面已建且可见才执行，否则记下最后一次的参数，返回 None
    show(pid)   切换到某页：需要时先 build，再按 RENDER_GUARDS 顺序补调记下的渲染，返回 True=本次新建
    """

    def __init__(self, build, order=None):
        self._build = build
        self.order = [n for n, _p in (order if order is not None else RENDER_GUARDS)]
        self.current = None
        self.built = set()
        self.building = None
        self.build_count = {}
        self.errors = {}
        self.pending = {}          # pid -> {方法名: (args, kwargs, fn)}

    def is_built(self, pid):
        return pid in self.built

    def is_visible(self, pid):
        return pid == self.current or pid == self.building

    def any_visible(self, pids):
        return any(self.is_visible(p) for p in pids)

    def guard(self, name, pid, fn):
        mgr = self

        def wrapped(*a, **k):
            if mgr.building == pid or (mgr.current == pid and pid in mgr.built):
                return fn(*a, **k)
            mgr.pending.setdefault(pid, {})[name] = (a, k, fn)
            return None
        wrapped.guard_page = pid
        return wrapped

    def show(self, pid):
        created = False
        if pid not in self.built:
            self.building = pid
            self.build_count[pid] = self.build_count.get(pid, 0) + 1
            try:
                self._build(pid)
            except Exception as e:  # noqa  一个页面建坏了不能拖垮整个窗口
                self.errors[pid] = e
            finally:
                self.building = None
                self.built.add(pid)
            created = True
        self.current = pid
        self.flush(pid)
        return created

    def flush(self, pid):
        calls = self.pending.pop(pid, {})
        rank = {n: i for i, n in enumerate(self.order)}
        for name in sorted(calls, key=lambda n: rank.get(n, len(rank))):
            a, k, fn = calls[name]
            try:
                fn(*a, **k)
            except Exception:  # noqa
                pass
        return len(calls)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def groups():
    return list(GROUPS)


def pages():
    """导航顺序的全部页面。"""
    return [p for g in GROUPS for p in g.pages]


def page_ids():
    return [p.pid for p in pages()]


def page(pid):
    for p in pages():
        if p.pid == pid:
            return p
    return None


def group_of(pid):
    for g in GROUPS:
        if any(p.pid == pid for p in g.pages):
            return g
    return None


def form_pages():
    return [p.pid for p in pages() if p.form]


def render_guards():
    return dict(RENDER_GUARDS)


def page_of_section(method):
    for p in pages():
        if method in p.methods():
            return p.pid
    return None


def legacy_map():
    """v3.1 标签页标题 → {v3.1 区域标题（整页为 ""）: v3.2 页面标识}。"""
    out = {t: {} for t in LEGACY_TITLES}
    for p in pages():
        if p.builder in WHOLE_SOURCES:
            out[WHOLE_SOURCES[p.builder]][""] = p.pid
        for s in p.sections():
            if s in SECTION_SOURCES:
                tab, sec = SECTION_SOURCES[s]
                out.setdefault(tab, {})[sec] = p.pid
    return out


def legacy_rows():
    """说明文档用的对照表行：[(v3.1 标签页, v3.1 区域, 新位置文字)]，新位置是中文原文。"""
    rows = []
    lm = legacy_map()
    for tab, secs in LEGACY_TABS:
        for sec in (secs or ("",)):
            pid = lm.get(tab, {}).get(sec)
            p = page(pid) if pid else None
            where = "（缺失）"
            if p is not None:
                g = group_of(pid)
                where = "%s › %s" % (g.title, p.title)
                for t, ss in p.subtabs:
                    if t and any(SECTION_SOURCES.get(s) == (tab, sec) for s in ss):
                        where += " → %s" % t
            rows.append((tab, sec, where))
    return rows


def texts():
    """导航、子标签、卡片、菜单、状态栏里所有要翻译的中文原文（i18n 测试用）。"""
    keys = set(TEXTS) | {LOG_PANEL_TITLE}
    for g in GROUPS:
        keys.add(g.title)
        for p in g.pages:
            keys.add(p.title)
            keys |= set(p.subtab_titles())
    for title, items in MENUS:
        keys.add(title)
        keys |= {it[1] for it in items if it is not None}
    keys |= {c[1] for c in CARDS}
    return keys


# ---------------------------------------------------------------------------
# 菜单 / 总览卡片 / 其它新文字
# ---------------------------------------------------------------------------

# 菜单栏：(菜单标题, [(键, 文字, DesktopWindow 方法名), None = 分隔线])。权限由方法本身检查（GUARDED）。
MENUS = (
    ("文件", (
        ("open_logs", "打开日志文件夹", "_open_logs"),
        ("open_log_file", "打开日志文件", "_open_log_file"),
        None,
        ("exit", "退出", "_on_close"),
    )),
    ("服务", (
        ("start", "启动服务", "_start_service"),
        ("stop", "停止服务", "_stop_service"),
        ("restart", "重启服务", "_restart_service"),
        None,
        ("selftest", "自检", "_selftest"),
    )),
    ("工具", (
        ("web", "打开网页设置页", "_open_web"),
        ("security", "安全设置…", "_open_security"),
        None,
        ("diagnose", "智能诊断", "_gui_diagnose"),
        ("transmission", "数据传输检测", "_menu_transmission_check"),
        ("send_test", "发送测试数据", "_menu_send_test"),
        ("rtu485", "485配型诊断", "_gui_rtu485"),
    )),
    ("视图", (
        ("home", "回到总览", "_go_home"),
        ("log", "显示 / 隐藏运行日志", "_toggle_log_panel"),
        None,
        ("expand", "展开全部分组", "_nav_expand_all"),
        ("collapse", "折叠全部分组", "_nav_collapse_all"),
    )),
)
LANG_MENU_TITLE = "语言 / Language"      # v3.1 起的双语固定菜单名，任何语言下都认得出来

# 总览页卡片：(键, 标题, 点击跳转的页面标识；"log" = 展开底部日志面板)
CARDS = (
    ("service", "服务状态", "log"),
    ("endpoint", "OPC UA 端点", "quick"),
    ("clients", "在线客户端", "clients"),
    ("channels", "通道数", "channels"),
    ("alarms", "当前告警", "alarms"),
    ("cycle", "最近周期时间", "preview"),
    ("modbus", "Modbus 转发", "modbus"),
)

TEXTS = (
    # 服务指示灯
    "运行中", "启动中", "已停止", "启动失败",
    # 状态栏
    "服务：%s", "端口：%s", "连接数：%s", "告警：%s", "最后扫描：%s",
    # 卡片说明
    "严重 %d / 警告 %d", "上限 %s", "服务未运行", "无告警", "点击查看", "运行中 %s:%d", "已启用，未运行", "未启用",
    "已应答 %d 次", "按服务快照", "按数据目录",
    # 锁
    "此页面需要“%s”或更高权限，当前身份：%s。", "现在登录吗？", "需要“%s”权限",
    # 其它
    "页面加载失败：%s", "最近告警（最多 5 条）", "快捷诊断", "运行日志（%d 条新警告 / 错误）",
    "清空显示", "打开日志文件", "打开日志文件夹", "（OPC UA 服务 / 高级设置 / 告警通知 等表单）",
    "网页设置", "桌面登录已关闭（全部可用）", "未登录（操作员，只读）", "操作员",
)


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------

def required_role(pid):
    """打开该页面需要的最低角色（operator / engineer / admin）；未知页面按管理员。"""
    import audit
    p = page(pid)
    if p is None:
        return "admin"
    return audit.PERMISSIONS.get(p.perm, "admin")


def allowed(pid, can=None):
    """can(权限) -> bool（通常是 audit.Session.can）。can=None 表示不检查（全部可用）。未知页面不允许。"""
    p = page(pid)
    if p is None:
        return False
    if p.perm == "view" or can is None:
        return True
    try:
        return bool(can(p.perm))
    except Exception:  # noqa
        return False


def locked_pages(can):
    return [p.pid for p in pages() if not allowed(p.pid, can)]


def lock_hint(pid, current_role_name="", tr=None):
    """无权限页面的提示文字（tr 给出时翻译）。"""
    import audit
    _t = tr or (lambda s: s)
    need = _t(audit.ROLE_NAMES.get(required_role(pid), "管理员"))
    return _t("此页面需要“%s”或更高权限，当前身份：%s。") % (need, current_role_name or _t("操作员"))


# ---------------------------------------------------------------------------
# 上次页面 / 窗口大小（配置 desktop_last_page / desktop_geometry）
# ---------------------------------------------------------------------------

_PID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_GEO_RE = re.compile(r"^(\d{1,5})x(\d{1,5})(?:([+-])(-?\d{1,6})([+-])(-?\d{1,6}))?$")


def normalize_last_page(value):
    """配置校验：不认识的页面（空、乱写、以后版本删掉的页面）一律回退总览，不报错——
    这只是界面记忆，不能因为它让服务启动失败。"""
    v = str(value if value is not None else "").strip().lower()
    if not _PID_RE.match(v) or page(v) is None:
        return DEFAULT_PAGE
    return v


def resolve_start_page(saved, can=None):
    """打开窗口时显示哪一页：上次的页面仍存在且当前身份能打开就用它，否则回退总览。"""
    pid = normalize_last_page(saved)
    if not allowed(pid, can):
        return DEFAULT_PAGE
    return pid


def parse_geometry(text):
    """'宽x高+X+Y' / '宽x高' → (w, h, x, y)（x / y 可为 None）；不合法返回 None。"""
    m = _GEO_RE.match(str(text or "").strip())
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w < 1 or h < 1:
        return None
    if m.group(3) is None:
        return w, h, None, None
    x = int(m.group(4)) * (-1 if m.group(3) == "-" else 1)
    y = int(m.group(6)) * (-1 if m.group(5) == "-" else 1)
    return w, h, x, y


def format_geometry(w, h, x=None, y=None):
    if x is None or y is None:
        return "%dx%d" % (w, h)
    return "%dx%d%s%d%s%d" % (w, h, "+" if x >= 0 else "-", abs(x), "+" if y >= 0 else "-", abs(y))


def normalize_geometry(value):
    """配置校验：空或不合法 = ""（用默认大小）；尺寸夹到 [最小尺寸, 10000]。同样不报错。"""
    g = parse_geometry(value)
    if g is None:
        return ""
    w, h, x, y = g
    w, h = max(MIN_SIZE[0], min(10000, w)), max(MIN_SIZE[1], min(10000, h))
    return format_geometry(w, h, x, y)


def default_geometry(screen_w=None, screen_h=None):
    w, h = DEFAULT_SIZE
    if screen_w and screen_h:
        w = min(w, max(MIN_SIZE[0], int(screen_w) - 40))
        h = min(h, max(MIN_SIZE[1], int(screen_h) - 80))
    return format_geometry(w, h)


def restore_geometry(saved, screen_w=None, screen_h=None):
    """打开窗口时用的 geometry：没保存过 / 不合法 → 默认 1100x720（不超过屏幕）；
    大小夹在 [最小尺寸, 屏幕] 之间；位置让窗口大半跑到屏幕外（换了显示器 / 分辨率）就丢掉位置。"""
    v = normalize_geometry(saved)
    if not v:
        return default_geometry(screen_w, screen_h)
    w, h, x, y = parse_geometry(v)
    if screen_w and screen_h:
        sw, sh = int(screen_w), int(screen_h)
        w, h = min(w, max(MIN_SIZE[0], sw)), min(h, max(MIN_SIZE[1], sh))
        if x is not None and (x < -w // 2 or y < 0 or x > sw - 100 or y > sh - 60):
            x = y = None
    return format_geometry(w, h, x, y)


# ---------------------------------------------------------------------------
# 导航宽度、面包屑
# ---------------------------------------------------------------------------

def estimate_px(text, char_px=7):
    """没有字体测量时的估算：中日韩字符按 2 个西文字符宽。"""
    return sum(2 * char_px if ord(c) > 0x2e80 else char_px for c in str(text))


def nav_labels(tr=None):
    """当前语言下导航上的全部文字（组标题 + 页面标题）。"""
    _t = tr or (lambda s: s)
    out = []
    for g in GROUPS:
        out.append(_t(g.title))
        out += [_t(p.title) for p in g.pages]
    return out


def nav_width(labels, measure=None, icon=ICON_PX, pad=44, scale=1.0):
    """导航栏宽度（像素）= 最长文字宽 + 图标 + 缩进与边距，夹在 NAV_WIDTH_RANGE 内。
    measure(text) -> 像素宽（窗口里用 tkinter.font.Font.measure）；不给就估算。
    scale（v3.3）= 高 DPI 缩放倍数：边距和上下限按倍数放大（测出来的文字宽已经是实际像素，不再乘）。"""
    m = measure or estimate_px
    longest = 0
    for t in labels:
        try:
            longest = max(longest, int(m(str(t))))
        except Exception:  # noqa
            longest = max(longest, estimate_px(t))
    try:
        k = max(1.0, float(scale or 1.0))
    except (TypeError, ValueError):
        k = 1.0
    lo, hi = NAV_WIDTH_RANGE
    lo, hi = int(round(lo * k)), int(round(hi * k))
    return max(lo, min(hi, longest + int(round((icon + pad) * k))))


def card_columns(width, min_card=190, max_cols=4, min_cols=2):
    """总览卡片排几列（v3.3）：内容区宽度 / 每张卡片的最小宽度，夹在 2～4 列之间。"""
    try:
        n = int(int(width) // max(1, int(min_card)))
    except (TypeError, ValueError):
        n = max_cols
    return max(min_cols, min(max_cols, n))


def breadcrumb(pid, tr=None):
    _t = tr or (lambda s: s)
    p, g = page(pid), group_of(pid)
    if p is None:
        return ""
    return "%s  ›  %s" % (_t(g.title), _t(p.title)) if g else _t(p.title)


# ---------------------------------------------------------------------------
# 服务指示灯、总览卡片、状态栏
# ---------------------------------------------------------------------------

COLORS = {"running": "#1a7f37", "starting": "#9a6700", "busy": "#9a6700", "error": "#cf222e",
          "stopped": "#6e7781", "info": "#1f4e79", "warn": "#9a6700"}


def service_state(running, up, error=None, busy=""):
    """顶部指示灯：(状态键, 中文原文, 颜色)。busy 是正在进行的操作文字（已翻译），原样显示。"""
    if busy:
        return "busy", busy, COLORS["busy"]
    if running and up:
        return "running", "运行中", COLORS["running"]
    if running:
        return "starting", "启动中", COLORS["starting"]
    if error:
        return "error", "启动失败", COLORS["error"]
    return "stopped", "已停止", COLORS["stopped"]


def endpoint_port(endpoint):
    """opc.tcp://host:port/path 的端口文字；没写端口按 4840；格式不对返回 "—"。"""
    m = re.match(r"^opc\.tcp://(\[[^\]]*\]|[^:/]*)(?::(\d+))?(/.*)?$", str(endpoint or "").strip(), re.I)
    if not m:
        return "—"
    return m.group(2) or "4840"


def latest_cycle(records):
    """所有通道里最新的周期时间（YYYY-MM-DD HH:MM:SS 字符串可直接比较）。"""
    times = [str(r.get("cycle_time")) for r in (records or []) if r.get("cycle_time")]
    return max(times) if times else ""


def _tr_id(s):
    return s


def overview_metrics(svc, records=None, alerts=None, sessions=None, modbus=None, n_channels=None, tr=None):
    """总览卡片：{键: (数值文字, 说明文字, 颜色)}，文字已按 tr 翻译。

    svc       {"running", "up", "error", "busy", "endpoint"}
    records   最近读到的通道记录（服务快照或数据目录）
    alerts    最近一次告警快照（logic.alerts()）；服务没运行时为 {"offline": True}
    sessions  最近一次 df.collect_sessions 结果
    modbus    {"running": (host, port, 应答次数) 或 None, "enabled": bool}
    n_channels 通道数（不给就用 alerts["channels"] 或记录数）
    """
    _t = tr or _tr_id
    svc = svc or {}
    alerts = alerts or {}
    up = bool(svc.get("up"))
    key, text, color = service_state(svc.get("running"), up, svc.get("error"), svc.get("busy"))
    out = {"service": (_t(text), _t("点击查看"), color)}
    ep = svc.get("endpoint") or "—"
    out["endpoint"] = (_t("端口：%s") % endpoint_port(ep), ep, COLORS["info"])
    if up and sessions and sessions.get("ok"):
        out["clients"] = (str(sessions.get("count", len(sessions.get("sessions") or []))),
                          _t("上限 %s") % (sessions.get("max") or "—"), COLORS["info"])
    elif up:
        out["clients"] = ("—", (sessions or {}).get("reason") or "", COLORS["stopped"])
    else:
        out["clients"] = ("0", _t("服务未运行"), COLORS["stopped"])
    online = up and not alerts.get("offline")
    if n_channels is None:
        if online and alerts.get("channels") is not None:
            n_channels = alerts.get("channels")
        else:
            keys = set()
            for r in records or []:
                keys.add((r.get("station"), r.get("app"), r.get("stream"), r.get("detector"), r.get("key")))
            n_channels = len(keys)
    out["channels"] = (str(n_channels), _t("按服务快照") if online else _t("按数据目录"), COLORS["info"])
    act = list(alerts.get("active") or []) if online else []
    crit = sum(1 for a in act if a.get("level") == "严重")
    if not online:
        out["alarms"] = ("—", _t("服务未运行"), COLORS["stopped"])
    elif act:
        out["alarms"] = (str(len(act)), _t("严重 %d / 警告 %d") % (crit, len(act) - crit),
                         COLORS["error"] if crit else COLORS["warn"])
    else:
        out["alarms"] = ("0", _t("无告警"), COLORS["running"])
    out["cycle"] = (latest_cycle(records) or "—", "", COLORS["info"])
    mb = modbus or {}
    run = mb.get("running")
    if run:
        out["modbus"] = (_t("运行中"), (_t("运行中 %s:%d") % (run[0], int(run[1]))) +
                         ("  " + _t("已应答 %d 次") % int(run[2]) if len(run) > 2 else ""), COLORS["running"])
    elif mb.get("enabled"):
        out["modbus"] = (_t("已启用，未运行"), "", COLORS["warn"])
    else:
        out["modbus"] = (_t("未启用"), "", COLORS["stopped"])
    return out


def status_bar_items(svc, alerts=None, sessions=None, tr=None):
    """底部状态栏 5 段：服务状态、端口、连接数、告警数、最后扫描时间（已翻译）。"""
    _t = tr or _tr_id
    svc = svc or {}
    alerts = alerts or {}
    up = bool(svc.get("up"))
    _k, text, _c = service_state(svc.get("running"), up, svc.get("error"), svc.get("busy"))
    online = up and not alerts.get("offline")
    if up and sessions and sessions.get("ok"):
        conn = str(sessions.get("count", 0))
    else:
        conn = "0" if not up else "—"
    return [_t("服务：%s") % _t(text),
            _t("端口：%s") % endpoint_port(svc.get("endpoint")),
            _t("连接数：%s") % conn,
            _t("告警：%s") % (str(len(alerts.get("active") or [])) if online else "—"),
            _t("最后扫描：%s") % ((alerts.get("last_scan") if online else "") or "—")]


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------

def check_layout():
    """结构自检：返回问题列表，空 = 没问题（单元测试与窗口启动时都可调用）。"""
    probs = []
    ids = page_ids()
    if len(ids) != len(set(ids)):
        probs.append("页面标识重复")
    if DEFAULT_PAGE not in ids:
        probs.append("总览不在导航里")
    if not 1 <= len(GROUPS) <= 5:
        probs.append("一级分组数应为 1～5，当前 %d" % len(GROUPS))
    for g in GROUPS:
        if not 3 <= len(g.pages) <= 5:
            probs.append("分组 %s 的页面数应为 3～5，当前 %d" % (g.gid, len(g.pages)))
    for p in pages():
        if not _PID_RE.match(p.pid):
            probs.append("页面标识不合法：%s" % p.pid)
        if not p.builder and not p.sections():
            probs.append("页面 %s 没有内容" % p.pid)
        if p.builder and p.subtabs:
            probs.append("页面 %s 既有整页构建又有区域" % p.pid)
    used = [s for p in pages() for s in p.sections()]
    if len(used) != len(set(used)):
        probs.append("有区域出现在多个页面")
    for tab, secs in LEGACY_TABS:
        got = legacy_map().get(tab, {})
        want = set(secs) if secs else {""}
        if set(got) != want:
            probs.append("v3.1 标签页 %s 的区域没有全部分配：缺 %s" % (tab, sorted(want - set(got))))
    for meth, pid in RENDER_GUARDS:
        if pid not in ids:
            probs.append("%s 绑定的页面不存在：%s" % (meth, pid))
    for pid in list(ON_SHOW) + [p for ps in POLL_PAGES.values() for p in ps]:
        if pid not in ids:
            probs.append("刷新表里的页面不存在：%s" % pid)
    for _k, _t, target in CARDS:
        if target != "log" and target not in ids:
            probs.append("卡片跳转的页面不存在：%s" % target)
    return probs
