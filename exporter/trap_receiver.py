#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
eNSP SNMP Trap 接收器
  - 监听 UDP 162，接收设备主动上报的 Trap（linkDown/linkUp/coldStart 等）
  - 解析后转成 Prometheus 指标，暴露在 :9117/metrics
  - 设备端配置：snmp-agent target-host trap address udp-domain <本机IP> params securityname Monitor@123 v2c
指标：
  ensp_trap_total{group,device,src_ip,trap,interface}        Counter
  ensp_last_trap_timestamp_seconds{group,device,trap}        Gauge
  ensp_trap_receiver_up                                      Gauge
"""
import os
import sys
import time
import threading
import logging
import collections

import yaml
from flask import Flask, Response, jsonify
from prometheus_client import CollectorRegistry, Gauge, Counter, generate_latest

from pysnmp.entity import engine, config as snmp_config
from pysnmp.carrier.asyncio.dgram import udp
# 兼容 pysnmp-lextudio 5.x/6.x：NotificationReceiver 已挪到 rfc3413 下
try:
    from pysnmp.ntfrcv import NotificationReceiver
except ImportError:
    from pysnmp.entity.rfc3413.ntfrcv import NotificationReceiver
from pysnmp.proto.api import v2c

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('trap-receiver')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG = yaml.safe_load(open(os.path.join(BASE_DIR, 'devices.yaml'),
                          encoding='utf-8'))
COMMUNITY = CFG.get('community', 'Monitor@123')
TRAP_PORT = int(os.environ.get('TRAP_PORT', '162'))
METRICS_PORT = int(os.environ.get('TRAP_METRICS_PORT', '9117'))

# IP -> (group, device) 反查表，用于把 Trap 源地址映射成设备名
IP_MAP = {}
for gname, ginfo in CFG.get('groups', {}).items():
    for dev in ginfo.get('devices', []):
        IP_MAP[dev['ip']] = (gname, dev['name'])

# 常见标准 Trap OID -> 名称；其余 enterprise Trap 取 OID 末两段
TRAP_NAMES = {
    '1.3.6.1.6.3.1.1.5.1': 'coldStart',
    '1.3.6.1.6.3.1.1.5.2': 'warmStart',
    '1.3.6.1.6.3.1.1.5.3': 'linkDown',
    '1.3.6.1.6.3.1.1.5.4': 'linkUp',
    '1.3.6.1.6.3.1.1.5.5': 'authenticationFailure',
}
SNMP_TRAP_OID = '1.3.6.1.6.3.1.1.4.1.0'   # snmpTrapOID.0
IF_INDEX = '1.3.6.1.2.1.2.2.1.1'          # ifIndex 前缀
IF_DESCR = '1.3.6.1.2.1.2.2.1.2'          # ifDescr 前缀

REG = CollectorRegistry()
c_trap = Counter('ensp_trap_total', '收到的 SNMP Trap 总数',
                 ['group', 'device', 'src_ip', 'trap', 'interface'], registry=REG)
g_last = Gauge('ensp_last_trap_timestamp_seconds', '最近一次 Trap 的 Unix 时间戳',
               ['group', 'device', 'trap'], registry=REG)
g_up = Gauge('ensp_trap_receiver_up', 'Trap 接收器运行状态', registry=REG)
g_up.set(1)

RECENT = collections.deque(maxlen=200)    # 最近 Trap 明细（/traps 调试用）

app = Flask(__name__)


def _trap_name(oid):
    if oid in TRAP_NAMES:
        return TRAP_NAMES[oid]
    parts = oid.split('.')
    return 'ent.' + '.'.join(parts[-2:]) if len(parts) > 2 else oid


def on_notification(snmpEngine, stateReference, contextEngineId,
                    contextName, varBinds, cbCtx):
    try:
        _, addr = snmpEngine.msgAndPduDsp.getTransportInfo(stateReference)
        src_ip = addr[0]
        trap_oid, if_name = '', ''
        extra = {}
        for name, val in varBinds:
            oid = name.prettyPrint()
            if oid == SNMP_TRAP_OID:
                trap_oid = val.prettyPrint()
            elif oid.startswith(IF_DESCR):
                if_name = val.prettyPrint()
            elif oid.startswith(IF_INDEX):
                extra['ifIndex'] = val.prettyPrint()
        trap = _trap_name(trap_oid) if trap_oid else 'unknown'
        interface = if_name or ''
        group, device = IP_MAP.get(src_ip, ('unknown', src_ip))

        c_trap.labels(group=group, device=device, src_ip=src_ip,
                      trap=trap, interface=interface).inc()
        g_last.labels(group=group, device=device, trap=trap).set(time.time())
        RECENT.appendleft({
            'time': time.strftime('%H:%M:%S'), 'device': device,
            'src_ip': src_ip, 'trap': trap, 'interface': interface,
        })
        log.info('Trap <- %s(%s) %s %s', device, src_ip, trap, interface)
    except Exception as e:
        log.warning('Trap 解析失败: %s', e)


def run_snmp_dispatcher():
    """pysnmp asyncio dispatcher 需要在独立线程内自建事件循环"""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    eng = engine.SnmpEngine()
    snmp_config.addTransport(
        eng, udp.domainName + (1,),
        udp.UdpTransport().openServerMode(('0.0.0.0', TRAP_PORT)))
    # 允许本工程 community 的 v1/v2c Trap 入网
    snmp_config.addV1System(eng, 'ensp-area', COMMUNITY)
    NotificationReceiver(eng, on_notification)

    eng.transportDispatcher.jobStarted(1)
    log.info('SNMP Trap 监听已启动: udp/0.0.0.0:%d community=%s',
             TRAP_PORT, COMMUNITY)
    try:
        eng.transportDispatcher.runDispatcher()
    except Exception as e:
        log.error('SNMP dispatcher 异常退出: %s', e)
        g_up.set(0)


@app.route('/metrics')
def metrics():
    return Response(generate_latest(REG), mimetype='text/plain; version=0.0.4')


@app.route('/traps')
def traps():
    return jsonify(list(RECENT))


@app.route('/healthz')
def healthz():
    return 'ok\n'


@app.route('/')
def index():
    return ('<h3>eNSP Trap Receiver</h3>'
            '<ul><li><a href="/metrics">/metrics</a> Prometheus 指标</li>'
            '<li><a href="/traps">/traps</a> 最近 200 条 Trap 明细</li></ul>')


if __name__ == '__main__':
    t = threading.Thread(target=run_snmp_dispatcher, daemon=True)
    t.start()
    log.info('指标端口 :%d，已加载 IP 反查表 %d 条', METRICS_PORT, len(IP_MAP))
    app.run(host='0.0.0.0', port=METRICS_PORT, threaded=True)
