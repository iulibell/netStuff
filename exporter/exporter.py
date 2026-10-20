#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eNSP 企业网 SNMP Exporter
=====================================================================
作用：按 devices.yaml 中的设备清单，主动轮询华为 VRP 设备
     (AR 路由器 / S5700 交换机 / USG 防火墙 / AC)，把 SNMP 数据
     转换成 Prometheus 文本格式指标，由 Prometheus 定时抓取。

整体数据流：
  Prometheus (30s/60s/120s 定时 HTTP GET)
      → 本程序的 /probe 接口
      → 按 group/tier 过滤出设备列表
      → 线程池并发向每台设备发 SNMP 请求
      → 汇总成 metrics 文本返回

端点:
  GET /metrics              轮询全部设备（调试用，量大时较慢）
  GET /probe?group=backbone 只轮询指定监控域（backbone/hq/divi）
  GET /probe?tier=core      只轮询指定层级（core/agg/access，分层轮询用）
  GET /healthz              健康检查
  GET /                     导航页
"""
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor   # 并发轮询，避免逐台串行等待

import yaml                                          # 读取 devices.yaml
from flask import Flask, request, Response           # 对外提供 HTTP 接口
from prometheus_client import CollectorRegistry, Gauge, generate_latest

# pysnmp 高层 API：发 SNMP GET / BULK WALK 请求
# 版本兼容：pysnmp 4.x 同步 API 在 pysnmp.hlapi；
# pysnmp-lextudio 5.x 默认 hlapi 是 asyncio 协程版（直接 next() 会抛 TypeError，
# 表现为所有设备静默 up=0），同步生成器 API 挪到了 hlapi.asyncore.sync。
try:
    from pysnmp.hlapi.asyncore.sync import (SnmpEngine, CommunityData,
                                            UdpTransportTarget, ContextData,
                                            ObjectType, ObjectIdentity,
                                            getCmd, bulkCmd)
except ImportError:
    from pysnmp.hlapi import (SnmpEngine, CommunityData, UdpTransportTarget,
                              ContextData, ObjectType, ObjectIdentity,
                              getCmd, bulkCmd)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ensp-exporter')

# ============================================================
# 第一部分：OID 定义
# ------------------------------------------------------------
# OID 是 SNMP 世界里"指标的唯一编号"，点分数字树。
# 1.3.6.1.2.1.*  是标准 MIB-2（所有网络设备通用）
# 1.3.6.1.4.1.2011.* 是华为私有 MIB（2011 = 华为企业号）
# ============================================================
OID_SYS_UPTIME = '1.3.6.1.2.1.1.3.0'                     # sysUpTime：设备开机时长(TimeTicks, 1/100秒)
OID_CPU_DUTY = '1.3.6.1.4.1.2011.5.25.31.1.1.1.1.5'      # 华为私有 hwCpuDuty：CPU 占用率%（按槽位索引，需 walk）

# 接口表的 6 个"列"。它们共享同一个 ifIndex 索引空间：
# 比如 ifIndex=5 的接口，它的名字/状态/速率/流量分别挂在
# 各列 OID 的 ".5" 叶子下。一次 walk 把整列取回来，再按 ifIndex 对齐合并。
IF_COLS = {
    'name':  '1.3.6.1.2.1.31.1.1.1.1',    # ifName：接口名 (GE0/0/0)
    'descr': '1.3.6.1.2.1.2.2.1.2',       # ifDescr：接口描述（ifName 为空时兜底）
    'oper':  '1.3.6.1.2.1.2.2.1.8',       # ifOperStatus：运行状态 (1=up)
    'speed': '1.3.6.1.2.1.31.1.1.1.15',   # ifHighSpeed：接口速率 (Mbps)
    'in':    '1.3.6.1.2.1.31.1.1.1.6',    # ifHCInOctets：入向累计字节 (Counter64)
    'out':   '1.3.6.1.2.1.31.1.1.1.10',   # ifHCOutOctets：出向累计字节 (Counter64)
}

# ---------------- 路径与端口 ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICES_YAML = os.environ.get('DEVICES_YAML', os.path.join(BASE_DIR, 'devices.yaml'))
LISTEN_PORT = int(os.environ.get('EXPORTER_PORT', '9116'))

app = Flask(__name__)


# ============================================================
# 第二部分：配置加载
# ------------------------------------------------------------
# devices.yaml 里是设备清单：分组(backbone/hq/divi)、每台设备的
# IP/tier/type/community 等。进程启动时加载一次，存进全局 CFG。
# ============================================================
def load_config():
    with open(DEVICES_YAML, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

CFG = load_config()


# ============================================================
# 第三部分：SNMP 原语封装
# ------------------------------------------------------------
# 把 pysnmp 的啰嗦调用封装成两个简短函数：
#   snmp_get_scalar  —— GET 单个标量（如 sysUpTime）
#   snmp_walk_column —— BULK WALK 一整列（如所有接口的 ifOperStatus）
# ============================================================
def _auth(dev):
    """构造 SNMPv2c 认证信息。community 相当于只读口令，
    必须与设备端 `snmp-agent community read cipher xxx` 一致。
    设备可单独配置 community，否则用全局默认值。"""
    community = dev.get('community', CFG.get('community', 'public'))
    return CommunityData(community, mpModel=1)  # mpModel=1 表示 SNMPv2c


def _target(dev):
    """构造 SNMP 目标地址 + 超时/重试参数。
    超时建议 ≤2s：轮询几十台设备时，单台慢会拖垮整轮采集。"""
    return UdpTransportTarget(
        (dev['ip'], int(dev.get('port', CFG.get('snmp_port', 161)))),
        timeout=float(CFG.get('snmp_timeout', 2.0)),
        retries=int(CFG.get('snmp_retries', 1)))


def snmp_get_scalar(dev, oid):
    """GET 单个 OID，返回 int 或 None。
    None 的语义 = 设备不可达 / community 不对 / 设备不支持该 OID。
    上层用它判断设备"在不在线"：sysUpTime 能取到就算在线。"""
    err_ind, err_st, _, var_binds = next(getCmd(
        SnmpEngine(), _auth(dev), _target(dev), ContextData(),
        ObjectType(ObjectIdentity(oid))))
    if err_ind or err_st or not var_binds:
        return None
    val = var_binds[0][1]
    if not val.isSameTypeWith(val.__class__) and val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def snmp_walk_column(dev, oid):
    """BULK WALK 一列，返回 {ifIndex: 值} 字典。
    bulkCmd 参数 (0, 10) = 每次报文打包取 10 行，减少往返次数；
    lexicographicMode=False 让 walk 在走出该列 OID 子树时自动停止。"""
    result = {}
    for err_ind, err_st, _, var_binds in bulkCmd(
            SnmpEngine(), _auth(dev), _target(dev), ContextData(),
            0, 10, ObjectType(ObjectIdentity(oid)),
            lexicographicMode=False):
        if err_ind or err_st:
            break
        for name, val in var_binds:
            name_str = name.prettyPrint()
            if not name_str.startswith(oid + '.'):
                continue
            idx = name_str[len(oid) + 1:]      # 截出叶子号 = ifIndex
            result[idx] = val
    return result


# ============================================================
# 第四部分：单台设备轮询
# ------------------------------------------------------------
# 对一台设备依次取：sysUpTime(探活) → CPU → 接口表 6 列。
# 任何一步失败都不抛异常，而是返回 (半成品数据, 错误描述)，
# 保证一台设备故障不影响其他设备。
# ============================================================
def poll_device(dev):
    """返回 (data_dict, error_str)。up=0 表示不可达/超时。"""
    t0 = time.time()
    data = {'up': 0, 'uptime_s': 0, 'cpu': None, 'interfaces': []}
    try:
        # --- 第一步：sysUpTime 探活。它是所有 SNMP 设备都必须支持的 OID ---
        uptime = snmp_get_scalar(dev, OID_SYS_UPTIME)
        if uptime is None:
            return data, 'sysUpTime 无响应（超时或 community 不匹配）'
        data['up'] = 1
        data['uptime_s'] = uptime / 100.0   # TimeTicks 单位是 1/100 秒

        # --- 第二步：CPU。华为私有 OID，按槽位返回多个值，取最大值。
        #     二层交换机可能不支持该 OID → 保持 None，面板显示 N/A ---
        cpu_vals = []
        for _, v in snmp_walk_column(dev, OID_CPU_DUTY).items():
            try:
                cpu_vals.append(int(v))
            except (ValueError, TypeError):
                pass
        if cpu_vals:
            data['cpu'] = max(cpu_vals)

        # --- 第三步：接口表。6 列各 walk 一次，按 ifIndex 对齐合并 ---
        cols = {k: snmp_walk_column(dev, oid) for k, oid in IF_COLS.items()}
        for idx in cols['oper']:
            # 接口名优先取 ifName，为空退到 ifDescr，再不行用 "if<序号>"
            name_val = cols['name'].get(idx) or cols['descr'].get(idx)
            if_name = str(name_val) if name_val is not None else 'if%s' % idx
            if not if_name:
                if_name = 'if%s' % idx

            def _int(col):
                v = cols[col].get(idx)
                try:
                    return int(v)
                except (ValueError, TypeError):
                    return None

            data['interfaces'].append({
                'name': if_name,
                'oper': _int('oper'),
                'speed': _int('speed'),
                'in': _int('in'),
                'out': _int('out'),
            })
        return data, ''
    except Exception as e:  # 网络不可达 / 解码失败等
        return data, str(e)
    finally:
        data['duration'] = time.time() - t0   # 记录单台耗时，用于排查慢设备


# ============================================================
# 第五部分：指标组装
# ------------------------------------------------------------
# 每次被 Prometheus 抓取时：
#   1) 新建一个一次性 CollectorRegistry（避免残留上次的数据）
#   2) 按 group/tier 过滤出本轮要轮询的设备
#   3) 线程池并发轮询
#   4) 把结果翻译成 Gauge 指标（打 group/device/interface 等标签）
# 流量类原始值是"累计字节数(Counter)"，速率由 Grafana 侧用
# rate()/irate() 计算，exporter 只负责忠实上报计数器。
# ============================================================
def collect(group_filter=None, tier_filter=None):
    reg = CollectorRegistry()
    g_up = Gauge('ensp_device_up', '设备 SNMP 可达(1) / 不可达(0)',
                 ['group', 'device', 'ip', 'type'], registry=reg)
    g_uptime = Gauge('ensp_device_uptime_seconds', 'sysUpTime 秒',
                     ['group', 'device'], registry=reg)
    g_cpu = Gauge('ensp_cpu_usage_percent', 'CPU 利用率(%)（取各槽位最大）',
                  ['group', 'device'], registry=reg)
    g_dur = Gauge('ensp_scrape_device_duration_seconds', '单台设备轮询耗时',
                  ['group', 'device'], registry=reg)
    g_if_up = Gauge('ensp_interface_oper_up', '接口运行状态 up(1)/其它(0)',
                    ['group', 'device', 'interface'], registry=reg)
    g_if_in = Gauge('ensp_interface_in_octets_total', '接口入向字节数(Counter64)',
                    ['group', 'device', 'interface'], registry=reg)
    g_if_out = Gauge('ensp_interface_out_octets_total', '接口出向字节数(Counter64)',
                     ['group', 'device', 'interface'], registry=reg)
    g_if_speed = Gauge('ensp_interface_speed_mbps', '接口速率(Mbps)',
                       ['group', 'device', 'interface'], registry=reg)
    g_scrape = Gauge('ensp_scrape_duration_seconds', '本次分组轮询总耗时',
                     ['group'], registry=reg)

    # --- 按 group + tier 双重过滤，生成 (组名, 设备) 任务清单 ---
    groups = CFG['groups']
    tasks = []
    for gname, ginfo in groups.items():
        if group_filter and gname != group_filter:
            continue
        for dev in ginfo['devices']:
            if tier_filter and dev.get('tier', 'core') != tier_filter:
                continue
            tasks.append((gname, dev))

    # --- 并发轮询：16 个线程同时发 SNMP，总耗时≈最慢的一台 ---
    t_all = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=int(CFG.get('max_workers', 16))) as ex:
        futs = {ex.submit(poll_device, dev): (gname, dev) for gname, dev in tasks}
        for fut, (gname, dev) in futs.items():
            results[(gname, dev['name'])] = fut.result()

    # --- 把轮询结果翻译成 Prometheus 指标 ---
    touched = set()
    for gname, dev in tasks:
        data, err = results[(gname, dev['name'])]
        lbl = {'group': gname, 'device': dev['name']}
        g_up.labels(group=gname, device=dev['name'], ip=dev['ip'],
                    type=dev.get('type', '')).set(data['up'])
        g_dur.labels(**lbl).set(data.get('duration', 0))
        if err:
            log.warning('[%s] %s(%s) 轮询失败: %s', gname, dev['name'], dev['ip'], err)
        if not data['up']:
            continue   # 离线设备只报 ensp_device_up=0，其余指标不出
        g_uptime.labels(**lbl).set(data['uptime_s'])
        if data['cpu'] is not None:
            g_cpu.labels(**lbl).set(data['cpu'])
        for itf in data['interfaces']:
            ilbl = dict(lbl, interface=itf['name'])
            if itf['oper'] is not None:
                g_if_up.labels(**ilbl).set(1 if itf['oper'] == 1 else 0)
            if itf['in'] is not None:
                g_if_in.labels(**ilbl).set(itf['in'])
            if itf['out'] is not None:
                g_if_out.labels(**ilbl).set(itf['out'])
            if itf['speed'] is not None:
                g_if_speed.labels(**ilbl).set(itf['speed'])
        touched.add(gname)

    for gname in touched:
        g_scrape.labels(group=gname).set(time.time() - t_all)
    return reg


# ============================================================
# 第六部分：HTTP 路由
# ------------------------------------------------------------
# /metrics 和 /probe 的本质相同：同步跑完一整轮轮询后才返回。
# 这就是 Prometheus "pull 模型"的标准玩法——exporter 不存历史数据，
# 每次抓取都是实时采集的最新快照。
# ============================================================
@app.route('/metrics')
def metrics_all():
    return Response(generate_latest(collect()), mimetype='text/plain; version=0.0.4')


@app.route('/probe')
def probe():
    """Prometheus 分层 job 的入口：?group=xx 按域过滤，?tier=xx 按层过滤，
    两者可叠加（与关系）。"""
    group = request.args.get('group')
    tier = request.args.get('tier')
    if group and group not in CFG['groups']:
        return Response('unknown group: %s' % group, status=404)
    return Response(generate_latest(collect(group, tier)), mimetype='text/plain; version=0.0.4')


@app.route('/healthz')
def healthz():
    return 'ok\n'


@app.route('/')
def index():
    """导航页：列出各域、各层级的抓取入口，方便手工点验。"""
    links = ''.join(
        '<li><a href="/probe?group={0}">/probe?group={0}</a> — {1}（{2} 台）</li>'.format(
            g, info.get('label', g), len(info['devices']))
        for g, info in CFG['groups'].items())
    return ('<h3>eNSP SNMP Exporter</h3><h4>按域抓取</h4><ul>%s</ul>'
            '<h4>按层抓取（分层轮询）</h4><ul>'
            '<li><a href="/probe?tier=core">/probe?tier=core</a> — 核心/骨干/防火墙（30s）</li>'
            '<li><a href="/probe?tier=agg">/probe?tier=agg</a> — 汇聚/出口（60s）</li>'
            '<li><a href="/probe?tier=access">/probe?tier=access</a> — 接入层（120s）</li>'
            '</ul>'
            '<p><a href="/metrics">/metrics 全部设备</a></p>' % links)


# ============================================================
# 第七部分：启动入口
# threaded=True：Flask 多线程，保证 Prometheus 多个 job 并发抓取时
# 不会互相阻塞（三个 tier job 可能同时打到 /probe）。
# ============================================================
if __name__ == '__main__':
    total = sum(len(g['devices']) for g in CFG['groups'].values())
    log.info('已加载 %d 个分组 / %d 台设备，监听 :%d',
             len(CFG['groups']), total, LISTEN_PORT)
    app.run(host='0.0.0.0', port=LISTEN_PORT, threaded=True)
